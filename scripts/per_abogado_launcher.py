"""SKETCH — per-abogado enrollment + scrape launcher (one command).

Flow:
  1. Launch a REAL Chrome (debug port + clean profile) at the PJUD home.
  2. Wait for the lawyer to log in — polling the debug port's HTTP /json endpoint
     (READ-ONLY; no CDP/Playwright attached during login, so reCAPTCHA v3 still
     scores it as a clean human browser).
  3. Once logged in, scrape the lawyer's cases over CDP via scrape_via_cdp()
     (injects the authenticated page into CivilScraper — reuses all scraper logic).

Run (pause the Carla freshness scraper first — one PJUD session per IP at a time):
    set -a; source .env.backfill; set +a
    PYTHONPATH=. <venv> python scripts/per_abogado_launcher.py
Env: TEST_ABOGADO_RUT (lawyer RUT). Optional: CHROME_BIN, DEBUG_PORT, PROFILE_DIR, LOGIN_TIMEOUT.

STATUS: sketch — needs a live login to validate end to end.
"""
import asyncio
import json
import os
import subprocess
import urllib.request

CHROME_BIN = os.environ.get(
    "CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
DEBUG_PORT = int(os.environ.get("DEBUG_PORT", "9222"))
PROFILE_DIR = os.environ.get("PROFILE_DIR", "/tmp/segal_clean_profile")
PJUD_HOME = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
LOGIN_TIMEOUT = int(os.environ.get("LOGIN_TIMEOUT", "300"))


def _tabs() -> list:
    """List the debug Chrome's tabs via HTTP — read-only, no automation attached."""
    try:
        with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=5) as r:
            return json.load(r)
    except Exception:
        return []


def _logged_in() -> bool:
    return any("indexN.php" in (t.get("url") or "") for t in _tabs())


def _launch_chrome() -> subprocess.Popen:
    return subprocess.Popen(
        [CHROME_BIN, f"--remote-debugging-port={DEBUG_PORT}",
         f"--user-data-dir={PROFILE_DIR}", PJUD_HOME],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def main() -> int:
    rut = os.environ.get("TEST_ABOGADO_RUT", "").strip()
    if not rut:
        print("ERROR: TEST_ABOGADO_RUT not set (lawyer RUT).")
        return 2
    if not os.path.exists(CHROME_BIN):
        print(f"ERROR: Chrome not found at {CHROME_BIN} (set CHROME_BIN).")
        return 2

    print(f"Launching real Chrome (debug {DEBUG_PORT}, clean profile) at PJUD...")
    _launch_chrome()  # left running; operator closes it when done

    print("\n👉 Logueate en el PJUD (Clave del Poder Judicial) en la ventana de Chrome.")
    print(f"   Esperando login (hasta {LOGIN_TIMEOUT}s)...")
    waited = 0
    while waited < LOGIN_TIMEOUT:
        if _logged_in():
            print("  ✓ Login detectado (indexN.php).")
            break
        await asyncio.sleep(4)
        waited += 4
    else:
        print("  ✗ Timeout esperando login.")
        return 1

    # Logged in → scrape over CDP (reuse the sketch's logic).
    from scripts.cdp_scraper_sketch import scrape_via_cdp

    print("\n[scrape] Trayendo causas vía CDP...")
    try:
        cases = await scrape_via_cdp(rut, max_pages=2)
    except Exception as e:
        print(f"  scrape_via_cdp failed: {type(e).__name__}: {str(e)[:160]}")
        return 1

    print(f"\n{'='*58}")
    print(f"  RESULTADO: {len(cases)} causas")
    for c in cases[:10]:
        print(f"    • {c.rol:<18} {(c.caratulado or '')[:50]}")
    print("=" * 58)
    if cases:
        print("  ✅ FLUJO PER-ABOGADO END-TO-END OK (launch → login → scrape).")
    print(f"\n(El Chrome de debug sigue abierto — cerralo con: "
          f"pkill -f 'remote-debugging-port={DEBUG_PORT}')")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
