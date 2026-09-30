"""``scripts/validar_clave.py`` validates a lawyer's PJUD clave on demand.

PJUD allows ONE active session per IP, so the script must refuse to log in
while the scraping station runs on this machine. ``_reauth`` is always mocked:
no test here performs a real login.
"""

import pytest

from app.models.lawyer import Lawyer

RUT = "20217325-K"
KNOWN_CLAVE = "SuperSecreta-9876"


@pytest.fixture
def script(db, monkeypatch):
    from scripts import validar_clave

    # The script opens its own session; point it at the test one.
    monkeypatch.setattr(validar_clave, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(validar_clave, "get_session_store", lambda: object())
    return validar_clave


@pytest.fixture
def lawyer(db):
    row = Lawyer(rut=RUT, name="Abogada Prueba", role="lawyer")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def reauth(script, monkeypatch):
    """Mocked ``_reauth``: records calls, succeeds by default."""
    calls = []
    result = {"value": (object(), None)}

    async def fake(lawyer, store):
        calls.append(lawyer.rut)
        return result["value"]

    monkeypatch.setattr(script, "_reauth", fake)
    fake.calls = calls
    fake.result = result
    return fake


def _station(monkeypatch, script, state):
    monkeypatch.setattr(script, "estacion_activa", lambda: state)


# --- the guard ---------------------------------------------------------------


def test_active_station_aborts_without_calling_reauth(script, lawyer, reauth, monkeypatch, capsys):
    _station(monkeypatch, script, True)

    code = script.main([RUT])

    assert reauth.calls == []
    assert code == script.EXIT_ABORTADO
    assert code != 0
    out = capsys.readouterr().out
    assert "UNA sola sesión" in out
    assert "--forzar" in out


def test_forzar_validates_even_with_active_station(script, lawyer, reauth, monkeypatch):
    _station(monkeypatch, script, True)

    code = script.main(["--forzar", RUT])

    assert reauth.calls == [RUT]
    assert code == 0


def test_inactive_station_validates_normally(script, lawyer, reauth, monkeypatch):
    _station(monkeypatch, script, False)

    assert script.main([RUT]) == 0
    assert reauth.calls == [RUT]


def test_unknown_station_state_does_not_block_and_warns(script, lawyer, reauth, monkeypatch, capsys):
    _station(monkeypatch, script, None)

    code = script.main([RUT])

    assert reauth.calls == [RUT]
    assert code == 0
    assert "no se pudo verificar" in capsys.readouterr().out.lower()


# --- station detection -------------------------------------------------------


def _fake_run(monkeypatch, script, launchctl, ps):
    def run(cmd):
        out = launchctl if cmd[0] == "launchctl" else ps
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(script, "_ejecutar", run)


LAUNCHCTL_RUNNING = "PID\tStatus\tLabel\n534\t0\tcom.segal.syncstation\n538\t0\tcom.segal.sqlproxy\n"
LAUNCHCTL_STOPPED = "PID\tStatus\tLabel\n-\t0\tcom.segal.syncstation\n538\t0\tcom.segal.sqlproxy\n"
LAUNCHCTL_ONLY_PROXY = "PID\tStatus\tLabel\n538\t0\tcom.segal.sqlproxy\n"
PS_NO_WORKER = "  100 /usr/bin/python other.py\n"
PS_WORKER = "  777 /venv/bin/python -m app.workers.sync_scheduler\n"


def test_detects_running_launch_agent(script, monkeypatch):
    _fake_run(monkeypatch, script, LAUNCHCTL_RUNNING, PS_NO_WORKER)
    assert script.estacion_activa() is True


def test_loaded_but_not_running_agent_is_inactive(script, monkeypatch):
    _fake_run(monkeypatch, script, LAUNCHCTL_STOPPED, PS_NO_WORKER)
    assert script.estacion_activa() is False


def test_sql_proxy_alone_is_not_the_station(script, monkeypatch):
    _fake_run(monkeypatch, script, LAUNCHCTL_ONLY_PROXY, PS_NO_WORKER)
    assert script.estacion_activa() is False


def test_detects_worker_process_outside_launchd(script, monkeypatch):
    _fake_run(monkeypatch, script, LAUNCHCTL_ONLY_PROXY, PS_WORKER)
    assert script.estacion_activa() is True


def test_missing_launchctl_is_undetermined(script, monkeypatch):
    _fake_run(monkeypatch, script, FileNotFoundError("launchctl"), PS_NO_WORKER)
    assert script.estacion_activa() is None


def test_missing_launchctl_but_worker_process_found_is_active(script, monkeypatch):
    _fake_run(monkeypatch, script, FileNotFoundError("launchctl"), PS_WORKER)
    assert script.estacion_activa() is True


def test_failing_probes_are_undetermined(script, monkeypatch):
    _fake_run(monkeypatch, script, OSError("boom"), OSError("boom"))
    assert script.estacion_activa() is None


# --- lawyers / RUTs ----------------------------------------------------------


def test_unknown_rut_does_not_explode_and_others_are_processed(script, lawyer, reauth, monkeypatch, capsys):
    _station(monkeypatch, script, False)

    code = script.main(["11111111-1", RUT])

    assert reauth.calls == [RUT]
    assert code != 0
    assert "11111111-1" in capsys.readouterr().out


def test_rut_is_normalized_before_lookup(script, lawyer, reauth, monkeypatch):
    _station(monkeypatch, script, False)

    assert script.main(["20.217.325-k"]) == 0
    assert reauth.calls == [RUT]


def test_nonzero_exit_when_a_validation_fails(script, lawyer, reauth, monkeypatch, capsys):
    _station(monkeypatch, script, False)
    reauth.result["value"] = (None, "invalid_credentials")

    code = script.main([RUT])

    assert code != 0
    out = capsys.readouterr().out
    assert "invalid_credentials" in out
    assert "Abogada Prueba" in out


def test_success_prints_name_and_result(script, lawyer, reauth, monkeypatch, capsys):
    _station(monkeypatch, script, False)

    script.main([RUT])

    out = capsys.readouterr().out
    assert "Abogada Prueba" in out
    assert "validada" in out.lower()


def test_output_never_contains_the_clave(script, lawyer, reauth, monkeypatch, capsys):
    from app.core.security import encrypt_pjud_password

    lawyer.encrypted_pjud_password = encrypt_pjud_password(KNOWN_CLAVE)
    _station(monkeypatch, script, False)

    async def fake(lw, store):
        # A buggy caller could leak the plaintext through the returned reason.
        from app.core.security import decrypt_pjud_password

        reauth.calls.append(decrypt_pjud_password(lw.encrypted_pjud_password))
        return None, "invalid_credentials"

    monkeypatch.setattr(script, "_reauth", fake)

    script.main([RUT])
    script.main(["--forzar", RUT])

    captured = capsys.readouterr()
    assert KNOWN_CLAVE not in captured.out + captured.err
    assert lawyer.encrypted_pjud_password not in captured.out + captured.err


def test_abort_message_never_contains_the_clave(script, lawyer, reauth, monkeypatch, capsys):
    from app.core.security import encrypt_pjud_password

    lawyer.encrypted_pjud_password = encrypt_pjud_password(KNOWN_CLAVE)
    _station(monkeypatch, script, True)

    script.main([RUT])

    captured = capsys.readouterr()
    assert KNOWN_CLAVE not in captured.out + captured.err
