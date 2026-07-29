"""Tests for app.services.daily_agenda — daily per-lawyer calendar guide."""

from datetime import date, datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services import daily_agenda
from app.services.daily_agenda import (
    lawyer_day_agenda,
    send_daily_calendar_emails,
)

TARGET_DAY = date(2026, 8, 1)
OTHER_DAY = date(2026, 8, 2)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def court(db):
    obj = Court(code="T1-AG", name="Juzgado Agenda", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def carla(db):
    obj = Lawyer(
        rut="10000000-1",
        name="Carla Admin",
        email="carla@segal.cl",
        role="admin",
        is_firm_lawyer=False,
        is_active=True,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def lawyer(db):
    obj = Lawyer(
        rut="20000000-2",
        name="Ana Firma",
        email="ana@segal.cl",
        role="lawyer",
        is_firm_lawyer=True,
        is_active=True,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(
    db,
    court,
    rol,
    *,
    owner,
    status="active",
    next_deadline_at=None,
    next_deadline_fatal=False,
    next_review_at=None,
    recommended_action_code=None,
):
    obj = Case(
        lawyer_id=owner.id,
        court_id=court.id,
        rol=rol,
        status=status,
        competencia="civil",
        plaintiff="BANCO DTE",
        defendant="DEUDOR DDO",
        next_deadline_at=next_deadline_at,
        next_deadline_fatal=next_deadline_fatal,
        next_review_at=next_review_at,
        recommended_action_code=recommended_action_code,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_abogado(db, case, lawyer):
    """Seed ``lawyer`` as AB.DDO abogado-of-record on ``case``."""
    lit = CaseLitigante(
        case_id=case.id,
        participante="AB.DDO",
        rut=lawyer.rut,
        persona_type="NATURAL",
        nombre=lawyer.name,
        natural_key=f"{case.id}-{lawyer.rut}",
    )
    db.add(lit)
    db.commit()


def _seed_demandado(db, case, rut, nombre="DEUDOR DDO"):
    """Seed the demandado (DDO.) party on ``case`` with ``rut``."""
    lit = CaseLitigante(
        case_id=case.id,
        participante="DDO.",
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case.id}-ddo-{rut}",
    )
    db.add(lit)
    db.commit()


# ---------------------------------------------------------------------------
# lawyer_day_agenda
# ---------------------------------------------------------------------------


class TestLawyerDayAgenda:
    def test_deadline_on_target_day_appears(self, db, court, lawyer):
        c = _make_case(db, court, "C-1-2026", owner=lawyer, next_deadline_at=TARGET_DAY)
        _seed_abogado(db, c, lawyer)

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert len(agenda.deadlines) == 1
        assert agenda.deadlines[0].rol == "C-1-2026"
        assert not agenda.reviews

    def test_review_on_target_day_appears(self, db, court, lawyer):
        c = _make_case(
            db, court, "C-2-2026", owner=lawyer,
            next_review_at=TARGET_DAY,
            recommended_action_code="oponer_excepciones",
        )
        _seed_abogado(db, c, lawyer)

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert len(agenda.reviews) == 1
        assert agenda.reviews[0].rol == "C-2-2026"
        assert not agenda.deadlines

    def test_deadline_on_different_day_excluded(self, db, court, lawyer):
        c = _make_case(db, court, "C-3-2026", owner=lawyer, next_deadline_at=OTHER_DAY)
        _seed_abogado(db, c, lawyer)

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert agenda.is_empty

    def test_archived_case_excluded(self, db, court, lawyer):
        c = _make_case(
            db, court, "C-4-2026", owner=lawyer, status="archived", next_deadline_at=TARGET_DAY
        )
        _seed_abogado(db, c, lawyer)

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert agenda.is_empty

    def test_other_lawyers_case_excluded(self, db, court, lawyer):
        # A case where a DIFFERENT rut is the abogado-of-record.
        other = Lawyer(
            rut="30000000-3", name="Otro", email="otro@segal.cl",
            role="lawyer", is_firm_lawyer=True, is_active=True,
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        c = _make_case(db, court, "C-5-2026", owner=other, next_deadline_at=TARGET_DAY)
        _seed_abogado(db, c, other)

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert agenda.is_empty

    def test_has_urgent_true_when_fatal(self, db, court, lawyer):
        c = _make_case(
            db, court, "C-6-2026", owner=lawyer, next_deadline_at=TARGET_DAY, next_deadline_fatal=True
        )
        _seed_abogado(db, c, lawyer)

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert agenda.has_urgent is True

    def test_has_urgent_false_when_not_fatal(self, db, court, lawyer):
        c = _make_case(
            db, court, "C-7-2026", owner=lawyer, next_deadline_at=TARGET_DAY, next_deadline_fatal=False
        )
        _seed_abogado(db, c, lawyer)

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert agenda.has_urgent is False

    def test_demandado_rut_populated_and_rendered(self, db, court, lawyer):
        from app.services.daily_agenda import render_daily_agenda_email

        c = _make_case(db, court, "C-8-2026", owner=lawyer, next_deadline_at=TARGET_DAY)
        _seed_abogado(db, c, lawyer)
        _seed_demandado(db, c, "17222333-8")

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert len(agenda.deadlines) == 1
        # format_rut formats with thousands separators.
        assert agenda.deadlines[0].demandado_rut == "17.222.333-8"

        # The RUT reaches both the HTML and the plain-text email.
        _, html_body, text_body = render_daily_agenda_email(lawyer, TARGET_DAY, agenda)
        assert "17.222.333-8" in html_body
        assert "RUT demandado: 17.222.333-8" in text_body

    def test_demandado_rut_none_when_no_ddo(self, db, court, lawyer):
        c = _make_case(db, court, "C-9-2026", owner=lawyer, next_deadline_at=TARGET_DAY)
        _seed_abogado(db, c, lawyer)  # only the abogado, no DDO. party

        agenda = lawyer_day_agenda(db, lawyer, TARGET_DAY)
        assert agenda.deadlines[0].demandado_rut is None


# ---------------------------------------------------------------------------
# send_daily_calendar_emails
# ---------------------------------------------------------------------------


class TestSendDailyCalendarEmails:
    def test_cc_carla_only_when_urgent_and_skips_null_email(
        self, db, court, carla, monkeypatch
    ):
        # Lawyer with a FATAL deadline that day → CC carla.
        fatal_lawyer = Lawyer(
            rut="20000000-2", name="Ana", email="ana@segal.cl",
            role="lawyer", is_firm_lawyer=True, is_active=True,
        )
        # Lawyer with a NON-fatal deadline that day → no CC.
        normal_lawyer = Lawyer(
            rut="30000000-3", name="Beto", email="beto@segal.cl",
            role="lawyer", is_firm_lawyer=True, is_active=True,
        )
        # Active firm lawyer with NULL email → skipped.
        noemail_lawyer = Lawyer(
            rut="40000000-4", name="Cami", email=None,
            role="lawyer", is_firm_lawyer=True, is_active=True,
        )
        db.add_all([fatal_lawyer, normal_lawyer, noemail_lawyer])
        db.commit()
        for lw in (fatal_lawyer, normal_lawyer, noemail_lawyer):
            db.refresh(lw)

        c_fatal = _make_case(
            db, court, "C-100-2026", owner=fatal_lawyer, next_deadline_at=TARGET_DAY, next_deadline_fatal=True
        )
        _seed_abogado(db, c_fatal, fatal_lawyer)
        c_normal = _make_case(
            db, court, "C-101-2026", owner=normal_lawyer, next_deadline_at=TARGET_DAY, next_deadline_fatal=False
        )
        _seed_abogado(db, c_normal, normal_lawyer)

        calls = []

        def _fake_send(to, subject, html_body, text_body, cc=None):
            calls.append((to, cc or []))
            return True

        monkeypatch.setattr(daily_agenda, "_send_email", _fake_send)

        summary = send_daily_calendar_emails(db, TARGET_DAY)

        by_to = {to: cc for to, cc in calls}
        # Fatal lawyer emailed with carla CC'd.
        assert by_to["ana@segal.cl"] == ["carla@segal.cl"]
        # Normal lawyer emailed with no CC.
        assert by_to["beto@segal.cl"] == []
        # Null-email lawyer never emailed.
        assert None not in by_to
        assert "cami" not in "".join(by_to.keys())

        assert summary == {
            "sent": 2,
            "cc_count": 1,
            "skipped_no_email": 1,
            "errors": 0,
            "target_day": str(TARGET_DAY),
        }
