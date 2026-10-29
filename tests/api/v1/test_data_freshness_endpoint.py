"""GET /api/v1/stats/data-freshness: is the PJUD data fresh enough to trust?

Open to EVERY signed-in role (unlike scraping-status, which is admin only and carries
the IP-block detail): lawyers need to know that plazos and semaforos may be stale.
It exposes only the age of the last successful sync, never why it stopped.
"""
from datetime import datetime, timedelta

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory

URL = "/api/v1/stats/data-freshness"
KEYS = {"verificado", "umbral_horas", "horas_sin_exito", "ultimo_exito"}


def _lawyer(db, rut: str, role: str) -> Lawyer:
    obj = Lawyer(rut=rut, name=f"User {role}", role=role)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _login_as(lawyer: Lawyer) -> None:
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock


def _run(db, lawyer, *, status: str, hace_horas: float):
    inicio = datetime.utcnow() - timedelta(hours=hace_horas)
    db.add(SyncHistory(
        lawyer_id=lawyer.id, competencia="civil", status=status,
        started_at=inicio, completed_at=inicio + timedelta(seconds=30),
    ))
    db.commit()


@pytest.fixture
def user(db):
    return _lawyer(db, "33333333-3", "lawyer")


class TestAuthorization:
    @pytest.mark.parametrize("role", ["lawyer", "auditor", "admin"])
    def test_every_signed_in_role_gets_200(self, client, db, role):
        _login_as(_lawyer(db, "44444444-4", role))

        assert client.get(URL).status_code == 200

    def test_anonymous_is_rejected(self, client, db):
        resp = client.get(URL)

        assert resp.status_code in (401, 403)
        assert "verificado" not in resp.text


class TestVerdict:
    def test_a_recent_sync_is_verified(self, client, db, user):
        _run(db, user, status="completed", hace_horas=3)
        _login_as(user)

        body = client.get(URL).json()

        assert set(body) == KEYS
        assert body["verificado"] is True
        assert body["umbral_horas"] == 24
        assert body["horas_sin_exito"] == pytest.approx(3, abs=0.2)

    def test_exactly_inside_the_threshold_is_still_verified(self, client, db, user):
        _run(db, user, status="completed", hace_horas=23.5)
        _login_as(user)

        assert client.get(URL).json()["verificado"] is True

    def test_past_the_threshold_is_not_verified(self, client, db, user):
        _run(db, user, status="completed", hace_horas=25)
        _login_as(user)

        body = client.get(URL).json()

        assert body["verificado"] is False
        assert body["horas_sin_exito"] == pytest.approx(25, abs=0.2)

    def test_failed_runs_never_count_as_verification(self, client, db, user):
        _run(db, user, status="completed", hace_horas=30)
        _run(db, user, status="failed", hace_horas=1)
        _login_as(user)

        assert client.get(URL).json()["verificado"] is False

    def test_no_sync_on_record_is_not_verified(self, client, db, user):
        _login_as(user)

        body = client.get(URL).json()

        assert body["verificado"] is False
        assert body["horas_sin_exito"] is None
        assert body["ultimo_exito"] is None

    def test_the_last_success_is_an_instant_the_browser_can_place(self, client, db, user):
        _run(db, user, status="completed", hace_horas=2)
        _login_as(user)

        stamp = client.get(URL).json()["ultimo_exito"]

        assert stamp.endswith("Z") or "+" in stamp


class TestNothingExtraLeaks:
    def test_no_ip_block_detail_for_a_plain_lawyer(self, client, db, user):
        _run(db, user, status="completed", hace_horas=30)
        _login_as(user)

        body = client.get(URL).json()

        assert set(body) == KEYS
        assert "bloqueo_ip" not in body and "motivo" not in body
