"""Totals must agree with /cases within the caller's scope (causa-activa S5).

Reproduces the 2026-11-16 production mismatches: firm-wide stats and the risk board only counted cases with a
resolved firm abogado; /stats/admin quality was all zeros; /stats/me missed cases that /cases shows its owner.
"""
from datetime import datetime

import pytest

from app.models.case import Case
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import app_settings
from tests.api.v1.procurador_support import headers, make_case, make_court, make_lawyer


def _bare_case(db, owner, court, rol, **fields):
    """A causa with NO litigantes (no resolved abogado owner)."""
    case = Case(
        lawyer_id=owner.id, court_id=court.id, rol=rol, status="active", competencia="civil",
        plaintiff="Banco X", defendant="Deudor Y", created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        **fields,
    )
    db.add(case)
    db.commit()
    return case


@pytest.fixture
def firm(db):
    """admin + 1 firm lawyer; owned: 2 activas, 1 terminada, 1 caducada; unowned: 1 activa, 1 terminada."""
    court = make_court(db)
    abogado = make_lawyer(db, "11111111-1", "Abogado A")
    admin = make_lawyer(db, "22222222-2", "Admin", role="admin")
    make_case(db, abogado, court, "C-1-2026", abogado)
    make_case(db, abogado, court, "C-2-2026", abogado)
    make_case(db, abogado, court, "C-3-2026", abogado).terminada_en_sysgal = True
    make_case(db, abogado, court, "C-4-2026", abogado).contrato_estado = "caducado"
    _bare_case(db, admin, court, "C-5-2026")  # activa, nobody resolved as its owner
    _bare_case(db, admin, court, "C-6-2026", terminada_en_sysgal=True)
    db.commit()
    return {"abogado": abogado, "admin": admin, "court": court}


def _total_cases(client, rut):
    return client.get("/api/v1/cases", headers=headers(rut)).json()


def test_admin_firm_total_includes_causas_without_a_resolved_owner(client, firm):
    h = headers(firm["admin"].rut)
    cases = _total_cases(client, firm["admin"].rut)
    stats = client.get("/api/v1/stats/firm", headers=h).json()
    board = client.get("/api/v1/stats/risk-board", headers=h).json()

    assert cases["total"] == 3
    assert stats["totals"]["cases"] == 3
    assert board["total"] == 3
    assert stats["excluidas"] == board["excluidas"] == cases["excluidas"] == {
        "terminada_sysgal": 2, "contrato_caducado": 1,
    }


def test_admin_attributed_causas_are_a_separate_figure(client, firm):
    h = headers(firm["admin"].rut)
    stats = client.get("/api/v1/stats/firm", headers=h).json()
    board = client.get("/api/v1/stats/risk-board", headers=h).json()

    assert stats["totals"]["atribuidas"] == 2
    assert board["atribuidas"] == 2
    assert [lw["case_count"] for lw in stats["by_lawyer"]] == [2]
    assert [lw["total"] for lw in board["by_lawyer"]] == [2]


@pytest.mark.parametrize("rut", ["22222222-2", "22.222.222-2"])
def test_admin_quality_counts_the_activas(client, db, firm, rut):
    firm["admin"].rut = rut
    db.commit()

    body = client.get("/api/v1/stats/admin", headers=headers(rut)).json()

    assert body["quality"]["total_cases"] == 3
    assert body["quality"]["with_litigantes"] == 2
    assert body["excluidas"] == {"terminada_sysgal": 2, "contrato_caducado": 1}


@pytest.fixture
def luis(db):
    """Lawyer with the shapes /cases shows him that litigante attribution alone does not give."""
    court = make_court(db)
    luis = make_lawyer(db, "33333333-3", "Luis")
    other = make_lawyer(db, "44444444-4", "Other")
    make_case(db, luis, court, "L-1-2026", luis)  # litigante-owned activa
    make_case(db, luis, court, "L-2-2026", luis).terminada_en_sysgal = True
    make_case(db, luis, court, "L-3-2026", luis).contrato_estado = "caducado"
    _bare_case(db, luis, court, "L-4-2026")  # bootstrap: his by lawyer_id, no litigantes yet
    _bare_case(db, luis, court, "L-5-2026", terminada_en_sysgal=True)
    pendiente = _bare_case(db, other, court, "L-6-2026", patrocinio_pendiente=True, assigned_lawyer_id=luis.id)
    db.commit()
    return {"luis": luis, "other": other, "court": court, "pendiente": pendiente}


def _assert_me_matches_cases(client, rut):
    cases = _total_cases(client, rut)
    me = client.get("/api/v1/stats/me", headers=headers(rut)).json()
    assert me["case_count"] == cases["total"]
    assert me["excluidas"] == cases["excluidas"]
    return cases, me


def test_stats_me_uses_the_same_scope_as_cases(client, luis):
    cases, me = _assert_me_matches_cases(client, luis["luis"].rut)

    assert cases["total"] == 3  # L-1, L-4, L-6
    assert me["excluidas"] == {"terminada_sysgal": 2, "contrato_caducado": 1}


def test_stats_me_matches_cases_with_the_sysgal_owner_switch_on(client, db, luis):
    court, luis_l, other = luis["court"], luis["luis"], luis["other"]
    db.add_all([
        SysgalUsuario(sysgal_id=10, nombre="luis", lawyer_id=luis_l.id),
        SysgalUsuario(sysgal_id=20, nombre="other", lawyer_id=other.id),
    ])
    # Sysgal gives L-1 away from Luis (he stays only a PJUD litigante) and gives him a case with no litigantes.
    l1 = db.query(Case).filter(Case.rol == "L-1-2026").one()
    sysgal_mine = _bare_case(db, other, court, "L-7-2026")
    for n, (case, sid) in enumerate([(l1, 20), (sysgal_mine, 10)], start=1):
        db.add(SysgalCausa(sysgal_id=n, rol=case.rol, rol_norm=case.rol, tribunal="J", estado="activa",
                           eliminada=False, abogado_sysgal_id=sid, sysgal_updated_at=datetime(2026, 11, 1),
                           case_id=case.id))
    app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, 1)
    db.commit()

    cases, me = _assert_me_matches_cases(client, luis_l.rut)

    assert cases["total"] == 3  # L-4, L-6, L-7 (L-1 moved to Other)


def test_stats_me_for_a_procurador_matches_cases(client, db, luis):
    from tests.api.v1.procurador_support import link

    proc = make_lawyer(db, "55555555-5", "Proc", role="procurador")
    link(db, proc, luis["luis"])

    _assert_me_matches_cases(client, proc.rut)
