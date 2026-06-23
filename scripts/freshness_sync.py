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
import random
from datetime import datetime
from zoneinfo import ZoneInfo

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
# Business-hours guard: courts (and a believable human) act in daytime. Off-hours
# scraping is both pointless (no new movements at 3am) and suspicious — pause it.
SCRAPE_HOURS_ENABLED = os.environ.get("SCRAPE_HOURS_ENABLED", "true").lower() == "true"
SCRAPE_HOURS_START = int(os.environ.get("SCRAPE_HOURS_START", "8"))   # inclusive
SCRAPE_HOURS_END = int(os.environ.get("SCRAPE_HOURS_END", "21"))      # exclusive
SCRAPE_SKIP_SUNDAY = os.environ.get("SCRAPE_SKIP_SUNDAY", "true").lower() == "true"
OFF_HOURS_SLEEP_MIN = int(os.environ.get("OFF_HOURS_SLEEP_MIN", "15"))
_CHILE_TZ = ZoneInfo("America/Santiago")


def _off_hours() -> bool:
    """True when we should NOT scrape (night, or optionally Sunday) — Chile time."""
    if not SCRAPE_HOURS_ENABLED:
        return False
    now = datetime.now(_CHILE_TZ)
    if SCRAPE_SKIP_SUNDAY and now.weekday() == 6:  # 6 = Sunday
        return True
    return now.hour < SCRAPE_HOURS_START or now.hour >= SCRAPE_HOURS_END


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
    cooldowns_total = 0  # cumulative likely-block events this session (metric)
    while True:
        if _off_hours():
            now = datetime.now(_CHILE_TZ)
            print(f"  off-hours ({now:%a %H:%M} Chile) — pausing {OFF_HOURS_SLEEP_MIN}min "
                  f"(courts act in daytime; quieter + more human)")
            await asyncio.sleep(OFF_HOURS_SLEEP_MIN * 60 * random.uniform(0.8, 1.2))
            continue
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
            cooldowns_total += 1
            print(f"  ⚠️ {consecutive_bad} bad rounds in a row — possible block/challenge. "
                  f"Backing off {COOLDOWN_MINUTES} min. (cooldown #{cooldowns_total})")
            # Early-warning alert: surface a likely PJUD block NOW, before it turns
            # into a silent stale-data outage (the freshness monitor only catches
            # staleness after the fact). Telegram failure must never break the loop.
            try:
                from scripts.freshness_monitor import send_telegram
                send_telegram(
                    f"⚠️ PJUD posible bloqueo/challenge (Carla): {consecutive_bad} rounds "
                    f"malos seguidos. Enfriando {COOLDOWN_MINUTES} min · evento #{cooldowns_total} "
                    f"esta sesión. Revisá si nos bloquearon."
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  (telegram alert failed: {exc})")
            consecutive_bad = 0
            await asyncio.sleep(COOLDOWN_MINUTES * 60 * random.uniform(0.9, 1.3))
        else:
            # ±jitter so rounds aren't machine-regular (anti-fingerprint).
            await asyncio.sleep(ROUND_SLEEP * random.uniform(0.7, 1.5))


if __name__ == "__main__":
    asyncio.run(main())
