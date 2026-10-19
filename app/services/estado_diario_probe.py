"""Observe-only probe of the OJV "estado diario" feed.

Measured 2026-10-13/14: the rotation learns about a movement a median of 17
days late (p99 99), while OJV emails every lawyer, daily, the list of causas
notified by estado diario. This module asks one question and changes nothing:
is the endpoint behind that email usable from the session the cycle already
holds, and how stale would the rotation have been for those causas?

It makes ONE in-page AJAX POST per lawyer per cycle (the same ``$.ajax``
pattern the listing uses, which Shape already tolerates) and writes ONE
greppable log line. It never visits, creates or reorders cases, never alerts,
never opens a page/context/session and never raises.

Four outcomes, deliberately distinct in the log:

* ``ok``               rows came back
* ``no_activity``      recognised table, empty ``<tbody>`` (expected on weekends)
* ``endpoint_changed`` dead endpoint or a response shape we do not recognise
* ``bounced``          login page, 401/403, or a Shape/TSPD challenge
  (if this shows up: switch ESTADO_DIARIO_PROBE_ENABLED off and report; never insist)

plus ``skipped`` (nothing to reuse) and ``probe_error`` (our own failure).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

from app.config import settings
from app.core.database import SessionLocal
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.scrapper.pjud.base import (
    _DETAIL_SESSION_URL_MARKERS,
    classify_small_detail_modal,
    detect_shape_challenge,
)
from app.scrapper.pjud.resilience.rate_limiter import pjud_action_limiter
from app.services.business_days import business_days_after, previous_business_day
from app.services.shape_cooldown import get_shape_cooldown

logger = logging.getLogger("estado_diario_probe")

OK = "ok"
NO_ACTIVITY = "no_activity"
ENDPOINT_CHANGED = "endpoint_changed"
BOUNCED = "bounced"
SKIPPED = "skipped"
PROBE_ERROR = "probe_error"

from app.scrapper.pjud.base import PJUD_BASE_URL

ENDPOINT = "/miEstadoDiario/civil/estadoDiarioFechaCivil.php"
# The AJAX has to be issued from its own page, the way the listing's is: that
# one works because it runs on the loaded Mis Causas panel. The 2026-07
# capture of this endpoint recorded this loader as part of the flow. POSTing
# cold from whatever page the detail phase left behind risks a rejection we
# would read as "the endpoint is dead" -- and this feed was already written
# off once by mistake, for three months. Navigating leaves the page off the
# Mis Causas panel; the cycle runs the probe before the detail phase, which
# re-loads the panel by itself (``_ensure_panel_loaded``) before its first fetch.
LOADER = "/miEstadoDiario.php"
_BOUNCE_STATUSES = (401, 403)

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(r"<t([dh])[^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class FeedRow:
    """One row of the feed.

    ``tribunal`` and ``caratulado`` are None when the feed row did not carry
    them. ``caratulado`` is what tells us WHOSE a case is: a ROL missing from
    our database says nothing on its own, but its caratulado says whether it is
    one of the firm's clients or an unrelated third party.
    """

    rol: str
    tribunal: Optional[str] = None
    caratulado: Optional[str] = None


@dataclass
class ProbeResult:
    outcome: str
    status: Optional[int] = None
    rows: List[FeedRow] = field(default_factory=list)
    detail: str = ""

    @property
    def rols(self) -> List[str]:
        return [r.rol for r in self.rows]


@dataclass
class Staleness:
    """Coverage of the feed for ONE lawyer.

    ``in_db``/``not_in_db`` partition the distinct ROLs of the feed by whether
    this lawyer has them. ``other_lawyer`` is the subset of ``not_in_db`` whose
    ROL exists in the firm under someone else. The court counters only look at
    ROLs found for this lawyer; ``court_mismatch`` is the identity-bug signal
    and is counted only when the comparison is certain.
    """

    in_db: int
    not_in_db: int
    never_checked: int
    median_days: Optional[float]
    max_days: Optional[float]
    other_lawyer: int = 0
    court_mismatch: int = 0
    court_unknown: int = 0
    # Full, sorted (rol, feed tribunal) lists behind the counters, keyed by
    # category (not_in_db = nowhere in the firm, other_lawyer, court_mismatch).
    samples: Dict[str, List[FeedRow]] = field(default_factory=dict)


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", cell)).strip()


def classify_response(response) -> ProbeResult:
    """Turn the raw in-page response into one of the outcomes. Pure."""
    if not isinstance(response, dict):
        return ProbeResult(ENDPOINT_CHANGED, detail="no response object")
    status = response.get("status")
    body = response.get("body")
    body = body if isinstance(body, str) else ""
    status_n = status if isinstance(status, int) else None

    is_shape, marker = detect_shape_challenge(body)
    if is_shape:
        return ProbeResult(BOUNCED, status_n, detail=f"shape challenge ({marker})")
    if status_n in _BOUNCE_STATUSES:
        return ProbeResult(BOUNCED, status_n, detail=f"http {status_n}")
    if status_n is not None and status_n != 200:
        return ProbeResult(ENDPOINT_CHANGED, status_n, detail=f"http {status_n}")

    rows = _ROW_RE.findall(body)
    header_cells: List[str] = []
    data_rows: List[List[str]] = []
    for row in rows:
        cells = _CELL_RE.findall(row)
        if not cells:
            continue
        if all(kind.lower() == "h" for kind, _ in cells):
            header_cells = [_clean(c).lower() for _, c in cells]
        elif any(kind.lower() == "d" for kind, _ in cells):
            data_rows.append([_clean(c) for _, c in cells])

    if "rol" not in header_cells or "caratulado" not in header_cells:
        # Not the table we captured. A login page is a bounce; anything else
        # (empty body, other HTML) means the endpoint changed or is dead.
        low = body.lower()
        if body.strip() and (
            any(m in low for m in _DETAIL_SESSION_URL_MARKERS)
            or classify_small_detail_modal(body) == "session"
        ):
            return ProbeResult(BOUNCED, status_n, detail="login page")
        return ProbeResult(ENDPOINT_CHANGED, status_n, detail="unrecognised response")

    rol_idx = header_cells.index("rol")
    tribunal_idx = header_cells.index("tribunal") if "tribunal" in header_cells else None
    # The header check above guarantees the column exists; a short data row
    # still must not raise -- the probe never breaks the cycle.
    caratulado_idx = header_cells.index("caratulado")
    # The caratulado came back empty on the first real run. Log the shape the
    # feed actually sends -- headers and the first row's cell count -- because
    # guessing why is what wastes a cycle. One short line per probe.
    logger.info(
        "ESTADO_DIARIO_PROBE_SHAPE headers=%r rol_idx=%d caratulado_idx=%d "
        "tribunal_idx=%s first_row_cells=%d first_row=%r",
        header_cells, rol_idx, caratulado_idx, tribunal_idx,
        len(data_rows[0]) if data_rows else 0,
        data_rows[0][:6] if data_rows else [],
    )
    feed_rows: List[FeedRow] = []
    for r in data_rows:
        if len(r) <= rol_idx or not r[rol_idx]:
            continue
        tribunal = None
        if tribunal_idx is not None and len(r) > tribunal_idx:
            tribunal = r[tribunal_idx] or None
        caratulado = r[caratulado_idx] or None if len(r) > caratulado_idx else None
        feed_rows.append(
            FeedRow(
                rol=r[rol_idx].upper(), tribunal=tribunal, caratulado=caratulado
            )
        )
    if not feed_rows:
        return ProbeResult(NO_ACTIVITY, status_n)
    return ProbeResult(OK, status_n, rows=feed_rows)


def normalize_court_name(name: Optional[str]) -> str:
    """Comparable form of a court name: upper case, no accents, no ordinal
    signs, single spaces. "21º Juzgado Civil" and "21 JUZGADO CIVIL" collapse
    to the same string. Exact equality of this form is the ONLY way two names
    are considered the same court: no fuzzy or substring matching."""
    if not name:
        return ""
    s = name.replace("\u00ba", "").replace("\u00b0", "")  # º and °
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().upper()


def staleness_stats(
    session_factory: Callable,
    rows: List[FeedRow],
    now: datetime,
    lawyer_id: int,
) -> Staleness:
    """Coverage and staleness of the feed's ROLs for ONE lawyer.

    Matches ``Case.rol`` AND ``Case.lawyer_id``: matching by ROL alone counted
    other lawyers' causas as ours (the same ROL repeats across lawyers and
    across courts) and was blind to the "same ROL, other court" bug.

    The court comparison fails toward "unknown", never toward "mismatch": a
    mismatch is counted only when the feed's tribunal resolves, by exact
    normalised equality, to a known ``courts.name`` and none of this lawyer's
    cases with that ROL sits in such a court.
    """
    unique = sorted({r.rol for r in rows})
    tribunals_by_rol: dict = {}
    for r in rows:
        tribunals_by_rol.setdefault(r.rol, set()).add(normalize_court_name(r.tribunal))

    db = session_factory()
    try:
        found = (
            db.query(Case.rol, Case.last_detail_checked_at, Case.court_id)
            .filter(Case.rol.in_(unique), Case.lawyer_id == lawyer_id)
            .all()
        )
        found_rols = {rol for rol, _, _ in found}
        missing = [r for r in unique if r not in found_rols]
        elsewhere = set()
        if missing:
            elsewhere = {
                rol
                for (rol,) in db.query(Case.rol)
                .filter(Case.rol.in_(missing), Case.lawyer_id != lawyer_id)
                .distinct()
                .all()
            }
        court_ids_by_name: dict = {}
        if found and any(t for ts in tribunals_by_rol.values() for t in ts):
            for cid, cname in db.query(Court.id, Court.name).all():
                key = normalize_court_name(cname)
                if key:
                    court_ids_by_name.setdefault(key, set()).add(cid)
    finally:
        db.close()

    checked: dict = {}
    court_ids: dict = {}
    for rol, ts, court_id in found:
        # keep the stalest timestamp if a ROL appears twice
        if rol not in checked or (ts is None) or (
            checked[rol] is not None and ts < checked[rol]
        ):
            checked[rol] = ts
        court_ids.setdefault(rol, set()).add(court_id)

    mismatch_rols: List[str] = []
    mismatch = unknown = 0
    for rol in checked:
        verdicts = set()
        for tribunal in tribunals_by_rol.get(rol, {""}):
            resolved = court_ids_by_name.get(tribunal) if tribunal else None
            if not resolved:
                verdicts.add("unknown")
            elif resolved & court_ids[rol]:
                verdicts.add("match")
            else:
                verdicts.add("mismatch")
        # One feed row matching our court settles the ROL. A mismatch needs
        # EVERY row for the ROL to be a certain mismatch; any doubt is unknown.
        if "match" in verdicts:
            continue
        if verdicts == {"mismatch"}:
            mismatch += 1
            mismatch_rols.append(rol)
        else:
            unknown += 1

    feed_tribunal: dict = {}
    for r in rows:
        if r.tribunal and (r.rol not in feed_tribunal or r.tribunal < feed_tribunal[r.rol]):
            feed_tribunal[r.rol] = r.tribunal

    def _sample(rols) -> List[FeedRow]:
        return [FeedRow(rol=r, tribunal=feed_tribunal.get(r)) for r in sorted(rols)]

    nowhere = [r for r in missing if r not in elsewhere]
    samples = {
        "not_in_db": _sample(nowhere),
        "other_lawyer": _sample(elsewhere),
        "court_mismatch": _sample(mismatch_rols),
    }

    ages = [
        (now - ts).total_seconds() / 86400.0
        for ts in checked.values()
        if ts is not None
    ]
    return Staleness(
        in_db=len(checked),
        not_in_db=len(unique) - len(checked),
        never_checked=sum(1 for ts in checked.values() if ts is None),
        median_days=round(statistics.median(ages), 1) if ages else None,
        max_days=round(max(ages), 1) if ages else None,
        other_lawyer=len(elsewhere),
        court_mismatch=mismatch,
        court_unknown=unknown,
        samples={k: v for k, v in samples.items() if v},
    )


async def _fetch_via_fetch(page, fecha: str) -> dict:
    """Same request as the `$.ajax` one, for pages that do not load jQuery.

    Mirrors what `$.ajax` sends by default: form-encoded body and the
    X-Requested-With header the server uses to tell an AJAX call apart from a
    navigation. Same-origin with the session cookies, like the rest of the
    in-page calls already do.
    """
    js = f"""
        () => fetch({json.dumps(ENDPOINT)}, {{
            method: 'POST',
            credentials: 'include',
            headers: {{
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest'
            }},
            body: 'fechaEstDiaCiv=' + encodeURIComponent({json.dumps(fecha)})
        }})
        .then(r => r.text().then(t => ({{status: r.status, body: t}})))
        .catch(e => ({{status: 0, body: String(e)}}))
    """
    return await page.evaluate(js)


async def fetch_estado_diario(page, fecha: str, *, navigate: bool = True) -> dict:
    """Load the estado-diario page, then ONE in-page ``$.ajax`` POST from it
    (the same shape the listing uses).

    ``navigate=False`` skips the page load for the 2nd..Nth day of a range: the
    page is already the loader, and re-navigating would double the requests.
    """
    if navigate:
        await page.goto(f"{PJUD_BASE_URL}{LOADER}", wait_until="domcontentloaded")

    # Measured on the first real run (2026-10-14): `/miEstadoDiario.php` does
    # NOT give us jQuery the way the Mis Causas panel does, and `$.ajax` blew
    # up with "ReferenceError: $ is not defined". Prefer $.ajax when it IS
    # there (it is the request shape Shape already tolerates on the listing)
    # and fall back to fetch with the same headers and body encoding.
    has_jquery = bool(await page.evaluate("() => typeof window.jQuery !== 'undefined'"))
    if not has_jquery:
        return await _fetch_via_fetch(page, fecha)

    js = f"""
        () => new Promise((resolve) => {{
            $.ajax({{
                url: '{ENDPOINT}',
                type: 'POST',
                data: {{ fechaEstDiaCiv: {json.dumps(fecha)} }},
                success: function(data, textStatus, xhr) {{
                    resolve({{status: xhr.status, body: String(data)}});
                }},
                error: function(xhr) {{
                    resolve({{status: xhr.status, body: xhr.responseText || ''}});
                }}
            }});
        }})
    """
    return await page.evaluate(js)


SAMPLE_LIMIT = 20
_SAMPLE_ORDER = ("not_in_db", "other_lawyer", "court_mismatch")


def _log_samples(lawyer_id, fecha: str, stats: Staleness) -> None:
    """One INFO line per non-empty category, at most SAMPLE_LIMIT ROLs each.

    Separate prefix token (ESTADO_DIARIO_PROBE_SAMPLE) so the parser of the
    counters line (``ESTADO_DIARIO_PROBE lawyer=...``) never sees these.
    Entries are ``ROL@tribunal`` (``-`` when the feed had none), ``;``-joined
    inside double quotes.
    """
    for category in _SAMPLE_ORDER:
        rows = stats.samples.get(category)
        if not rows:
            continue
        shown = rows[:SAMPLE_LIMIT]
        entries = "; ".join(
            f"{r.rol}@{(r.tribunal or '-').replace(chr(34), chr(39))}"
            f" | {(r.caratulado or '-').replace(chr(34), chr(39))}"
            for r in shown
        )
        logger.info(
            'ESTADO_DIARIO_PROBE_SAMPLE lawyer=%s date=%s category=%s total=%d '
            'shown=%d omitted=%d rols="%s"',
            lawyer_id, fecha, category, len(rows), len(shown),
            len(rows) - len(shown), entries,
        )


def _fmt(value) -> str:
    return "n/a" if value is None else str(value)


def days_to_read(
    last_read: Optional[date], latest: date, max_days: int
) -> List[date]:
    """Business days (Mon-Fri, no feriados) one probe should request, oldest first.

    * never read          -> just ``latest`` (no month of history on first run)
    * read up to ``latest`` -> ``latest`` again: the delta-sync re-reads the
      newest day every cycle until its causas are consumed
    * otherwise           -> every business day after ``last_read``, capped at
      ``max_days`` keeping the OLDEST ones, so the watermark moves contiguously
      and a long absence is caught up over several cycles with no hole.
    """
    if last_read is None:
        return [latest]
    days = business_days_after(last_read, latest, include_saturday=False)
    if not days:
        return [latest]
    return days[: max(1, max_days)]


def _load_watermark(session_factory: Callable, lawyer_id) -> Optional[date]:
    db = session_factory()
    try:
        return (
            db.query(Lawyer.estado_diario_last_read_on)
            .filter(Lawyer.id == lawyer_id)
            .scalar()
        )
    finally:
        db.close()


def _save_watermark(session_factory: Callable, lawyer_id, day: date) -> None:
    db = session_factory()
    try:
        db.query(Lawyer).filter(Lawyer.id == lawyer_id).update(
            {Lawyer.estado_diario_last_read_on: day}, synchronize_session=False
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_estado_diario_probe(
    *,
    scraper,
    lawyer_id: int,
    competencia: str = "civil",
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    session_factory: Callable = SessionLocal,
) -> List[FeedRow]:
    """Probe the business days since this lawyer's last successful read.

    Logs one line (plus samples) per day and hands back the rows of every day
    read. Never raises. Returns ``[]`` whenever there is nothing usable: flag
    off, non-civil, cooldown, no live page, no activity, bounced, endpoint
    changed or our own failure.

    A day counts as read only when its response classified as ``ok`` or
    ``no_activity``; the watermark advances day by day and the range stops at
    the first day that does not, so that day is asked again next cycle. The
    range also stops, without another request, as soon as the Shape cooldown
    is active.
    """
    if not settings.ESTADO_DIARIO_PROBE_ENABLED:
        return []
    if (competencia or "").lower() != "civil":
        return []
    rows: List[FeedRow] = []
    fecha = "?"
    # Same guard the detail phase uses (detect_and_sync_movements): while Shape
    # is challenging the station nothing may navigate, and the probe runs BEFORE
    # that phase, so it cannot rely on the detail phase's own abstention.
    cooldown = get_shape_cooldown()

    def _log_cooldown() -> None:
        logger.info(
            "ESTADO_DIARIO_PROBE lawyer=%s outcome=%s reason=shape_cooldown "
            "remaining_s=%.0f",
            lawyer_id, SKIPPED, cooldown.remaining(),
        )

    if cooldown.active():
        _log_cooldown()
        return []
    try:
        latest = previous_business_day(today or date.today(), include_saturday=False)
        try:
            last_read = _load_watermark(session_factory, lawyer_id)
        except Exception:  # noqa: BLE001 — no watermark: behave like a first run
            logger.warning(
                "ESTADO_DIARIO_PROBE lawyer=%s watermark unreadable; asking one day",
                lawyer_id, exc_info=True,
            )
            last_read = None
        targets = days_to_read(
            last_read, latest, settings.ESTADO_DIARIO_MAX_DAYS_PER_CYCLE
        )

        page = scraper.live_page()
        if page is None:
            fecha = targets[0].strftime("%d/%m/%Y")
            logger.info(
                "ESTADO_DIARIO_PROBE lawyer=%s date=%s outcome=%s reason=no_live_page",
                lawyer_id, fecha, SKIPPED,
            )
            return []

        for index, target in enumerate(targets):
            if index and cooldown.active():
                # Shape started challenging mid-range: stop asking. The days left
                # are not marked read, so they are requested again later.
                _log_cooldown()
                break
            fecha = target.strftime("%d/%m/%Y")
            await pjud_action_limiter("estado_diario").acquire()
            await asyncio.sleep(settings.DETAIL_FETCH_DELAY)
            result = classify_response(
                await fetch_estado_diario(page, fecha, navigate=index == 0)
            )

            if result.outcome == OK:
                rows.extend(result.rows)  # first: nothing below may lose them
            if result.outcome in (OK, NO_ACTIVITY):
                # Read: remember it before the statistics, which are only logging.
                try:
                    _save_watermark(session_factory, lawyer_id, target)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ESTADO_DIARIO_PROBE lawyer=%s date=%s outcome=%s "
                        "error=watermark_not_saved %s: %s",
                        lawyer_id, fecha, PROBE_ERROR, type(exc).__name__, exc,
                    )
                    break  # cannot advance contiguously: ask the rest again

            if result.outcome == OK:
                stats = staleness_stats(
                    session_factory, result.rows, now or datetime.utcnow(), lawyer_id
                )
                # Sample lines go first so the counters line stays the last one of
                # the probe for this lawyer-day, as before.
                _log_samples(lawyer_id, fecha, stats)
                # Existing keys keep their names and order (a script parses this
                # line); new keys are only appended.
                logger.info(
                    "ESTADO_DIARIO_PROBE lawyer=%s date=%s outcome=%s rows=%d in_db=%d "
                    "not_in_db=%d never_checked=%d age_days_median=%s age_days_max=%s "
                    "other_lawyer=%d court_mismatch=%d court_unknown=%d",
                    lawyer_id, fecha, OK, len(result.rows), stats.in_db,
                    stats.not_in_db, stats.never_checked, _fmt(stats.median_days),
                    _fmt(stats.max_days), stats.other_lawyer, stats.court_mismatch,
                    stats.court_unknown,
                )
            elif result.outcome == NO_ACTIVITY:
                logger.info(
                    "ESTADO_DIARIO_PROBE lawyer=%s date=%s outcome=%s rows=0",
                    lawyer_id, fecha, NO_ACTIVITY,
                )
            else:
                logger.warning(
                    "ESTADO_DIARIO_PROBE lawyer=%s date=%s outcome=%s status=%s detail=%s",
                    lawyer_id, fecha, result.outcome, _fmt(result.status),
                    result.detail,
                )
                break  # not read: this day and the ones after it wait for next cycle
    except Exception as exc:  # noqa: BLE001 — a probe failure never breaks the cycle
        logger.warning(
            "ESTADO_DIARIO_PROBE lawyer=%s date=%s outcome=%s error=%s: %s",
            lawyer_id, fecha, PROBE_ERROR, type(exc).__name__, exc,
        )
    return rows
