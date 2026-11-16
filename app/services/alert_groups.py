"""Grouping of in-app alerts by (case, Chile calendar day).

Everything is computed in SQL (GROUP BY). `Alert.created_at` is a naive UTC
datetime; the group day is the America/Santiago calendar day of that instant.

Postgres converts with `AT TIME ZONE`. SQLite (tests) has no tz database, so
the Santiago UTC offset is expressed as a CASE over the offset segments taken
from `zoneinfo`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Query, Session

from app.models.alert import Alert

CHILE_TZ = ZoneInfo("America/Santiago")
MAX_ALERT_IDS = 200
_SEGMENT_FIRST_YEAR = 2015

_KEY_RE = re.compile(r"^(case|type):(.+):(\d{4}-\d{2}-\d{2})$")


class InvalidGroupKey(ValueError):
    pass


def _offset_segments(first_year: int, last_year: int) -> List[Tuple[datetime, int]]:
    """(utc_start, offset_seconds) segments where the Santiago offset is constant."""
    t = datetime(first_year, 1, 1, tzinfo=timezone.utc)
    end = datetime(last_year + 1, 1, 1, tzinfo=timezone.utc)
    current = t.astimezone(CHILE_TZ).utcoffset()
    segments = [(t.replace(tzinfo=None), int(current.total_seconds()))]
    while t < end:
        t += timedelta(hours=1)
        off = t.astimezone(CHILE_TZ).utcoffset()
        if off != current:
            current = off
            segments.append((t.replace(tzinfo=None), int(off.total_seconds())))
    return segments


_SEGMENT_CACHE: Dict[int, List[Tuple[datetime, int]]] = {}


def _segments_for(last_year: int) -> List[Tuple[datetime, int]]:
    if last_year not in _SEGMENT_CACHE:
        _SEGMENT_CACHE[last_year] = _offset_segments(_SEGMENT_FIRST_YEAR, last_year)
    return _SEGMENT_CACHE[last_year]


def chile_day_expr(db: Session):
    """SQL expression: Santiago calendar day (date) of `Alert.created_at`."""
    if db.get_bind().dialect.name == "postgresql":
        return func.date(
            func.timezone("America/Santiago", func.timezone("UTC", Alert.created_at))
        )
    segs = _segments_for(datetime.utcnow().year + 1)
    whens = [
        (Alert.created_at >= start, f"{off:+d} seconds") for start, off in reversed(segs)
    ]
    return func.date(Alert.created_at, case(*whens, else_="+0 seconds"))


def _type_key():
    # Alerts without a case form their own group per type.
    return case((Alert.case_id.is_(None), Alert.type), else_=None)


def day_str(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)[:10]


def make_group_key(case_id: Optional[int], type_: Optional[str], day: str) -> str:
    return f"case:{case_id}:{day}" if case_id is not None else f"type:{type_}:{day}"


def parse_group_key(key: str) -> Tuple[Optional[int], Optional[str], str]:
    """Return (case_id, type, day); raises InvalidGroupKey if malformed."""
    m = _KEY_RE.match(key or "")
    if not m:
        raise InvalidGroupKey(key)
    kind, ident, day = m.groups()
    try:
        date.fromisoformat(day)
        if kind == "case":
            return int(ident), None, day
    except ValueError:
        raise InvalidGroupKey(key)
    return None, ident, day


def group_filter(db: Session, case_id: Optional[int], type_: Optional[str], day: str):
    clauses = [chile_day_expr(db) == day]
    if case_id is not None:
        clauses.append(Alert.case_id == case_id)
    else:
        clauses += [Alert.case_id.is_(None), Alert.type == type_]
    return and_(*clauses)


def _grouped(db: Session, base: Query, unread_only: bool):
    day = chile_day_expr(db).label("day")
    tkey = _type_key().label("tkey")
    unread = func.sum(case((Alert.read.is_(False), 1), else_=0))
    q = base.with_entities(
        Alert.case_id.label("case_id"),
        day,
        tkey,
        func.count(Alert.id).label("count"),
        unread.label("unread"),
        func.max(Alert.created_at).label("latest"),
    ).group_by(Alert.case_id, day, tkey)
    if unread_only:
        q = q.having(unread > 0)
    return q


def list_groups(db: Session, base: Query, *, page: int, per_page: int, unread_only: bool):
    """Return (rows, total, unread_groups); rows are plain dicts for the page.

    `base` is already scoped to the caller's alerts and category.
    """
    grouped = _grouped(db, base, unread_only)
    total = grouped.order_by(None).count()
    unread_groups = _grouped(db, base, True).order_by(None).count()

    page_rows = (
        grouped.order_by(
            func.max(Alert.created_at).desc(),
            Alert.case_id.desc(),
            chile_day_expr(db).desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    if not page_rows:
        return [], total, unread_groups

    day = chile_day_expr(db)
    in_page = or_(
        *[group_filter(db, r.case_id, r.tkey, day_str(r.day)) for r in page_rows]
    )
    ranked = (
        base.filter(in_page)
        .with_entities(
            Alert.id.label("id"),
            Alert.case_id.label("case_id"),
            day.label("day"),
            Alert.type.label("type"),
            Alert.title.label("title"),
            Alert.message.label("message"),
            func.row_number()
            .over(
                partition_by=(Alert.case_id, day, _type_key()),
                order_by=(Alert.created_at.desc(), Alert.id.desc()),
            )
            .label("rn"),
        )
        .subquery()
    )
    top = (
        db.query(ranked)
        .filter(ranked.c.rn <= MAX_ALERT_IDS)
        .order_by(ranked.c.rn)
        .all()
    )
    types_rows = (
        base.filter(in_page).with_entities(Alert.case_id, day, Alert.type).distinct().all()
    )

    ids: Dict[tuple, List[int]] = {}
    latest: Dict[tuple, object] = {}
    for r in top:
        k = (r.case_id, day_str(r.day), r.type if r.case_id is None else None)
        ids.setdefault(k, []).append(r.id)
        if r.rn == 1:
            latest[k] = r
    types: Dict[tuple, set] = {}
    for case_id, d, t in types_rows:
        k = (case_id, day_str(d), t if case_id is None else None)
        types.setdefault(k, set()).add(t)

    rows = []
    for r in page_rows:
        k = (r.case_id, day_str(r.day), r.tkey)
        lt = latest.get(k)
        created = r.latest
        if not isinstance(created, datetime):
            created = datetime.fromisoformat(str(created))
        rows.append(
            {
                "case_id": r.case_id,
                "day": k[1],
                "type": r.tkey,
                "count": int(r.count),
                "unread_count": int(r.unread or 0),
                "latest_created_at": created,
                "latest_title": lt.title if lt else None,
                "latest_message": lt.message if lt else None,
                "types": sorted(types.get(k, ())),
                "alert_ids": ids.get(k, []),
            }
        )
    return rows, total, unread_groups
