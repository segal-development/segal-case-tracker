"""A case whose FATAL plazo expires within days must be re-read before it does.

Real incident, 2026-10-20. The system scraped C-4124-2026 at 19:26 on the 19th
and emitted a RED "ratificar firma, vence el 20-10" alert four seconds later.
The poder had been ratified before the tribunal THAT SAME DAY. By the time the
firm's admin opened the email, the case had not been re-read for 18 hours, so
the app still said it was pending and sent her chasing something already done.

Measured that morning: 14 fatal plazos expiring that day or the next whose case
had not been re-read since the previous day. Each one is the same false alarm
waiting to happen -- and the opposite failure too, since a case we do not
re-read is also one where a real lapse goes unseen.

The detail rotation already has a priority slot (pending-docs backfill). This
adds a higher one: an imminent fatal plazo jumps the queue.
"""

from datetime import date, datetime, timedelta

import pytest

from app.config import settings
from app.models.case_deadline import CaseDeadline
from app.services.sync_service import _select_cases_for_detail_rotation

from tests.services.test_detail_rotation import (  # reuse the file's helpers
    _make_api_case,
    _seed_case,
)


def _plazo(db, case, *, tipo="acreditar_poder_3d", vence_en_dias=1):
    db.add(
        CaseDeadline(
            case_id=case.id,
            deadline_type=tipo,
            legal_basis="test",
            due_date=date.today() + timedelta(days=vence_en_dias),
            triggered_at=date.today() - timedelta(days=2),
            status="active",
        )
    )
    db.flush()


class TestImminentFatalPlazoJumpsTheQueue:
    def test_a_case_with_a_fatal_plazo_due_tomorrow_is_read_first(self, db):
        # Freshly read, so plain rotation would put it LAST.
        urgente = _seed_case(
            db, 1, "civil", "C-URGENTE-2026",
            last_detail_checked_at=datetime.utcnow(),
        )
        _plazo(db, urgente, vence_en_dias=1)
        # Never read, so plain rotation would put it FIRST.
        vieja = _seed_case(db, 1, "civil", "C-VIEJA-2026", last_detail_checked_at=None)
        db.commit()

        api = [_make_api_case("C-VIEJA-2026"), _make_api_case("C-URGENTE-2026")]
        elegidas = _select_cases_for_detail_rotation(
            db, urgente.lawyer_id, "civil", api, batch_size=1
        )
        assert [c.rol for c in elegidas] == ["C-URGENTE-2026"]

    def test_a_plazo_further_out_than_the_window_does_not_jump(self, db):
        lejano = _seed_case(
            db, 1, "civil", "C-LEJANA-2026",
            last_detail_checked_at=datetime.utcnow(),
        )
        _plazo(db, lejano, vence_en_dias=settings.DETAIL_FATAL_PLAZO_DAYS + 5)
        vieja = _seed_case(db, 1, "civil", "C-VIEJA-2026", last_detail_checked_at=None)
        db.commit()

        api = [_make_api_case("C-VIEJA-2026"), _make_api_case("C-LEJANA-2026")]
        elegidas = _select_cases_for_detail_rotation(
            db, lejano.lawyer_id, "civil", api, batch_size=1
        )
        assert [c.rol for c in elegidas] == ["C-VIEJA-2026"]

    def test_an_already_expired_plazo_does_not_jump(self, db):
        # Re-reading it changes nothing: the window is gone. The expired-notice
        # path covers these, and letting them jump would starve the live ones.
        vencido = _seed_case(
            db, 1, "civil", "C-VENCIDA-2026",
            last_detail_checked_at=datetime.utcnow(),
        )
        _plazo(db, vencido, vence_en_dias=-3)
        vieja = _seed_case(db, 1, "civil", "C-VIEJA-2026", last_detail_checked_at=None)
        db.commit()

        api = [_make_api_case("C-VIEJA-2026"), _make_api_case("C-VENCIDA-2026")]
        elegidas = _select_cases_for_detail_rotation(
            db, vencido.lawyer_id, "civil", api, batch_size=1
        )
        assert [c.rol for c in elegidas] == ["C-VIEJA-2026"]

    def test_a_non_fatal_plazo_does_not_jump(self, db):
        no_fatal = _seed_case(
            db, 1, "civil", "C-NOFATAL-2026",
            last_detail_checked_at=datetime.utcnow(),
        )
        _plazo(db, no_fatal, tipo="observaciones_prueba_6d", vence_en_dias=1)
        vieja = _seed_case(db, 1, "civil", "C-VIEJA-2026", last_detail_checked_at=None)
        db.commit()

        api = [_make_api_case("C-VIEJA-2026"), _make_api_case("C-NOFATAL-2026")]
        elegidas = _select_cases_for_detail_rotation(
            db, no_fatal.lawyer_id, "civil", api, batch_size=1
        )
        assert [c.rol for c in elegidas] == ["C-VIEJA-2026"]
