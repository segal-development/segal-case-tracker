"""SPIKE step 1: is the AUTO-generated reCAPTCHA token CONSISTENTLY accepted?

Repeats N times: mint a token via grecaptcha.execute (no human) → login with the
firm's 2ª clave → get_my_cases. Reports the acceptance rate. If it's near 100%,
the auto-token is reliable enough to automate the whole 2ª-clave fleet.

Pause Carla first. Run:  N=6 PYTHONPATH=. <venv> python scripts/spike_token_consistency.py
"""
import asyncio
import os

SITEKEY = "6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt"
ACTION = "validate_captcha_seg_clave_hn"
FIRM_RUT = "16021492-9"
N = int(os.environ.get("N", "6"))
DELAY = int(os.environ.get("DELAY", "12"))


async def _mint(sc) -> str:
    # Fresh page each iteration — login_with_token consumes/closes the prior one.
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


async def main() -> int:
    from app.core.database import SessionLocal
    from app.core.security import decrypt_pjud_password
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.civil import CivilScraper

    db = SessionLocal()
    law = db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).first()
    password = decrypt_pjud_password(law.encrypted_pjud_password)

    sc = CivilScraper(headless=False)
    await sc.start()

    print(f"Validating auto-token consistency: {N} iterations, {DELAY}s apart\n")
    ok = 0
    for i in range(1, N + 1):
        try:
            token = await _mint(sc)
            session = await sc.login_with_token(FIRM_RUT, password, token)
            cases = await sc.get_my_cases(session=session, max_pages=1)
            ok += 1
            print(f"  iter {i}/{N}: ✓ ACCEPTED (token len {len(token)}, {len(cases) if cases else 0} cases)")
        except Exception as e:
            print(f"  iter {i}/{N}: ✗ REJECTED ({type(e).__name__}: {str(e)[:60]})")
        if i < N:
            await asyncio.sleep(DELAY)

    await sc.stop()
    pct = round(100 * ok / N)
    print(f"\n==== CONSISTENCY: {ok}/{N} accepted ({pct}%) ====")
    if pct >= 80:
        print("==== Reliable enough to automate the 2ª-clave fleet. ====")
    else:
        print("==== Flaky — needs stealth tuning or human fallback. ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
