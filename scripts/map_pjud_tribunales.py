"""Scrape PJUD Consulta Unificada tribunal codes and map them to courts in the DB.

IMPORTANT — before running:
  1. Pause Carla (the freshness scraper) — PJUD allows one active session per IP.
  2. Must run on the residential Mac (PJUD's F5 Shape blocks datacenter/headless IPs).
  3. Set CU_RUT and CU_PASSWORD in the environment (e.g. source .env.backfill).

Usage:
    source .env.backfill
    PYTHONPATH=. python scripts/map_pjud_tribunales.py

This script is idempotent: re-running it overwrites the JSON file and re-applies the
mapping (courts already matched will be updated to the same values; run is safe).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure app config loads in production mode so DATABASE_URL is read from env.
os.environ.setdefault("ENVIRONMENT", "production")

CU_RUT = os.environ.get("CU_RUT", "")
CU_PASSWORD = os.environ.get("CU_PASSWORD", "")
LAWYER_RUT = os.environ.get("LAWYER_RUT", "16021492-9")

# All Corte de Apelaciones codes for the Civil competencia (3).
# Source: PJUD Consulta Unificada #conCorte select options.
CIVIL_CORTES = [10, 11, 15, 20, 25, 30, 35, 40, 45, 46, 50, 55, 56, 60, 61, 90, 91]

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "pjud_tribunales_civil.json"


async def _scrape_directory(page) -> list[dict]:
    """Drive the PJUD Consulta Unificada selects and return all civil tribunal entries."""
    directory: list[dict] = []

    # Trigger the Consulta Unificada panel (loaded via JS on PJUD home).
    await page.evaluate("consultaUnificada && consultaUnificada()")
    await asyncio.sleep(4)

    # Select competencia = 3 (Civil).
    await page.select_option("#competencia", "3")
    await asyncio.sleep(2)

    for corte in CIVIL_CORTES:
        await page.select_option("#conCorte", str(corte))
        await asyncio.sleep(2)

        # Read all <option> elements from #conTribunal.
        # Each option value is "<code>|<name>" after our JS mapping.
        options: list[str] = await page.evaluate(
            "() => [...document.querySelector('#conTribunal').options]"
            ".map(o => o.value + '|' + o.text.trim())"
        )

        corte_count = 0
        for opt in options:
            parts = opt.split("|", 1)
            if len(parts) != 2:
                continue
            code, name = parts[0].strip(), parts[1].strip()
            # Skip the "Todos" / empty placeholder option.
            if not code or code == "0" or not name or name.lower() in ("todos", "-- todos --"):
                continue
            try:
                tribunal_code = int(code)
            except ValueError:
                continue
            directory.append({"corte": corte, "tribunal": tribunal_code, "name": name})
            corte_count += 1

        print(f"  corte {corte}: {corte_count} tribunals")

    return directory


async def main() -> None:
    if not (CU_RUT and CU_PASSWORD):
        print("ERROR: CU_RUT and CU_PASSWORD environment variables must be set.")
        print("  source .env.backfill  — then re-run this script.")
        sys.exit(1)

    # Lazy imports so the script can be linted/imported without a running DB/browser.
    from sqlalchemy import or_

    from app.core.database import SessionLocal
    from app.models.court import Court
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
    from app.scrapper.pjud.civil import CivilScraper
    from app.services.tribunal_matcher import match_tribunal

    # --- Resolve lawyer_id (needed by ClaveUnicaAuth.login for session tracking) ---
    db = SessionLocal()
    try:
        lawyer = db.query(Lawyer).filter(Lawyer.rut == LAWYER_RUT).first()
        if not lawyer:
            print(
                f"WARNING: No lawyer found for LAWYER_RUT={LAWYER_RUT!r}. "
                "Using lawyer_id=0 — login will still work but session won't be attributed."
            )
            lawyer_id = 0
        else:
            lawyer_id = int(lawyer.id)
    finally:
        db.close()

    # --- Browser: login via Clave Única, keep page open for Consulta Unificada ---
    sc = CivilScraper(headless=False)
    await sc.start()

    directory: list[dict] = []
    try:
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
            creds = ClaveUnicaCredentials(rut=CU_RUT, password=CU_PASSWORD)
            print("Logging in via Clave Única...")
            # login() navigates to PJUD home and returns a PJUDSession.
            # We keep the page open (unlike freshness_sync which closes ctx after login)
            # because we need the live JS environment to drive the cascading selects.
            await ClaveUnicaAuth().login(page, creds, lawyer_id)
            print("Login successful. Scraping Consulta Unificada tribunal directory...")

            directory = await _scrape_directory(page)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
    finally:
        try:
            await sc.stop()
        except Exception:
            pass

    print(f"\nScraped {len(directory)} tribunal entries total.")

    # --- Persist the directory to JSON (for reuse / auditing) ---
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(directory, fh, ensure_ascii=False, indent=2)
    print(f"Wrote directory to {OUTPUT_FILE}")

    # --- Match courts in the DB and update pjud_corte / pjud_tribunal ---
    db = SessionLocal()
    try:
        courts = (
            db.query(Court)
            .filter(
                or_(
                    Court.type == "civil",
                    Court.name.ilike("%Civil%"),
                    Court.name.ilike("%Letras%"),
                )
            )
            .all()
        )

        matched_names: list[str] = []
        unmatched_names: list[str] = []

        for court in courts:
            entry = match_tribunal(court.name, directory)
            if entry:
                court.pjud_competencia = 3
                court.pjud_corte = entry["corte"]
                court.pjud_tribunal = entry["tribunal"]
                matched_names.append(court.name)
            else:
                unmatched_names.append(court.name)

        db.commit()

        print(f"\n{'='*60}")
        print(f"Summary")
        print(f"{'='*60}")
        print(f"  Civil courts queried : {len(courts)}")
        print(f"  Matched              : {len(matched_names)}")
        print(f"  Unmatched            : {len(unmatched_names)}")

        if unmatched_names:
            print(f"\nUnmatched courts (inspect manually — name may differ from PJUD):")
            for name in sorted(unmatched_names):
                print(f"  - {name}")
        else:
            print("\nAll civil courts matched successfully.")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
