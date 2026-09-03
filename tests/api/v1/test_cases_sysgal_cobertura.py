"""Tests for the Sysgal cobertura tag on GET /api/v1/cases, /cases/summary and
/cases/{id}."""

from datetime import date, datetime

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.models.court import Court
from app.models.lawyer import Lawyer

ADMIN_RUT = "11111111-1"


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Admin SC", role="admin")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-SC", name="Juzgado SC", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, admin):
    async def _mock():
        return {"sub": str(admin.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client


def _make_case(db, lawyer, court, rol, **flags):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **flags,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _add_litigante(db, case_id, participante, rut):
    db.add(
        CaseLitigante(
            case_id=case_id,
            participante=participante,
            rut=rut,
            persona_type="NATURAL",
            nombre="Test",
            natural_key=f"{case_id}-{participante}-{rut}",
        )
    )
    db.commit()


def _cache(db, rut, code, hasta=None, encontrado=True):
    db.add(
        ClienteSysgalEstado(
            rut=rut,
            encontrado=encontrado,
            estado_codigo=code,
            estado_label=code.title() if code else None,
            tiene_contrato=hasta is not None,
            vigencia_hasta=hasta,
            sysgal_updated_at=datetime(2026, 8, 1, 12, 0, 0),
            synced_at=datetime(2026, 9, 1, 3, 0, 0),
        )
    )
    db.commit()


@pytest.fixture
def dataset(db, admin, court):
    """Six cases:
    activo    — abandono, DDO rut cached ACTIVO vigente
    moroso    — apremio,  DDO rut cached MOROSO_INACTIVO
    caducado  — presc.,   DDO rut cached ACTIVO but vigencia expired (stale)
    sin_dato  — abandono, DDO rut with NO cache row
    no_ddo    — abandono, no DDO litigante at all → None
    outside   — no state flag, DDO rut cached ACTIVO → None (out of scope)
    """
    activo = _make_case(db, admin, court, "C-1-2025", abandono_disponible=True)
    moroso = _make_case(db, admin, court, "C-2-2025", en_apremio=True)
    caducado = _make_case(db, admin, court, "C-3-2025", prescripcion_cumplida=True)
    sin_dato = _make_case(db, admin, court, "C-4-2025", abandono_disponible=True)
    no_ddo = _make_case(db, admin, court, "C-5-2025", abandono_disponible=True)
    outside = _make_case(db, admin, court, "C-6-2025")

    _add_litigante(db, activo.id, "DDO.", "11.111.111-1")
    _add_litigante(db, activo.id, "AB.DDO", "55555555-5")
    _add_litigante(db, moroso.id, "DDOR.", "22222222-2")
    _add_litigante(db, caducado.id, "DDO.", "33333333-3")
    _add_litigante(db, sin_dato.id, "DDO.", "44444444-4")
    _add_litigante(db, no_ddo.id, "DTE.", "66666666-6")
    _add_litigante(db, outside.id, "DDO.", "11111111-1")

    _cache(db, "11111111-1", "ACTIVO", hasta=date(2099, 12, 31))
    _cache(db, "22222222-2", "MOROSO_INACTIVO", hasta=date(2099, 12, 31))
    _cache(db, "33333333-3", "ACTIVO", hasta=date(2020, 1, 1))
    _cache(db, "55555555-5", "ACTIVO", hasta=date(2099, 12, 31))  # AB.DDO rut — must be ignored

    return {
        "activo": activo,
        "moroso": moroso,
        "caducado": caducado,
        "sin_dato": sin_dato,
        "no_ddo": no_ddo,
        "outside": outside,
    }


def _by_id(resp):
    return {item["id"]: item for item in resp.json()["items"]}


class TestListEnrichment:
    def test_cobertura_populated_for_state_cases(self, authed_client, dataset):
        resp = authed_client.get("/api/v1/cases?per_page=100")
        assert resp.status_code == 200
        items = _by_id(resp)

        a = items[dataset["activo"].id]
        assert a["sysgal_cobertura"] == "activo"
        assert a["sysgal_estado_codigo"] == "ACTIVO"
        assert a["sysgal_vigencia_hasta"] == "2099-12-31"
        assert a["sysgal_synced_at"] is not None

        assert items[dataset["moroso"].id]["sysgal_cobertura"] == "moroso"
        assert items[dataset["caducado"].id]["sysgal_cobertura"] == "caducado"

        sd = items[dataset["sin_dato"].id]
        assert sd["sysgal_cobertura"] == "sin_dato"
        assert sd["sysgal_estado_codigo"] is None
        assert sd["sysgal_synced_at"] is None

    def test_none_when_no_ddo_or_outside_states(self, authed_client, dataset):
        items = _by_id(authed_client.get("/api/v1/cases?per_page=100"))
        for key in ("no_ddo", "outside"):
            item = items[dataset[key].id]
            assert item["sysgal_cobertura"] is None
            assert item["sysgal_estado_codigo"] is None
            assert item["sysgal_vigencia_hasta"] is None
            assert item["sysgal_synced_at"] is None

    def test_no_pii_in_response(self, authed_client, dataset):
        body = authed_client.get("/api/v1/cases?per_page=100").text
        assert "nombre_cliente" not in body
        assert "telefono" not in body


class TestCoberturaFilter:
    @pytest.mark.parametrize(
        "value,key",
        [("activo", "activo"), ("moroso", "moroso"), ("caducado", "caducado"), ("sin_dato", "sin_dato")],
    )
    def test_filter_returns_only_matching(self, authed_client, dataset, value, key):
        resp = authed_client.get(f"/api/v1/cases?cobertura={value}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert [i["id"] for i in data["items"]] == [dataset[key].id]

    def test_bogus_value_is_422(self, authed_client, dataset):
        assert authed_client.get("/api/v1/cases?cobertura=bogus").status_code == 422

    def test_filter_combines_with_sem(self, authed_client, dataset):
        resp = authed_client.get("/api/v1/cases?cobertura=activo&sem=apremio")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestSummaryChips:
    def test_summary_has_sysgal_counts(self, authed_client, dataset):
        resp = authed_client.get("/api/v1/cases/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sysgal_activo"] == 1
        assert data["sysgal_moroso"] == 1
        assert data["sysgal_caducado"] == 1
        assert data["sysgal_sin_dato"] == 1
        # Existing chips untouched
        assert data["abandono"] == 3
        assert data["apremio"] == 1
        assert data["prescripcion"] == 1


class TestDetailEnrichment:
    def test_detail_has_cobertura(self, authed_client, dataset):
        resp = authed_client.get(f"/api/v1/cases/{dataset['caducado'].id}")
        assert resp.status_code == 200
        case = resp.json()["case"]
        assert case["sysgal_cobertura"] == "caducado"
        assert case["sysgal_estado_codigo"] == "ACTIVO"
        assert case["sysgal_vigencia_hasta"] == "2020-01-01"
        assert case["prescripcion_cumplida"] is True

    def test_detail_outside_states_is_none(self, authed_client, dataset):
        resp = authed_client.get(f"/api/v1/cases/{dataset['outside'].id}")
        assert resp.status_code == 200
        assert resp.json()["case"]["sysgal_cobertura"] is None

    def test_detail_sin_dato(self, authed_client, dataset):
        resp = authed_client.get(f"/api/v1/cases/{dataset['sin_dato'].id}")
        assert resp.json()["case"]["sysgal_cobertura"] == "sin_dato"
