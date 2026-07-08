"""Tests for the Alert.read / Alert.read_at read-state columns.

Adds an in-app alert feed read/unread state to the previously
email/webhook-only Alert model (migration 026).
"""

from datetime import datetime

from app.models.alert import Alert
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


class TestAlertReadState:
    def test_new_alert_defaults_to_unread(self, db):
        lawyer = Lawyer(rut="11111111-1", name="Lawyer", role="lawyer")
        db.add(lawyer)
        db.flush()
        court = Court(code="T-ALERT", name="Juzgado Alert Test", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-1-2026", competencia="civil")
        db.add(case)
        db.flush()

        alert = Alert(
            lawyer_id=lawyer.id,
            case_id=case.id,
            type="new_movement",
            title="Nuevo movimiento",
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)

        assert alert.read is False
        assert alert.read_at is None

    def test_alert_can_be_marked_read(self, db):
        lawyer = Lawyer(rut="22222222-2", name="Lawyer 2", role="lawyer")
        db.add(lawyer)
        db.flush()
        court = Court(code="T-ALERT2", name="Juzgado Alert Test 2", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-2-2026", competencia="civil")
        db.add(case)
        db.flush()

        alert = Alert(
            lawyer_id=lawyer.id,
            case_id=case.id,
            type="new_movement",
            title="Nuevo movimiento",
        )
        db.add(alert)
        db.commit()

        now = datetime.utcnow()
        alert.read = True
        alert.read_at = now
        db.commit()
        db.refresh(alert)

        assert alert.read is True
        assert alert.read_at is not None
