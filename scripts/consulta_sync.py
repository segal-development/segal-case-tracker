"""Consulta-by-rol runner — keep a lawyer's PUBLIC cases fresh automatically.

Runs in the FIRM's Clave Única session (Carla's creds — NO captcha, NO per-lawyer
login). For the target lawyer it loads their civil cases that are (a) not flagged
reserved and (b) have a known PJUD corte, then runs sync_via_consulta: public cases
get re-scraped via the Consulta Unificada (movimientos + documentos persisted);
cases not in the consulta get flagged consulta_reserved=True so they can be routed
to the per-abogado 2ª-clave rotation (rotate_per_abogado / Mis Causas).

Pause Carla before running — one PJUD session per IP at a time.

Env:
    LAWYER     rut or id of the case-owner lawyer whose cases to sync (required)
    DRY_RUN    "1" (default) = no writes; "0" = persist
    LIMIT      max cases this run (default 50) — least-recently-checked first

Run (dry-run):
    LAWYER=7 PYTHONPATH=. <venv> python scripts/consulta_sync.py
Real sync:
    LAWYER=7 DRY_RUN=0 LIMIT=10 ... python scripts/consulta_sync.py
"""
import asyncio
import os

from app.core.database import SessionLocal
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
from app.services.sync_service import sync_via_consulta

CU_RUT = os.environ.get("CU_RUT", "")
CU_PASSWORD = os.environ.get("CU_PASSWORD", "")


def _resolve_lawyer(db, key: str):
    q = db.query(Lawyer)
    if key.isdigit():
        return q.filter(Lawyer.id == int(key)).first()
    return q.filter(Lawyer.rut == key).first()


async def main() -> int:
    if not (CU_RUT and CU_PASSWORD):
        print("CU_RUT/CU_PASSWORD not set (source .env.backfill). Stopping.")
        return 2
    key = os.environ.get("LAWYER", "").strip()
    if not key:
        print("Set LAWYER=<rut|id>.")
        return 2
    dry = os.environ.get("DRY_RUN", "1").strip() != "0"
    limit = int(os.environ.get("LIMIT", "50"))

    db = SessionLocal()
    lawyer = _resolve_lawyer(db, key)
    if not lawyer:
        print(f"No lawyer for '{key}'.")
        return 2

    cases = (
        db.query(Case)
        .join(Court, Case.court_id == Court.id)
        .filter(
            Case.lawyer_id == lawyer.id,
            Case.competencia == "civil",
            Case.consulta_reserved.is_(False),
            Court.pjud_corte.isnot(None),
        )
        .order_by(Case.last_detail_checked_at.asc().nullsfirst())
        .limit(limit)
        .all()
    )
    print(
        f"consulta_sync · lawyer={lawyer.name or lawyer.rut} (id={lawyer.id}) "
        f"· {len(cases)} case(s) · dry_run={dry} · limit={limit}"
    )
    if not cases:
        print("Nothing to sync.")
        return 0

    sc = CivilScraper(headless=False)
    sc.reuse_context = True
    if not sc._browser:
        await sc.start()
    ctx = await sc._browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    page = await ctx.new_page()
    print("  Clave Única login (firma)...")
    session = await ClaveUnicaAuth().login(
        page, ClaveUnicaCredentials(rut=CU_RUT, password=CU_PASSWORD), int(lawyer.id)
    )
    await ctx.close()
    sc._page = None

    try:
        created, alerts, reserved, errors = await sync_via_consulta(
            db, lawyer, sc, session, cases, dry_run=dry
        )
        if not dry:
            db.commit()
        print(
            f"\n{'='*56}\n  CONSULTA SYNC SUMMARY (dry_run={dry})\n"
            f"    new movements: {created}\n    alerts:        {alerts}\n"
            f"    reserved:      {reserved}  (→ need 2ª clave / Mis Causas)\n"
            f"    errors:        {errors}\n{'='*56}"
        )
    finally:
        await sc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
