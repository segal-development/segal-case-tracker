"""Diagnostic: is a session captured from the real Chrome actually AUTHENTICATED
when restored into our Playwright browser? Dumps cookie names + localStorage keys,
restores them into a fresh Playwright context, navigates to Mis Causas, and reports
whether it's logged in or bounced to the login page (+ screenshot)."""
import asyncio
import json
import os

INDEX_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"


def _fix(c: dict) -> dict:
    out = {k: c[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly") if k in c}
    exp = c.get("expires", -1)
    if exp not in (-1, None):
        out["expires"] = exp
    ss = c.get("sameSite")
    if ss in ("Strict", "Lax", "None"):
        out["sameSite"] = ss
    return out


async def main() -> int:
    from playwright.async_api import async_playwright

    cookies: list = []
    ls = "{}"

    # 1) capture from the live real Chrome (read-only)
    async with async_playwright() as pw:
        rc = await pw.chromium.connect_over_cdp("http://localhost:9222")
        for ctx in rc.contexts:
            cs = await ctx.cookies()
            pj = [dict(c) for c in cs if "pjud.cl" in c.get("domain", "")]
            if pj:
                cookies = pj
            for p in ctx.pages:
                if "pjud.cl" in p.url:
                    try:
                        ls = await p.evaluate("JSON.stringify(localStorage)")
                    except Exception:
                        pass
        await rc.close()

    try:
        ls_keys = list(json.loads(ls).keys())
    except Exception:
        ls_keys = []
    print(f"COOKIE NAMES ({len(cookies)}):", [c["name"] for c in cookies])
    print(f"LOCALSTORAGE KEYS ({len(ls_keys)}):", ls_keys)

    # 2) restore into a fresh Playwright Chromium + check auth
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = await b.new_context()
        try:
            await ctx.add_cookies([_fix(c) for c in cookies])
            print("  cookies restored into Playwright context")
        except Exception as e:
            print(f"  add_cookies error: {e}")
        page = await ctx.new_page()
        await page.goto(INDEX_URL, wait_until="domcontentloaded", timeout=30000)
        # restore localStorage then reload (some apps read auth from there)
        if ls_keys:
            try:
                await page.evaluate(
                    "(d)=>{const o=JSON.parse(d);for(const k in o)localStorage.setItem(k,o[k]);}", ls
                )
                await page.goto(INDEX_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"  localStorage restore error: {e}")
        await asyncio.sleep(3)

        url = page.url
        txt = await page.evaluate("document.body.innerText")
        low = txt.lower()
        logged_in = ("mis causas" in low) or ("acevedo" in low) or ("cerrar ses" in low)
        at_login = ("clave única" in low or "clave unica" in low) or ("contáctenos" in low and "mis causas" not in low)
        await page.screenshot(path="/tmp/diag_restored.png")

        print("\n" + "=" * 56)
        print("  RESTORED URL :", url)
        print("  logged-in?   :", logged_in, "(Mis Causas / Acevedo / Cerrar sesión)")
        print("  at login/home:", at_login)
        print("  body snippet :", txt[:160].replace("\n", " "))
        print("  screenshot   : /tmp/diag_restored.png")
        print("=" * 56)
        await b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
