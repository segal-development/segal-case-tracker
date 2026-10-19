"""Tests for GET /api/v1/cases/diagnostico (why is a case not showing what the client reports)."""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement

URL = "/api/v1/cases/diagnostico"
SECRET_PJUD = "SECRET-CIPHERTEXT-PJUD-xyz123"
SECRET_CU = "SECRET-CIPHERTEXT-CLAVEUNICA-abc789"


def _headers(rut):
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def admin(db):
    obj = Lawyer(rut="11111111-1", name="Admin", role="admin")
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut="77777777-7", name="Auditor", role="auditor")
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture
def plain(db):
    obj = Lawyer(rut="88888888-8", name="Plain", role="lawyer")
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="DIAG-1", name="1 Juzgado Civil de Santiago", region="RM", type="civil")
    db.add(obj)
    db.commit()
    return obj


def _lawyer(db, rut, name, *, active=True, pjud=SECRET_PJUD, cu=None):
    obj = Lawyer(
        rut=rut,
        name=name,
        role="lawyer",
        is_active=active,
        encrypted_pjud_password=pjud,
        encrypted_clave_unica_password=cu,
    )
    db.add(obj)
    db.commit()
    return obj


def _case(db, lawyer, court, rol="C-13510-2024", checked_days_ago=None, **kw):
    checked = (
        datetime.utcnow() - timedelta(days=checked_days_ago)
        if checked_days_ago is not None
        else None
    )
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        plaintiff="BANCO X",
        defendant="PEREZ JUAN",
        last_detail_checked_at=checked,
        **kw,
    )
    db.add(obj)
    db.commit()
    return obj


def _get(client, headers, **params):
    return client.get(URL, params=params, headers=headers)


def test_nunca_revisada(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    _case(db, ab, court)
    body = _get(client, _headers(admin.rut), rol="C-13510-2024").json()
    assert body["encontrado"] is True
    assert body["buscado"]
    fila = body["filas"][0]
    assert fila["veredicto"] == "nunca_revisada"
    assert fila["dias_desde_revision"] is None
    assert fila["tribunal"] == "1 Juzgado Civil de Santiago"
    assert fila["abogado_nombre"] == "Ana"
    assert fila["abogado_activo"] is True
    assert fila["abogado_tiene_credencial"] is True
    assert fila["caratula"] == "BANCO X/PEREZ JUAN"
    assert fila["movimientos"] == 0
    assert fila["ultimo_movimiento"] is None


def test_al_dia(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    _case(db, ab, court, checked_days_ago=1)
    fila = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"][0]
    assert fila["veredicto"] == "al_dia"
    assert fila["dias_desde_revision"] == 1


def test_latencia(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    _case(db, ab, court, checked_days_ago=settings.DIAGNOSTICO_LATENCIA_DIAS + 3)
    fila = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"][0]
    assert fila["veredicto"] == "latencia"
    assert fila["dias_desde_revision"] == settings.DIAGNOSTICO_LATENCIA_DIAS + 3


def test_latency_threshold_comes_from_settings(client, db, admin, court, monkeypatch):
    ab = _lawyer(db, "20000000-1", "Ana")
    _case(db, ab, court, checked_days_ago=3)
    monkeypatch.setattr(settings, "DIAGNOSTICO_LATENCIA_DIAS", 2)
    fila = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"][0]
    assert fila["veredicto"] == "latencia"


def test_sin_credencial(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana", pjud=None, cu=None)
    _case(db, ab, court, checked_days_ago=1)
    fila = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"][0]
    assert fila["veredicto"] == "sin_credencial"
    assert fila["abogado_tiene_credencial"] is False


def test_clave_unica_alone_counts_as_credential(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana", pjud=None, cu=SECRET_CU)
    _case(db, ab, court, checked_days_ago=1)
    fila = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"][0]
    assert fila["abogado_tiene_credencial"] is True
    assert fila["veredicto"] == "al_dia"


def test_abogado_inactivo_wins_over_missing_credential(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana", active=False, pjud=None, cu=None)
    _case(db, ab, court)
    fila = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"][0]
    assert fila["veredicto"] == "abogado_inactivo"
    assert fila["abogado_activo"] is False


def test_same_rol_under_two_lawyers_returns_every_row(client, db, auditor, court):
    a = _lawyer(db, "20000000-1", "Ana")
    b = _lawyer(db, "20000000-2", "Beto")
    _case(db, a, court, checked_days_ago=1)
    _case(db, b, court)
    _case(db, a, court, rol="C-999-2024")
    body = _get(client, _headers(auditor.rut), rol="c-13510-2024").json()
    assert len(body["filas"]) == 2
    assert {f["abogado_nombre"] for f in body["filas"]} == {"Ana", "Beto"}


def test_movements_summary(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    c = _case(db, ab, court, checked_days_ago=1)
    db.add(Movement(case_id=c.id, description="Antiguo", movement_date=datetime(2026, 1, 1)))
    db.add(Movement(case_id=c.id, description="Reciente", movement_date=datetime(2026, 3, 1)))
    db.commit()
    fila = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"][0]
    assert fila["movimientos"] == 2
    assert fila["ultimo_movimiento"]["descripcion"] == "Reciente"


def test_not_found(client, db, admin):
    body = _get(client, _headers(admin.rut), rol="C-1-1999").json()
    assert body["encontrado"] is False
    assert body["filas"] == []


def test_search_by_rut(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    c = _case(db, ab, court, checked_days_ago=1)
    db.add(
        CaseLitigante(
            case_id=c.id, participante="DDO.", rut="12345678-9",
            persona_type="NATURAL", nombre="PEREZ JUAN", natural_key=f"{c.id}-12345678-9",
        )
    )
    db.commit()
    body = _get(client, _headers(admin.rut), rut="12.345.678-9").json()
    assert body["encontrado"] is True
    assert body["filas"][0]["case_id"] == c.id


def test_requires_exactly_one_of_rol_or_rut(client, admin):
    h = _headers(admin.rut)
    assert _get(client, h).status_code == 400
    assert _get(client, h, rol="C-1-2024", rut="1-9").status_code == 400


def test_plain_lawyer_is_forbidden(client, db, plain, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    _case(db, ab, court)
    r = _get(client, _headers(plain.rut), rol="C-13510-2024")
    assert r.status_code == 403
    assert "Ana" not in r.text


def test_no_credential_material_in_response(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana", pjud=SECRET_PJUD, cu=SECRET_CU)
    _case(db, ab, court, checked_days_ago=1)
    r = _get(client, _headers(admin.rut), rol="C-13510-2024")
    assert r.status_code == 200
    assert SECRET_PJUD not in r.text
    assert SECRET_CU not in r.text
    assert "SECRET" not in r.text
    fila = r.json()["filas"][0]
    assert set(fila) == {
        "case_id", "rol", "caratula", "tribunal", "estado", "abogado_id", "abogado_nombre",
        "abogado_activo", "abogado_tiene_credencial", "last_detail_checked_at",
        "dias_desde_revision", "last_movement_at", "movimientos", "ultimo_movimiento",
        "veredicto", "veredicto_detalle",
    }
    assert fila["abogado_tiene_credencial"] is True


def test_detail_copy_has_no_voseo(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana", active=False)
    _case(db, ab, court)
    ab2 = _lawyer(db, "20000000-2", "Beto", pjud=None)
    _case(db, ab2, court)
    ab3 = _lawyer(db, "20000000-3", "Cata")
    _case(db, ab3, court, checked_days_ago=1)
    ab4 = _lawyer(db, "20000000-4", "Dani")
    _case(db, ab4, court, checked_days_ago=40)
    ab5 = _lawyer(db, "20000000-5", "Eli")
    _case(db, ab5, court)
    filas = _get(client, _headers(admin.rut), rol="C-13510-2024").json()["filas"]
    assert len(filas) == 5
    for f in filas:
        assert f["veredicto_detalle"]
        for bad in ("revisá", "tenés", "sos ", "podés", "mirá"):
            assert bad not in f["veredicto_detalle"].lower()


def _seed_ddo(db, case, rut):
    db.add(
        CaseLitigante(
            case_id=case.id, participante="DDO.", rut=rut,
            persona_type="NATURAL", nombre="PEREZ JUAN", natural_key=f"{case.id}-{rut}",
        )
    )
    db.commit()


def test_rut_with_and_without_dots_find_the_same_case(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    c = _case(db, ab, court, checked_days_ago=1)
    _seed_ddo(db, c, "8430773-4")
    h = _headers(admin.rut)
    for typed in ("8.430.773-4", "8430773-4", "84307734", " 8.430.773-4 "):
        body = _get(client, h, rut=typed).json()
        assert body["encontrado"] is True, typed
        assert [f["case_id"] for f in body["filas"]] == [c.id], typed


def test_rut_check_digit_k_is_case_insensitive(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    c = _case(db, ab, court, checked_days_ago=1)
    _seed_ddo(db, c, "9876543-K")
    body = _get(client, _headers(admin.rut), rut="9.876.543-k").json()
    assert [f["case_id"] for f in body["filas"]] == [c.id]


def test_rut_matches_when_stored_with_dots(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    c = _case(db, ab, court, checked_days_ago=1)
    _seed_ddo(db, c, "8.430.773-4")
    body = _get(client, _headers(admin.rut), rut="8430773-4").json()
    assert [f["case_id"] for f in body["filas"]] == [c.id]


def test_rut_is_not_a_substring_match_on_defendant_text(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    c = _case(db, ab, court, checked_days_ago=1)
    c.defendant = "PEREZ 8430773-4"
    db.commit()
    body = _get(client, _headers(admin.rut), rut="8430773-4").json()
    assert body["encontrado"] is False


def test_rut_only_matches_defendant_side(client, db, admin, court):
    ab = _lawyer(db, "20000000-1", "Ana")
    c = _case(db, ab, court, checked_days_ago=1)
    db.add(CaseLitigante(case_id=c.id, participante="DTE.", rut="8430773-4",
                         persona_type="JURIDICA", nombre="BANCO", natural_key=f"{c.id}-dte"))
    db.commit()
    assert _get(client, _headers(admin.rut), rut="8430773-4").json()["encontrado"] is False



def _case_con_demandado(db, ab, court, rol, rut):
    c = _case(db, ab, court, rol=rol, checked_days_ago=1)
    db.add(
        CaseLitigante(
            case_id=c.id, participante="DDO.", rut=rut,
            persona_type="JURIDICA", nombre="BANCO X",
            natural_key=f"{c.id}-{rut}",
        )
    )
    return c


class TestResultsAreCapped:
    """A company RUT (a bank being sued) matches hundreds of cases. Measured on
    the real database: one such RUT returns 512. The endpoint builds a full
    diagnosis per row, with a movements query each, so without a cap the screen
    hangs for minutes -- and it is used with a client on the phone."""

    def test_over_the_cap_returns_the_cap_and_reports_the_total(
        self, client, db, admin, court
    ):
        from app.api.v1.cases import DIAGNOSTICO_MAX_FILAS

        ab = _lawyer(db, "20000000-1", "Ana")
        rut = "90743000-6"
        for i in range(DIAGNOSTICO_MAX_FILAS + 3):
            _case_con_demandado(db, ab, court, f"C-{9000 + i}-2026", rut)
        db.commit()

        body = _get(client, _headers(admin.rut), rut=rut).json()
        assert body["encontrado"] is True
        assert len(body["filas"]) == DIAGNOSTICO_MAX_FILAS
        assert body["total"] == DIAGNOSTICO_MAX_FILAS + 3

    def test_under_the_cap_the_total_equals_the_rows(self, client, db, admin, court):
        ab = _lawyer(db, "20000000-2", "Luis")
        rut = "11111111-1"
        _case_con_demandado(db, ab, court, "C-8001-2026", rut)
        _case_con_demandado(db, ab, court, "C-8002-2026", rut)
        db.commit()

        body = _get(client, _headers(admin.rut), rut=rut).json()
        assert body["total"] == 2
        assert len(body["filas"]) == 2
