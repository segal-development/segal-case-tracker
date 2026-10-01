"""Tests for GET /api/v1/sync/frescura: how fresh the detail-rotation universe is."""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.core.security import create_access_token
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer

URL = "/api/v1/sync/frescura"
ADMIN_RUT = "16021492-9"
AUDITOR_RUT = "12121212-1"
LAWYER_RUT = "77777777-7"


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


@pytest.fixture(autouse=True)
def _piso_2021(monkeypatch):
    monkeypatch.setattr(settings, "DETAIL_MIN_YEAR", 2021)


@pytest.fixture
def admin(db):
    lawyer = Lawyer(rut=ADMIN_RUT, name="Admin", role="admin")
    db.add(lawyer)
    db.commit()
    return lawyer


@pytest.fixture
def court(db):
    c = Court(code="C-FRESC", name="Test Court", region="RM", type="civil")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture
def make_case(db, admin, court):
    counter = {"n": 0}

    def _make(rol=None, competencia="civil", poda_at=None, checked_days_ago=None):
        counter["n"] += 1
        checked = None
        if checked_days_ago is not None:
            checked = datetime.utcnow() - timedelta(days=checked_days_ago)
        case = Case(
            lawyer_id=admin.id,
            court_id=court.id,
            rol=rol or f"C-{counter['n']}-2024",
            competencia=competencia,
            poda_at=poda_at,
            last_detail_checked_at=checked,
        )
        db.add(case)
        db.commit()
        return case

    return _make


def _get(client):
    r = client.get(URL, headers=_h(ADMIN_RUT))
    assert r.status_code == 200, r.text
    return r.json()


def test_alcance_excluye_causas_podadas(client, make_case):
    make_case()
    make_case(poda_at=datetime.utcnow())
    assert _get(client)["causas_en_alcance"] == 1


def test_alcance_excluye_causas_anteriores_al_piso_de_anio(client, make_case):
    make_case(rol="C-1-2024")
    make_case(rol="C-2-2021")      # justo en el piso: entra
    make_case(rol="C-3-2020")      # anterior: afuera
    make_case(rol="SIN-ANIO")      # sin año reconocible: falla abierto, entra
    assert _get(client)["causas_en_alcance"] == 3


def test_alcance_excluye_competencias_que_no_son_civil(client, make_case):
    make_case()
    make_case(competencia="laboral")
    make_case(competencia="penal")
    assert _get(client)["causas_en_alcance"] == 1


def test_nunca_revisadas_cuenta_las_de_fecha_nula(client, make_case):
    make_case()
    make_case()
    make_case(checked_days_ago=3)
    body = _get(client)
    assert body["causas_en_alcance"] == 3
    assert body["nunca_revisadas"] == 2


def test_nunca_revisadas_ignora_las_fuera_de_alcance(client, make_case):
    make_case(poda_at=datetime.utcnow())
    make_case(rol="C-9-2019")
    assert _get(client)["nunca_revisadas"] == 0


def test_ventanas_de_7_y_30_dias(client, make_case):
    make_case(checked_days_ago=3)    # 7d y 30d
    make_case(checked_days_ago=20)   # solo 30d
    make_case(checked_days_ago=60)   # ninguna
    make_case()                      # nunca
    body = _get(client)
    assert body["revisadas_7d"] == 1
    assert body["revisadas_30d"] == 2


def test_ritmo_diario_divide_por_dias_corridos_no_por_dias_activos(client, make_case):
    """Actividad solo en 2 de los últimos 14 días: 28 revisiones / 14 = 2.0.
    Dividir por los 2 días activos daría 14.0, un número optimista que no
    describe la realidad: la estación no trabaja todos los días."""
    for _ in range(20):
        make_case(checked_days_ago=2)
    for _ in range(8):
        make_case(checked_days_ago=5)
    body = _get(client)
    assert body["ritmo_diario"] == pytest.approx(2.0)
    assert body["dias_vuelta_completa"] == 14  # 28 causas / 2.0 por día


def test_ritmo_diario_no_cuenta_revisiones_mas_viejas_que_14_dias(client, make_case):
    make_case(checked_days_ago=3)
    make_case(checked_days_ago=20)
    assert _get(client)["ritmo_diario"] == pytest.approx(1 / 14, abs=0.01)


def test_sin_actividad_en_14_dias_la_vuelta_completa_es_none(client, make_case):
    make_case()
    make_case(checked_days_ago=40)
    body = _get(client)
    assert body["ritmo_diario"] == 0
    assert body["dias_vuelta_completa"] is None


def test_cartera_vacia(client, admin):
    body = _get(client)
    assert body == {
        "causas_en_alcance": 0,
        "nunca_revisadas": 0,
        "revisadas_7d": 0,
        "revisadas_30d": 0,
        "ritmo_diario": 0,
        "dias_vuelta_completa": None,
    }


def test_exige_autenticacion(client):
    assert client.get(URL).status_code in (401, 403)


def test_abogado_comun_recibe_403(client, db, admin):
    db.add(Lawyer(rut=LAWYER_RUT, name="Abogado", role="lawyer"))
    db.commit()
    assert client.get(URL, headers=_h(LAWYER_RUT)).status_code == 403


def test_auditor_tiene_acceso(client, db, admin):
    db.add(Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor"))
    db.commit()
    assert client.get(URL, headers=_h(AUDITOR_RUT)).status_code == 200
