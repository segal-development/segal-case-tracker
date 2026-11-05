"""GET /api/v1/stats/scraping-status: the admin-only view behind the app banner.

Admin gets the exact shape; auditors, lawyers and anonymous callers do not get in.
``bloqueo_ip`` exists only for a breaker event of the last 12 hours. The endpoint
evaluates with the real clock, so every row is anchored to ``utcnow()``.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.alerta_operativa import AlertaOperativa
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory

URL = "/api/v1/stats/scraping-status"
TIPO_BLOQUEO = "scraping_bloqueo_ip"
KEYS = {
    "estado",
    "motivo",
    "ultimo_exito",
    "horas_sin_exito",
    "fallas_consecutivas",
    "bloqueo_ip",
}


def _lawyer(db, rut: str, role: str) -> Lawyer:
    obj = Lawyer(rut=rut, name=f"User {role}", role=role)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def admin(db):
    return _lawyer(db, "11111111-1", "admin")


def _login_as(lawyer: Lawyer) -> None:
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock


def _run(db, lawyer, *, status: str, hace_horas: float, error: str | None = None):
    inicio = datetime.utcnow() - timedelta(hours=hace_horas)
    db.add(SyncHistory(
        lawyer_id=lawyer.id,
        competencia="civil",
        status=status,
        error_message=error,
        started_at=inicio,
        completed_at=inicio + timedelta(seconds=30),
    ))
    db.commit()


def _event(db, *, hace_horas: float, detalle: str = "bloqueo", tipo: str = TIPO_BLOQUEO):
    db.add(AlertaOperativa(
        tipo=tipo,
        estado=None,
        detalle=detalle,
        enviada_at=datetime.utcnow() - timedelta(hours=hace_horas),
    ))
    db.commit()


class TestAuthorization:
    def test_admin_gets_200(self, client, db, admin):
        _login_as(admin)

        assert client.get(URL).status_code == 200

    @pytest.mark.parametrize("role", ["auditor", "lawyer"])
    def test_other_roles_get_403(self, client, db, role):
        other = _lawyer(db, "22222222-2", role)
        _login_as(other)

        resp = client.get(URL)

        assert resp.status_code == 403
        assert "estado" not in resp.json()

    def test_anonymous_is_rejected(self, client, db):
        assert client.get(URL).status_code in (401, 403)

    def test_garbage_token_is_rejected(self, client, db):
        resp = client.get(URL, headers={"Authorization": "Bearer not-a-jwt"})

        assert resp.status_code in (401, 403)


class TestShape:
    def test_failing_scraping_has_the_exact_shape(self, client, db, admin):
        _run(db, admin, status="completed", hace_horas=9)
        for h in (5, 4, 3, 2, 1):
            _run(db, admin, status="failed", hace_horas=h, error="Not authenticated")
        _login_as(admin)

        body = client.get(URL).json()

        assert set(body) == KEYS
        assert body["estado"] == "fallando"
        assert "fallaron seguidas" in body["motivo"]
        assert isinstance(body["motivo"], str)
        assert body["fallas_consecutivas"] == 5
        assert body["horas_sin_exito"] == pytest.approx(9, abs=0.2)
        assert body["bloqueo_ip"] is None
        parsed = datetime.fromisoformat(body["ultimo_exito"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None, "an ISO instant the browser can place in time"
        assert abs((datetime.now(timezone.utc) - parsed) - timedelta(hours=9)) < timedelta(minutes=5)

    def test_stale_scraping_is_caido(self, client, db, admin):
        _run(db, admin, status="completed", hace_horas=10)
        _login_as(admin)

        body = client.get(URL).json()

        assert body["estado"] == "caido"
        assert body["fallas_consecutivas"] == 0

    def test_healthy_scraping(self, client, db, admin):
        _run(db, admin, status="completed", hace_horas=0.5)
        _login_as(admin)

        body = client.get(URL).json()

        assert set(body) == KEYS
        assert body["estado"] == "ok"
        assert body["bloqueo_ip"] is None

    def test_empty_database_is_ok_with_nulls(self, client, db, admin):
        _login_as(admin)

        body = client.get(URL).json()

        assert body["estado"] == "ok"
        assert body["ultimo_exito"] is None
        assert body["horas_sin_exito"] is None
        assert body["fallas_consecutivas"] == 0
        assert set(body) == KEYS


class TestBloqueoIp:
    def test_present_for_an_event_of_the_last_12_hours(self, client, db, admin):
        _event(db, hace_horas=2, detalle="El PJUD rechazó 3 sesiones")
        _login_as(admin)

        bloqueo = client.get(URL).json()["bloqueo_ip"]

        assert set(bloqueo) == {"desde", "detalle"}
        assert bloqueo["detalle"] == "El PJUD rechazó 3 sesiones"
        desde = datetime.fromisoformat(bloqueo["desde"].replace("Z", "+00:00"))
        assert desde.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - desde) - timedelta(hours=2)) < timedelta(minutes=5)

    def test_present_just_inside_the_window(self, client, db, admin):
        _event(db, hace_horas=11.5)
        _login_as(admin)

        assert client.get(URL).json()["bloqueo_ip"] is not None

    def test_null_just_outside_the_window(self, client, db, admin):
        _event(db, hace_horas=12.5)
        _login_as(admin)

        assert client.get(URL).json()["bloqueo_ip"] is None

    def test_null_without_events(self, client, db, admin):
        _login_as(admin)

        assert client.get(URL).json()["bloqueo_ip"] is None

    def test_other_alert_types_do_not_show_as_a_block(self, client, db, admin):
        _event(db, hace_horas=1, tipo="scraping_detenido")
        _login_as(admin)

        assert client.get(URL).json()["bloqueo_ip"] is None

    def test_the_most_recent_event_wins(self, client, db, admin):
        _event(db, hace_horas=9, detalle="viejo")
        _event(db, hace_horas=1, detalle="nuevo")
        _login_as(admin)

        assert client.get(URL).json()["bloqueo_ip"]["detalle"] == "nuevo"

    def test_a_block_shows_even_if_the_run_history_looks_healthy(self, client, db, admin):
        _run(db, admin, status="completed", hace_horas=0.2)
        _event(db, hace_horas=1)
        _login_as(admin)

        body = client.get(URL).json()

        assert body["estado"] == "ok"
        assert body["bloqueo_ip"] is not None


class TestReadOnly:
    def test_get_writes_nothing(self, client, db, admin):
        _event(db, hace_horas=1)
        _login_as(admin)
        before = db.query(AlertaOperativa).count()

        client.get(URL)

        assert db.query(AlertaOperativa).count() == before

    def test_non_get_methods_are_not_allowed(self, client, db, admin):
        _login_as(admin)

        assert client.post(URL).status_code == 405
        assert client.delete(URL).status_code == 405


class TestPublicHealthUnchanged:
    def test_public_endpoint_stays_minimal_and_unauthenticated(self, client, db, admin):
        _run(db, admin, status="completed", hace_horas=0.5)
        _event(db, hace_horas=1)

        resp = client.get("/health/scraping")

        assert resp.status_code == 200
        assert resp.json() == {"scraping": "ok"}

    def test_public_endpoint_still_answers_503_when_failing(self, client, db, admin):
        _run(db, admin, status="completed", hace_horas=9)
        for h in (5, 4, 3, 2, 1):
            _run(db, admin, status="failed", hace_horas=h)

        resp = client.get("/health/scraping")

        assert resp.status_code == 503
        assert resp.json() == {"detail": "fallando"}
