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
from datetime import datetime, timezone
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
# Per-abogado consulta-by-rol, folded into Carla's CU session: every
# CONSULTA_EVERY rounds, refresh a batch of the LAWYERS' public cases by rol
# (no captcha, no per-lawyer login). OPT-IN (default off) so it never changes
# Carla's firm-freshness behaviour unless explicitly enabled.
CONSULTA_ENABLED = os.environ.get("CONSULTA_ENABLED", "false").lower() == "true"
CONSULTA_EVERY = int(os.environ.get("CONSULTA_EVERY", "5"))
CONSULTA_BATCH = int(os.environ.get("CONSULTA_BATCH", "15"))
# Per-lawyer CU scrape folded into Carla's loop: every CU_ROTATOR_EVERY rounds,
# log in as one CU-enrolled lawyer (round-robin) and scrape their full Mis Causas
# including reserved cases. OPT-IN (default off) — Carla is unchanged unless set.
CU_ROTATOR_ENABLED = os.environ.get("CU_ROTATOR_ENABLED", "false").lower() == "true"
CU_ROTATOR_EVERY = int(os.environ.get("CU_ROTATOR_EVERY", "12"))
CU_ROTATOR_BATCH = int(os.environ.get("CU_ROTATOR_BATCH", "15"))
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


def _select_consulta_cases(db, firm_lawyer_id: int, limit: int):
    """The LAWYERS' civil cases to refresh via consulta-by-rol (not the firm's own).

    Excludes the firm account (those are kept fresh by the normal Mis-Causas
    round), reserved cases (only visible via Mis Causas), and courts with no known
    PJUD corte. Least-recently-checked first so coverage rotates fairly.
    """
    from app.models.case import Case
    from app.models.court import Court

    return (
        db.query(Case)
        .join(Court, Case.court_id == Court.id)
        .filter(
            Case.lawyer_id != firm_lawyer_id,
            Case.competencia == COMPETENCIA,
            Case.consulta_reserved.is_(False),
            Court.pjud_corte.isnot(None),
        )
        .order_by(Case.last_detail_checked_at.asc().nullsfirst())
        .limit(limit)
        .all()
    )


async def _sync_after_login(db, sc, session, lawyer_id: int) -> int:
    """Run get_my_cases + sync_cases + detect_and_sync_movements for one lawyer.

    Intended as the immediate-sync step executed right after a successful
    login in handle_connection.  Returns the number of API cases found
    (used as ``cases_synced`` in the connected-status payload).

    Does NOT commit — the caller owns the transaction.
    """
    from app.services.sync_service import (
        ScrapedCase,
        SyncService,
        _select_cases_for_detail_rotation,
        detect_and_sync_movements,
    )

    api_cases = await sc.get_my_cases(session=session, max_pages=0)
    scraped = [
        ScrapedCase(
            rol=c.rol,
            tribunal=c.tribunal,
            caratulado=c.caratulado,
            fecha_ingreso=c.fecha_ingreso,
            estado_cuaderno=c.estado_cuaderno or "",
            cuaderno=c.cuaderno or "",
            institucion=c.institucion,
        )
        for c in api_cases
    ]
    SyncService(db).sync_cases(lawyer_id, scraped, competencia="civil")
    selected = _select_cases_for_detail_rotation(db, lawyer_id, "civil", api_cases, BATCH)
    await detect_and_sync_movements(
        db=db,
        scraper=sc,
        pjud_session=session,
        lawyer_id=lawyer_id,
        api_cases=api_cases,
        selected_cases=selected,
        delay_between_fetches=FETCH_DELAY,
    )
    return len(api_cases)


async def handle_connection(job: dict, redis, db, sc, lock: asyncio.Lock) -> None:
    """Process one PJUD connection job from the Redis queue.

    Lifecycle states written to Redis:
      pending (set by the API) -> connecting -> connected | failed

    The browser lock is held around the entire login + save + sync section so
    only one live PJUD browser session is active at a time on this machine.

    Security: the decrypted password is NEVER passed to any logger (INV-2).
    """
    from app.models.lawyer import Lawyer
    from app.core.security import decrypt_pjud_password
    from app.services.connection_queue import set_status
    from app.services.session_store import SessionStore

    connection_id = job["connection_id"]
    lawyer_id = int(job["lawyer_id"])
    rut = job["rut"]
    auth_method = job["auth_method"]
    captcha_token = job.get("captcha_token")

    def _now() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    await set_status(redis, connection_id, {"status": "connecting", "updated_at": _now()})

    try:
        lawyer = db.query(Lawyer).filter(Lawyer.id == lawyer_id).first()
        if lawyer is None:
            raise ValueError(f"Lawyer {lawyer_id} not found in DB")

        if auth_method == "segunda_clave":
            if not lawyer.encrypted_pjud_password:
                raise ValueError("No segunda_clave credential stored for this lawyer")
            # Decrypt locally — NEVER log or propagate the plaintext (INV-2).
            password = decrypt_pjud_password(lawyer.encrypted_pjud_password)

            async with lock:
                session = await sc.login_with_token(rut, password, captcha_token)
                store = SessionStore(redis)
                await store.asave_session(session)
                cases_synced = await _sync_after_login(db, sc, session, lawyer_id)
        else:
            # CU path — follow-on slice; stub raises so callers surface intent.
            raise NotImplementedError(
                f"auth_method {auth_method!r} is not implemented in this slice; "
                "the CU login path is a follow-on task."
            )

        db.commit()
        await set_status(redis, connection_id, {
            "status": "connected",
            "session_id": session.session_id,
            "cases_synced": cases_synced,
            "updated_at": _now(),
        })

    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        reason = str(exc) if str(exc) else type(exc).__name__
        await set_status(redis, connection_id, {
            "status": "failed",
            "reason": reason,
            "updated_at": _now(),
        })


async def connection_watcher(
    redis,
    db,
    sc,
    lock: asyncio.Lock,
    pending: dict,
) -> None:
    """Background Task: BLPOP connection jobs from Redis and dispatch each as a Task.

    Each job is dispatched via ``asyncio.create_task`` so the handler runs
    independently of this watcher loop — long-running login + sync does not
    stall the watcher from picking up the next job.

    *pending* is a shared ``{"count": int}`` mutable dict.  The counter is
    incremented when a job is popped and decremented when its task finishes
    (including on error), so the main loop can check it before acquiring
    ``pjud_browser_lock`` and yield priority to connection tasks.
    """
    from app.services.connection_queue import blpop_connection

    while True:
        try:
            job = await blpop_connection(redis, timeout=5)
            if job is None:
                continue

            pending["count"] += 1

            async def _handle_and_decrement(j: dict = job) -> None:
                try:
                    await handle_connection(j, redis, db, sc, lock)
                finally:
                    pending["count"] -= 1

            asyncio.create_task(_handle_and_decrement())

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"  connection_watcher error: {exc}")


async def main() -> None:
    from app.scrapper.pjud_civil import PJUDCivilScraper
    from app.core.database import SessionLocal
    from app.models.lawyer import Lawyer
    from app.services.sync_service import (
        detect_and_sync_movements,
        sync_via_consulta,
        _select_cases_for_detail_rotation,
    )
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
    from scripts.cu_rotator import _select_cu_lawyers, sync_one_cu_lawyer, _next_cu_index
    from app.core.redis import get_async_redis_client

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

    # Concurrent-browser safety: one lock serializes both the freshness round
    # and any on-demand connection tasks so only one live PJUD session is
    # active at a time (mirrors the "one session per residential IP" invariant).
    pjud_browser_lock = asyncio.Lock()
    # Shared counter: incremented by connection_watcher on BLPOP, decremented
    # when the dispatched task completes. The main loop checks it before
    # acquiring the lock to yield priority to connection jobs (shorter captcha
    # window than a full freshness batch).
    pending_connections: dict = {"count": 0}

    redis_client = await get_async_redis_client()
    if redis_client is not None:
        asyncio.create_task(
            connection_watcher(redis_client, db, sc, pjud_browser_lock, pending_connections)
        )
        print("  connection_watcher started")
    else:
        print("  Redis not available — connection_watcher disabled")

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

    # Round-robin cursor for CU-enrolled lawyers (advances each CU-rotator invocation).
    cu_cursor = 0

    round_n = 0
    consecutive_bad = 0
    cooldowns_total = 0  # cumulative likely-block events this session (metric)
    # Challenge/failure telemetry: classify each failure so we SEE what's going on
    # (session death = recoverable re-auth; block = likely Shape pushing back).
    signals = {"session": 0, "block": 0, "transient": 0}

    def classify(exc) -> str:
        n = type(exc).__name__
        if "Session" in n or "Login" in n or "Auth" in n:
            return "session"
        if "Scraping" in n:
            return "block"
        return "transient"

    while True:
        if _off_hours():
            now = datetime.now(_CHILE_TZ)
            print(f"  off-hours ({now:%a %H:%M} Chile) — pausing {OFF_HOURS_SLEEP_MIN}min "
                  f"(courts act in daytime; quieter + more human)")
            await asyncio.sleep(OFF_HOURS_SLEEP_MIN * 60 * random.uniform(0.8, 1.2))
            continue
        round_n += 1
        round_bad = False

        # Connection tasks have a shorter captcha validity window — yield so
        # they can acquire pjud_browser_lock before we start a new batch.
        if pending_connections["count"] > 0:
            await asyncio.sleep(0)

        await pjud_browser_lock.acquire()
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
                    signals[classify(e)] += 1
                    print(f"  list fetch failed [{classify(e)}] ({str(e)[:60]}); re-auth next round")
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
                        signals["block"] += 1  # touched cases, ALL failed → likely Shape block/challenge
                        round_bad = True

                    # Per-abogado consulta-by-rol, interleaved into the SAME CU
                    # session (no captcha, no per-lawyer login). Opt-in + isolated:
                    # any failure here must NEVER break the firm freshness loop.
                    if CONSULTA_ENABLED and not round_bad and round_n % CONSULTA_EVERY == 0:
                        try:
                            consulta_cases = _select_consulta_cases(db, lawyer_id, CONSULTA_BATCH)
                            if consulta_cases:
                                c_created, c_alerts, c_reserved, c_errors = await sync_via_consulta(
                                    db, lawyer, sc, session, consulta_cases,
                                    dry_run=False, reauth_callback=reauth_cb,
                                )
                                db.commit()
                                print(f"  consulta round {round_n}: {len(consulta_cases)} abogado-cases · "
                                      f"movements+{c_created} reserved={c_reserved} errors={c_errors}")
                        except Exception as e:
                            db.rollback()
                            signals[classify(e)] += 1
                            print(f"  consulta sync failed [{classify(e)}]: {str(e)[:100]}")

                    # Per-lawyer CU scrape (Mis Causas including reserved). Opt-in +
                    # isolated: any failure here must NEVER break the firm loop.
                    if CU_ROTATOR_ENABLED and not round_bad and round_n % CU_ROTATOR_EVERY == 0:
                        cu_lawyers = _select_cu_lawyers(db, LAWYER_RUT)
                        if cu_lawyers:
                            pick_idx, cu_cursor = _next_cu_index(cu_cursor, len(cu_lawyers))
                            cu_lawyer = cu_lawyers[pick_idx]
                            try:
                                cu_created, cu_updated, cu_errors = await sync_one_cu_lawyer(
                                    db, sc, cu_lawyer, batch=CU_ROTATOR_BATCH
                                )
                                db.commit()
                                print(
                                    f"  cu-rotator round {round_n}: {cu_lawyer.rut} · "
                                    f"movements+{cu_created} updated={cu_updated} errors={cu_errors}"
                                )
                            except Exception as e:
                                db.rollback()
                                signals[classify(e)] += 1
                                print(
                                    f"  cu-rotator failed [{classify(e)}]: {str(e)[:100]}"
                                )
                            finally:
                                # CRITICAL: logging in AS the lawyer replaces the firm
                                # session on this IP/browser. Always invalidate so
                                # ensure_session() re-auths the FIRM CU on the next round.
                                held["session"] = None
                                sc._page = None
                                sc._panel_loaded = False
        except Exception as e:
            db.rollback()
            signals[classify(e)] += 1
            print(f"  round {round_n} error [{classify(e)}]: {str(e)[:120]}")
            round_bad = True
        finally:
            pjud_browser_lock.release()

        # Telemetry summary every 20 rounds — running picture of failure kinds.
        if round_n % 20 == 0:
            print(f"  📊 telemetry (round {round_n}): session={signals['session']} "
                  f"block={signals['block']} transient={signals['transient']} "
                  f"cooldowns={cooldowns_total}")

        # Back-off policy: consecutive bad rounds likely mean PJUD is pushing back
        # (challenge/block/session trouble). Cool down — never hammer or rotate harder.
        consecutive_bad = consecutive_bad + 1 if round_bad else 0
        if consecutive_bad >= COOLDOWN_THRESHOLD:
            cooldowns_total += 1
            breakdown = (f"sesión {signals['session']} / bloqueo {signals['block']} / "
                         f"transitorio {signals['transient']}")
            # When blocks dominate the recent failures it's likely Shape; when session
            # dominates it's just re-auth churn. Label the alert accordingly.
            likely = "BLOQUEO (Shape)" if signals["block"] >= signals["session"] else "sesión/re-auth"
            print(f"  ⚠️ {consecutive_bad} bad rounds in a row — possible block/challenge. "
                  f"Backing off {COOLDOWN_MINUTES} min. (cooldown #{cooldowns_total}; {breakdown})")
            # Early-warning alert: surface a likely PJUD block NOW, before it turns
            # into a silent stale-data outage (the freshness monitor only catches
            # staleness after the fact). Telegram failure must never break the loop.
            try:
                from scripts.freshness_monitor import send_telegram
                send_telegram(
                    f"⚠️ PJUD — probable {likely} (Carla): {consecutive_bad} rounds malos seguidos. "
                    f"Señales acumuladas: {breakdown}. Enfriando {COOLDOWN_MINUTES} min · "
                    f"evento #{cooldowns_total} esta sesión."
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
