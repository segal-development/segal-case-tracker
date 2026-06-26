"""Segunda-clave rotator — auto-token + login + sync for lawyers with a 2ª clave.

Like cu_rotator, but for the 2ª clave: Carla's own headful browser mints the
reCAPTCHA v3 token automatically (no human, no 2captcha), logs in with each
lawyer's stored 2ª clave, and syncs their Mis Causas — attributing the cases to
that lawyer. Validated: auto-token acceptance is 100% (spike_token_consistency).

Pause Carla before running — one PJUD session per IP.

Env:
    DRY_RUN  "1" (default) = login + get_my_cases only, no writes; "0" = persist
    LIMIT    max lawyers this run (default 50)
    LAWYER   optional rut to target one lawyer

Run (dry):  PYTHONPATH=. <venv> python scripts/segunda_clave_rotator.py
Real:       DRY_RUN=0 LAWYER=16021492-9 ... python scripts/segunda_clave_rotator.py
"""
import asyncio
import os

SITEKEY = "6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt"
ACTION = "validate_captcha_seg_clave_hn"
DRY_RUN = os.environ.get("DRY_RUN", "1").strip() != "0"
LIMIT = int(os.environ.get("LIMIT", "50"))
TARGET = os.environ.get("LAWYER", "").strip()


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


async def sync_one_segunda_clave(db, sc, lawyer, *, dry_run: bool) -> int:
    """Auto-token → login with the lawyer's 2ª clave → sync their Mis Causas.

    Returns the number of API cases found. Attributes cases to lawyer.id.
    """
    from app.core.security import decrypt_pjud_password

    password = decrypt_pjud_password(lawyer.encrypted_pjud_password)
    token = await _mint_token(sc)
    session = await sc.login_with_token(lawyer.rut, password, token)

    if dry_run:
        cases = await sc.get_my_cases(session=session, max_pages=0)
        return len(cases) if cases else 0

    from scripts.freshness_sync import _sync_after_login
    return await _sync_after_login(db, sc, session, int(lawyer.id))


async def main() -> int:
    from app.core.database import SessionLocal
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.civil import CivilScraper
    from app.scrapper.pjud.exceptions import InvalidCredentialsError, LoginError

    db = SessionLocal()
    q = db.query(Lawyer).filter(Lawyer.encrypted_pjud_password.isnot(None))
    if TARGET:
        q = q.filter(Lawyer.rut == TARGET)
    lawyers = q.limit(LIMIT).all()

    print(f"segunda_clave_rotator · {len(lawyers)} lawyer(s) with 2ª clave · dry_run={DRY_RUN}\n")
    if not lawyers:
        print("No lawyers with a stored 2ª clave. Nothing to do.")
        return 0

    sc = CivilScraper(headless=False)
    await sc.start()
    total = 0
    bad_clave = []   # (name, rut) — wrong/changed password → must update
    other_fail = []  # (name, rut, msg) — token/network/other → retry-able
    try:
        for lw in lawyers:
            try:
                n = await sync_one_segunda_clave(db, sc, lw, dry_run=DRY_RUN)
                if not DRY_RUN:
                    db.commit()
                total += n
                print(f"  ✓ {lw.name} ({lw.rut}): {n} causas")
            except InvalidCredentialsError as e:
                if not DRY_RUN:
                    db.rollback()
                bad_clave.append((lw.name, lw.rut))
                print(f"  🔑 {lw.name} ({lw.rut}): CLAVE INVÁLIDA — pedir que la actualice [{str(e)[:50]}]")
            except LoginError as e:
                if not DRY_RUN:
                    db.rollback()
                other_fail.append((lw.name, lw.rut, str(e)))
                print(f"  ✗ {lw.name} ({lw.rut}): login falló (token/otro, reintentable): {str(e)[:50]}")
            except Exception as e:
                if not DRY_RUN:
                    db.rollback()
                other_fail.append((lw.name, lw.rut, f"{type(e).__name__}: {e}"))
                print(f"  ✗ {lw.name} ({lw.rut}): {type(e).__name__}: {str(e)[:50]}")
    finally:
        await sc.stop()

    print(f"\n==== done · {len(lawyers)} lawyer(s) · {total} cases total (dry_run={DRY_RUN}) ====")
    if bad_clave:
        print(f"\n🔑 {len(bad_clave)} abogado(s) con CLAVE INVÁLIDA — pedirles que la actualicen:")
        for name, rut in bad_clave:
            print(f"    - {name} ({rut})")
    if other_fail:
        print(f"\n✗ {len(other_fail)} fallo(s) reintentables (token/red — NO es la clave):")
        for name, rut, msg in other_fail:
            print(f"    - {name} ({rut}): {msg[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
