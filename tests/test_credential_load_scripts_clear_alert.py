"""Key-loading scripts must reset the failure-alert marker.

``PUT /credentials/me`` already sets ``credential_alert_sent_at = None`` when a
new key is stored, so a future failure alerts again. The two loader scripts
stored keys without doing it: a new key that is also wrong would never alert.
"""

from datetime import datetime

import pytest

from app.models.lawyer import Lawyer

RUT = "19586894-8"
ALERTED_AT = datetime(2026, 9, 30, 13, 0, 0)


@pytest.fixture
def alerted_lawyer(db, monkeypatch):
    lawyer = Lawyer(rut=RUT, name="Alerted Lawyer", role="lawyer")
    lawyer.credential_alert_sent_at = ALERTED_AT
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    # The scripts open their own session; point them at the test one.
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db)
    # The interactive script closes its session; keep the test one attached.
    monkeypatch.setattr(db, "close", lambda: None)
    return lawyer


def test_bulk_loader_clears_alert_marker(alerted_lawyer, db, tmp_path, monkeypatch):
    from scripts import load_pjud_passwords

    claves = tmp_path / "claves.txt"
    claves.write_text(f"{RUT}=new-secret-value\n")
    monkeypatch.setattr(load_pjud_passwords, "CLAVES_FILE", str(claves))

    assert load_pjud_passwords.main() == 0

    db.refresh(alerted_lawyer)
    assert alerted_lawyer.encrypted_pjud_password
    assert alerted_lawyer.credential_alert_sent_at is None


def test_interactive_loader_clears_alert_marker(alerted_lawyer, db, monkeypatch):
    from scripts import cargar_clave_pjud_interactivo as script

    answers = iter([RUT, ""])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(script.getpass, "getpass", lambda *_a, **_k: "new-secret-value")

    assert script.main() == 0

    db.refresh(alerted_lawyer)
    assert alerted_lawyer.encrypted_pjud_password
    assert alerted_lawyer.credential_alert_sent_at is None


def test_interactive_loader_keeps_marker_when_confirmation_fails(alerted_lawyer, db, monkeypatch):
    from scripts import cargar_clave_pjud_interactivo as script

    answers = iter([RUT, ""])
    passwords = iter(["one", "two"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(script.getpass, "getpass", lambda *_a, **_k: next(passwords))

    script.main()

    db.refresh(alerted_lawyer)
    assert alerted_lawyer.encrypted_pjud_password is None
    assert alerted_lawyer.credential_alert_sent_at == ALERTED_AT
