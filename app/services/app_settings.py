"""Integer server-side settings stored in the existing ``goals`` key/value table.

Used for knobs that must be changeable without touching the station's environment
(the station reads them from the DB each time it runs).
"""

from typing import Optional

from sqlalchemy.orm import Session

from app.models.goal import Goal

KEY_POR_DIA = "sysgal_incorporacion_por_dia"
KEY_POR_HORA = "sysgal_incorporacion_por_hora"
#: yyyymmdd (Chile) of the day the incorporation was braked; 0/absent = not braked.
KEY_FRENO_DIA = "sysgal_incorporacion_freno_dia"

#: 1 = no e-mail leaves, from ANY process (VM or station); read by app/services/email_gate.py.
KEY_CORREOS_APAGADOS = "correos_apagados"

DEFAULT_POR_DIA = 0  # 0 = OFF
DEFAULT_POR_HORA = 10


def _row(db: Session, key: str) -> Optional[Goal]:
    return db.query(Goal).filter_by(key=key).first()


def get_int(db: Session, key: str, default: int) -> int:
    row = _row(db, key)
    return row.value if row is not None and isinstance(row.value, int) else default


def set_int(db: Session, key: str, value: int) -> None:
    """Upsert without committing; the caller owns the transaction."""
    row = _row(db, key)
    if row is None:
        db.add(Goal(key=key, value=value))
    else:
        row.value = value
    db.flush()


def get_ritmo(db: Session) -> dict:
    return {
        "por_dia": get_int(db, KEY_POR_DIA, DEFAULT_POR_DIA),
        "por_hora": get_int(db, KEY_POR_HORA, DEFAULT_POR_HORA),
    }


def set_ritmo(db: Session, por_dia: Optional[int] = None, por_hora: Optional[int] = None) -> None:
    for value in (por_dia, por_hora):
        if value is not None and value < 0:
            raise ValueError("el ritmo no puede ser negativo")
    if por_dia is not None:
        set_int(db, KEY_POR_DIA, por_dia)
    if por_hora is not None:
        set_int(db, KEY_POR_HORA, por_hora)
