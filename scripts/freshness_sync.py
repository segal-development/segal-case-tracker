"""Continuous freshness re-sync: keep already-detailed cases up to date by
re-checking the STALEST ones first, forever, to catch new movements (the deltas
that drive the frontend: new plazos, state changes, new documents).

Unlike backfill_detail.py (which stops once every case has been detailed once),
this NEVER stops: each round it re-syncs a batch of the cases checked longest ago
(``last_detail_checked_at ASC NULLS FIRST``), so coverage rotates continuously.

Runs HEADFUL from a residential machine (PJUD's F5 Shape blocks headless +
datacenter IPs) with autonomous Clave Única re-auth on session death.

Env (from .env.backfill): CU_RUT, CU_PASSWORD, DATABASE_URL, etc.
  FRESHNESS_BATCH        cases per round (default 40)
  FRESHNESS_ROUND_SLEEP  seconds to sleep between rounds (default 45)
  FRESHNESS_FETCH_DELAY  seconds between case fetches (default 2.5)
  BACKFILL_HEADLESS      "true" to force headless (default headful)

Run (supervised): scripts/run_freshness.sh
"""
import asyncio
import os

os.environ.setdefault("ENVIRONMENT", "production")

LAWYER_RUT = os.environ.get("LAWYER_RUT", "16021492-9")
COMPETENCIA = "civil"
CU_RUT = os.environ.get("CU_RUT", "")
CU_PASSWORD = os.environ.get("CU_PASSWORD", "")
HEADLESS = os.environ.get("BACKFILL_HEADLESS", "false").lower() == "true"
BATCH = int(os.environ.get("FRESHNESS_BATCH", "40"))
ROUND_SLEEP = int(os.environ.get("FRESHNESS_ROUND_SLEEP", "45"))
FETCH_DELAY = float(os.environ.get("FRESHNESS_FETCH_DELAY", "2.5"))
# Block/challenge back-off: after this many consecutive "bad" rounds (likely
# PJUD pushing back), cool down instead of hammering — never rotate harder.
COOLDOWN_THRESHOLD = int(os.environ.get("FRESHNESS_COOLDOWN_THRESHOLD", "3"))
COOLDOWN_MINUTES = int(os.environ.get("FRESHNESS_COOLDOWN_MINUTES", "20"))


async def main() -> None:
    from app.scrapper.pjud_civil import PJUDCivilScraper
    from app.core.database import SessionLocal
    from app.models.lawyer import Lawyer
    from app.services.sync_service import (
        detect_and_sync_movements,
        _select_cases_for_detail_rotation,
    )
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials

    db = SessionLocal()
    lawyer = db.query(Lawyer).filter(Lawyer.rut == LAWYER_RUT).first()
    if not lawyer:
        print(f"No lawyer for rut {LAWYER_RUT} — run a sync/login first.")
        return
    lawyer_id = int(lawyer.id)
    sc = PJUDCivilScraper(headless=HEADLESS)
    sc.reuse_context = True  # headful: reuse one window across cases (no pop per case)
    print(f"freshness sync · browser={'HEADLESS' if HEADLESS else 'HEADFUL'} · "
          f"batch={BATCH} · round_sleep={ROUND_SLEEP}s")

    # In-process session — no Redis dependency (robust for the always-on box).
    held: dict = {"session": None}

    async def clave_unica_login():
        creds = ClaveUnicaCredentials(rut=CU_RUT, password=CU_PASSWORD)
        # Reuse the scraper's SINGLE browser instead of launching a separate
        # BrowserFactory — that's what made a NEW OS window pop on every login.
        # The scraper launches chromium with the same anti-detection args as
        # BrowserFactory, and login() navigates to PJUD itself, so a blank
        # context/page in the same browser is equivalent for Shape.
        if not sc._browser:
            await sc.start()
        ctx = await sc._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        try:
            new_session = await ClaveUnicaAuth().login(page, creds, lawyer_id)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
        # Force the scraper to rebuild its working context with the new session
        # on the next operation (reuses the same browser → no new window).
        sc._page = None
        sc._panel_loaded = False
        held["session"] = new_session
        return new_session

    if not (CU_RUT and CU_PASSWORD):
        print("CU_RUT/CU_PASSWORD not set — cannot run unattended. Stopping.")
        return
    reauth_cb = clave_unica_login

    async def ensure_session():
        s = held["session"]
        if s is None or s.is_expired():
            print("  logging in via Clave Única...")
            s = await clave_unica_login()
        return s

    round_n = 0
    consecutive_bad = 0
    while True:
        round_n += 1
        round_bad = False
        try:
            session = await ensure_session()
            if session is None:
                print("  could not obtain a session; retrying")
                round_bad = True
            else:
                api_cases = None
                try:
                    api_cases = await sc.get_my_cases(session=session, max_pages=0)
                except Exception as e:
                    print(f"  list fetch failed ({str(e)[:60]}); re-auth next round")
                    try:
                        await clave_unica_login()
                    except Exception:
                        pass
                    round_bad = True

                if api_cases is not None:
                    batch = _select_cases_for_detail_rotation(
                        db, lawyer_id, COMPETENCIA, api_cases, BATCH
                    )
                    created, updated, errors = await detect_and_sync_movements(
                        db=db, scraper=sc, pjud_session=session, lawyer_id=lawyer_id,
                        api_cases=api_cases, selected_cases=batch,
                        delay_between_fetches=FETCH_DELAY, reauth_callback=reauth_cb,
                    )
                    db.commit()
                    print(f"  round {round_n}: {len(batch)} cases re-synced · "
                          f"movements+{created} updated={updated} errors={len(errors)}")
                    # A round that touched cases but EVERY one failed (and nothing
                    # advanced) looks like PJUD blocking/challenging, not bad data.
                    if batch and len(errors) >= len(batch) and created == 0 and updated == 0:
                        round_bad = True
        except Exception as e:
            db.rollback()
            print(f"  round {round_n} error: {str(e)[:120]}")
            round_bad = True

        # Back-off policy: consecutive bad rounds likely mean PJUD is pushing back
        # (challenge/block/session trouble). Cool down — never hammer or rotate harder.
        consecutive_bad = consecutive_bad + 1 if round_bad else 0
        if consecutive_bad >= COOLDOWN_THRESHOLD:
            print(f"  ⚠️ {consecutive_bad} bad rounds in a row — possible block/challenge. "
                  f"Backing off {COOLDOWN_MINUTES} min.")
            consecutive_bad = 0
            await asyncio.sleep(COOLDOWN_MINUTES * 60)
        else:
            await asyncio.sleep(ROUND_SLEEP)


if __name__ == "__main__":
    asyncio.run(main())
