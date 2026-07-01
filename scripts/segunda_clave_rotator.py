"""Segunda-clave rotator — auto-token + login + sync for lawyers with a 2ª clave.

Like cu_rotator, but for the 2ª clave: Carla's own headful browser mints the
reCAPTCHA v3 token automatically (no human, no 2captcha), logs in with each
lawyer's stored 2ª clave, and syncs their Mis Causas — attributing the cases to
that lawyer. Validated: auto-token acceptance is 100% (spike_token_consistency).

Pause Carla before running — one PJUD session per IP is allowed.

Modes:
  dry_run    — login + get_my_cases only, no writes
  list_only  — persist just the case LIST (fast); Carla backfills detail later
  (default)  — full sync: list + detail rotation (use LAWYER_DELAY between
               lawyers to avoid 403 bursts)

Env:
  DRY_RUN        "1" (default) dry-run only; "0" = persist
  LIST_ONLY      "1" = persist the case list only (fast, no detail rotation)
  LIMIT          max lawyers this run (default 50)
  LAWYER         optional rut to target ONE lawyer
  LAWYER_DELAY   seconds between lawyers (default 60, 0=no wait)
  COOLDOWN_403   seconds to pause after a 403 (default 300)
  TARGETS        optional "rut1:esperadas1,rut2:esperadas2" for progress reports

Examples:
  # Dry-run (check counts only)
  PYTHONPATH=. <venv> python scripts/segunda_clave_rotator.py

  # List-only persist for Benjamin (fast, ~30s)
  LIST_ONLY=1 DRY_RUN=0 LAWYER=16021492-9 ... python scripts/segunda_clave_rotator.py

  # Full persist, gradual (60s between lawyers)
  DRY_RUN=0 LIST_ONLY=0 ... python scripts/segunda_clave_rotator.py

  # Full persist, gradual, with target counts
  TARGETS="16021492-9:278,19456852-5:2078" DRY_RUN=0 ... python scripts/segunda_clave_rotator.py
"""
import asyncio
import os
import re

SITEKEY = "6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt"
ACTION = "validate_captcha_seg_clave_hn"
DRY_RUN = os.environ.get("DRY_RUN", "1").strip() != "0"
LIST_ONLY = os.environ.get("LIST_ONLY", "0").strip() == "1"
LIMIT = int(os.environ.get("LIMIT", "50"))
TARGET = os.environ.get("LAWYER", "").strip()
LAWYER_DELAY = int(os.environ.get("LAWYER_DELAY", "60"))
COOLDOWN_403 = int(os.environ.get("COOLDOWN_403", "300"))

# Parse optional target counts: "rut1:count1,rut2:count2"
_TARGETS_RAW = os.environ.get("TARGETS", "").strip()
TARGET_COUNTS: dict[str, int] = {}
if _TARGETS_RAW:
    for pair in _TARGETS_RAW.split(","):
        if ":" in pair:
            r, c = pair.split(":", 1)
            TARGET_COUNTS[r.strip()] = int(c.strip())


async def _mint_token(sc) -> str:
    """Generate a reCAPTCHA v3 token automatically in Carla's browser."""
    page = await sc._get_page()
    await page.goto(f"file://{os.getcwd()}/scripts/recaptcha_token.html")
    await page.wait_for_function(
        "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'",
        timeout=30000,
    )
    return await page.evaluate(
        """() => new Promise((res, rej) => {
            grecaptcha.ready(() => grecaptcha.execute('%s', {action: '%s'}).then(res).catch(e => rej(String(e))));
        })""" % (SITEKEY, ACTION)
    )


async def sync_one_segunda_clave(db, sc, lawyer, *, dry_run: bool, list_only: bool = False) -> int:
    """Auto-token → login with the lawyer's 2ª clave → sync their Mis Causas.

    Returns the number of API cases found. Attributes cases to lawyer.id.

    Modes:
      dry_run   — login + get_my_cases only, no writes.
      list_only — persist just the case LIST (fast); skip the slow per-case
                  detail scrape. Carla's rotation backfills movements later.
      (default) — full sync (list + detail rotation batch).
    """
    from app.core.security import decrypt_pjud_password

    password = decrypt_pjud_password(lawyer.encrypted_pjud_password)
    token = await _mint_token(sc)
    session = await sc.login_with_token(lawyer.rut, password, token)

    if dry_run:
        cases = await sc.get_my_cases(session=session, max_pages=0)
        return len(cases) if cases else 0

    if list_only:
        from app.services.sync_service import ScrapedCase, SyncService

        api_cases = await sc.get_my_cases(session=session, max_pages=0)
        scraped = [
            ScrapedCase(
                rol=c.rol, tribunal=c.tribunal, caratulado=c.caratulado,
                fecha_ingreso=c.fecha_ingreso, estado_cuaderno=c.estado_cuaderno or "",
                cuaderno=c.cuaderno or "", institucion=c.institucion,
            )
            for c in api_cases
        ]
        SyncService(db).sync_cases(int(lawyer.id), scraped, competencia="civil")
        db.commit()  # sync_cases only flushes — commit the list explicitly
        return len(api_cases)

    from scripts.freshness_sync import _sync_after_login
    return await _sync_after_login(db, sc, session, int(lawyer.id))


async def main() -> int:
    from app.core.database import SessionLocal
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.civil import CivilScraper
    from app.scrapper.pjud.exceptions import InvalidCredentialsError, LoginError, ShapeChallengeError

    db = SessionLocal()
    q = db.query(Lawyer).filter(Lawyer.encrypted_pjud_password.isnot(None))
    if TARGET:
        q = q.filter(Lawyer.rut == TARGET)
    lawyers = q.limit(LIMIT).all()

    mode = "dry-run" if DRY_RUN else ("list-only" if LIST_ONLY else "full")
    print(f"segunda_clave_rotator · {len(lawyers)} lawyer(s) · mode={mode} · delay={LAWYER_DELAY}s\n")
    if not lawyers:
        print("No lawyers with a stored 2ª clave. Nothing to do.")
        return 0

    sc = CivilScraper(headless=False)
    await sc.start()
    total = 0
    bad_clave = []   # (name, rut) — wrong/changed password → must update
    other_fail = []  # (name, rut, msg) — token/network/other → retry-able
    pjud_block_seen = False
    cooldown_pending = False
    try:
        for i, lw in enumerate(lawyers):
            # After a PJUD block, apply cooldown before the next lawyer.
            # Keep pjud_block_seen true for the final summary/exit code.
            did_cooldown = False
            if cooldown_pending:
                print(f"\n  ⏳ 403 cooldown {COOLDOWN_403}s…")
                await asyncio.sleep(COOLDOWN_403)
                cooldown_pending = False
                did_cooldown = True

            # Spacing between lawyers (skip delay after cooldown and before first)
            if i > 0 and not did_cooldown and LAWYER_DELAY > 0:
                print(f"  ⏱  espera {LAWYER_DELAY}s entre abogados…")
                await asyncio.sleep(LAWYER_DELAY)

            try:
                n = await sync_one_segunda_clave(db, sc, lw, dry_run=DRY_RUN, list_only=LIST_ONLY)
                if not DRY_RUN:
                    db.commit()

                target = TARGET_COUNTS.get(lw.rut)
                if target is not None:
                    falta = target - n
                    if falta > 0:
                        print(f"  ✓ {lw.name} ({lw.rut}): {n}/{target} (faltan {falta})")
                    else:
                        print(f"  ✓ {lw.name} ({lw.rut}): {n}/{target} COMPLETO 🎯")
                else:
                    print(f"  ✓ {lw.name} ({lw.rut}): {n} causas")
                total += n

            except InvalidCredentialsError as e:
                if not DRY_RUN:
                    db.rollback()
                bad_clave.append((lw.name, lw.rut))
                print(f"  🔑 {lw.name} ({lw.rut}): CLAVE INVÁLIDA — pedir que la actualice [{str(e)[:50]}]")

            except ShapeChallengeError as e:
                if not DRY_RUN:
                    db.rollback()
                pjud_block_seen = True
                cooldown_pending = True
                marker = getattr(e, "marker", "Shape/TSPD")
                other_fail.append((lw.name, lw.rut, f"SHAPE BLOQUEO — {marker}"))
                print(f"  🚫 {lw.name} ({lw.rut}): SHAPE/TSPD BLOQUEADO — cooldown tras terminar la ronda [{marker}]")

            except LoginError as e:
                if not DRY_RUN:
                    db.rollback()
                other_fail.append((lw.name, lw.rut, str(e)))
                print(f"  ✗ {lw.name} ({lw.rut}): login falló (token/otro, reintentable): {str(e)[:50]}")

            except Exception as e:
                if not DRY_RUN:
                    db.rollback()
                msg = f"{type(e).__name__}: {e}"
                # Detect 403 (PJUD rate-limit / block) by error text or status
                if re.search(r"\b403\b|Forbidden|HTTP.*403|too_many|rate.limit|Shape/TSPD|TSPD|failureConfig", str(e), re.IGNORECASE):
                    pjud_block_seen = True
                    cooldown_pending = True
                    other_fail.append((lw.name, lw.rut, f"PJUD BLOQUEO — {str(e)[:60]}"))
                    print(f"  🚫 {lw.name} ({lw.rut}): PJUD BLOQUEADO — cooldown tras terminar la ronda")
                else:
                    other_fail.append((lw.name, lw.rut, msg))
                    print(f"  ✗ {lw.name} ({lw.rut}): {type(e).__name__}: {str(e)[:50]}")
    finally:
        await sc.stop()

    print(f"\n==== done · {len(lawyers)} abogado(s) · {total} causas totales (mode={mode}) ====")
    if pjud_block_seen:
        print(f"\n🚫 Se detectó bloqueo PJUD (403/Shape/TSPD). Esperá {COOLDOWN_403}s+ antes de reintentar.")
    if bad_clave:
        print(f"\n🔑 {len(bad_clave)} abogado(s) con CLAVE INVÁLIDA — pedirles que la actualicen:")
        for name, rut in bad_clave:
            print(f"    - {name} ({rut})")
    if other_fail:
        label = "bloqueo(s)/fallo(s) PJUD" if pjud_block_seen else "fallo(s) reintentables (token/red)"
        print(f"\n✗ {len(other_fail)} {label}:")
        for name, rut, msg in other_fail:
            print(f"    - {name} ({rut}): {msg[:70]}")
    return int(pjud_block_seen)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
