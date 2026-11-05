"""The detail rotation must not spend its capacity on causas that stopped moving.

Measured on the live QA DB (2026-10-21), over the 15.201 rows in rotation
(``status='active' AND poda_at IS NULL``):

    sin movimiento hace +1 ano   2.976   (no deadline alive on any of them)
    sin movimiento hace +2 anos  1.245
    sin movimiento hace +3 anos    367
    sin ningun movimiento        3.699   (1.758 of them never detail-scraped)

A causa dormant for years is rotated at the same frequency as one notified
yesterday. The rotation is the channel for what nobody told us about; a dormant
causa that wakes up is caught by the OJV estado-diario feed instead.

THE SAFETY CONSTRAINT, same shape as the secondary-cuaderno demotion:
dormancy DEPRIORITIZES, it never drops. Past the re-read window the row
rejoins the rotation at its true age, a cartera made only of dormant rows
still fills its batch, a row carrying a LIVE deadline keeps its normal
priority, and a row with no ``last_movement_at`` is treated as a real case --
3.699 rows have none, 1.758 of them because nobody ever looked at them.
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

AHORA = datetime.utcnow()
RECIENTE = AHORA - timedelta(days=1)
MAS_VIEJA = AHORA - timedelta(days=3)

SE_MOVIO_AYER = AHORA - timedelta(days=1)


def _dormida_desde(extra_dias: int = 30) -> datetime:
    """A ``last_movement_at`` comfortably past the dormancy threshold."""
    return AHORA - timedelta(days=settings.DETAIL_DORMANT_DAYS + extra_dias)


def _plazo(db, case, *, tipo="acreditar_poder_3d", vence_en_dias=30, status="active"):
    db.add(
        CaseDeadline(
            case_id=case.id,
            deadline_type=tipo,
            legal_basis="test",
            due_date=date.today() + timedelta(days=vence_en_dias),
            triggered_at=date.today() - timedelta(days=2),
            status=status,
        )
    )
    db.flush()


def _elegir(db, lawyer_id, rols, batch_size):
    api = [_make_api_case(r) for r in rols]
    return [c.rol for c in _select_cases_for_detail_rotation(
        db, lawyer_id, "civil", api, batch_size=batch_size
    )]


class TestDormantCaseDemotion:
    def test_a_dormant_row_sinks_below_a_recently_moved_one_of_equal_age(self, db):
        """Same ``last_detail_checked_at``, one slot: the live causa takes it."""
        dormida = _seed_case(
            db, 1, "civil", "C-DORMIDA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=_dormida_desde(),
            procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, dormida.lawyer_id, ["C-DORMIDA-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-VIVA-2026"]

    def test_a_dormant_row_carrying_a_live_deadline_does_not_sink(self, db):
        """A plazo alive on a dormant causa is exactly what we must not miss.

        The dormant row is OLDER, so plain age order puts it first. The
        demotion must not steal its slot.
        """
        con_plazo = _seed_case(
            db, 1, "civil", "C-DORMIDA-CON-PLAZO-2026",
            last_detail_checked_at=MAS_VIEJA,
            last_movement_at=_dormida_desde(),
            procedure="Principal",
        )
        # Far outside DETAIL_FATAL_PLAZO_DAYS: this is the live-deadline
        # protection, not the imminent-fatal jump.
        _plazo(db, con_plazo, vence_en_dias=30)
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(
            db, con_plazo.lawyer_id, ["C-DORMIDA-CON-PLAZO-2026", "C-VIVA-2026"], 1
        )
        assert elegidas == ["C-DORMIDA-CON-PLAZO-2026"]

    def test_an_expired_deadline_does_not_protect_a_dormant_row(self, db):
        """Only LIVE deadlines protect; an expired one leaves the row dormant."""
        vencido = _seed_case(
            db, 1, "civil", "C-VENCIDO-2026",
            last_detail_checked_at=MAS_VIEJA,
            last_movement_at=_dormida_desde(),
            procedure="Principal",
        )
        _plazo(db, vencido, vence_en_dias=-10)
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, vencido.lawyer_id, ["C-VENCIDO-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-VIVA-2026"]

    def test_a_row_with_no_last_movement_at_is_treated_as_a_real_case(self, db):
        """3.699 rows carry no ``last_movement_at``; 1.758 were never scraped.

        "We have no movement on record" is not evidence of dormancy, it is the
        absence of evidence. Being wrong towards "real case" costs capacity;
        being wrong the other way blinds the firm.
        """
        sin_senal = _seed_case(
            db, 1, "civil", "C-SIN-SENAL-2026",
            last_detail_checked_at=MAS_VIEJA,
            last_movement_at=None,
            procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, sin_senal.lawyer_id, ["C-SIN-SENAL-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-SIN-SENAL-2026"]

    def test_an_imminent_fatal_plazo_still_beats_the_demotion(self, db):
        """The highest priority slot stays the highest priority slot."""
        urgente = _seed_case(
            db, 1, "civil", "C-URGENTE-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=_dormida_desde(),
            procedure="Principal",
        )
        _plazo(db, urgente, vence_en_dias=1)
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=MAS_VIEJA,
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, urgente.lawyer_id, ["C-URGENTE-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-URGENTE-2026"]

    def test_a_dormant_row_past_the_window_rejoins_the_rotation(self, db):
        """Deprioritize, never drop: past the re-read window it competes on age."""
        dias = settings.DETAIL_DORMANT_MIN_DAYS
        dormida = _seed_case(
            db, 1, "civil", "C-DORMIDA-2026",
            last_detail_checked_at=AHORA - timedelta(days=dias + 5),
            last_movement_at=_dormida_desde(),
            procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=AHORA - timedelta(days=dias + 1),
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, dormida.lawyer_id, ["C-DORMIDA-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-DORMIDA-2026"]

    def test_a_cartera_of_only_dormant_rows_still_fills_its_batch(self, db):
        """A cartera where everything stopped moving must not idle the scraper."""
        solo_dormidas = _seed_case(
            db, 1, "civil", "C-DORMIDA-A-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=_dormida_desde(),
            procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-DORMIDA-B-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=_dormida_desde(60),
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(
            db, solo_dormidas.lawyer_id, ["C-DORMIDA-A-2026", "C-DORMIDA-B-2026"], 10
        )
        assert sorted(elegidas) == ["C-DORMIDA-A-2026", "C-DORMIDA-B-2026"]

    def test_a_case_just_under_the_threshold_is_not_dormant(self, db):
        """The threshold is a real cut, not "anything older than a few weeks"."""
        casi = _seed_case(
            db, 1, "civil", "C-CASI-2026",
            last_detail_checked_at=MAS_VIEJA,
            last_movement_at=AHORA - timedelta(days=settings.DETAIL_DORMANT_DAYS - 10),
            procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, casi.lawyer_id, ["C-CASI-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-CASI-2026"]

    def test_the_kill_switch_restores_the_legacy_age_ordering(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_DORMANT_DEMOTE", False)
        dormida = _seed_case(
            db, 1, "civil", "C-DORMIDA-2026",
            last_detail_checked_at=MAS_VIEJA,
            last_movement_at=_dormida_desde(),
            procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, dormida.lawyer_id, ["C-DORMIDA-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-DORMIDA-2026"]


class TestDormancyComposesWithTheCuadernoDemotion:
    """Both demotions are ORDER BY terms of the SAME statement.

    The sort key is ``(cuaderno inerte, durmiente, edad)``, so the four buckets
    are: real+viva, real+durmiente, cuaderno+viva, cuaderno+durmiente. A row
    that is BOTH sinks to the last bucket -- it is the least deserving row in
    the cartera -- but the two windows do NOT add up: they are independent
    "read recently enough for its class" predicates over the same
    ``last_detail_checked_at``, so the effective minimum interval of a row that
    is both is max(cuaderno window, dormancy window), never their sum.
    """

    def _cuatro_cubos(self, db):
        _seed_case(
            db, 1, "civil", "C-REAL-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER, procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-REAL-DORMIDA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=_dormida_desde(), procedure="Principal",
        )
        _seed_case(
            db, 1, "civil", "C-CUADERNO-VIVA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=SE_MOVIO_AYER, procedure="Medida prejudicial",
        )
        _seed_case(
            db, 1, "civil", "C-CUADERNO-DORMIDA-2026",
            last_detail_checked_at=RECIENTE,
            last_movement_at=_dormida_desde(), procedure="Medida prejudicial",
        )
        db.commit()

    def test_the_two_demotions_order_into_four_coherent_buckets(self, db):
        self._cuatro_cubos(db)
        rols = [
            "C-CUADERNO-DORMIDA-2026", "C-CUADERNO-VIVA-2026",
            "C-REAL-DORMIDA-2026", "C-REAL-VIVA-2026",
        ]
        assert _elegir(db, 1, rols, 4) == [
            "C-REAL-VIVA-2026",
            "C-REAL-DORMIDA-2026",
            "C-CUADERNO-VIVA-2026",
            "C-CUADERNO-DORMIDA-2026",
        ]

    def test_the_two_windows_do_not_add_up(self, db):
        """Past the LONGER of the two windows, a both-row is fully back.

        If the demotions compounded, this row would still be sunk below a
        causa viva read more recently than it was.
        """
        ventana = max(
            settings.DETAIL_DORMANT_MIN_DAYS,
            settings.DETAIL_SECONDARY_CUADERNO_MIN_DAYS,
        )
        ambas = _seed_case(
            db, 1, "civil", "C-AMBAS-2026",
            last_detail_checked_at=AHORA - timedelta(days=ventana + 5),
            last_movement_at=_dormida_desde(),
            procedure="Medida prejudicial",
        )
        _seed_case(
            db, 1, "civil", "C-VIVA-2026",
            last_detail_checked_at=AHORA - timedelta(days=ventana + 1),
            last_movement_at=SE_MOVIO_AYER,
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, ambas.lawyer_id, ["C-AMBAS-2026", "C-VIVA-2026"], 1)
        assert elegidas == ["C-AMBAS-2026"]
