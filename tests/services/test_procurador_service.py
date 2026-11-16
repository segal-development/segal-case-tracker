"""Procurador service: validation, links, alert recipients (S2, S3)."""
import pytest
from fastapi import HTTPException

from app.models.case_litigante import CaseLitigante
from app.services import lawyer_roster
from app.services import procurador as svc
from tests.api.v1.procurador_support import link, make_case, make_court, make_lawyer


@pytest.fixture
def w(db):
    firm = make_lawyer(db, "16021492-9", "Firma", role="admin")
    a = make_lawyer(db, "11111111-1", "A")
    b = make_lawyer(db, "22222222-2", "B")
    p = make_lawyer(db, "44444444-4", "P", role="procurador")
    return firm, a, b, p


def test_validate_counts_and_message(db, w):
    _, a, b, _ = w
    for ids in ([], [1, 2, 3]):
        with pytest.raises(HTTPException) as e:
            svc.validate_abogado_ids(db, ids)
        assert e.value.status_code == 422 and e.value.detail == "Un procurador debe apoyar a 1 o 2 abogados."
    assert [x.id for x in svc.validate_abogado_ids(db, [a.id, a.id])] == [a.id]  # dedup
    assert len(svc.validate_abogado_ids(db, [a.id, b.id])) == 2


def test_linked_abogados_only_active_firm(db, w):
    _, a, b, p = w
    link(db, p, a, b)
    b.is_active = False
    db.commit()
    assert [x.id for x in svc.linked_abogados(db, p.id)] == [a.id]


def test_set_links_replaces(db, w):
    _, a, b, p = w
    link(db, p, a)
    svc.set_links(db, p, [b])
    db.commit()
    assert svc.linked_abogado_ids(db, p.id) == {b.id}


def test_alert_recipients_never_include_a_procurador(db, w):
    firm, a, _, p = w
    case = make_case(db, firm, make_court(db), "C-1-2024", a)
    db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=p.rut, persona_type="NATURAL",
                         nombre="P", natural_key=f"{case.id}-{p.rut}"))
    db.commit()
    ids = {lw.id for lw in lawyer_roster.resolve_case_alert_recipients(db, case)}
    assert ids == {a.id}


def test_assigned_override_to_a_procurador_is_not_a_recipient(db, w):
    firm, a, _, p = w
    case = make_case(db, firm, make_court(db), "C-2-2024", a)
    case.assigned_lawyer_id = p.id
    db.commit()
    assert p.id not in {lw.id for lw in lawyer_roster.resolve_case_alert_recipients(db, case)}
