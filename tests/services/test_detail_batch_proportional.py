"""The per-cycle detail batch is proportional to the cartera a lawyer holds.

Measured 2026-10-20 on the live QA DB and the station worker log: every lawyer
received the same flat ``DETAIL_BATCH_SIZE`` (80 on the station), so the full
lap over a cartera was a function of its size alone. Lawyer 1 holds 38% of the
portfolio (5.698 of 14.981 causas) and received 11% of the daily detail reads
(115 of 1.261) -- a 49,5 day lap -- while a lawyer with 253 causas received 156
reads/day, a 1,6 day lap. Nothing hit the PJUD per-account limit in three days
(zero "Limite de detalle de PJUD" and zero "Rotacion preventiva" messages), and
14 of 16 batches ended on ``batch_size``: the ceiling is ours, not PJUD's.

The allocation conserves the firm-wide budget (``DETAIL_BATCH_SIZE`` x number
of lawyers with a cartera), so the cycle takes the same wall clock it takes
today; it only moves the slots to where the work is.
"""

from datetime import date, datetime, timedelta

import pytest

from app.config import Settings, settings
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.sync_service import (
    _select_cases_for_detail_rotation,
    detail_batch_size_for_lawyer,
)

from tests.services.test_detail_rotation import _make_api_case


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _court(db) -> Court:
    court = db.query(Court).first()
    if not court:
        court = Court(code="T1", name="Tribunal", region="RM", type="civil")
        db.add(court)
        db.flush()
    return court


def _lawyer_with_cartera(db, *, name: str, size: int, is_active: bool = True) -> Lawyer:
    """One lawyer holding *size* civil causas, all eligible for the rotation."""
    lawyer = Lawyer(rut=f"{abs(hash(name)) % 10**7:07d}-1", name=name, is_active=is_active)
    db.add(lawyer)
    db.flush()
    court = _court(db)
    base = datetime(2026, 1, 1)
    db.add_all([
        Case(
            lawyer_id=lawyer.id,
            court_id=court.id,
            rol=f"C-{lawyer.id}-{i}-2024",
            competencia="civil",
            status="active",
            last_detail_checked_at=base + timedelta(minutes=i),
        )
        for i in range(size)
    ])
    db.flush()
    return lawyer


@pytest.fixture
def proportional(monkeypatch):
    """Small, round numbers so the arithmetic is readable in the assertions."""
    monkeypatch.setattr(settings, "DETAIL_BATCH_PROPORTIONAL", True)
    monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 10)
    monkeypatch.setattr(settings, "DETAIL_BATCH_MIN_SIZE", 3)
    monkeypatch.setattr(settings, "DETAIL_BATCH_MAX_MULTIPLIER", 5.0)
    monkeypatch.setattr(settings, "DETAIL_MIN_YEAR", 2021)


# ---------------------------------------------------------------------------
# Defaults live in code, never only in the station's plist
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_proportional_is_on_by_default_in_code(self):
        assert Settings.model_fields["DETAIL_BATCH_PROPORTIONAL"].default is True

    def test_floor_and_ceiling_defaults(self):
        assert Settings.model_fields["DETAIL_BATCH_MIN_SIZE"].default == 15
        assert Settings.model_fields["DETAIL_BATCH_MAX_MULTIPLIER"].default == 2.0


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------

class TestProportionalAllocation:
    def test_a_big_cartera_gets_a_bigger_batch_than_a_small_one(self, db, proportional):
        grande = _lawyer_with_cartera(db, name="Grande", size=100)
        chica = _lawyer_with_cartera(db, name="Chica", size=10)
        db.commit()

        lote_grande = detail_batch_size_for_lawyer(db, grande.id, "civil")
        lote_chica = detail_batch_size_for_lawyer(db, chica.id, "civil")

        assert lote_grande > lote_chica, (
            "the lawyer holding 100 causas must not receive the same batch as "
            "the one holding 10"
        )

    def test_the_batch_tracks_the_share_of_the_portfolio(self, db, proportional):
        # 120 + 80 + 200 = 400 causas, budget = 10 x 3 lawyers = 30 slots.
        # Shares: 30 x 120/400 = 9, 30 x 80/400 = 6, 30 x 200/400 = 15.
        a = _lawyer_with_cartera(db, name="A", size=120)
        b = _lawyer_with_cartera(db, name="B", size=80)
        c = _lawyer_with_cartera(db, name="C", size=200)
        db.commit()

        assert detail_batch_size_for_lawyer(db, c.id, "civil") == 15
        assert detail_batch_size_for_lawyer(db, a.id, "civil") == 9
        assert detail_batch_size_for_lawyer(db, b.id, "civil") == 6

    def test_the_slots_add_up_to_the_budget_exactly(self, db, proportional):
        a = _lawyer_with_cartera(db, name="A", size=120)
        b = _lawyer_with_cartera(db, name="B", size=80)
        c = _lawyer_with_cartera(db, name="C", size=200)
        db.commit()

        lotes = [
            detail_batch_size_for_lawyer(db, x.id, "civil") for x in (a, b, c)
        ]
        assert sum(lotes) == settings.DETAIL_BATCH_SIZE * 3

    def test_the_small_cartera_keeps_its_floor(self, db, proportional):
        # 1000 + 6: the small one's proportional share is 0,1 slots. It must
        # still be read, so the floor (3) wins.
        _lawyer_with_cartera(db, name="Enorme", size=1000)
        chica = _lawyer_with_cartera(db, name="Chica", size=6)
        db.commit()

        assert detail_batch_size_for_lawyer(db, chica.id, "civil") == 3

    def test_the_floor_never_exceeds_the_cartera_it_protects(self, db, proportional):
        _lawyer_with_cartera(db, name="Enorme", size=1000)
        minima = _lawyer_with_cartera(db, name="Minima", size=2)
        db.commit()

        assert detail_batch_size_for_lawyer(db, minima.id, "civil") == 2

    def test_a_huge_cartera_is_bounded_by_the_ceiling(self, db, monkeypatch):
        """No lawyer may eat the whole cycle and delay everyone behind it."""
        monkeypatch.setattr(settings, "DETAIL_BATCH_PROPORTIONAL", True)
        monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 10)
        monkeypatch.setattr(settings, "DETAIL_BATCH_MIN_SIZE", 3)
        monkeypatch.setattr(settings, "DETAIL_BATCH_MAX_MULTIPLIER", 1.5)  # ceiling 15
        monkeypatch.setattr(settings, "DETAIL_MIN_YEAR", 2021)

        enorme = _lawyer_with_cartera(db, name="Enorme", size=1000)
        _lawyer_with_cartera(db, name="Chica", size=20)
        db.commit()

        assert detail_batch_size_for_lawyer(db, enorme.id, "civil") == 15

    def test_the_total_per_cycle_stays_bounded(self, db, proportional):
        """The firm-wide budget is conserved: no cycle grows against PJUD."""
        abogados = [
            _lawyer_with_cartera(db, name=f"L{i}", size=size)
            for i, size in enumerate([500, 200, 120, 60, 8])
        ]
        db.commit()

        lotes = [detail_batch_size_for_lawyer(db, a.id, "civil") for a in abogados]
        presupuesto = settings.DETAIL_BATCH_SIZE * len(abogados)

        assert sum(lotes) <= presupuesto, (
            f"the cycle must not grow: {sum(lotes)} slots against a budget of "
            f"{presupuesto}"
        )
        techo = int(settings.DETAIL_BATCH_SIZE * settings.DETAIL_BATCH_MAX_MULTIPLIER)
        assert max(lotes) <= techo

    def test_an_inactive_lawyer_does_not_consume_the_budget(self, db, proportional):
        activa = _lawyer_with_cartera(db, name="Activa", size=100)
        _lawyer_with_cartera(db, name="Retirada", size=100, is_active=False)
        db.commit()

        # Only the active lawyer funds and spends the budget: 10 x 1 lawyer.
        assert detail_batch_size_for_lawyer(db, activa.id, "civil") == 10


# ---------------------------------------------------------------------------
# The off switch
# ---------------------------------------------------------------------------

class TestOffSwitch:
    def test_the_off_switch_restores_the_flat_batch_exactly(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_BATCH_PROPORTIONAL", False)
        monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 80)
        monkeypatch.setattr(settings, "DETAIL_MIN_YEAR", 2021)

        grande = _lawyer_with_cartera(db, name="Grande", size=300)
        chica = _lawyer_with_cartera(db, name="Chica", size=5)
        db.commit()

        assert detail_batch_size_for_lawyer(db, grande.id, "civil") == 80
        assert detail_batch_size_for_lawyer(db, chica.id, "civil") == 80

    def test_an_unreadable_db_falls_back_to_the_flat_batch(self, monkeypatch):
        """A counting failure must never starve a lawyer's batch."""
        monkeypatch.setattr(settings, "DETAIL_BATCH_PROPORTIONAL", True)
        monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 42)

        class _Broken:
            def query(self, *a, **kw):
                raise RuntimeError("connection dropped")

        assert detail_batch_size_for_lawyer(_Broken(), 1, "civil") == 42


# ---------------------------------------------------------------------------
# The priority order inside the batch is untouched
# ---------------------------------------------------------------------------

class TestPriorityInsideTheBatch:
    def test_the_fatal_plazo_priority_still_wins_inside_the_batch(self, db, proportional):
        """A bigger batch must not dilute the imminent-fatal-plazo slot."""
        grande = _lawyer_with_cartera(db, name="Grande", size=100)
        _lawyer_with_cartera(db, name="Chica", size=10)

        court = _court(db)
        urgente = Case(
            lawyer_id=grande.id,
            court_id=court.id,
            rol="C-URGENTE-2026",
            competencia="civil",
            status="active",
            # Just read, so pure age rotation would put it LAST of all.
            last_detail_checked_at=datetime.utcnow(),
        )
        db.add(urgente)
        db.flush()
        db.add(
            CaseDeadline(
                case_id=urgente.id,
                deadline_type="acreditar_poder_3d",
                legal_basis="test",
                due_date=date.today() + timedelta(days=1),
                triggered_at=date.today() - timedelta(days=2),
                status="active",
            )
        )
        db.commit()

        lote = detail_batch_size_for_lawyer(db, grande.id, "civil")
        assert lote > 1, "the big cartera must get more than one slot"

        viejas = (
            db.query(Case)
            .filter(Case.lawyer_id == grande.id, Case.rol != "C-URGENTE-2026")
            .all()
        )
        api = [_make_api_case(c.rol) for c in viejas] + [_make_api_case("C-URGENTE-2026")]
        elegidas = _select_cases_for_detail_rotation(
            db, grande.id, "civil", api, batch_size=lote
        )

        assert [c.rol for c in elegidas][0] == "C-URGENTE-2026"
        assert len(elegidas) == lote


# ---------------------------------------------------------------------------
# Wiring: the cycle must USE the proportional size, not the flat setting
# ---------------------------------------------------------------------------

class TestSchedulerWiring:
    @pytest.mark.asyncio
    async def test_the_cycle_sizes_the_batch_per_lawyer(self, monkeypatch):
        """Both the selection and the end-reason cap get the per-lawyer size."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.workers import sync_scheduler as scheduler

        mock_db = MagicMock()
        store = MagicMock()
        store.get_session_by_lawyer = AsyncMock(return_value=MagicMock())
        scraper = MagicMock()
        scraper.get_my_cases = AsyncMock(return_value=[])
        scraper.close = AsyncMock()
        sync_cls = MagicMock()
        sync_cls.return_value.sync_cases.return_value = MagicMock(
            cases_total=0, cases_new=0
        )
        detect = AsyncMock(return_value=(0, 0, []))
        select = MagicMock(return_value=[])

        with patch.object(scheduler, "get_session_store", return_value=store), \
             patch("app.api.v1.pjud.get_scraper", return_value=scraper), \
             patch.object(scheduler, "SyncService", sync_cls), \
             patch.object(scheduler, "_select_cases_for_detail_rotation", select), \
             patch.object(scheduler, "detect_and_sync_movements", detect), \
             patch.object(scheduler, "detail_batch_size_for_lawyer", return_value=137), \
             patch.object(scheduler.settings, "DETAIL_BATCH_SIZE", 17):
            await scheduler.sync_lawyer_cases(1, "civil", mock_db)

        assert select.call_args.kwargs["batch_size"] == 137
        assert detect.await_args.kwargs["batch_size_cap"] == 137
