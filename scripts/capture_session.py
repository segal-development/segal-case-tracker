"""Capture a PJUD session from a REAL local browser (residential IP).

Why: PJUD's F5 Shape bot-defense blocks the automated Clave Única login from the
GCP VM (datacenter IP). But authenticated *scraping* with a valid session works
fine from the VM. So we decouple: a human logs in here, in a real headful
browser on a residential IP (passes Shape naturally), and this script captures
the session cookies + localStorage into ``pjud_session.json``. That session is
then pushed to the VM for the autonomous backfill to use.

This script is SELF-CONTAINED: it only needs Playwright installed locally
(``pip install playwright && playwright install chromium``). It does NOT import
the app, touch the DB, or need any env config.

Run it:
    python scripts/capture_session.py

A Chrome window opens on PJUD. Log in via "Clave Única". The script auto-detects
the logged-in state, writes ``pjud_session.json``, and exits.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from playwright.async_api import async_playwright

PJUD_HOME_URL = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
OUTPUT_FILE = "pjud_session.json"
# How long the captured session is considered valid by our store. PJUD's real
# server TTL may be longer; the backfill will detect death and ask for a refresh.
SESSION_EXPIRY_MINUTES = 25
POLL_SECONDS = 3
MAX_WAIT_SECONDS = 360


async def is_logged_in(page) -> bool:
    """Robust authenticated-state check (mirrors ClaveUnicaAuth._verify_logged_in)."""
    try:
        url = page.url
        if "indexN.php" in url or "miscausas" in url.lower():
            checks = await page.evaluate(
                """() => ({
                    misCausas: typeof misCausas === 'function',
                    logout: /cerrar sesi/i.test(document.body.innerText),
                })"""
            )
            if checks.get("misCausas") or checks.get("logout"):
                return True
        # Even on the home page, a logged-in session exposes the Mis Causas link.
        return await page.locator("a[onclick='misCausas();']").first.is_visible(timeout=1000)
    except Exception:
        return False


async def main() -> None:
    print("Opening a real Chrome window on PJUD...")
    print("→ Log in using 'Clave Única'. This window is a normal browser on your")
    print("  residential IP, so PJUD's bot-defense will let you through.")
    print(f"→ I'll auto-detect the login (polling every {POLL_SECONDS}s, up to "
          f"{MAX_WAIT_SECONDS // 60} min) and save the session.\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        await page.goto(PJUD_HOME_URL, timeout=60000)

        waited = 0
        while waited < MAX_WAIT_SECONDS:
            if await is_logged_in(page):
                break
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        else:
            print("✗ Timed out waiting for login. Nothing saved. Re-run and log in faster.")
            await browser.close()
            return

        # Live baseline: does misCausas() load in THIS logged-in browser (zero
        # latency, residential IP)? Compares against the cross-IP VM test.
        try:
            await page.goto("https://oficinajudicialvirtual.pjud.cl/indexN.php",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            resp = []
            page.on("response", lambda r: resp.append((r.status, r.url)))
            has_fn = await page.evaluate("() => typeof misCausas === 'function'")
            if has_fn:
                await page.evaluate("() => misCausas()")
                await asyncio.sleep(5)
                bad = [(s, u) for s, u in resp if "pjud.cl" in u and s >= 400]
                print(f"\n[LIVE BASELINE] misCausas_fn={has_fn} ajax_4xx_5xx={bad[:3]}")
            else:
                print(f"\n[LIVE BASELINE] misCausas_fn=False (not on authed page: {page.url})")
        except Exception as e:
            print(f"\n[LIVE BASELINE] error: {str(e)[:120]}")

        # Capture cookies (pjud.cl only) + localStorage.
        cookies = await context.cookies()
        pjud_cookies = [c for c in cookies if "pjud.cl" in c.get("domain", "")]
        try:
            local_storage = await page.evaluate("JSON.stringify(localStorage)")
        except Exception:
            local_storage = "{}"

        now = datetime.now(timezone.utc)
        session = {
            "session_id": str(uuid4()),
            "lawyer_id": 0,  # set on the VM side when loaded
            "rut": "",       # set on the VM side (known there)
            "cookies": pjud_cookies,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=SESSION_EXPIRY_MINUTES)).isoformat(),
            "local_storage": local_storage,
            "last_used_at": None,
            "auth_method": "clave_unica",
        }
        with open(OUTPUT_FILE, "w") as f:
            json.dump(session, f, indent=2)

        await browser.close()
        print(f"\n✓ Logged in. Saved {len(pjud_cookies)} PJUD cookies to {OUTPUT_FILE}")
        print("  Hand this file off to load it onto the VM.")


if __name__ == "__main__":
    asyncio.run(main())
