"""/avance-cartera endpoints: admin only, Sysgal mocked via dependency override."""

from datetime import date, datetime, timedelta

import pytest

from app.api.v1 import avance_cartera as api
from app.core.security import create_access_token
from app.main import app
from app.models.avance_cartera import AvanceCarteraSnapshot
from app.models.bono_cierre import CIERRE_CERRADO, BonoCierre
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import avance_cartera as svc
from app.services.sysgal_client import SysgalError

ADMIN_RUT, AUDITOR_RUT, LAWYER_RUT = "11111111-1", "33333333-3", "22222222-2"
BASE = "/api/v1/avance-cartera"
DOWN = "Sysgal no respondió. Intenta de nuevo en unos minutos."


def _headers(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))}


class FakeClient:
    def __init__(self, stats=None, page=None, error=None):
        self.stats, self.page, self.error = stats, page, error
        self.stats_calls, self.page_calls = [], []

    def estadisticas_cartera(self, desde, hasta):
        self.stats_calls.append((desde, hasta))
        if self.error:
            raise self.error
        return self.stats or {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "cobertura": 41.66,
                              "por_matriz": [], "por_abogado": [
                                  {"abogado_id": 238, "nombre": "L", "total": 10, "gestionadas": 3, "cobertura": 30.0},
                                  {"abogado_id": 5, "nombre": "X", "total": 1, "gestionadas": 1, "cobertura": 100}]}

    def causas_sin_gestion(self, desde, hasta, abogado_id=None, desde_id=None, limite=500):
        self.page_calls.append((desde, hasta, abogado_id, desde_id, limite))
        if self.error:
            raise self.error
        return self.page or {"success": True, "desde": desde.isoformat(), "hasta": hasta.isoformat(),
                             "data": [{"id_causa": 10, "rol": "C-1-2020", "tribunal": "T", "abogado_id": 238,
                                       "ultima_gestion": "2026-10-28 13:31:36"},
                                      {"id_causa": 11, "rol": "C-2-2020", "tribunal": "T", "abogado_id": 5,
                                       "ultima_gestion": None}],
                             "siguiente_desde_id": 11}


@pytest.fixture
def people(db):
    db.add_all([Lawyer(rut=ADMIN_RUT, name="Admin", role="admin"),
                Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor"),
                Lawyer(rut=LAWYER_RUT, name="Lawyer", role="lawyer")])
    db.commit()
    lw = db.query(Lawyer).filter(Lawyer.rut == LAWYER_RUT).one()
    db.add(SysgalUsuario(sysgal_id=238, nombre="L", lawyer_id=lw.id, synced_at=datetime.utcnow()))
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
    @pytest.mark.parametrize("method,path", [
        ("get", "/estadisticas?desde=2026-11-01&hasta=2026-11-10"),
        ("get", "/sin-gestion?desde=2026-11-01&hasta=2026-11-10"),
        ("get", "/snapshots/2026-10"),
        ("post", "/snapshots/2026-10"),
    ])
    def test_non_admin_forbidden(self, client, people, fake, rut, method, path):
        assert getattr(client, method)(BASE + path, headers=_headers(rut)).status_code == 403
        assert fake.stats_calls == [] and fake.page_calls == []


class TestEstadisticas:
    def test_enriches_lawyer_id(self, client, admin, people, fake):
        r = client.get(f"{BASE}/estadisticas?desde=2026-11-01&hasta=2026-11-10", headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert body["cobertura"] == 41.66 and body["desde"] == "2026-11-01"
        assert [(a["abogado_id"], a["lawyer_id"]) for a in body["por_abogado"]] == [(238, people.id), (5, None)]
        assert fake.stats_calls == [(date(2026, 11, 1), date(2026, 11, 10))]

    def test_range_over_six_months(self, client, admin, fake):
        r = client.get(f"{BASE}/estadisticas?desde=2026-01-01&hasta=2026-11-10", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == "El rango máximo es de 6 meses."
        assert fake.stats_calls == []

    def test_182_days_ok(self, client, admin, fake):
        hasta = date(2026, 1, 1) + timedelta(days=182)
        r = client.get(f"{BASE}/estadisticas?desde=2026-01-01&hasta={hasta}", headers=admin)
        assert r.status_code == 200

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


class TestSinGestion:
    def test_enriches_lawyer_and_case(self, client, admin, people, db, fake):
        db.add(SysgalCausa(sysgal_id=10, rol="C-1-2020", rol_norm="c-1-2020", tribunal="T", case_id=None,
                           synced_at=datetime.utcnow()))
        db.commit()
        r = client.get(f"{BASE}/sin-gestion?desde=2026-11-01&hasta=2026-11-10&abogado_id=238&desde_id=3&limite=50",
                       headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert body["siguiente_desde_id"] == 11
        assert [(i["id_causa"], i["lawyer_id"], i["case_id"]) for i in body["data"]] == [
            (10, people.id, None), (11, None, None)]
        assert fake.page_calls == [(date(2026, 11, 1), date(2026, 11, 10), 238, 3, 50)]

    def test_case_id_when_linked(self, client, admin, people, db, fake):
        from app.models.case import Case
        from app.models.court import Court
        court = Court(code="C1", name="J", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(lawyer_id=people.id, court_id=court.id, rol="C-1-2020", competencia="civil")
        db.add(case)
        db.flush()
        db.add(SysgalCausa(sysgal_id=10, rol="C-1-2020", rol_norm="c-1-2020", tribunal="T", case_id=case.id,
                           synced_at=datetime.utcnow()))
        db.commit()
        body = client.get(f"{BASE}/sin-gestion?desde=2026-11-01&hasta=2026-11-10", headers=admin).json()
        assert body["data"][0]["case_id"] == case.id

    def test_validates_range_and_limite(self, client, admin, fake):
        r = client.get(f"{BASE}/sin-gestion?desde=2026-01-01&hasta=2026-11-10", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == "El rango máximo es de 6 meses."
        r = client.get(f"{BASE}/sin-gestion?desde=2026-11-10&hasta=2026-11-01", headers=admin)
        assert r.json()["detail"] == "La fecha desde debe ser anterior a hasta."
        r = client.get(f"{BASE}/sin-gestion?desde=2026-11-01&hasta=2026-11-10&limite=501", headers=admin)
        assert r.status_code == 422
        assert fake.page_calls == []

    def test_sysgal_down_is_502(self, client, admin):
        fc = FakeClient(error=SysgalError("HTTP 503"))
        app.dependency_overrides[api.get_sysgal_client] = lambda: fc
        try:
            r = client.get(f"{BASE}/sin-gestion?desde=2026-11-01&hasta=2026-11-10", headers=admin)
        finally:
            app.dependency_overrides.pop(api.get_sysgal_client, None)
        assert r.status_code == 502 and r.json()["detail"] == DOWN


@pytest.fixture
def after_nov(monkeypatch):
    """Freeze the service clock after November 2026 ended (the real clock moves)."""

    class _Date(date):
        @classmethod
        def today(cls):
            return cls(2026, 12, 10)

    monkeypatch.setattr(svc, "date", _Date)


class TestSnapshots:
    def test_get_404_when_missing(self, client, admin, fake):
        assert client.get(f"{BASE}/snapshots/2026-10", headers=admin).status_code == 404

    def test_get_invalid_periodo_400(self, client, admin, fake):
        assert client.get(f"{BASE}/snapshots/2026-13", headers=admin).status_code == 400

    def test_post_then_get(self, client, admin, people, db, fake, after_nov):
        r = client.post(f"{BASE}/snapshots/2026-11", headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"periodo", "desde", "hasta", "taken_at", "taken_by", "payload"}
        assert body["periodo"] == "2026-11" and body["desde"] == "2026-11-01" and body["hasta"] == "2026-11-30"
        admin_row = db.query(Lawyer).filter(Lawyer.rut == ADMIN_RUT).one()
        assert body["taken_by"] == admin_row.id
        assert body["payload"]["cobertura"] == 41.66
        assert fake.stats_calls == [(date(2026, 11, 1), date(2026, 11, 30))]
        got = client.get(f"{BASE}/snapshots/2026-11", headers=admin).json()
        for row in got["payload"]["por_abogado"]:
            row.pop("en_cartera"), row.pop("lawyer_id")
        assert got == body

    def test_post_current_month_refused(self, client, admin, db, fake):
        today = date.today()
        r = client.post(f"{BASE}/snapshots/{today:%Y-%m}", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == "El mes aún no termina."
        assert db.query(AvanceCarteraSnapshot).count() == 0

    def test_post_closed_is_409(self, client, admin, db, fake, after_nov):
        db.add(BonoCierre(periodo="2026-11", estado=CIERRE_CERRADO))
        db.commit()
        r = client.post(f"{BASE}/snapshots/2026-11", headers=admin)
        assert r.status_code == 409 and r.json()["detail"] == "La liquidación de ese mes ya está cerrada."
        assert db.query(AvanceCarteraSnapshot).count() == 0

    def test_post_sysgal_down_is_502(self, client, admin, db, after_nov):
        fc = FakeClient(error=SysgalError("HTTP 503"))
        app.dependency_overrides[api.get_sysgal_client] = lambda: fc
        try:
            r = client.post(f"{BASE}/snapshots/2026-11", headers=admin)
        finally:
            app.dependency_overrides.pop(api.get_sysgal_client, None)
        assert r.status_code == 502 and r.json()["detail"] == DOWN
        assert db.query(AvanceCarteraSnapshot).count() == 0


class TestFotoDesde:
    """S11: photos start with November 2026."""

    MSG = "Las fotos de Sysgal se toman desde noviembre de 2026."

    def test_post_before_november_is_400_nothing_stored_sysgal_not_called(self, client, admin, db, fake, after_nov):
        r = client.post(f"{BASE}/snapshots/2026-10", headers=admin)
        assert r.status_code == 400 and r.json()["detail"] == self.MSG
        assert db.query(AvanceCarteraSnapshot).count() == 0
        assert fake.stats_calls == []

    def test_post_november_once_ended_is_allowed(self, client, admin, db, fake, after_nov):
        r = client.post(f"{BASE}/snapshots/2026-11", headers=admin)
        assert r.status_code == 200 and r.json()["periodo"] == "2026-11"
        assert fake.stats_calls == [(date(2026, 11, 1), date(2026, 11, 30))]
        assert db.query(AvanceCarteraSnapshot).count() == 1


class TestEnCartera:
    """en_cartera = mapped lawyer AND is_active AND role != admin (any nivel or none)."""

    ROWS = [
        {"abogado_id": 901, "nombre": "Sin abogado", "total": 1, "gestionadas": 0, "cobertura": 0},
        {"abogado_id": 902, "nombre": "Junior", "total": 1, "gestionadas": 1, "cobertura": 100},
        {"abogado_id": 903, "nombre": "Pleno", "total": 1, "gestionadas": 1, "cobertura": 100},
        {"abogado_id": 904, "nombre": "Inactivo", "total": 1, "gestionadas": 1, "cobertura": 100},
        {"abogado_id": 905, "nombre": "Admin", "total": 1, "gestionadas": 1, "cobertura": 100},
        {"abogado_id": 906, "nombre": "Senior", "total": 1, "gestionadas": 1, "cobertura": 100},
        {"abogado_id": 907, "nombre": "Sin nivel", "total": 1, "gestionadas": 1, "cobertura": 100},
        {"abogado_id": 908, "nombre": "Inactivo con nivel", "total": 1, "gestionadas": 1, "cobertura": 100},
    ]
    EXPECTED = {901: False, 902: True, 903: True, 904: False, 905: False, 906: True, 907: True, 908: False}

    @pytest.fixture
    def mapped(self, db, people):
        specs = [
            (902, "90000001-1", "junior", True, "lawyer"),
            (903, "90000002-2", "pleno", True, "lawyer"),
            (904, "90000003-3", None, False, "lawyer"),
            (905, "90000004-4", None, True, "admin"),
            (906, "90000005-5", "senior", True, "lawyer"),
            (907, "90000006-6", None, True, "lawyer"),
            (908, "90000007-7", "junior", False, "lawyer"),
        ]
        for sid, rut, nivel, active, role in specs:
            lw = Lawyer(rut=rut, name=f"L{sid}", role=role, nivel=nivel, is_active=active)
            db.add(lw)
            db.flush()
            db.add(SysgalUsuario(sysgal_id=sid, nombre=f"L{sid}", lawyer_id=lw.id, synced_at=datetime.utcnow()))
        db.commit()

    @staticmethod
    def _stats(rows):
        return {"desde": "2026-11-01", "hasta": "2026-11-10", "cobertura": 1.0, "por_matriz": [],
                "por_abogado": [dict(r) for r in rows]}

    def test_estadisticas_en_cartera_per_case(self, client, admin, mapped):
        fc = FakeClient(stats=self._stats(self.ROWS))
        app.dependency_overrides[api.get_sysgal_client] = lambda: fc
        try:
            r = client.get(f"{BASE}/estadisticas?desde=2026-11-01&hasta=2026-11-10", headers=admin)
        finally:
            app.dependency_overrides.pop(api.get_sysgal_client, None)
        assert r.status_code == 200
        rows = r.json()["por_abogado"]
        assert {a["abogado_id"]: a["en_cartera"] for a in rows} == self.EXPECTED
        assert all(isinstance(a["en_cartera"], bool) and "en_bono" not in a for a in rows)

    def test_cartera_lawyer_ids_one_query(self, db, mapped):
        from sqlalchemy import event
        from app.services import avance_cartera as svc

        stmts = []
        event.listen(db.get_bind(), "before_cursor_execute", lambda *a: stmts.append(a[2]))
        ids = svc.cartera_lawyer_ids(db)
        assert len(stmts) == 1
        names = {x.name for x in db.query(Lawyer).filter(Lawyer.id.in_(ids))}
        assert names == {"Lawyer", "L902", "L903", "L906", "L907"}  # "Lawyer" = mapped fixture user

    def test_snapshot_get_enriched_stored_payload_verbatim(self, client, admin, mapped, db):
        payload = self._stats(self.ROWS)
        db.add(AvanceCarteraSnapshot(periodo="2026-10", desde=date(2026, 10, 1), hasta=date(2026, 10, 31),
                                     taken_at=datetime.utcnow(), payload=payload))
        db.commit()
        r = client.get(f"{BASE}/snapshots/2026-10", headers=admin)
        assert r.status_code == 200
        assert {a["abogado_id"]: a["en_cartera"] for a in r.json()["payload"]["por_abogado"]} == self.EXPECTED
        db.expire_all()
        stored = db.query(AvanceCarteraSnapshot).filter_by(periodo="2026-10").one().payload
        assert stored == self._stats(self.ROWS)
        assert all("en_cartera" not in a for a in stored["por_abogado"])
