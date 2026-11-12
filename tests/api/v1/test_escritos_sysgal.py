"""/escritos-sysgal endpoints: admin only, Sysgal mocked via dependency override."""

from datetime import date, datetime, timedelta

import pytest

from app.api.v1 import escritos_sysgal as api
from app.core.security import create_access_token
from app.main import app
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services.sysgal_client import SysgalError

ADMIN_RUT, AUDITOR_RUT, LAWYER_RUT = "11111111-1", "33333333-3", "22222222-2"
BASE = "/api/v1/escritos-sysgal"
DOWN = "Sysgal no respondió. Intenta de nuevo en unos minutos."
TIPO = "93bc9391-8b3c-4bac-9aa2-91c081f71e06"

STATS = {
    "desde": "2026-10-06", "hasta": "2026-11-12", "total": 27,
    "por_abogado": [{"abogado_id": 272, "nombre": "Valentina Gomez Diaz", "total": 10},
                    {"abogado_id": 93, "nombre": "Carla Lavin Benito", "total": 1},
                    {"abogado_id": 5, "nombre": "Procurador", "total": 2}],
    "por_tipo": [{"tipo_id": TIPO, "tipo": "CURSO_PROGRESIVO_ABAN", "total": 12},
                 {"tipo_id": None, "tipo": "ABANDONO_3_ANOS", "total": 3}],
}
PAGE = {
    "success": True, "desde": "2026-11-01", "hasta": "2026-11-12", "siguiente_desde_id": 5,
    "data": [
        {"id_escrito": 3, "id_causa": 115260, "rol": "C-9061-2026", "tribunal": "19º Juzgado Civil de Santiago",
         "tipo_id": TIPO, "tipo": "CURSO_PROGRESIVO_ABAN", "fecha_generacion": "2026-11-03 16:53:28",
         "abogado_id": 93, "nombre": "Carla Lavin Benito", "origen": "autoservicio"},
        {"id_escrito": 5, "id_causa": 1, "rol": "C-1-2020", "tribunal": "T", "tipo_id": None,
         "tipo": "ABANDONO_3_ANOS", "fecha_generacion": "2026-11-04 11:01:57", "abogado_id": 5,
         "nombre": "Procurador", "origen": "encargo"},
    ],
}


def _headers(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))}


class FakeClient:
    def __init__(self, stats=None, page=None, error=None):
        self.stats, self.page, self.error = stats or STATS, page or PAGE, error
        self.stats_calls, self.page_calls = [], []

    def escritos_estadisticas(self, desde, hasta):
        self.stats_calls.append((desde, hasta))
        if self.error:
            raise self.error
        return dict(self.stats)

    def escritos(self, desde, hasta, abogado_id=None, tipo_id=None, desde_id=None, limite=500):
        self.page_calls.append((desde, hasta, abogado_id, tipo_id, desde_id, limite))
        if self.error:
            raise self.error
        return dict(self.page)


@pytest.fixture
def people(db):
    db.add_all([Lawyer(rut=ADMIN_RUT, name="Admin", role="admin"),
                Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor"),
                Lawyer(rut=LAWYER_RUT, name="Lawyer", role="lawyer")])
    db.commit()
    lw = db.query(Lawyer).filter(Lawyer.rut == LAWYER_RUT).one()
    db.add(SysgalUsuario(sysgal_id=93, nombre="Carla", lawyer_id=lw.id, synced_at=datetime.utcnow()))
    db.add(SysgalUsuario(sysgal_id=272, nombre="V", lawyer_id=None, synced_at=datetime.utcnow()))
    db.commit()
    return lw


@pytest.fixture
def admin(people):
    return _headers(ADMIN_RUT)


@pytest.fixture
def fake():
    fc = FakeClient()
    app.dependency_overrides[api.get_sysgal_client] = lambda: fc
    yield fc
    app.dependency_overrides.pop(api.get_sysgal_client, None)


class TestAuth:
    @pytest.mark.parametrize("rut", [LAWYER_RUT, AUDITOR_RUT])
    @pytest.mark.parametrize("path", ["/estadisticas?desde=2026-11-01&hasta=2026-11-10",
                                      "/listado?desde=2026-11-01&hasta=2026-11-10"])
    def test_non_admin_forbidden(self, client, people, fake, rut, path):
        assert client.get(BASE + path, headers=_headers(rut)).status_code == 403
        assert fake.stats_calls == [] and fake.page_calls == []

    def test_anonymous_rejected(self, client, people, fake):
        assert client.get(f"{BASE}/estadisticas?desde=2026-11-01&hasta=2026-11-10").status_code in (401, 403)


class TestEstadisticas:
    def test_unwrapped_stats_with_lawyer_id(self, client, admin, people, fake):
        r = client.get(f"{BASE}/estadisticas?desde=2026-10-06&hasta=2026-11-12", headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 27 and body["desde"] == "2026-10-06" and body["por_tipo"] == STATS["por_tipo"]
        assert [(a["abogado_id"], a["total"], a["lawyer_id"]) for a in body["por_abogado"]] == [
            (272, 10, None), (93, 1, people.id), (5, 2, None)]
        assert fake.stats_calls == [(date(2026, 10, 6), date(2026, 11, 12))]

    def test_empty_period(self, client, admin, people):
        empty = {"desde": "2026-09-01", "hasta": "2026-09-30", "total": 0, "por_abogado": [], "por_tipo": []}
        fc = FakeClient(stats=empty)
        app.dependency_overrides[api.get_sysgal_client] = lambda: fc
        try:
            r = client.get(f"{BASE}/estadisticas?desde=2026-09-01&hasta=2026-09-30", headers=admin)
        finally:
            app.dependency_overrides.pop(api.get_sysgal_client, None)
        assert r.status_code == 200 and r.json() == empty

    def test_range_over_six_months(self, client, admin, fake):
        r = client.get(f"{BASE}/estadisticas?desde=2026-01-01&hasta=2026-11-10", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == "El rango máximo es de 6 meses."
        assert fake.stats_calls == []

    def test_182_days_ok(self, client, admin, fake):
        hasta = date(2026, 1, 1) + timedelta(days=182)
        assert client.get(f"{BASE}/estadisticas?desde=2026-01-01&hasta={hasta}", headers=admin).status_code == 200

    def test_desde_after_hasta(self, client, admin, fake):
        r = client.get(f"{BASE}/estadisticas?desde=2026-11-10&hasta=2026-11-01", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == "La fecha desde debe ser anterior a hasta."
        assert fake.stats_calls == []

    def test_sysgal_down_is_502(self, client, admin):
        fc = FakeClient(error=SysgalError("HTTP 503"))
        app.dependency_overrides[api.get_sysgal_client] = lambda: fc
        try:
            r = client.get(f"{BASE}/estadisticas?desde=2026-11-01&hasta=2026-11-10", headers=admin)
        finally:
            app.dependency_overrides.pop(api.get_sysgal_client, None)
        assert r.status_code == 502 and r.json()["detail"] == DOWN


class TestListado:
    def test_shape_and_enrichment(self, client, admin, people, db, fake):
        from app.models.case import Case
        from app.models.court import Court

        court = Court(code="C1", name="J", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(lawyer_id=people.id, court_id=court.id, rol="C-9061-2026", competencia="civil")
        db.add(case)
        db.flush()
        db.add(SysgalCausa(sysgal_id=115260, rol="C-9061-2026", rol_norm="c-9061-2026", tribunal="T",
                           case_id=case.id, synced_at=datetime.utcnow()))
        db.commit()
        r = client.get(f"{BASE}/listado?desde=2026-11-01&hasta=2026-11-12", headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"success", "desde", "hasta", "data", "siguiente_desde_id"}
        assert body["desde"] == "2026-11-01" and body["siguiente_desde_id"] == 5
        assert set(body["data"][0]) == {"id_escrito", "id_causa", "rol", "tribunal", "tipo_id", "tipo",
                                        "fecha_generacion", "abogado_id", "nombre", "origen",
                                        "lawyer_id", "case_id"}
        assert [(e["id_escrito"], e["lawyer_id"], e["case_id"]) for e in body["data"]] == [
            (3, people.id, case.id), (5, None, None)]
        assert body["data"][1]["tipo_id"] is None
        assert fake.page_calls == [(date(2026, 11, 1), date(2026, 11, 12), None, None, None, 100)]

    def test_passes_filters_and_cursor(self, client, admin, fake):
        r = client.get(f"{BASE}/listado?desde=2026-11-01&hasta=2026-11-12&abogado_id=93&tipo_id={TIPO}"
                       "&desde_id=4&limite=50", headers=admin)
        assert r.status_code == 200
        assert fake.page_calls == [(date(2026, 11, 1), date(2026, 11, 12), 93, TIPO, 4, 50)]

    def test_empty_last_page(self, client, admin, people):
        fc = FakeClient(page={"success": True, "desde": "2026-09-01", "hasta": "2026-09-30", "data": [],
                              "siguiente_desde_id": None})
        app.dependency_overrides[api.get_sysgal_client] = lambda: fc
        try:
            body = client.get(f"{BASE}/listado?desde=2026-09-01&hasta=2026-09-30", headers=admin).json()
        finally:
            app.dependency_overrides.pop(api.get_sysgal_client, None)
        assert body["data"] == [] and body["siguiente_desde_id"] is None

    def test_validates_range_and_limite(self, client, admin, fake):
        r = client.get(f"{BASE}/listado?desde=2026-01-01&hasta=2026-11-10", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == "El rango máximo es de 6 meses."
        r = client.get(f"{BASE}/listado?desde=2026-11-10&hasta=2026-11-01", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == "La fecha desde debe ser anterior a hasta."
        for limite in (0, 501):
            r = client.get(f"{BASE}/listado?desde=2026-11-01&hasta=2026-11-10&limite={limite}", headers=admin)
            assert r.status_code == 422
        assert fake.page_calls == []

    def test_sysgal_down_is_502(self, client, admin):
        fc = FakeClient(error=SysgalError("HTTP 503"))
        app.dependency_overrides[api.get_sysgal_client] = lambda: fc
        try:
            r = client.get(f"{BASE}/listado?desde=2026-11-01&hasta=2026-11-10", headers=admin)
        finally:
            app.dependency_overrides.pop(api.get_sysgal_client, None)
        assert r.status_code == 502 and r.json()["detail"] == DOWN
