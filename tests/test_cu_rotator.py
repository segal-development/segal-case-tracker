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
