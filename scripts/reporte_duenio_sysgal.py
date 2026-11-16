"""Read-only report: how many cases each lawyer gains/loses if ``sysgal_es_fuente_duenio`` is turned on.

Never touches the switch nor any data. Output (Spanish, fixed-width, sorted by gain desc then name):

    Cambio de dueño si se activa sysgal_es_fuente_duenio (el switch NO se toca)
    abogado        gana  pierde  neto
    ...
    TOTAL          ...

Run:
    PYTHONPATH=$(pwd) poetry run python scripts/reporte_duenio_sysgal.py

``run`` and ``format_report`` are the testable core (session injected); ``main`` owns the real session.
"""
import os

os.environ.setdefault("ENVIRONMENT", "production")

HEADER = "Cambio de dueño si se activa sysgal_es_fuente_duenio (el switch NO se toca)"


def run(db) -> dict:
    from app.services.sysgal_owner import reporte_cambio_duenio

    try:
        return reporte_cambio_duenio(db)
    finally:
        db.rollback()  # read-only: leave nothing pending


def format_report(report: dict) -> str:
    if not report:
        return f"{HEADER}\nSin cambios: ningún caso cambiaría de dueño."
    rows = sorted(report.items(), key=lambda kv: (-kv[1]["gana"], kv[0]))
    width = max(len("abogado"), len("TOTAL"), *(len(name) for name, _ in rows))
    total_g = sum(v["gana"] for _, v in rows)
    total_p = sum(v["pierde"] for _, v in rows)

    def line(name, g, p):
        return f"{name:<{width}}  {g:>6}  {p:>6}  {g - p:>+6}"

    lines = [HEADER, f"{'abogado':<{width}}  {'gana':>6}  {'pierde':>6}  {'neto':>6}"]
    lines += [line(n, v["gana"], v["pierde"]) for n, v in rows]
    lines.append(line("TOTAL", total_g, total_p))
    return "\n".join(lines)


def main() -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        print(format_report(run(db)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
