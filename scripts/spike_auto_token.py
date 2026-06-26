"""SPIKE: can Carla generate a valid reCAPTCHA v3 token AUTOMATICALLY?

Carla's own headful Playwright browser (residential IP) loads the reCAPTCHA,
runs grecaptcha.execute() to mint a token with NO human, then tries to log in
with the firm's stored 2ª clave. If PJUD accepts it → automation works; if it
rejects (low score) → we need stealth or a human.

Pause Carla first (one PJUD session per IP). Run:
  PYTHONPATH=. <venv> python scripts/spike_auto_token.py
"""
import asyncio
import os

SITEKEY = "6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt"
ACTION = "validate_captcha_seg_clave_hn"
FIRM_RUT = "16021492-9"


async def main() -> int:
    from app.core.database import SessionLocal
    from app.core.security import decrypt_pjud_password
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.civil import CivilScraper

    db = SessionLocal()
    law = db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).first()
    if not law or not law.encrypted_pjud_password:
        print("No stored 2ª clave for the firm. Stopping.")
        return 2
    password = decrypt_pjud_password(law.encrypted_pjud_password)

    sc = CivilScraper(headless=False)
    await sc.start()
    page = await sc._get_page()

    print("1) loading reCAPTCHA page (scripts/recaptcha_token.html) in Carla's browser…")
    await page.goto(f"file://{os.getcwd()}/scripts/recaptcha_token.html")
    await page.wait_for_function(
        "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'",
        timeout=30000,
    )

    print("2) minting token automatically via grecaptcha.execute (NO human)…")
    token = await page.evaluate(
        """() => new Promise((resolve, reject) => {
            grecaptcha.ready(() => {
                grecaptcha.execute('%s', {action: '%s'}).then(resolve).catch(e => reject(String(e)));
            });
        })""" % (SITEKEY, ACTION)
    )
    print(f"   ✓ token minted automatically (len {len(token)}): {token[:24]}…")

    print("3) attempting PJUD login with the AUTO-generated token…")
    try:
        session = await sc.login_with_token(FIRM_RUT, password, token)
        print("   ✓ login_with_token returned a session")
        cases = await sc.get_my_cases(session=session, max_pages=1)
        n = len(cases) if cases else 0
        if n >= 0 and session is not None:
            print(f"\n==== RESULT: AUTO-TOKEN ACCEPTED ✓✓✓  (get_my_cases → {n} cases pg1) ====")
            print("==== Automation is viable: no human needed for the captcha. ====")
    except Exception as e:
        print(f"\n==== RESULT: AUTO-TOKEN REJECTED ✗  ({type(e).__name__}: {str(e)[:90]}) ====")
        print("==== Score too low — need stealth or a human-generated token. ====")
    finally:
        await sc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
