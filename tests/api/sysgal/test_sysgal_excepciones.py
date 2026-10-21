"""Tests for GET /api/sysgal/v1/excepciones (read-only audit of the excepciones plazo).

The rules under test are about NOT inventing facts on a fatal plazo:
  - "presentó" and "a tiempo" are separate answers;
  - a Resolución (the court's ruling) is never a presentation;
  - a filing with no computed plazo is never reported as "cumplido";
  - "sin_determinar" is a valid, explicit answer.
"""

import hashlib
from datetime import datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sysgal_api_key import SysgalApiKey
from app.services.deadline_engine import _today_chile

VALID_KEY = "sysgal-valid-key"
CLIENT_RUT = "18765432-1"
OTHER_RUT = "9111111-1"
URL = "/api/sysgal/v1/excepciones"


def _auth():
    return {"X-API-Key": VALID_KEY}


def _get(client, rut=CLIENT_RUT):
    resp = client.get(URL, params={"cliente_rut": rut}, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _only(body):
    assert len(body["causas"]) == 1
    return body["causas"][0]


@pytest.fixture
def env(db):
    key = SysgalApiKey(
        label="t", key_hash=hashlib.sha256(VALID_KEY.encode()).hexdigest(), is_active=True
    )
    lawyer = Lawyer(rut="16021492-9", name="Firm")
    court = Court(code="T1", name="1º Juzgado Civil de Santiago", region="RM", type="civil")
    db.add_all([key, lawyer, court])
    db.commit()
    return {"db": db, "lawyer": lawyer, "court": court, "n": 0}


def make_case(env, *, status="active", rut=CLIENT_RUT, participante="DDO.", rol=None):
    db = env["db"]
    env["n"] += 1
    case = Case(
        lawyer_id=env["lawyer"].id, court_id=env["court"].id,
        rol=rol or f"C-{100 + env['n']}-2026", status=status, competencia="civil",
        plaintiff="BANCO X", defendant="PEREZ",
    )
    db.add(case)
    db.commit()
    db.add(CaseLitigante(
        case_id=case.id, participante=participante, rut=rut, persona_type="NATURAL",
        nombre="JUAN PEREZ", natural_key=f"k{env['n']}",
    ))
    db.commit()
    return case


def add_movement(env, case, description, *, procedure="Escrito", days_ago=0):
    mv = Movement(
        case_id=case.id, description=description, procedure=procedure,
        movement_date=datetime.combine(_today_chile() - timedelta(days=days_ago), datetime.min.time()),
    )
    env["db"].add(mv)
    env["db"].commit()
    return mv


def add_escrito(env, case, *, days_ago=0, tipo="Opone excepciones", solicitante="Demandado"):
    """A scraped escrito: it exists from the day the lawyer FILES, before any movement."""
    env["n"] += 1
    row = CaseEscrito(
        case_id=case.id, tipo_escrito=tipo, solicitante=solicitante,
        fecha_ingreso=datetime.combine(_today_chile() - timedelta(days=days_ago), datetime.min.time()),
        natural_key=f"e{env['n']}",
    )
    env["db"].add(row)
    env["db"].commit()
    return row


def add_deadline(env, case, *, due_in, triggered_ago=8, dtype="excepciones_8d",
                 status="active", verdict=None):
    today = _today_chile()
    row = CaseDeadline(
        case_id=case.id, deadline_type=dtype, due_date=today + timedelta(days=due_in),
        triggered_at=today - timedelta(days=triggered_ago), status=status, verdict=verdict,
    )
    env["db"].add(row)
    env["db"].commit()
    return row


# --------------------------------------------------------------------------- auth / params


def test_missing_api_key_is_rejected(client, env):
    resp = client.get(URL, params={"cliente_rut": CLIENT_RUT})
    assert resp.status_code == 401


def test_invalid_api_key_is_rejected(client, env):
    resp = client.get(URL, params={"cliente_rut": CLIENT_RUT}, headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


def test_cliente_rut_is_required(client, env):
    assert client.get(URL, headers=_auth()).status_code == 422


# --------------------------------------------------------------------------- scope


def test_scope_only_returns_the_clients_causas(client, env):
    mine = make_case(env)
    make_case(env, rut=OTHER_RUT)
    body = _get(client)
    assert [c["rol"] for c in body["causas"]] == [mine.rol]


def test_terminated_causa_is_still_audited(client, env):
    """Historical audit: an archived causa must not disappear from the answer."""
    case = make_case(env, status="archived")
    row = _only(_get(client))
    assert row["rol"] == case.rol
    assert row["estado_causa"] == "archived"


def test_disclaimer_is_always_present(client, env):
    assert _get(client)["disclaimer"]
    make_case(env)
    assert _get(client)["disclaimer"]


# --------------------------------------------------------------------------- verdicts


def test_filing_within_plazo_is_cumplido_with_date_and_movement_id(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=3)
    mv = add_movement(env, case, "Opone excepciones", days_ago=2)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["excepciones"]["fecha"] == (_today_chile() - timedelta(days=2)).isoformat()
    assert row["excepciones"]["movimiento_id"] == mv.id
    assert row["veredicto"]["valor"] == "cumplido"


def test_filing_after_plazo_is_fuera_de_plazo(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=-15, triggered_ago=25)
    add_movement(env, case, "Opone excepciones", days_ago=1)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["veredicto"]["valor"] == "fuera_de_plazo"


def test_resolucion_is_not_a_presentation(client, env):
    """procedure='Resolución' is the court's ruling on the excepciones, not the filing."""
    case = make_case(env)
    add_deadline(env, case, due_in=-3, triggered_ago=12)
    add_movement(env, case, "Opone excepciones", procedure="Resolución", days_ago=5)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is False
    assert row["excepciones"]["fecha"] is None
    assert row["excepciones"]["movimiento_id"] is None
    assert row["veredicto"]["valor"] == "no_cumplido"


def test_filing_without_plazo_is_presented_but_never_cumplido(client, env):
    case = make_case(env)
    mv = add_movement(env, case, "Opone excepciones", days_ago=4)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["excepciones"]["movimiento_id"] == mv.id
    assert row["plazo_excepciones"] is None
    assert row["veredicto"]["valor"] == "presentado_sin_ancla"
    assert row["veredicto"]["valor"] != "cumplido"


def test_running_plazo_without_filing_is_sin_determinar_not_no_cumplido(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=4, triggered_ago=4)
    add_movement(env, case, "Se provee demanda", procedure="Resolución", days_ago=4)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is False
    assert row["veredicto"]["valor"] == "sin_determinar"


def test_expired_plazo_without_filing_is_no_cumplido(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=-2, triggered_ago=12)
    add_movement(env, case, "Se provee demanda", procedure="Resolución", days_ago=12)
    assert _only(_get(client))["veredicto"]["valor"] == "no_cumplido"


def test_no_movements_never_invents_a_miss(client, env):
    """Nothing scraped for the causa: we cannot say they did not file."""
    case = make_case(env)
    add_deadline(env, case, due_in=-2, triggered_ago=12)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is None
    assert row["veredicto"]["valor"] == "sin_determinar"


def test_creditor_side_client_gets_no_verdict(client, env):
    """An 'Opone excepciones' escrito is the ejecutado's; if our client is the DTE it is not theirs."""
    case = make_case(env, participante="DTE.")
    add_movement(env, case, "Opone excepciones", days_ago=2)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is None
    assert row["veredicto"]["valor"] == "sin_determinar"
    assert row["rol_cliente"] == ["DTE."]


# --------------------------------------------------------------------------- plazo


def test_plazo_shows_anchor_due_date_and_business_days_left(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=0, triggered_ago=0)
    row = _only(_get(client))
    plazo = row["plazo_excepciones"]
    assert plazo["desde"] == _today_chile().isoformat()
    assert plazo["vence"] == _today_chile().isoformat()
    assert plazo["dias_habiles_restantes"] == 0
    assert row["tribunal"] == "1º Juzgado Civil de Santiago"
    assert row["caratulado"] == "BANCO X/PEREZ"


def test_stale_superseded_row_is_ignored_for_the_current_one(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=-9, triggered_ago=20, status="superseded")
    current = add_deadline(env, case, due_in=5, triggered_ago=3)
    row = _only(_get(client))
    assert row["plazo_excepciones"]["vence"] == current.due_date.isoformat()


# --------------------------------------------------------------------------- ratificación


def test_ratificacion_present_when_row_exists(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=2, triggered_ago=1, dtype="acreditar_poder_3d",
                 status="cumplido")
    rat = _only(_get(client))["ratificacion"]
    assert rat["desde"] == (_today_chile() - timedelta(days=1)).isoformat()
    assert rat["vence"] == (_today_chile() + timedelta(days=2)).isoformat()
    assert rat["cumplida"] is True


def test_ratificacion_open_is_not_reported_as_fulfilled(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=-1, triggered_ago=5, dtype="acreditar_poder_3d")
    rat = _only(_get(client))["ratificacion"]
    assert rat["cumplida"] is None
    assert rat["vencida"] is True


def test_ratificacion_is_null_when_absent(client, env):
    make_case(env)
    assert _only(_get(client))["ratificacion"] is None


def test_only_a_stale_superseded_row_gives_no_plazo(client, env):
    """A re-anchored row is superseded with its verdict cleared: its due_date is wrong."""
    case = make_case(env)
    add_deadline(env, case, due_in=-9, triggered_ago=20, status="superseded", verdict=None)
    add_movement(env, case, "Opone excepciones", days_ago=1)
    row = _only(_get(client))
    assert row["plazo_excepciones"] is None
    assert row["veredicto"]["valor"] == "presentado_sin_ancla"


def test_superseded_row_that_carries_a_verdict_is_kept(client, env):
    """The case moved on and the plazo was judged before superseding: that anchor is valid."""
    case = make_case(env)
    add_deadline(env, case, due_in=3, triggered_ago=5, status="superseded", verdict="cumplido")
    add_movement(env, case, "Opone excepciones", days_ago=2)
    row = _only(_get(client))
    assert row["plazo_excepciones"] is not None
    assert row["veredicto"]["valor"] == "cumplido"


def test_filing_just_after_plazo_is_registro_tardio_with_neutral_spanish_reason(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=-3, triggered_ago=13)
    add_movement(env, case, "Opone excepciones", days_ago=1)  # 2 days past due
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["excepciones"]["fecha"] == (_today_chile() - timedelta(days=1)).isoformat()
    assert row["veredicto"]["valor"] == "registro_tardio"
    motivo = row["veredicto"]["motivo"]
    assert "verifica" in motivo.lower()
    assert "verificá" not in motivo.lower()


# --------------------------------------------------------------------------- escritos
# The escrito exists from the day the lawyer files; the Movement only appears when
# the court provides it. In that window a movements-only audit claims "no escrito".


def test_escrito_within_plazo_without_movement_yet_is_cumplido(client, env):
    """The C-11622-2026 case: filed on time, court has not provided it yet."""
    case = make_case(env)
    add_deadline(env, case, due_in=-2, triggered_ago=12)
    add_movement(env, case, "Se provee demanda", procedure="Resolución", days_ago=12)
    add_escrito(env, case, days_ago=5)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["excepciones"]["fecha"] == (_today_chile() - timedelta(days=5)).isoformat()
    assert row["veredicto"]["valor"] == "cumplido"


def test_escrito_after_plazo_is_fuera_de_plazo(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=-6, triggered_ago=16)
    add_movement(env, case, "Se provee demanda", procedure="Resolución", days_ago=16)
    add_escrito(env, case, days_ago=1)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["veredicto"]["valor"] == "fuera_de_plazo"


def test_causa_without_movements_but_with_escrito_is_not_sin_determinar(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=3, triggered_ago=5)
    add_escrito(env, case, days_ago=2)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["veredicto"]["valor"] == "cumplido"


def test_unrelated_escrito_without_movements_still_never_invents_a_miss(client, env):
    """An escrito that is not the excepciones filing is no evidence either way."""
    case = make_case(env)
    add_deadline(env, case, due_in=-2, triggered_ago=12)
    add_escrito(env, case, days_ago=3, tipo="Téngase presente")
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is None
    assert row["veredicto"]["valor"] == "sin_determinar"


def test_creditor_side_client_with_escrito_is_still_not_the_ejecutado(client, env):
    case = make_case(env, participante="DTE.")
    add_escrito(env, case, days_ago=2)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is None
    assert row["veredicto"]["valor"] == "sin_determinar"


def test_movement_and_escrito_best_verdict_wins_without_contradiction(client, env):
    """Movement says late, escrito says on time: the better one wins, both fields agree."""
    case = make_case(env)
    add_deadline(env, case, due_in=-6, triggered_ago=16)
    add_movement(env, case, "Opone excepciones", days_ago=1)  # registered 5 days past due
    add_escrito(env, case, days_ago=8)  # filed before due
    row = _only(_get(client))
    assert row["veredicto"]["valor"] == "cumplido"
    assert row["excepciones"]["presentadas"] is True
    assert row["excepciones"]["fecha"] == (_today_chile() - timedelta(days=8)).isoformat()


def test_equal_verdicts_keep_the_movement_as_evidence(client, env):
    case = make_case(env)
    add_deadline(env, case, due_in=3)
    mv = add_movement(env, case, "Opone excepciones", days_ago=2)
    add_escrito(env, case, days_ago=3)
    row = _only(_get(client))
    assert row["veredicto"]["valor"] == "cumplido"
    assert row["excepciones"]["movimiento_id"] == mv.id


def test_causa_without_escrito_is_unchanged(client, env):
    """No-regression: with no escritos the answer is exactly the movements-only one."""
    case = make_case(env)
    add_deadline(env, case, due_in=-2, triggered_ago=12)
    add_movement(env, case, "Se provee demanda", procedure="Resolución", days_ago=12)
    row = _only(_get(client))
    assert row["excepciones"] == {"presentadas": False, "fecha": None, "movimiento_id": None}
    assert row["veredicto"]["valor"] == "no_cumplido"


def test_movimiento_id_is_null_when_the_evidence_is_an_escrito(client, env):
    """There is no movement to point at: never invent one."""
    case = make_case(env)
    add_deadline(env, case, due_in=3, triggered_ago=5)
    add_movement(env, case, "Se provee demanda", procedure="Resolución", days_ago=5)
    add_escrito(env, case, days_ago=2)
    row = _only(_get(client))
    assert row["excepciones"]["presentadas"] is True
    assert row["excepciones"]["movimiento_id"] is None
    assert row["excepciones"]["fecha"] == (_today_chile() - timedelta(days=2)).isoformat()


def test_escritos_of_other_causas_do_not_leak(client, env):
    mine = make_case(env)
    other = make_case(env)
    add_deadline(env, mine, due_in=-2, triggered_ago=12)
    add_movement(env, mine, "Se provee demanda", procedure="Resolución", days_ago=12)
    add_escrito(env, other, days_ago=5)
    rows = {c["rol"]: c for c in _get(client)["causas"]}
    assert rows[mine.rol]["veredicto"]["valor"] == "no_cumplido"
