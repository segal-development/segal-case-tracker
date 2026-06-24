"""Tests for cu_rotator helper functions and set_lawyer_clave_unica script.

All tests are mocked — no network, no live DB, no Playwright.
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.core.security import decrypt_pjud_password, encrypt_pjud_password


# ---------------------------------------------------------------------------
# _select_cu_lawyers — uses the conftest `db` fixture (in-memory SQLite)
# ---------------------------------------------------------------------------


def test_select_cu_lawyers_excludes_firm_rut(db):
    """Lawyers with CU are returned; the firm account is excluded."""
    from app.models.lawyer import Lawyer
    from scripts.cu_rotator import _select_cu_lawyers

    firm = Lawyer(
        rut="11111111-1",
        name="Firm Lawyer",
        encrypted_clave_unica_password=encrypt_pjud_password("firm-pass"),
    )
    associate = Lawyer(
        rut="22222222-2",
        name="Associate",
        encrypted_clave_unica_password=encrypt_pjud_password("assoc-pass"),
    )
    no_cu = Lawyer(rut="33333333-3", name="No CU Lawyer")
    db.add_all([firm, associate, no_cu])
    db.commit()

    result = _select_cu_lawyers(db, firm_rut="11111111-1")

    ruts = {la.rut for la in result}
    assert "11111111-1" not in ruts, "firm account must be excluded"
    assert "22222222-2" in ruts, "associate with CU must be included"
    assert "33333333-3" not in ruts, "lawyer without CU must be excluded"


def test_select_cu_lawyers_only_filter(db):
    """`only` param returns just the targeted lawyer."""
    from app.models.lawyer import Lawyer
    from scripts.cu_rotator import _select_cu_lawyers

    l1 = Lawyer(
        rut="11111111-1",
        name="L1",
        encrypted_clave_unica_password=encrypt_pjud_password("p1"),
    )
    l2 = Lawyer(
        rut="22222222-2",
        name="L2",
        encrypted_clave_unica_password=encrypt_pjud_password("p2"),
    )
    db.add_all([l1, l2])
    db.commit()

    result = _select_cu_lawyers(db, firm_rut=None, only="11111111-1")

    assert len(result) == 1
    assert result[0].rut == "11111111-1"


def test_select_cu_lawyers_only_no_cu(db):
    """`only` param for a lawyer without CU returns empty list."""
    from app.models.lawyer import Lawyer
    from scripts.cu_rotator import _select_cu_lawyers

    no_cu = Lawyer(rut="11111111-1", name="No CU")
    db.add(no_cu)
    db.commit()

    result = _select_cu_lawyers(db, firm_rut=None, only="11111111-1")

    assert result == []


# ---------------------------------------------------------------------------
# Encryption roundtrip — pure unit test, no DB
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    """Fernet roundtrip: decrypt(encrypt(x)) == x."""
    plaintext = "s3cr3t-p@ssword!"
    assert decrypt_pjud_password(encrypt_pjud_password(plaintext)) == plaintext


# ---------------------------------------------------------------------------
# set_lawyer_clave_unica.main — mock DB session
# ---------------------------------------------------------------------------


def test_set_lawyer_clave_unica_sets_fields(monkeypatch):
    """main() stores an encrypted CU password and the correct cu_rut."""
    from scripts.set_lawyer_clave_unica import main

    mock_lawyer = MagicMock()
    mock_lawyer.name = "Test Lawyer"
    mock_lawyer.rut = "12345678-9"

    mock_db = MagicMock()
    # query(Lawyer).filter(...).first() → return the existing mock lawyer
    mock_db.query.return_value.filter.return_value.first.return_value = mock_lawyer

    with patch("scripts.set_lawyer_clave_unica.SessionLocal", return_value=mock_db):
        monkeypatch.setattr("sys.argv", ["set_lawyer_clave_unica.py", "12345678-9", "secret123"])
        main()

    # encrypted_clave_unica_password must be set and decryptable
    stored = mock_lawyer.encrypted_clave_unica_password
    assert stored is not None
    assert isinstance(stored, str)
    assert decrypt_pjud_password(stored) == "secret123"

    # cu_rut defaults to pjud_rut when not supplied as third arg
    assert mock_lawyer.clave_unica_rut == "12345678-9"

    mock_db.commit.assert_called_once()


def test_set_lawyer_clave_unica_cu_rut_override(monkeypatch):
    """When cu_rut differs from pjud_rut, it is stored correctly."""
    from scripts.set_lawyer_clave_unica import main

    mock_lawyer = MagicMock()
    mock_lawyer.name = "Test Lawyer"
    mock_lawyer.rut = "12345678-9"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_lawyer

    with patch("scripts.set_lawyer_clave_unica.SessionLocal", return_value=mock_db):
        monkeypatch.setattr(
            "sys.argv",
            ["set_lawyer_clave_unica.py", "12345678-9", "secret123", "98765432-1"],
        )
        main()

    assert mock_lawyer.clave_unica_rut == "98765432-1"
    assert decrypt_pjud_password(mock_lawyer.encrypted_clave_unica_password) == "secret123"


# ---------------------------------------------------------------------------
# _next_cu_index — round-robin cursor helper
# ---------------------------------------------------------------------------


def test_next_cu_index_basic():
    """Round-robin cursor returns correct pick and advances."""
    from scripts.cu_rotator import _next_cu_index

    pick, next_cur = _next_cu_index(0, 3)
    assert pick == 0
    assert next_cur == 1

    pick, next_cur = _next_cu_index(2, 3)
    assert pick == 2
    assert next_cur == 3

    # Wraps around on the next call
    pick, next_cur = _next_cu_index(3, 3)
    assert pick == 0  # 3 % 3 == 0
    assert next_cur == 4


def test_next_cu_index_single_item():
    """Single-item list always picks index 0."""
    from scripts.cu_rotator import _next_cu_index

    for cursor in range(5):
        pick, _ = _next_cu_index(cursor, 1)
        assert pick == 0


def test_next_cu_index_round_trips():
    """Cursor advances monotonically; picks rotate correctly over n items."""
    from scripts.cu_rotator import _next_cu_index

    n = 4
    cursor = 0
    for i in range(n * 3):
        pick, cursor = _next_cu_index(cursor, n)
        assert pick == i % n


# ---------------------------------------------------------------------------
# sync_one_cu_lawyer — mocked, no network / no Playwright
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_one_cu_lawyer_calls_cu_login_and_returns_counts():
    """sync_one_cu_lawyer: logs in with decrypted CU, calls reserved rotation, returns tuple."""
    from unittest.mock import AsyncMock, MagicMock, patch, call
    from app.core.security import encrypt_pjud_password
    from scripts.cu_rotator import sync_one_cu_lawyer

    # Build a fake lawyer object.
    lawyer = MagicMock()
    lawyer.id = 7
    lawyer.rut = "22222222-2"
    lawyer.clave_unica_rut = None  # falls back to lawyer.rut
    lawyer.encrypted_clave_unica_password = encrypt_pjud_password("cu-secret")
    lawyer.name = "Test Abogado"

    # Fake PJUDCase objects returned by get_my_cases.
    fake_case = MagicMock()
    fake_case.rol = "C-1234-2024"
    fake_case.tribunal = "1JL Santiago"
    fake_case.caratulado = "Foo c/ Bar"
    fake_case.fecha_ingreso = "2024-01-01"
    fake_case.estado_cuaderno = "vigente"
    fake_case.cuaderno = "principal"
    fake_case.institucion = "PJU"

    # Fake db — must NOT have .commit called on it.
    fake_db = MagicMock()

    # Fake scraper.
    fake_page = AsyncMock()
    fake_ctx = AsyncMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)
    fake_browser = AsyncMock()
    fake_browser.new_context = AsyncMock(return_value=fake_ctx)

    fake_sc = MagicMock()
    fake_sc._browser = fake_browser
    fake_sc._page = None
    fake_sc.get_my_cases = AsyncMock(return_value=[fake_case])

    fake_session = MagicMock()
    fake_session.is_expired.return_value = False

    fake_sync_result = MagicMock()
    fake_sync_result.cases_total = 1
    fake_sync_result.cases_new = 1
    fake_sync_result.cases_updated = 0
    fake_sync_result.errors = []

    with (
        patch(
            "app.scrapper.pjud.clave_unica.ClaveUnicaAuth.login",
            new_callable=AsyncMock,
            return_value=fake_session,
        ) as mock_cu_login,
        patch(
            "app.services.sync_service.SyncService.sync_cases",
            return_value=fake_sync_result,
        ) as mock_sync_cases,
        patch(
            "app.services.sync_service._select_cases_for_detail_rotation",
            return_value=[fake_case],
        ) as mock_select,
        patch(
            "app.services.sync_service.detect_and_sync_movements",
            new_callable=AsyncMock,
            return_value=(3, 1, []),  # created=3, updated=1, errors=[]
        ) as mock_detect,
    ):
        result = await sync_one_cu_lawyer(fake_db, fake_sc, lawyer, batch=50)

    # Should return (created, updated, len(errors)) = (3, 1, 0)
    assert result == (3, 1, 0)

    # Verify CU login was called with the right rut (falls back to lawyer.rut)
    mock_cu_login.assert_called_once()
    login_call_args = mock_cu_login.call_args
    # Second positional arg is ClaveUnicaCredentials
    creds = login_call_args.args[1]
    assert creds.rut == "22222222-2"

    # Verify _select_cases_for_detail_rotation called with reserved_first=True
    mock_select.assert_called_once()
    _, select_kwargs = mock_select.call_args
    assert select_kwargs.get("reserved_first") is True or mock_select.call_args.args[5] is True

    # Verify db.commit was NEVER called (caller owns transaction)
    fake_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_one_cu_lawyer_uses_clave_unica_rut_override():
    """When lawyer.clave_unica_rut is set, it is used for CU login (not lawyer.rut)."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.core.security import encrypt_pjud_password
    from scripts.cu_rotator import sync_one_cu_lawyer

    lawyer = MagicMock()
    lawyer.id = 8
    lawyer.rut = "22222222-2"
    lawyer.clave_unica_rut = "99999999-9"  # override
    lawyer.encrypted_clave_unica_password = encrypt_pjud_password("pass2")

    fake_case = MagicMock()
    fake_case.rol = "C-999-2024"
    fake_case.tribunal = "T"
    fake_case.caratulado = "X c/ Y"
    fake_case.fecha_ingreso = "2024-01-01"
    fake_case.estado_cuaderno = ""
    fake_case.cuaderno = ""
    fake_case.institucion = "PJU"

    fake_ctx = AsyncMock()
    fake_ctx.new_page = AsyncMock(return_value=AsyncMock())
    fake_browser = AsyncMock()
    fake_browser.new_context = AsyncMock(return_value=fake_ctx)
    fake_sc = MagicMock()
    fake_sc._browser = fake_browser
    fake_sc._page = None
    fake_sc.get_my_cases = AsyncMock(return_value=[fake_case])

    fake_session = MagicMock()
    fake_sync_result = MagicMock()
    fake_sync_result.errors = []

    with (
        patch(
            "app.scrapper.pjud.clave_unica.ClaveUnicaAuth.login",
            new_callable=AsyncMock,
            return_value=fake_session,
        ) as mock_cu_login,
        patch("app.services.sync_service.SyncService.sync_cases", return_value=fake_sync_result),
        patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=[]),
        patch(
            "app.services.sync_service.detect_and_sync_movements",
            new_callable=AsyncMock,
            return_value=(0, 0, []),
        ),
    ):
        await sync_one_cu_lawyer(fake_db := MagicMock(), fake_sc, lawyer)

    creds = mock_cu_login.call_args.args[1]
    assert creds.rut == "99999999-9", "must use clave_unica_rut override, not lawyer.rut"
