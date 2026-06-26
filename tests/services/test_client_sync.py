"""Tests for app/services/client_sync.py — sync_one_cu_client.

All tests are mocked — no live PJUD, no Playwright.
Uses the shared `db` fixture from conftest.py (SQLite).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.security import encrypt_pjud_password


# ---------------------------------------------------------------------------
# Helpers — build minimal Lawyer and Client objects in the test DB
# ---------------------------------------------------------------------------


def _make_lawyer(db, rut="11111111-1", name="Firm Lawyer"):
    from app.models.lawyer import Lawyer

    lawyer = Lawyer(rut=rut, name=name)
    db.add(lawyer)
    db.flush()
    return lawyer


def _make_client(
    db,
    *,
    rut="22222222-2",
    nombre="Test Client",
    assigned_lawyer_id,
    clave_unica_rut=None,
    cu_password="cu-secret",
):
    from app.models.client import Client

    client = Client(
        rut=rut,
        nombre=nombre,
        clave_unica_rut=clave_unica_rut,
        encrypted_clave_unica_password=encrypt_pjud_password(cu_password),
        assigned_lawyer_id=assigned_lawyer_id,
    )
    db.add(client)
    db.flush()
    return client


def _fake_scraper():
    """Return a lightweight MagicMock that looks like CivilScraper."""
    sc = MagicMock()
    sc._browser = MagicMock()

    fake_ctx = MagicMock()
    fake_page = MagicMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)
    fake_ctx.close = AsyncMock()
    sc._browser.new_context = AsyncMock(return_value=fake_ctx)

    sc.start = AsyncMock()
    sc._page = None
    return sc


def _fake_api_case(rol="C-1234-2024"):
    """Return a minimal object that looks like a PJUDCase."""
    c = MagicMock()
    c.rol = rol
    c.tribunal = "1er Juzgado Civil de Santiago"
    c.caratulado = "Smith con Jones"
    c.fecha_ingreso = None
    c.estado_cuaderno = "Vigente"
    c.cuaderno = "Principal"
    c.institucion = "Poder Judicial"
    return c


# ---------------------------------------------------------------------------
# a) Happy-path: attributes to assigned lawyer, correct CU login RUT, counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_one_cu_client_attributes_to_assigned_lawyer(db):
    """sync_cases is called with assigned_lawyer_id, not client.id; returns correct counts."""
    from app.services.client_sync import sync_one_cu_client

    lawyer = _make_lawyer(db)
    client = _make_client(db, assigned_lawyer_id=lawyer.id)
    db.commit()

    fake_sc = _fake_scraper()
    fake_session = MagicMock()
    fake_api_cases = [_fake_api_case()]
    fake_sc.get_my_cases = AsyncMock(return_value=fake_api_cases)

    fake_sync_result = MagicMock()

    with (
        patch(
            "app.scrapper.pjud.clave_unica.ClaveUnicaAuth.login",
            new_callable=AsyncMock,
            return_value=fake_session,
        ),
        patch(
            "app.services.sync_service.SyncService.sync_cases",
            return_value=fake_sync_result,
        ) as mock_sync_cases,
        patch(
            "app.services.sync_service._select_cases_for_detail_rotation",
            return_value=[],
        ),
        patch(
            "app.services.sync_service.detect_and_sync_movements",
            new_callable=AsyncMock,
            return_value=(5, 2, []),
        ),
    ):
        result = await sync_one_cu_client(fake_sc, db, client, batch=50)

    # sync_cases must be called with the FIRM lawyer id, not the client id
    assert mock_sync_cases.called
    call_args = mock_sync_cases.call_args
    called_lawyer_id = call_args[0][0] if call_args[0] else call_args[1]["lawyer_id"]
    assert called_lawyer_id == lawyer.id

    assert result == (5, 2, 0)


# ---------------------------------------------------------------------------
# b) client_id is stamped onto Case rows after sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_one_cu_client_sets_client_id_on_cases(db):
    """After sync, Case.client_id is set to client.id for matching rol rows."""
    from app.models.case import Case
    from app.models.court import Court
    from app.services.client_sync import sync_one_cu_client

    lawyer = _make_lawyer(db)
    client = _make_client(db, assigned_lawyer_id=lawyer.id)

    # Need a court row because Case.court_id is NOT NULL
    court = Court(code="JT01", name="Juzgado Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1234-2024",
        competencia="civil",
    )
    db.add(case)
    db.commit()

    fake_sc = _fake_scraper()
    fake_session = MagicMock()
    fake_api_cases = [_fake_api_case(rol="C-1234-2024")]
    fake_sc.get_my_cases = AsyncMock(return_value=fake_api_cases)

    with (
        patch(
            "app.scrapper.pjud.clave_unica.ClaveUnicaAuth.login",
            new_callable=AsyncMock,
            return_value=fake_session,
        ),
        patch("app.services.sync_service.SyncService.sync_cases"),
        patch(
            "app.services.sync_service._select_cases_for_detail_rotation",
            return_value=[],
        ),
        patch(
            "app.services.sync_service.detect_and_sync_movements",
            new_callable=AsyncMock,
            return_value=(0, 0, []),
        ),
    ):
        await sync_one_cu_client(fake_sc, db, client, batch=50)

    db.expire_all()
    reloaded = db.query(Case).filter(Case.rol == "C-1234-2024").first()
    assert reloaded is not None
    assert reloaded.client_id == client.id


# ---------------------------------------------------------------------------
# c) ValueError when no assigned_lawyer_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_one_cu_client_raises_if_no_assigned_lawyer():
    """Raises ValueError when client.assigned_lawyer_id is None."""
    from app.services.client_sync import sync_one_cu_client

    client = MagicMock()
    client.rut = "22222222-2"
    client.assigned_lawyer_id = None
    client.encrypted_clave_unica_password = encrypt_pjud_password("pwd")

    with pytest.raises(ValueError, match="assigned_lawyer_id"):
        await sync_one_cu_client(MagicMock(), MagicMock(), client)


# ---------------------------------------------------------------------------
# d) ValueError when no encrypted_clave_unica_password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_one_cu_client_raises_if_no_cu():
    """Raises ValueError when client.encrypted_clave_unica_password is None."""
    from app.services.client_sync import sync_one_cu_client

    client = MagicMock()
    client.rut = "22222222-2"
    client.assigned_lawyer_id = 42
    client.encrypted_clave_unica_password = None

    with pytest.raises(ValueError, match="encrypted_clave_unica_password"):
        await sync_one_cu_client(MagicMock(), MagicMock(), client)


# ---------------------------------------------------------------------------
# e) clave_unica_rut override is passed to CU login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_one_cu_client_uses_clave_unica_rut_override(db):
    """When clave_unica_rut differs from rut, CU login uses clave_unica_rut."""
    from app.services.client_sync import sync_one_cu_client
    from app.scrapper.pjud.clave_unica import ClaveUnicaCredentials

    lawyer = _make_lawyer(db)
    client = _make_client(
        db,
        rut="11111111-1",
        clave_unica_rut="99999999-9",
        assigned_lawyer_id=lawyer.id,
        cu_password="pw",
    )
    db.commit()

    fake_sc = _fake_scraper()
    fake_session = MagicMock()
    fake_sc.get_my_cases = AsyncMock(return_value=[])

    captured_creds: list[ClaveUnicaCredentials] = []

    async def mock_login(_self, _page, creds, _lawyer_id):
        # Patching the class method: Python injects `self` as first arg.
        captured_creds.append(creds)
        return fake_session

    with (
        patch(
            "app.scrapper.pjud.clave_unica.ClaveUnicaAuth.login",
            new=mock_login,
        ),
        patch("app.services.sync_service.SyncService.sync_cases"),
        patch(
            "app.services.sync_service._select_cases_for_detail_rotation",
            return_value=[],
        ),
        patch(
            "app.services.sync_service.detect_and_sync_movements",
            new_callable=AsyncMock,
            return_value=(0, 0, []),
        ),
    ):
        await sync_one_cu_client(fake_sc, db, client, batch=50)

    assert len(captured_creds) == 1
    assert captured_creds[0].rut == "99999999-9"


# ---------------------------------------------------------------------------
# f) _select_cu_clients query logic (integration test)
# ---------------------------------------------------------------------------


def test_select_cu_clients_query(db):
    """Active clients with CU + assigned_lawyer are selected; others excluded."""
    from app.models.lawyer import Lawyer
    from app.models.client import Client
    from scripts.cu_client_sync import _select_cu_clients

    lawyer = Lawyer(rut="11111111-1", name="Firm Lawyer")
    db.add(lawyer)
    db.flush()

    # Should be selected: active, has CU, has assigned lawyer
    good = Client(
        rut="22222222-2",
        encrypted_clave_unica_password=encrypt_pjud_password("p1"),
        assigned_lawyer_id=lawyer.id,
        is_active=True,
    )
    # Should be excluded: no CU
    no_cu = Client(
        rut="33333333-3",
        assigned_lawyer_id=lawyer.id,
        is_active=True,
    )
    # Should be excluded: no assigned lawyer
    no_lawyer = Client(
        rut="44444444-4",
        encrypted_clave_unica_password=encrypt_pjud_password("p3"),
        is_active=True,
    )
    # Should be excluded: inactive
    inactive = Client(
        rut="55555555-5",
        encrypted_clave_unica_password=encrypt_pjud_password("p4"),
        assigned_lawyer_id=lawyer.id,
        is_active=False,
    )
    db.add_all([good, no_cu, no_lawyer, inactive])
    db.commit()

    result = _select_cu_clients(db)
    ruts = {c.rut for c in result}

    assert "22222222-2" in ruts, "active client with CU + lawyer must be included"
    assert "33333333-3" not in ruts, "client without CU must be excluded"
    assert "44444444-4" not in ruts, "client without assigned lawyer must be excluded"
    assert "55555555-5" not in ruts, "inactive client must be excluded"
