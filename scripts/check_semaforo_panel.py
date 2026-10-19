"""Count what the home-screen semaforo panel would return, per group and type.

Read-only: it calls the same service function the endpoint uses
(``build_panel``) against the live database and prints the counts.

Usage:
    PYTHONPATH=. python scripts/check_semaforo_panel.py
"""

from collections import Counter

from app.core.database import SessionLocal
from app.services.deadline_engine import _today_chile
from app.services.semaforo_panel import build_panel


def main() -> None:
    db = SessionLocal()
    try:
        today = _today_chile()
        panel = build_panel(db, today, None)  # None = the whole firm
        print(f"today: {today}")
        for name, rows in (("por_vencer", panel.por_vencer), ("vencidos", panel.vencidos)):
            print(f"{name}: {len(rows)}")
            for dtype, n in sorted(Counter(r.deadline_type for r in rows).items()):
                print(f"  {dtype}: {n}")
            if name == "por_vencer":
                for level, n in sorted(Counter(r.level for r in rows).items()):
                    print(f"  level {level}: {n}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
