"""The detail rotation must spend its budget on real cases, not inert cuadernos.

PJUD splits one judicial case into cuadernos: the main one ("Principal") plus
secondary ones (medidas prejudiciales, incidentes, apremios). We store ONE
``cases`` row per cuaderno, so the rotation treats every cuaderno as an
independent case to visit.

Measured on the live QA DB (2026-10-20): of 1.082 detail reads in 24 h, 646
(60%) landed on a cuaderno that is not Principal, while Principal rows are
only 66% of the population. Small carteras were the worst: one lawyer spent
131 of its 156 daily reads re-reading 199 medidas prejudiciales every 1,5 days.

THE SAFETY CONSTRAINT: secondary cuadernos are deprioritized, NEVER dropped.
Measured the same day, 13 of 54 live (not yet expired) deadlines live on a
secondary cuaderno. A cuaderno row that carries a live deadline keeps its
normal priority, and a row whose cuaderno we cannot identify is treated as a
real case -- being wrong in that direction is safe, the other one is not.
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

RECIENTE = datetime.utcnow() - timedelta(days=1)
MAS_VIEJA = datetime.utcnow() - timedelta(days=3)


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


class TestSecondaryCuadernoDemotion:
    def test_principal_wins_over_an_inert_cuaderno_of_equal_age(self, db):
        """Same age, one slot: the real case must take it."""
        inerte = _seed_case(
            db, 1, "civil", "C-INERTE-2026",
            last_detail_checked_at=RECIENTE, procedure="Medida prejudicial",
        )
        _seed_case(
            db, 1, "civil", "C-PRINCIPAL-2026",
            last_detail_checked_at=RECIENTE, procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, inerte.lawyer_id, ["C-INERTE-2026", "C-PRINCIPAL-2026"], 1)
        assert elegidas == ["C-PRINCIPAL-2026"]

    def test_a_cuaderno_carrying_a_live_deadline_is_not_demoted(self, db):
        """26% of live fatal deadlines live on a secondary cuaderno.

        The secondary row is OLDER, so plain age order puts it first. The
        demotion must not steal its slot.
        """
        con_plazo = _seed_case(
            db, 1, "civil", "C-PREJUDICIAL-2026",
            last_detail_checked_at=MAS_VIEJA, procedure="Medida prejudicial",
        )
        # Far outside DETAIL_FATAL_PLAZO_DAYS: this is the live-deadline
        # protection, not the imminent-fatal jump.
        _plazo(db, con_plazo, vence_en_dias=30)
        _seed_case(
            db, 1, "civil", "C-PRINCIPAL-2026",
            last_detail_checked_at=RECIENTE, procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(
            db, con_plazo.lawyer_id, ["C-PREJUDICIAL-2026", "C-PRINCIPAL-2026"], 1
        )
        assert elegidas == ["C-PREJUDICIAL-2026"]

    def test_an_expired_deadline_does_not_protect_an_inert_cuaderno(self, db):
        """Only LIVE deadlines protect; an expired one leaves the row inert."""
        vencido = _seed_case(
            db, 1, "civil", "C-VENCIDO-2026",
            last_detail_checked_at=MAS_VIEJA, procedure="Incidente Abandono del Proc.",
        )
        _plazo(db, vencido, vence_en_dias=-10)
        _seed_case(
            db, 1, "civil", "C-PRINCIPAL-2026",
            last_detail_checked_at=RECIENTE, procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(
            db, vencido.lawyer_id, ["C-VENCIDO-2026", "C-PRINCIPAL-2026"], 1
        )
        assert elegidas == ["C-PRINCIPAL-2026"]

    @pytest.mark.parametrize("cuaderno", [None, "", "   "])
    def test_an_unknown_cuaderno_is_treated_as_a_real_case(self, db, cuaderno):
        """Neither field is fully trustworthy. Unknown must never mean skip."""
        desconocida = _seed_case(
            db, 1, "civil", "C-DESCONOCIDA-2026",
            last_detail_checked_at=MAS_VIEJA, procedure=cuaderno,
        )
        _seed_case(
            db, 1, "civil", "C-PRINCIPAL-2026",
            last_detail_checked_at=RECIENTE, procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(
            db, desconocida.lawyer_id, ["C-DESCONOCIDA-2026", "C-PRINCIPAL-2026"], 1
        )
        assert elegidas == ["C-DESCONOCIDA-2026"]

    def test_an_imminent_fatal_plazo_still_beats_the_demotion(self, db):
        """The highest priority slot stays the highest priority slot."""
        urgente = _seed_case(
            db, 1, "civil", "C-URGENTE-2026",
            last_detail_checked_at=RECIENTE, procedure="Medida prejudicial",
        )
        _plazo(db, urgente, vence_en_dias=1)
        _seed_case(
            db, 1, "civil", "C-PRINCIPAL-2026",
            last_detail_checked_at=MAS_VIEJA, procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(
            db, urgente.lawyer_id, ["C-URGENTE-2026", "C-PRINCIPAL-2026"], 1
        )
        assert elegidas == ["C-URGENTE-2026"]

    def test_an_inert_cuaderno_past_the_window_rejoins_the_rotation(self, db):
        """Deprioritize, never drop: past the minimum interval it competes on age."""
        dias = settings.DETAIL_SECONDARY_CUADERNO_MIN_DAYS
        vieja = _seed_case(
            db, 1, "civil", "C-INERTE-2026",
            last_detail_checked_at=datetime.utcnow() - timedelta(days=dias + 5),
            procedure="Medida prejudicial",
        )
        _seed_case(
            db, 1, "civil", "C-PRINCIPAL-2026",
            last_detail_checked_at=datetime.utcnow() - timedelta(days=dias + 1),
            procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, vieja.lawyer_id, ["C-INERTE-2026", "C-PRINCIPAL-2026"], 1)
        assert elegidas == ["C-INERTE-2026"]

    def test_a_demoted_cuaderno_still_fills_a_batch_with_nothing_else_to_do(self, db):
        """A cartera of only secondary cuadernos must not idle the scraper."""
        solo_inertes = _seed_case(
            db, 1, "civil", "C-INERTE-A-2026",
            last_detail_checked_at=RECIENTE, procedure="Medida prejudicial",
        )
        _seed_case(
            db, 1, "civil", "C-INERTE-B-2026",
            last_detail_checked_at=RECIENTE, procedure="Apremio Ejecutivo Obligación de Dar",
        )
        db.commit()

        elegidas = _elegir(
            db, solo_inertes.lawyer_id, ["C-INERTE-A-2026", "C-INERTE-B-2026"], 10
        )
        assert sorted(elegidas) == ["C-INERTE-A-2026", "C-INERTE-B-2026"]

    def test_the_kill_switch_restores_the_legacy_age_ordering(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_SECONDARY_CUADERNO_DEMOTE", False)
        inerte = _seed_case(
            db, 1, "civil", "C-INERTE-2026",
            last_detail_checked_at=MAS_VIEJA, procedure="Medida prejudicial",
        )
        _seed_case(
            db, 1, "civil", "C-PRINCIPAL-2026",
            last_detail_checked_at=RECIENTE, procedure="Principal",
        )
        db.commit()

        elegidas = _elegir(db, inerte.lawyer_id, ["C-INERTE-2026", "C-PRINCIPAL-2026"], 1)
        assert elegidas == ["C-INERTE-2026"]
