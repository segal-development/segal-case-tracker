"""Activity audit: middleware recording (actions, logins), S2 safety, S3 resilience."""

import logging

import pytest

from app.core.security import create_access_token, hash_password
from app.models.activity_event import ActivityEvent
from app.models.lawyer import Lawyer
from app.services import activity_audit
from tests.conftest import TestingSessionLocal

PASSWORD = "Sup3r-Secret-Pass-9137"


@pytest.fixture(autouse=True)
def audit_on(monkeypatch):
    monkeypatch.setattr(activity_audit, "ENABLED", True)
    activity_audit.set_session_factory(TestingSessionLocal)
    yield
    activity_audit.set_session_factory(None)


def _lawyer(db, rut, email, role="lawyer", name="Ana Abogada", password=PASSWORD):
    obj = Lawyer(
        rut=rut, name=name, email=email, role=role, is_active=True,
        password_hash=hash_password(password),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _headers(rut):
    return {"Authorization": f"Bearer {create_access_token({'sub': rut})}"}


def _events(db):
    db.expire_all()
    return db.query(ActivityEvent).order_by(ActivityEvent.id).all()


def _all_text(db):
    out = []
    for ev in _events(db):
        out.extend(str(getattr(ev, c.name)) for c in ActivityEvent.__table__.columns)
    return " | ".join(out)


def test_action_recorded_with_template_status_user_and_case_id(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    r = client.put(
        "/api/v1/cases/42/deadlines/7/status",
        json={"status": "cumplido"},
        headers=_headers(lw.rut),
    )
    assert r.status_code == 403
    (ev,) = _events(db)
    assert ev.kind == "action"
    assert ev.method == "PUT"
    assert ev.route == "/api/v1/cases/{case_id}/deadlines/{deadline_id}/status"
    assert ev.path == "/api/v1/cases/42/deadlines/7/status"
    assert ev.status_code == 403
    assert ev.lawyer_id == lw.id
    assert ev.actor_name == "Ana Abogada"
    assert ev.actor_role == "lawyer"
    assert ev.case_id == 42
    assert ev.target == "42"
    assert ev.created_at is not None


def test_case_id_derived_from_id_under_cases(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl", role="admin")
    client.delete("/api/v1/cases/9", headers=_headers(lw.rut))
    (ev,) = _events(db)
    assert ev.case_id == 9
    assert ev.target == "9"


def test_unauthenticated_and_bad_token_recorded_without_lawyer(client, db):
    r1 = client.post("/api/v1/alerts/read-all")
    r2 = client.post("/api/v1/alerts/read-all", headers={"Authorization": "Bearer nope"})
    assert r1.status_code == 401 and r2.status_code == 401
    evs = _events(db)
    assert len(evs) == 2
    assert all(e.lawyer_id is None and e.status_code == 401 for e in evs)


def test_get_requests_not_recorded(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    client.get("/api/v1/auth/me", headers=_headers(lw.rut))
    assert _events(db) == []


def test_excluded_and_outside_api_paths_not_recorded(client, db):
    client.post("/api/v1/pjud/ingest/cases", json={})
    client.post("/api/v1/activity/views", json={"path": "/causas"})
    client.post("/api/sysgal/v1/causas")
    client.post("/health")
    assert _events(db) == []


def test_unknown_path_falls_back_to_path_as_route(client, db):
    client.post("/api/v1/does-not-exist")
    (ev,) = _events(db)
    assert ev.route == "/api/v1/does-not-exist"
    assert ev.status_code == 404


def test_query_string_never_stored(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    client.delete("/api/v1/cases/3?token=SECRETQS&api_key=KEYQS", headers=_headers(lw.rut))
    text = _all_text(db)
    assert "SECRETQS" not in text and "KEYQS" not in text and "?" not in text


def test_bodies_tokens_and_passwords_never_stored(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    token = create_access_token({"sub": lw.rut})
    client.post("/api/v1/auth/web-login", json={"email": "a@x.cl", "password": PASSWORD})
    client.post("/api/v1/auth/web-login", json={"email": "a@x.cl", "password": "WRONGPW-555"})
    client.post(
        "/api/v1/auth/login/clave-unica",
        json={"rut": "11111111-1", "password": "CUPASS-777"},
    )
    client.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "NEWPW-12345678"},
        headers={"Authorization": f"Bearer {token}"},
    )
    client.put(
        "/api/v1/credentials/me",
        json={"password": "CREDPW-888", "system": "pjud"},
        headers={"Authorization": f"Bearer {token}", "X-Api-Key": "KEYHEADER-1"},
    )
    assert len(_events(db)) >= 4
    text = _all_text(db)
    for secret in (PASSWORD, "WRONGPW-555", "CUPASS-777", "NEWPW-12345678", "CREDPW-888",
                   "KEYHEADER-1", token):
        assert secret not in text


def test_login_success_and_last_login_at(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl", role="admin")
    assert lw.last_login_at is None
    r = client.post("/api/v1/auth/web-login", json={"email": "A@x.cl", "password": PASSWORD})
    assert r.status_code == 200
    (ev,) = _events(db)
    assert ev.kind == "login"
    assert ev.route == "/api/v1/auth/web-login"
    assert ev.lawyer_id == lw.id
    assert ev.actor_name == "Ana Abogada"
    assert ev.actor_role == "admin"
    assert ev.status_code == 200
    assert ev.identifier is None
    db.expire_all()
    assert db.get(Lawyer, lw.id).last_login_at is not None


def test_login_failed_records_identifier_only(client, db):
    _lawyer(db, "11111111-1", "a@x.cl")
    r = client.post("/api/v1/auth/web-login", json={"email": "a@x.cl", "password": "bad-pw-1"})
    assert r.status_code == 401
    (ev,) = _events(db)
    assert ev.kind == "login_failed"
    assert ev.identifier == "a@x.cl"
    assert ev.lawyer_id is None
    assert ev.status_code == 401
    assert "bad-pw-1" not in _all_text(db)


def test_login_failed_identifier_trimmed_and_blocked_account(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    lw.is_active = False
    db.commit()
    r = client.post("/api/v1/auth/web-login", json={"email": "a@x.cl", "password": PASSWORD})
    assert r.status_code == 403
    client.post("/api/v1/auth/web-login", json={"email": "e" * 300, "password": "x"})
    evs = _events(db)
    assert [e.kind for e in evs] == ["login_failed", "login_failed"]
    assert len(evs[1].identifier) == 120


def test_login_failed_without_parseable_body_has_no_identifier(client, db):
    client.post("/api/v1/auth/web-login", content=b"not json",
                headers={"Content-Type": "application/json"})
    (ev,) = _events(db)
    assert ev.kind == "action" or ev.kind == "login_failed"
    assert ev.identifier is None


def test_clave_unica_failure_records_rut_identifier(client, db, monkeypatch):
    from app.api.v1 import auth as auth_mod
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuthError

    class _Boom:
        async def __aenter__(self):
            raise ClaveUnicaAuthError("no")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(auth_mod, "BrowserFactory", lambda headless=True: _Boom())
    r = client.post("/api/v1/auth/login/clave-unica",
                    json={"rut": "22222222-2", "password": "CUPASS-777"})
    assert r.status_code == 401
    (ev,) = _events(db)
    assert ev.kind == "login_failed"
    assert ev.identifier == "22222222-2"
    assert "CUPASS-777" not in _all_text(db)


def test_audit_failure_never_breaks_request_and_is_logged(client, db, monkeypatch, caplog):
    lw = _lawyer(db, "11111111-1", "a@x.cl")

    def boom(*a, **k):
        raise RuntimeError("insert exploded")

    monkeypatch.setattr(activity_audit, "insert_event", boom)
    with caplog.at_level(logging.WARNING):
        r = client.delete("/api/v1/cases/5", headers=_headers(lw.rut))
    assert r.status_code == 404
    assert _events(db) == []
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_audit_session_factory_failure_never_breaks_request(client, db):
    def bad_factory():
        raise RuntimeError("no db")

    activity_audit.set_session_factory(bad_factory)
    r = client.post("/api/v1/auth/web-login", json={"email": "z@x.cl", "password": "x"})
    assert r.status_code == 401


def test_user_agent_trimmed(client, db):
    client.post("/api/v1/alerts/read-all", headers={"User-Agent": "U" * 400})
    (ev,) = _events(db)
    assert len(ev.user_agent) == 255


def test_ip_stored_from_asgi_client(db):
    import asyncio

    from app.middleware.activity_audit import ActivityAuditMiddleware

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def noop_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def noop_send(message):
        pass

    scope = {"type": "http", "method": "POST", "path": "/api/v1/alerts/read-all",
             "headers": [], "client": ("10.1.2.3", 5555), "query_string": b""}
    asyncio.run(ActivityAuditMiddleware(inner)(scope, noop_receive, noop_send))
    (ev,) = _events(db)
    assert ev.ip == "10.1.2.3"


def test_public_form_token_never_stored(client, db):
    """The public hito form links carry a bearer-style token in the URL (verifier finding)."""
    client.post("/api/v1/hitos/public/SECRETTOKEN123", json={})
    client.post("/api/v1/hitos/public-procuradores/SECRETTOKEN123", json={})
    client.put("/api/v1/hitos/public/SECRETTOKEN123/hitos/5/evidencia", json={})
    assert "SECRETTOKEN123" not in _all_text(db)


def test_redacted_public_form_event_keeps_the_rest(client, db):
    client.put("/api/v1/hitos/public/SECRETTOKEN123/hitos/5/evidencia", json={})
    ev = _events(db)[-1]
    assert ev.path == "/api/v1/hitos/public/***/hitos/5/evidencia"
    assert ev.route == "/api/v1/hitos/public/{token}/hitos/{hito_id}/evidencia" or "{token}" in ev.route
    assert ev.target != "SECRETTOKEN123"


def test_view_path_with_query_or_fragment_is_rejected(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    for bad in ("/causas?token=abc", "/causas#x", "/causas?a=1#b"):
        r = client.post("/api/v1/activity/views", json={"path": bad}, headers=_headers(lw.rut))
        assert r.status_code == 422
    text = _all_text(db)
    assert "token=abc" not in text and "?" not in text and "#" not in text
