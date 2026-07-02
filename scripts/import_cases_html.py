"""Import a lawyer's Mis Causas from HTML captured in the user's OWN browser.

The winning path against F5 Shape: instead of automating the login (which Shape
now blocks — even a Chrome started with --remote-debugging-port is detected), the
USER runs a small jQuery snippet in their normal, already-logged-in Chrome console.
It calls PJUD's own consultaMisCausasCivil.php endpoint (same call the page makes),
collects every page's HTML, and downloads a JSON array of page-HTML strings.

This script parses that JSON with the proven CivilScraper._parse_cases_html and
persists via SyncService.sync_cases (list only). No browser, no automation, no
Shape challenge — the data was already fetched by a real human session.

Run:
  LAWYER=16021492-9 HTML_FILE=~/Downloads/carla_causas.json DRY_RUN=0 \
    PYTHONPATH=. <venv> python scripts/import_cases_html.py
"""
import json
import os

LAWYER = os.environ.get("LAWYER", "").strip()
HTML_FILE = os.path.expanduser(os.environ.get("HTML_FILE", "").strip())
DRY_RUN = os.environ.get("DRY_RUN", "1").strip() != "0"


def main() -> int:
    if not LAWYER or not HTML_FILE or not os.path.exists(HTML_FILE):
        print("Set LAWYER=<rut> and HTML_FILE=<path to the downloaded JSON>.")
        return 2

    from app.core.database import SessionLocal
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.civil import CivilScraper
    from app.services.sync_service import ScrapedCase, SyncService

    with open(HTML_FILE) as f:
        pages = json.load(f)
    if isinstance(pages, str):
        pages = [pages]
    print(f"import_cases_html · {len(pages)} page(s) of HTML · lawyer={LAWYER} · dry_run={DRY_RUN}")

    # Pure parsing — no browser is started.
    sc = CivilScraper(headless=True)
    all_cases = []
    for i, html in enumerate(pages, 1):
        if not isinstance(html, str) or html.startswith("ERROR:"):
            print(f"  page {i}: skipped ({str(html)[:40]})")
            continue
        try:
            cases = sc._parse_cases_html(html)
        except Exception as e:
            print(f"  page {i}: parse error {type(e).__name__}: {str(e)[:60]}")
            continue
        all_cases.extend(cases)
        print(f"  page {i}: {len(cases)} causas")

    # Dedup by rol (same case can appear once per page only, but be safe).
    seen, unique = set(), []
    for c in all_cases:
        if c.rol not in seen:
            seen.add(c.rol)
            unique.append(c)
    print(f"\n  parsed {len(all_cases)} rows → {len(unique)} unique causas")
    if not unique:
        print("  nothing to import (check the JSON / snippet output).")
        return 1

    from datetime import datetime
    from app.models.case import Case
    from app.models.court import Court

    db = SessionLocal()
    lawyer = db.query(Lawyer).filter(Lawyer.rut == LAWYER).first()
    if not lawyer:
        print(f"  lawyer {LAWYER} not found in DB.")
        return 1
    lid = int(lawyer.id)

    # Preload existing rols for this lawyer + the court name→id map (few queries,
    # not one-per-case — that's what made sync_cases crawl through the proxy).
    existing_rols = {r for (r,) in db.query(Case.rol).filter(Case.lawyer_id == lid)}
    courts = {name: cid for name, cid in db.query(Court.name, Court.id)}
    new_cases = [c for c in unique if c.rol not in existing_rols]
    print(f"  {len(existing_rols)} ya en DB · {len(new_cases)} NUEVAS para {lawyer.name}")

    if DRY_RUN:
        print(f"  DRY_RUN: insertaría {len(new_cases)} causas nuevas. No writes.")
        return 0

    if not new_cases:
        print("  nada nuevo que insertar (la lista ya está completa en la DB).")
        return 0

    # Ensure courts exist for the new cases (create the few missing ones). Reuse
    # SyncService's court creator so code/region/type are set correctly.
    now = datetime.utcnow()
    sync = SyncService(db)
    for c in new_cases:
        name = (c.tribunal or "Desconocido").strip() or "Desconocido"
        if name not in courts:
            court = sync._get_or_create_court(name, "civil")
            courts[name] = court.id

    def _split(carat):
        parts = (carat or "").split("/", 1)
        p = parts[0].strip() if parts else None
        d = parts[1].strip() if len(parts) > 1 else None
        return (p or None), (d or None)

    mappings = []
    for c in new_cases:
        name = (c.tribunal or "Desconocido").strip() or "Desconocido"
        plaintiff, defendant = _split(c.caratulado)
        mappings.append({
            "lawyer_id": lid, "court_id": courts[name], "rol": c.rol.strip().upper(),
            "competencia": "civil", "plaintiff": plaintiff, "defendant": defendant,
            "procedure": (c.cuaderno or None), "status": "active",
            "created_at": now, "updated_at": now,
        })

    db.bulk_insert_mappings(Case, mappings)
    db.commit()
    print(f"\n==== imported · {lawyer.name} ({LAWYER}) · {len(mappings)} causas NUEVAS insertadas ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
