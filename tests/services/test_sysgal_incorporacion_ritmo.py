"""Server-side pace of the Sysgal incorporation: DB-stored settings and quota from the queue rows."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.models.goal import Goal
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_incorporacion import SysgalIncorporacion
from app.services import app_settings
from app.services.sysgal_incorporacion_pjud import consumo

CHILE = ZoneInfo("America/Santiago")


def test_defaults_are_off_and_ten_per_hour(db):
    assert app_settings.get_ritmo(db) == {"por_dia": 0, "por_hora": 10}


def test_set_ritmo_persists_in_the_goals_table_and_partial_updates_keep_the_other(db):
    app_settings.set_ritmo(db, por_dia=100)
    app_settings.set_ritmo(db, por_hora=5)
    db.commit()

    assert app_settings.get_ritmo(db) == {"por_dia": 100, "por_hora": 5}
    keys = {g.key: g.value for g in db.query(Goal).all()}
    assert keys == {"sysgal_incorporacion_por_dia": 100, "sysgal_incorporacion_por_hora": 5}


def test_set_ritmo_rejects_negative_values_and_stores_nothing(db):
    with pytest.raises(ValueError):
        app_settings.set_ritmo(db, por_dia=-1)
    assert db.query(Goal).count() == 0


def _row(db, sid, intento_at):
    causa = SysgalCausa(sysgal_id=sid, rol=f"C-{sid}-2026", rol_norm=f"C-{sid}-2026", tribunal="T",
                        synced_at=datetime.utcnow())
    db.add(causa)
    db.flush()
    db.add(SysgalIncorporacion(sysgal_causa_id=causa.id, ultimo_intento_at=intento_at))
    db.flush()


def test_consumo_counts_attempts_per_chile_day_and_clock_hour_from_the_rows(db):
    # 2026-11-10 14:30 Chile (UTC-3 in November) == 17:30 UTC.
    now = datetime(2026, 11, 10, 14, 30, tzinfo=CHILE)
    _row(db, 1, datetime(2026, 11, 10, 17, 5))   # this hour
    _row(db, 2, datetime(2026, 11, 10, 16, 59))  # earlier today, previous hour
    _row(db, 3, datetime(2026, 11, 10, 3, 0))    # 00:00 Chile exactly -> today
    _row(db, 4, datetime(2026, 11, 10, 2, 59))   # yesterday in Chile
    _row(db, 5, None)                            # never attempted

    assert consumo(db, now) == {"dia": 3, "hora": 1}
