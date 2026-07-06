"""Tests for resolve_case_alert_recipients (ADR-005 recipient resolution).

Under Approach C, alert/webhook fan-out (sync_service.sync_movements,
deadlines.py) must resolve recipients from a Case's abogado-of-record
litigantes (AB.DTE/AB.DDO/AP.*), not from the single ``case.lawyer_id``.
Opposing/external counsel (no internal ``Lawyer`` row) are naturally
excluded — they have no row to match.
"""

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import resolve_case_alert_recipients


@pytest.fixture
def seeded_case(db):
    court = Court(code="T1-RECIP", name="Juzgado Recipients Test", region="RM", type="civil")
    db.add(court)
    db.flush()
    firm = Lawyer(rut="16021492-9", name="Firm Lawyer")
    db.add(firm)
    db.flush()
    case = Case(lawyer_id=firm.id, court_id=court.id, rol="C-1-2026", competencia="civil")
    db.add(case)
    db.commit()
    return case


def _add_litigante(db, case_id, participante, rut, nombre):
    lit = CaseLitigante(
        case_id=case_id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case_id}-{participante}-{rut}",
    )
    db.add(lit)
    db.commit()
    return lit


class TestResolveCaseAlertRecipients:
    def test_returns_internal_lawyers_only_ab_ap_litigantes(self, db, seeded_case):
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        marcela = Lawyer(rut="17222222-2", name="Marcela")
        db.add_all([sandy, marcela])
        db.commit()

        _add_litigante(db, seeded_case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, seeded_case.id, "AP.DTE", marcela.rut, "Marcela")
        # A non-abogado party litigante should never be a recipient.
        _add_litigante(db, seeded_case.id, "DTE.", "99999999-9", "Cliente Demandante")

        recipients = resolve_case_alert_recipients(db, seeded_case)

        assert {r.id for r in recipients} == {sandy.id, marcela.id}

    def test_excludes_opposing_counsel_no_lawyer_row(self, db, seeded_case):
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        db.add(sandy)
        db.commit()

        _add_litigante(db, seeded_case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, seeded_case.id, "AB.DTE", "88888888-8", "Abogado Contrario")

        recipients = resolve_case_alert_recipients(db, seeded_case)

        assert {r.id for r in recipients} == {sandy.id}

    def test_dedupes_same_lawyer_appearing_in_multiple_litigante_rows(self, db, seeded_case):
        sandy = Lawyer(rut="17111111-1", name="Sandy")
        db.add(sandy)
        db.commit()

        _add_litigante(db, seeded_case.id, "AB.DDO", sandy.rut, "Sandy")
        _add_litigante(db, seeded_case.id, "AP.DDO", sandy.rut, "Sandy")

        recipients = resolve_case_alert_recipients(db, seeded_case)

        assert len(recipients) == 1
        assert recipients[0].id == sandy.id

    def test_returns_empty_when_no_internal_litigante_present(self, db, seeded_case):
        _add_litigante(db, seeded_case.id, "AB.DTE", "88888888-8", "Abogado Contrario")

        recipients = resolve_case_alert_recipients(db, seeded_case)

        assert recipients == []
