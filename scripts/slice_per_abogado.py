"""
Proof-of-concept: per-abogado scraping with segunda clave (Clave del Poder
Judicial) + human-solved reCAPTCHA, running headful on a residential machine.

READ-ONLY — fetches and prints only; never writes to the DB.

Prerequisites
-------------
Source .env.backfill (sets DATABASE_URL, TEST_ABOGADO_RUT, etc.) and make the
app importable via PYTHONPATH:

    set -a && source .env.backfill && set +a
    PYTHONPATH=$(pwd) python scripts/slice_per_abogado.py

Required env vars
-----------------
TEST_ABOGADO_RUT      — RUT with DV, e.g. "17814818-4"
TEST_ABOGADO_PASSWORD — Segunda-clave password
TEST_ABOGADO_NAME     — Human-readable name (for display only)

Optional
--------
TEST_ACCOUNT_RUT      — Firm main-account RUT for DB cross-check.
                        If absent the cross-check is skipped with a clear note.
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: env var {name!r} is not set. Source .env.backfill first.", file=sys.stderr)
        sys.exit(1)
    return value


def _strip_dv(rut: str) -> str:
    """'17814818-4'  →  '17814818'  (PJUD segunda-clave USERNAME format)."""
    return rut.split("-")[0].replace(".", "")


# ---------------------------------------------------------------------------
# Login (headful, human-solved captcha)
# ---------------------------------------------------------------------------

async def _login_headful(rut_username: str, password: str) -> tuple:
    """
    Open a headful Chromium window, pre-fill the segunda-clave form, wait for
    the human to solve the reCAPTCHA and submit, then return
    (pjud_cookies_list, local_storage_str).

    Waits up to 180 s for the post-login redirect to indexN.php.
    """
    # Import here so the module is still loadable without a live browser
    from app.scrapper.pjud.base import PJUD_HOME_URL

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Capture any JS alert/confirm the login shows. Playwright auto-dismisses
        # dialogs by default (the human only sees a flash), so we print the text
        # then accept it — that's how we learn WHY the login is rejected.
        def _on_dialog(dialog):
            print(f"  ⚠️ PJUD ALERT: {dialog.message!r}", flush=True)
            asyncio.create_task(dialog.accept())
        page.on("dialog", _on_dialog)

        # Capture login-related network responses + console errors to learn WHY
        # the login is rejected (captcha refusal, bad creds, etc.).
        def _on_response(resp):
            try:
                u = resp.url
                if any(k in u.lower() for k in ("session", "login", "captcha", "recaptcha", "indexn", "autentica")):
                    print(f"  ↩ RESP {resp.status} {u[:95]}", flush=True)
            except Exception:
                pass
        page.on("response", _on_response)
        page.on("console", lambda m: (
            print(f"  ▸ console[{m.type}]: {m.text[:140]}", flush=True)
            if m.type in ("error", "warning") else None
        ))

        print(f"  Navigating to {PJUD_HOME_URL} ...")
        await page.goto(PJUD_HOME_URL, wait_until="domcontentloaded", timeout=30_000)

        # ------------------------------------------------------------------
        # Pre-fill the visible form fields so the human only needs to solve
        # the reCAPTCHA and press "Ingresar".
        #
        # The segunda-clave login form POSTs to sessionN.php with field names
        # "rut" (cleaned, no DV) and "password".  We try those name attributes
        # first, then fall back to broader selectors.
        # ------------------------------------------------------------------
        filled = await page.evaluate(
            """(args) => {
                const results = { rut: false, pwd: false };

                // --- RUT field ---
                const rutCandidates = [
                    document.querySelector('input[name="rut"]'),
                    document.querySelector('#rut'),
                    document.querySelector('input[placeholder*="RUT" i]'),
                    document.querySelector('input[type="text"]'),
                ];
                for (const el of rutCandidates) {
                    if (el) {
                        el.focus();
                        el.value = args.rut;
                        el.dispatchEvent(new Event('input',  { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        results.rut = true;
                        break;
                    }
                }

                // --- Password field ---
                const pwdCandidates = [
                    document.querySelector('input[name="password"]'),
                    document.querySelector('input[name="pass"]'),
                    document.querySelector('input[type="password"]'),
                ];
                for (const el of pwdCandidates) {
                    if (el) {
                        el.focus();
                        el.value = args.pwd;
                        el.dispatchEvent(new Event('input',  { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        results.pwd = true;
                        break;
                    }
                }

                return results;
            }""",
            {"rut": rut_username, "pwd": password},
        )

        rut_ok = filled.get("rut", False)
        pwd_ok = filled.get("pwd", False)
        print(f"  Pre-fill: RUT field={rut_ok}, password field={pwd_ok}")
        if not rut_ok or not pwd_ok:
            print(
                "  WARNING: one or more fields were not found — "
                "fill them manually in the browser window before solving the captcha."
            )

        # ------------------------------------------------------------------
        # Prompt human
        # ------------------------------------------------------------------
        login_timeout = int(os.environ.get("SLICE_LOGIN_TIMEOUT", "360"))
        sep = "!" * 62
        print(f"\n{sep}")
        print("  En la ventana: entrá a 'Clave del Poder Judicial', poné el RUT")
        print("  (sin DV), la clave, RESOLVÉ EL CAPTCHA y apretá Ingresar.")
        print(f"  Esperando hasta {login_timeout}s para que el login complete...")
        print(f"{sep}\n")

        # Wait for the redirect to the authenticated landing page
        try:
            await page.wait_for_url("**/indexN.php**", timeout=login_timeout * 1000)
        except PlaywrightTimeoutError:
            pass  # Fall through; URL check below decides

        current_url = page.url
        if "indexN.php" not in current_url:
            print(
                f"ERROR: Login did not complete (URL: {current_url}). "
                "Either the captcha was not solved or the timeout expired.",
                file=sys.stderr,
            )
            await browser.close()
            sys.exit(1)

        print(f"  Login confirmed — {current_url}")

        # ------------------------------------------------------------------
        # Extract session data (mirror clave_unica._extract_session)
        # ------------------------------------------------------------------
        all_cookies = await context.cookies()
        pjud_cookies = [
            dict(c) for c in all_cookies if "pjud.cl" in c.get("domain", "")
        ]

        try:
            local_storage = await page.evaluate("JSON.stringify(localStorage)")
        except Exception:
            local_storage = "{}"

        print(f"  Captured {len(pjud_cookies)} pjud.cl cookies.")

        await browser.close()

    return pjud_cookies, local_storage


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    # ------------------------------------------------------------------
    # 1. Validate env
    # ------------------------------------------------------------------
    abogado_rut  = _require_env("TEST_ABOGADO_RUT")       # e.g. "17814818-4"
    password     = _require_env("TEST_ABOGADO_PASSWORD")
    abogado_name = _require_env("TEST_ABOGADO_NAME")

    rut_username = _strip_dv(abogado_rut)  # PJUD login username = RUT sin DV

    # Optional — needed only for the DB cross-check
    account_rut = os.environ.get("TEST_ACCOUNT_RUT", "").strip() or None

    # Late import: requires PYTHONPATH and DATABASE_URL in env
    from app.services.pjud_session import PJUDSession
    from app.scrapper.pjud.civil import CivilScraper

    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  slice_per_abogado  —  {abogado_name}  ({abogado_rut})")
    print(f"  PJUD username      :  {rut_username}  (RUT sin DV)")
    print(f"{sep}\n")

    # ------------------------------------------------------------------
    # 2. Headful login — human solves reCAPTCHA
    # ------------------------------------------------------------------
    print("[Step 1] Opening headful browser for segunda-clave login...")
    pjud_cookies, local_storage = await _login_headful(rut_username, password)

    session = PJUDSession.create(
        rut=abogado_rut,
        cookies=pjud_cookies,
        local_storage=local_storage,
        auth_method="captcha",
    )
    print(f"  PJUDSession created — id={session.session_id}  expires={session.expires_at.isoformat()}\n")

    # ------------------------------------------------------------------
    # 3. Fetch this lawyer's cases via civil scraper
    # ------------------------------------------------------------------
    print("[Step 2] Fetching this lawyer's civil cases (max 5 pages = ~75 cases)...")

    scraper = CivilScraper(headless=False)
    try:
        # get_my_cases starts the scraper's own browser, restores session
        # cookies into a new context, and navigates to misCausas via AJAX.
        cases = await scraper.get_my_cases(session, max_pages=5)
    finally:
        await scraper.stop()

    print(f"  Cases fetched: {len(cases)}  (limited to 5 pages; set max_pages=0 for all)")

    # ------------------------------------------------------------------
    # 4. Print sample ROLs
    # ------------------------------------------------------------------
    sample = cases[:5]
    print(f"\n  First {len(sample)} ROLs:")
    for c in sample:
        print(f"    • {c.rol:<20}  {c.caratulado[:55]}")

    # ------------------------------------------------------------------
    # 5. DB cross-check
    # ------------------------------------------------------------------
    print(f"\n[Step 3] DB cross-check for abogado {abogado_rut}...")

    db_case_ids: set = set()
    crosscheck_ok = False

    if not account_rut:
        print(
            "  Skipped — TEST_ACCOUNT_RUT is not set.\n"
            "  Set TEST_ACCOUNT_RUT=<firm-account-rut> to compare live cases\n"
            "  against what the DB already attributes to this abogado."
        )
    else:
        try:
            from app.core.database import SessionLocal
            from app.services.lawyer_roster import case_ids_for_abogado

            db = SessionLocal()
            try:
                db_case_ids = case_ids_for_abogado(db, account_rut, abogado_rut)
            finally:
                db.close()

            crosscheck_ok = True
            print(f"  account_rut  : {account_rut}")
            print(f"  DB case IDs attributed to this abogado: {len(db_case_ids)}")
            print(
                "  Note: DB uses integer case IDs; live returns ROL strings.\n"
                "  A full ROL→ID join is out of scope for this slice.\n"
                "  Compare counts as a rough signal."
            )
        except Exception as exc:
            print(f"  DB cross-check error: {exc}")

    # ------------------------------------------------------------------
    # 6. Final report
    # ------------------------------------------------------------------
    print(f"\n{sep}")
    print("  REPORT")
    print(sep)
    print(f"  login_ok       : True")
    print(f"  auth_method    : captcha (segunda clave / Clave del Poder Judicial)")
    print(f"  abogado        : {abogado_name}  ({abogado_rut})")
    print(f"  case_count     : {len(cases)}  (≤5 pages fetched)")
    print(f"  sample_rols    : {', '.join(c.rol for c in sample) or '(none)'}")
    if crosscheck_ok:
        print(f"  db_attributed  : {len(db_case_ids)} DB case IDs for this abogado under account {account_rut}")
    else:
        print(f"  db_attributed  : (cross-check skipped — see note above)")
    print(sep)
    print()


if __name__ == "__main__":
    asyncio.run(main())
