"""Backfill full case detail (movements + entities + documents) for every case
that has not been detail-scraped yet, with AUTONOMOUS Clave Única re-auth.

Set CU_RUT + CU_PASSWORD in the env to enable unattended mode: when a PJUD
session dies mid-run, the backfill logs in again via Clave Única (no captcha)
and continues — grinding through ALL not-yet-checked cases without manual
tokens. Without CU_* it falls back to a single live session (seed via
/pjud/login first).

Run inside the api container:
    docker compose -f docker-compose.qa.yml exec -T \
        -e PYTHONPATH=/app -e CU_RUT=16021492-9 -e CU_PASSWORD='...' \
        api python scripts/backfill_detail.py

Resumable + idempotent: re-run any time; it continues with whatever is still
unchecked (scoped to ROL year >= DETAIL_MIN_YEAR).
"""
import asyncio
import os

os.environ.setdefault("ENVIRONMENT", "production")

LAWYER_RUT = "16021492-9"
COMPETENCIA = "civil"
CU_RUT = os.environ.get("CU_RUT", "")
CU_PASSWORD = os.environ.get("CU_PASSWORD", "")
# PJUD's F5 Shape blocks headless browsers AND datacenter IPs. This backfill now
# runs from a RESIDENTIAL machine with a HEADFUL browser (the only combo that
# passes Shape). Headless is opt-in for legacy/testing only.
HEADLESS = os.environ.get("BACKFILL_HEADLESS", "false").lower() == "true"


async def main() -> None:
    from app.services.session_store import get_session_store
    from app.scrapper.pjud_civil import PJUDCivilScraper
    from app.core.database import SessionLocal
    from app.config import settings
    from app.models.case import Case
    from app.models.lawyer import Lawyer
    from app.services.sync_service import (
        detect_and_sync_movements,
        _select_cases_for_detail_rotation,
    )
    from app.scrapper.pjud.browser import BrowserFactory
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials

    db = SessionLocal()
    lawyer = db.query(Lawyer).filter(Lawyer.rut == LAWYER_RUT).first()
    if not lawyer:
        print(f"No lawyer for rut {LAWYER_RUT} — run a sync/login first.")
        return
    lawyer_id = int(lawyer.id)
    store = get_session_store()
    sc = PJUDCivilScraper(headless=HEADLESS)
    print(f"browser mode: {'HEADLESS' if HEADLESS else 'HEADFUL'} (Shape needs headful+residential)")

    min_year = settings.DETAIL_MIN_YEAR

    def _year_ok(rol: str) -> bool:
        if min_year <= 0:
            return True
        try:
            last = rol.rsplit("-", 1)[-1]
        except (AttributeError, IndexError):
            return True
        if len(last) != 4 or not last.isdigit():
            return True
        return int(last) >= min_year

    def remaining() -> tuple[int, int]:
        rows = (
            db.query(Case.rol, Case.last_detail_checked_at)
            .filter(Case.lawyer_id == lawyer_id, Case.competencia == COMPETENCIA)
            .all()
        )
        scoped = [(r, c) for r, c in rows if _year_ok(r)]
        return len(scoped), sum(1 for _, c in scoped if c is None)

    async def clave_unica_login():
        """Full Clave Única login (no captcha) → fresh PJUDSession, saved + scraper reset."""
        creds = ClaveUnicaCredentials(rut=CU_RUT, password=CU_PASSWORD)
        async with BrowserFactory(headless=HEADLESS) as factory:
            page = await factory.new_page()
            new_session = await ClaveUnicaAuth().login(page, creds, lawyer_id)
        await store.asave_session(new_session)
        # Reset the scraper's page/panel so the next fetch rebuilds from the NEW session.
        try:
            if sc._page and not sc._page.is_closed():
                await sc._page.close()
        except Exception:
            pass
        sc._page = None
        sc._panel_loaded = False
        return new_session

    auto = bool(CU_RUT and CU_PASSWORD)
    reauth_cb = clave_unica_login if auto else None
    print(f"mode: {'AUTONOMOUS (Clave Única re-auth)' if auto else 'single-session'}")

    async def ensure_session():
        s = await store.get_session_by_lawyer(lawyer_id)
        if s is None and auto:
            print("  no live session — logging in via Clave Única...")
            s = await clave_unica_login()
        return s

    total, rem = remaining()
    print(f"coverage before: {total - rem}/{total} checked, {rem} remaining (year >= {min_year})")

    no_progress = 0
    round_n = 0
    while True:
        rem_before = remaining()[1]
        if rem_before == 0:
            print("✅ 100% of in-scope cases now have detail."); break

        session = await ensure_session()
        if session is None:
            print("No session and no Clave Única creds — set CU_RUT/CU_PASSWORD. Stopping."); break

        round_n += 1
        print(f"\n--- round {round_n}: fetching case list... ({rem_before} remaining) ---")
        try:
            api_cases = await sc.get_my_cases(session=session, max_pages=0)
        except Exception as e:
            if auto:
                print(f"  list fetch failed ({str(e)[:60]}); re-auth + retry next round")
                await clave_unica_login()
                continue
            print(f"  list fetch failed: {e}"); break

        batch = _select_cases_for_detail_rotation(db, lawyer_id, COMPETENCIA, api_cases, 100000)
        print(f"  processing {len(batch)} unchecked cases (auto-reauth on session death)...")
        try:
            created, updated, errors = await detect_and_sync_movements(
                db=db, scraper=sc, pjud_session=session, lawyer_id=lawyer_id,
                api_cases=api_cases, selected_cases=batch,
                delay_between_fetches=1.0, reauth_callback=reauth_cb,
            )
            db.commit()
            print(f"  round done: movements+{created}, errors={len(errors)}")
        except Exception as e:
            db.rollback()
            print(f"  round error: {str(e)[:100]}")

        rem_after = remaining()[1]
        done = rem_before - rem_after
        print(f"  progress: {done} cases detailed this round; {rem_after} remaining")
        if rem_after == 0:
            print("✅ 100% of in-scope cases now have detail."); break
        if done <= 0:
            no_progress += 1
            if no_progress >= 3:
                print("⚠️ no progress for 3 rounds — stopping (investigate)."); break
            # force a fresh login before retrying
            if auto:
                await clave_unica_login()
        else:
            no_progress = 0

    total, rem = remaining()
    print(f"\n=== FINAL: {total - rem}/{total} in-scope cases have detail, {rem} remaining ===")
    try:
        await sc.stop()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
