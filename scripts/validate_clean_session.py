"""Validation: capture a PJUD session from a CLEAN real-Chrome login (good
reCAPTCHA score) and prove our scraper can use it for get_my_cases.

Flow (two steps, run by the operator):
  1. Launch real Chrome with a debug port + clean profile, pointed at PJUD:
       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         --remote-debugging-port=9222 --user-data-dir=/tmp/segal_clean_profile \
         https://oficinajudicialvirtual.pjud.cl/home/index.php
     The human logs in (Clave del Poder Judicial) IN THAT real Chrome — no
     automation attached during login, so reCAPTCHA scores it as human.
  2. Run THIS script. It connects to that Chrome over CDP (read-only — login is
     already done), reads the pjud.cl cookies + localStorage, builds a
     PJUDSession, and runs get_my_cases with OUR scraper (a separate Playwright
     browser; get_my_cases needs no captcha, just the session cookies).

Env: TEST_ABOGADO_RUT (for session metadata), DATABASE_URL, PYTHONPATH.
"""
import asyncio
import os
import sys

CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")


async def main() -> int:
    rut = os.environ.get("TEST_ABOGADO_RUT", "").strip()
    if not rut:
        print("ERROR: TEST_ABOGADO_RUT not set.", file=sys.stderr)
        return 2

    from playwright.async_api import async_playwright
    from app.services.pjud_session import PJUDSession

    # ---- Step A: connect to the real Chrome over CDP and capture the session
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"ERROR: could not connect to Chrome at {CDP_URL}: {e}", file=sys.stderr)
            print("Did you launch Chrome with --remote-debugging-port=9222 ?", file=sys.stderr)
            return 1

        pjud_page = None
        pjud_cookies: list = []
        local_storage = "{}"
        for ctx in browser.contexts:
            cookies = await ctx.cookies()
            ctx_pjud = [dict(c) for c in cookies if "pjud.cl" in c.get("domain", "")]
            if ctx_pjud:
                pjud_cookies = ctx_pjud
            for p in ctx.pages:
                if "pjud.cl" in p.url:
                    pjud_page = p
        if pjud_page is not None:
            print(f"  PJUD tab found: {pjud_page.url}")
            try:
                local_storage = await pjud_page.evaluate("JSON.stringify(localStorage)")
            except Exception:
                pass

        await browser.close()  # CDP: disconnects, does NOT kill the real Chrome

    print(f"  Captured {len(pjud_cookies)} pjud.cl cookies.")
    if not pjud_cookies:
        print("ERROR: no pjud.cl cookies — did you finish logging in in the Chrome window?",
              file=sys.stderr)
        return 1

    session = PJUDSession.create(
        rut=rut, cookies=pjud_cookies, local_storage=local_storage, auth_method="captcha",
    )
    print(f"  PJUDSession built — id={session.session_id}")

    # ---- Step B: use the captured session with OUR scraper (no captcha needed)
    print("\n[get_my_cases] Fetching cases with the captured session...")
    from app.scrapper.pjud.civil import CivilScraper

    sc = CivilScraper(headless=False)
    try:
        cases = await sc.get_my_cases(session, max_pages=5)
    finally:
        await sc.stop()

    print("\n" + "=" * 60)
    print(f"  RESULT: get_my_cases returned {len(cases)} cases")
    for c in cases[:8]:
        print(f"    • {c.rol:<20} {c.caratulado[:50]}")
    print("=" * 60)
    if cases:
        print("  ✅ VALIDATED: clean-Chrome login → captured session → scraper WORKS.")
    else:
        print("  ⚠️ 0 cases — session captured but scraper got nothing (check login / account).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
