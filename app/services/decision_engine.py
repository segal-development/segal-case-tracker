"""DecisionEngine — DEFENSE-side action recommendation (req #5/#11).

Pure rules engine: evaluates ``DECISION_RULES`` (``app/core/decision_rules.py``)
in priority order against a case's ALREADY-COMPUTED state (written by
``DeadlineEngine.recompute_case`` — see ``app/services/deadline_engine.py``).
Never recomputes procedural state itself, never queries the DB, never raises.

Mirrors ``DeadlineEngine``'s safe-fail contract: any unexpected error is
logged and swallowed so a bug here can NEVER break the caller
(``DeadlineEngine._recompute_safe``, which has already flushed its own
computation by the time it calls this engine).

Note on ``_today_chile``: intentionally duplicated from
``deadline_engine._today_chile`` rather than imported — ``deadline_engine``
imports ``DecisionEngine`` to wire it into ``recompute_case``, so this
module must not import back from ``deadline_engine`` (would be circular).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.decision_rules import DECISION_RULES, DecisionContext, Urgency

logger = logging.getLogger(__name__)

_CHILE_TZ = ZoneInfo("America/Santiago")

# Horizon used by compute_next_review_at when a standing opportunity
# (abandono / prescripción / apremio) is pending but there's no dated
# deadline to anchor the review to. Short on purpose — these are
# time-sensitive advisory signals, not fire-and-forget.
_STANDING_OPPORTUNITY_REVIEW_DAYS = 7


def _today_chile() -> date:
    """Return today's date in the America/Santiago timezone."""
    return datetime.now(_CHILE_TZ).date()


@dataclass(frozen=True)
class Recommendation:
    """The DecisionEngine's top-priority action for a case."""

    code: str
    action_text: str
    legal_basis: str
    urgency: Urgency
    related_deadline_at: Optional[date] = None


class DecisionEngine:
    """Stateless — reasons only over the attributes it reads off *case*."""

    @classmethod
    def recommend(cls, case, *, today: Optional[date] = None) -> Optional[Recommendation]:
        """Return the top-priority DEFENSE action for *case*, or None.

        NEVER raises — any error (malformed/partial case, unexpected
        attribute types) is logged and swallowed, returning None. Mirrors
        ``DeadlineEngine.recompute_case``'s safe-fail contract.

        ``today`` is optional so callers already holding "today" (e.g. the
        deadline-engine pipeline) can pass it through for consistency
        instead of triggering a second clock read.
        """
        try:
            ctx = cls._build_context(case, today)
            for rule in DECISION_RULES:
                if rule.predicate(ctx):
                    return Recommendation(
                        code=rule.code,
                        action_text=rule.action_text,
                        legal_basis=rule.legal_basis,
                        urgency=rule.urgency,
                        related_deadline_at=ctx.next_deadline_at,
                    )
            return None
        except Exception:
            logger.exception(
                "DecisionEngine.recommend failed for case_id=%s — returning None",
                getattr(case, "id", "?"),
            )
            return None

    @classmethod
    def compute_next_review_at(cls, case, *, today: Optional[date] = None) -> Optional[date]:
        """Return the next date this case should be manually reviewed, or None.

        Derivation (kept simple on purpose):
          1. A dated actionable deadline (``next_deadline_at``) IS the next
             thing that matters — review then.
          2. Else, if a standing opportunity is pending (abandono /
             prescripción / apremio) with no dated deadline to anchor to,
             review in ``_STANDING_OPPORTUNITY_REVIEW_DAYS`` — these don't
             expire on their own, but shouldn't be silently forgotten.
          3. Else (nothing pending) → None.

        NEVER raises — mirrors ``DeadlineEngine``'s safe-fail contract.
        """
        try:
            ctx = cls._build_context(case, today)
            if ctx.next_deadline_at is not None:
                return ctx.next_deadline_at
            if ctx.abandono_disponible or ctx.prescripcion_cumplida or ctx.en_apremio:
                return ctx.today + timedelta(days=_STANDING_OPPORTUNITY_REVIEW_DAYS)
            return None
        except Exception:
            logger.exception(
                "DecisionEngine.compute_next_review_at failed for case_id=%s — returning None",
                getattr(case, "id", "?"),
            )
            return None

    @staticmethod
    def _build_context(case, today: Optional[date]) -> DecisionContext:
        return DecisionContext(
            procedural_state=getattr(case, "procedural_state", None),
            semaforo=getattr(case, "semaforo", None),
            abandono_disponible=bool(getattr(case, "abandono_disponible", False)),
            prescripcion_cumplida=bool(getattr(case, "prescripcion_cumplida", False)),
            en_apremio=bool(getattr(case, "en_apremio", False)),
            next_deadline_at=getattr(case, "next_deadline_at", None),
            next_deadline_fatal=bool(getattr(case, "next_deadline_fatal", False)),
            today=today if today is not None else _today_chile(),
        )
