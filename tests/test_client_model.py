"""Tests for the Client model — DB persistence, defaults, and crypto roundtrip."""

import pytest
from app.core.security import decrypt_pjud_password, encrypt_pjud_password


# ---------------------------------------------------------------------------
# test_client_create_and_query — requires db fixture (SQLite)
# ---------------------------------------------------------------------------


def test_client_create_and_query(db):
    """Create a Client with an assigned Lawyer, persist, and verify all fields."""
    from app.models.lawyer import Lawyer
    from app.models.client import Client

    lawyer = Lawyer(rut="11111111-1", name="Test Lawyer")
    db.add(lawyer)
    db.flush()  # get lawyer.id without committing

    client = Client(
        rut="22222222-2",
        nombre="Juan Pérez",
        clave_unica_rut="22222222-2",
        encrypted_clave_unica_password=encrypt_pjud_password("secret-cu"),
        assigned_lawyer_id=lawyer.id,
        is_active=True,
        source="crm",
    )
    db.add(client)
    db.commit()

    loaded = db.query(Client).filter(Client.rut == "22222222-2").first()
    assert loaded is not None
    assert loaded.nombre == "Juan Pérez"
    assert loaded.clave_unica_rut == "22222222-2"
    assert loaded.assigned_lawyer_id == lawyer.id
    assert loaded.is_active is True
    assert loaded.source == "crm"
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


# ---------------------------------------------------------------------------
# test_client_encrypt_decrypt_roundtrip — pure unit, no DB
# ---------------------------------------------------------------------------


def test_client_encrypt_decrypt_roundtrip():
    """Fernet roundtrip: decrypt(encrypt(x)) == x."""
    plaintext = "super-secret-cu-password!"
    assert decrypt_pjud_password(encrypt_pjud_password(plaintext)) == plaintext


# ---------------------------------------------------------------------------
# test_client_cu_roundtrip — store encrypted, reload, decrypt
# ---------------------------------------------------------------------------


def test_client_cu_roundtrip(db):
    """Create Client with encrypted CU password, reload from DB, decrypt correctly."""
    from app.models.lawyer import Lawyer
    from app.models.client import Client

    lawyer = Lawyer(rut="33333333-3", name="Firm Lawyer")
    db.add(lawyer)
    db.flush()

    original_password = "my-cu-secret-2024"
    client = Client(
        rut="44444444-4",
        nombre="María González",
        encrypted_clave_unica_password=encrypt_pjud_password(original_password),
        assigned_lawyer_id=lawyer.id,
    )
    db.add(client)
    db.commit()

    loaded = db.query(Client).filter(Client.rut == "44444444-4").first()
    assert loaded is not None
    decrypted = decrypt_pjud_password(loaded.encrypted_clave_unica_password)
    assert decrypted == original_password


# ---------------------------------------------------------------------------
# test_client_defaults — is_active and source use correct defaults
# ---------------------------------------------------------------------------


def test_client_defaults(db):
    """is_active defaults to True; source defaults to 'crm'."""
    from app.models.lawyer import Lawyer
    from app.models.client import Client

    lawyer = Lawyer(rut="55555555-5", name="Default Test Lawyer")
    db.add(lawyer)
    db.flush()

    client = Client(
        rut="66666666-6",
        assigned_lawyer_id=lawyer.id,
    )
    db.add(client)
    db.commit()

    loaded = db.query(Client).filter(Client.rut == "66666666-6").first()
    assert loaded is not None
    assert loaded.is_active is True
    assert loaded.source == "crm"
    assert loaded.nombre is None
    assert loaded.encrypted_clave_unica_password is None
