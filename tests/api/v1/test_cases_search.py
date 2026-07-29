"""Tests for server-side search/filters/facets/summary on GET /api/v1/cases.

The authed lawyer is role='admin' so ``resolve_case_scope`` returns ALL_CASES
and every seeded case is visible without seeding litigantes (mirrors how the
auditor/admin scope short-circuits the litigante-derived visibility).
"""

import pytest
from datetime import datetime

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


ACCOUNT_RUT = "11111111-1"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=ACCOUNT_RUT, name="Admin Lawyer", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-SR", name="Primer Juzgado", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court2(db):
    obj = Court(code="T2-SR", name="Segundo Juzgado", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client
    app.dependency_overrides.pop(get_current_lawyer, None)


def _make_case(
    db,
    lawyer,
    court,
    rol,
    *,
    plaintiff="BANCO SA",
    defendant="DEUDOR DDO",
    procedure="Ejecutivo",
    semaforo=None,
    abandono_disponible=False,
    en_apremio=False,
    prescripcion_cumplida=False,
):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        plaintiff=plaintiff,
        defendant=defendant,
        procedure=procedure,
        semaforo=semaforo,
        abandono_disponible=abandono_disponible,
        en_apremio=en_apremio,
        prescripcion_cumplida=prescripcion_cumplida,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# q — free-text search
# ---------------------------------------------------------------------------


class TestSearchQ:
    def test_q_matches_rol(self, authed_client, db, lawyer, court):
        target = _make_case(db, lawyer, court, "C-7001-2025")
        _make_case(db, lawyer, court, "C-7002-2025")

        r = authed_client.get("/api/v1/cases?q=7001")
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {target.id}

    def test_q_matches_plaintiff_case_insensitive(self, authed_client, db, lawyer, court):
        target = _make_case(db, lawyer, court, "C-7003-2025", plaintiff="BANCO FALABELLA")
        _make_case(db, lawyer, court, "C-7004-2025", plaintiff="OTRO BANCO")

        r = authed_client.get("/api/v1/cases?q=falabella")
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {target.id}

    def test_q_matches_defendant_case_insensitive(self, authed_client, db, lawyer, court):
        target = _make_case(db, lawyer, court, "C-7005-2025", defendant="Juan Perez Soto")
        _make_case(db, lawyer, court, "C-7006-2025", defendant="Maria Lopez")

        r = authed_client.get("/api/v1/cases?q=PEREZ")
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {target.id}

    def test_q_excludes_non_matches(self, authed_client, db, lawyer, court):
        _make_case(db, lawyer, court, "C-7007-2025", plaintiff="A", defendant="B")

        r = authed_client.get("/api/v1/cases?q=zzzznomatch")
        assert r.status_code == 200
        assert r.json()["items"] == []
        assert r.json()["total"] == 0

    def test_blank_q_is_ignored(self, authed_client, db, lawyer, court):
        _make_case(db, lawyer, court, "C-7008-2025")
        _make_case(db, lawyer, court, "C-7009-2025")

        r = authed_client.get("/api/v1/cases?q=%20%20")
        assert r.status_code == 200
        assert r.json()["total"] == 2


# ---------------------------------------------------------------------------
# sem — category quick-filter
# ---------------------------------------------------------------------------


class TestSemFilter:
    def test_sem_rojo(self, authed_client, db, lawyer, court):
        rojo = _make_case(db, lawyer, court, "C-7101-2025", semaforo="rojo")
        _make_case(db, lawyer, court, "C-7102-2025", semaforo="verde")

        r = authed_client.get("/api/v1/cases?sem=rojo")
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {rojo.id}

    def test_sem_sin_seguimiento(self, authed_client, db, lawyer, court):
        none_case = _make_case(db, lawyer, court, "C-7103-2025", semaforo=None)
        _make_case(db, lawyer, court, "C-7104-2025", semaforo="amarillo")

        r = authed_client.get("/api/v1/cases?sem=sin-seguimiento")
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {none_case.id}

    def test_sem_abandono(self, authed_client, db, lawyer, court):
        aban = _make_case(db, lawyer, court, "C-7105-2025", abandono_disponible=True)
        _make_case(db, lawyer, court, "C-7106-2025", abandono_disponible=False)

        r = authed_client.get("/api/v1/cases?sem=abandono")
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {aban.id}

    def test_sem_apremio(self, authed_client, db, lawyer, court):
        apr = _make_case(db, lawyer, court, "C-7107-2025", en_apremio=True)
        _make_case(db, lawyer, court, "C-7108-2025", en_apremio=False)

        r = authed_client.get("/api/v1/cases?sem=apremio")
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {apr.id}

    def test_sem_prescripcion(self, authed_client, db, lawyer, court):
        pre = _make_case(db, lawyer, court, "C-7109-2025", prescripcion_cumplida=True)
        _make_case(db, lawyer, court, "C-7110-2025", prescripcion_cumplida=False)

        r = authed_client.get("/api/v1/cases?sem=prescripcion")
        items = r.json()["items"]
        ids = {i["id"] for i in items}
        assert ids == {pre.id}
        assert items[0]["prescripcion_cumplida"] is True

    def test_sem_todas_is_ignored(self, authed_client, db, lawyer, court):
        _make_case(db, lawyer, court, "C-7111-2025", semaforo="rojo")
        _make_case(db, lawyer, court, "C-7112-2025", semaforo="verde")

        r = authed_client.get("/api/v1/cases?sem=todas")
        assert r.json()["total"] == 2


# ---------------------------------------------------------------------------
# tribunal + materia
# ---------------------------------------------------------------------------


class TestTribunalMateriaFilter:
    def test_tribunal_filter(self, authed_client, db, lawyer, court, court2):
        a = _make_case(db, lawyer, court, "C-7201-2025")
        _make_case(db, lawyer, court2, "C-7202-2025")

        r = authed_client.get("/api/v1/cases?tribunal=Primer Juzgado")
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {a.id}

    def test_materia_filter(self, authed_client, db, lawyer, court):
        a = _make_case(db, lawyer, court, "C-7203-2025", procedure="Ordinario")
        _make_case(db, lawyer, court, "C-7204-2025", procedure="Ejecutivo")

        r = authed_client.get("/api/v1/cases?materia=Ordinario")
        ids = {i["id"] for i in r.json()["items"]}
        assert ids == {a.id}


# ---------------------------------------------------------------------------
# /cases/summary
# ---------------------------------------------------------------------------


class TestCasesSummary:
    def test_summary_counts(self, authed_client, db, lawyer, court):
        _make_case(db, lawyer, court, "C-7301-2025", semaforo="rojo")
        _make_case(db, lawyer, court, "C-7302-2025", semaforo="rojo")
        _make_case(db, lawyer, court, "C-7303-2025", semaforo="amarillo")
        _make_case(db, lawyer, court, "C-7304-2025", semaforo="verde")
        _make_case(db, lawyer, court, "C-7305-2025", semaforo=None)
        _make_case(db, lawyer, court, "C-7306-2025", abandono_disponible=True)
        _make_case(db, lawyer, court, "C-7307-2025", en_apremio=True)
        _make_case(db, lawyer, court, "C-7308-2025", prescripcion_cumplida=True)

        r = authed_client.get("/api/v1/cases/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 8
        assert data["rojo"] == 2
        assert data["amarillo"] == 1
        assert data["verde"] == 1
        # C-7305, C-7306, C-7307, C-7308 all have semaforo None
        assert data["sin_seguimiento"] == 4
        assert data["abandono"] == 1
        assert data["apremio"] == 1
        assert data["prescripcion"] == 1

    def test_summary_ignores_quick_filters(self, authed_client, db, lawyer, court):
        """summary reports quick-filter TOTALS, so it must ignore q/sem itself."""
        _make_case(db, lawyer, court, "C-7311-2025", semaforo="rojo")
        _make_case(db, lawyer, court, "C-7312-2025", semaforo="verde")

        r = authed_client.get("/api/v1/cases/summary?sem=rojo&q=nomatch")
        assert r.json()["total"] == 2


# ---------------------------------------------------------------------------
# /cases/facets
# ---------------------------------------------------------------------------


class TestCasesFacets:
    def test_facets_distinct_sorted(self, authed_client, db, lawyer, court, court2):
        _make_case(db, lawyer, court2, "C-7401-2025", procedure="Ordinario")
        _make_case(db, lawyer, court, "C-7402-2025", procedure="Ejecutivo")
        _make_case(db, lawyer, court, "C-7403-2025", procedure="Ejecutivo")

        r = authed_client.get("/api/v1/cases/facets")
        assert r.status_code == 200
        data = r.json()
        assert data["tribunals"] == ["Primer Juzgado", "Segundo Juzgado"]
        assert data["materias"] == ["Ejecutivo", "Ordinario"]

    def test_facets_excludes_empty_materia(self, authed_client, db, lawyer, court):
        _make_case(db, lawyer, court, "C-7404-2025", procedure="Ejecutivo")
        _make_case(db, lawyer, court, "C-7405-2025", procedure=None)

        r = authed_client.get("/api/v1/cases/facets")
        data = r.json()
        assert data["materias"] == ["Ejecutivo"]


def test_summary_requires_auth(client):
    r = client.get("/api/v1/cases/summary")
    assert r.status_code == 401


def test_facets_requires_auth(client):
    r = client.get("/api/v1/cases/facets")
    assert r.status_code == 401


class TestYearFloor:
    """Cases with a ROL year below DETAIL_MIN_YEAR (2021) are hidden server-side;
    ROLs without a -YYYY suffix fail open (kept)."""

    def test_pre_floor_case_excluded_from_list(self, authed_client, db, lawyer, court):
        recent = _make_case(db, lawyer, court, "C-8001-2024")
        _make_case(db, lawyer, court, "C-8000-2019")  # pre-2021 → hidden
        r = authed_client.get("/api/v1/cases")
        ids = {i["id"] for i in r.json()["items"]}
        assert recent.id in ids
        assert all(i["rol"] != "C-8000-2019" for i in r.json()["items"])

    def test_pre_floor_case_excluded_from_summary(self, authed_client, db, lawyer, court):
        _make_case(db, lawyer, court, "C-8002-2024")
        _make_case(db, lawyer, court, "C-8003-2018")
        r = authed_client.get("/api/v1/cases/summary")
        assert r.json()["total"] == 1  # only the 2024 case

    def test_non_standard_rol_fails_open(self, authed_client, db, lawyer, court):
        kept = _make_case(db, lawyer, court, "SIN-ANIO")  # no 4-digit year → kept
        r = authed_client.get("/api/v1/cases")
        ids = {i["id"] for i in r.json()["items"]}
        assert kept.id in ids
