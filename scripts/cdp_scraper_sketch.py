"""SKETCH — per-abogado scraper driving a REAL Chrome via CDP.

WHY (validated empirically, see memory per-abogado/validated-architecture):
- The segunda-clave login reCAPTCHA is v3 (score-based). A real Chrome passes;
  an automated Playwright browser scores low and is rejected.
- The session is F5-Shape-bound to the browser that created it — it CANNOT be
  moved to a separate Playwright browser (PJUD redirects to home / unauth).
- Therefore the scraper must run IN the real Chrome where the lawyer logged in.

APPROACH:
- The lawyer logs into PJUD in a real Chrome launched with a debug port.
- We connect via Playwright connect_over_cdp, grab the already-authenticated page,
  and INJECT it into the existing CivilScraper (sc._page = page). get_my_cases /
  get_case_detail then reuse all their proven logic (AJAX + parsing + pagination)
  ON the real Chrome — no session move, Shape stays happy.

HOW TO RUN (needs a live, logged-in debug Chrome):
  1. "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
       --remote-debugging-port=9222 --user-data-dir=/tmp/segal_clean_profile \
       https://oficinajudicialvirtual.pjud.cl/home/index.php
  2. Lawyer logs in (Clave del Poder Judicial). Leave the tab on Mis Causas.
  3. set -a; source .env.backfill; set +a   (TEST_ABOGADO_RUT = the lawyer's RUT)
     PYTHONPATH=. <venv> python scripts/cdp_scraper_sketch.py

STATUS: first draft — wire is in place; needs a live login to validate end to end
(rut/DV handling, panel load, the AJAX over the injected page may need iteration).
Production version would add a clean `attach_cdp()` on the scraper + per-lawyer
rotation + detail/downloads + re-auth signaling.
"""
import asyncio
import os

CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")


def _compute_dv(num: str) -> str:
    """Chilean RUT check digit (mod 11), so get_my_cases gets a well-formed RUT."""
    total, factor = 0, 2
    for digit in reversed(num):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    rem = 11 - (total % 11)
    return "0" if rem == 11 else "K" if rem == 10 else str(rem)


async def attach_authenticated_page(pw):
    """connect_over_cdp to the real Chrome and return (browser, authenticated page)."""
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    # Prefer the logged-in index page; fall back to any pjud.cl tab.
    for want in ("indexN.php", "pjud.cl"):
        for ctx in browser.contexts:
            for page in ctx.pages:
                if want in page.url:
                    return browser, page
    return browser, None


def normalize_rut(rut: str) -> str:
    """Ensure the RUT has its check digit (the scraper needs rut_num + DV)."""
    rut = rut.strip()
    return rut if "-" in rut else f"{rut}-{_compute_dv(rut)}"


async def _try_download(sc, session, detail) -> None:
    """Validate the document-download path over CDP: grab one available doc that
    has a token + endpoint from the detail and fetch its bytes."""
    docs = [d for m in detail.movements for d in getattr(m, "documentos", [])]
    docs += list(getattr(detail, "case_documents", []))
    dl = next(
        (d for d in docs if getattr(d, "available", True) and d.token and d.endpoint and d.doc_type),
        None,
    )
    if dl is None:
        print("  (no downloadable doc with token+endpoint in this case)")
        return
    try:
        pdf = await sc.download_document_generic(
            session=session, endpoint=dl.endpoint, doc_type=dl.doc_type, token=dl.token
        )
        is_pdf = isinstance(pdf, (bytes, bytearray)) and bytes(pdf[:5]).startswith(b"%PDF")
        print(f"  download[{dl.url_type}/{dl.doc_type}]: {len(pdf)} bytes · PDF={is_pdf}  ✓")
    except Exception as e:
        print(f"  download_document_generic over CDP raised: {type(e).__name__}: {str(e)[:140]}")


async def scrape_via_cdp(rut: str, max_pages: int = 2, with_detail: bool = True,
                         with_download: bool = True) -> list:
    """Attach to the logged-in real Chrome over CDP and fetch the lawyer's cases
    by INJECTING its authenticated page into CivilScraper — reuses all the proven
    scraper logic (AJAX + parsing + pagination) inside the real Chrome. With
    with_detail / with_download it also validates get_case_detail and a document
    download over CDP. Returns the case list.
    """
    from app.scrapper.pjud.civil import CivilScraper
    from app.services.pjud_session import PJUDSession

    rut = normalize_rut(rut)
    print(f"  Lawyer RUT (normalized): {rut}")
    sc = CivilScraper(headless=False)
    session = PJUDSession.create(rut=rut, cookies=[], local_storage="{}", auth_method="captcha")
    # Clean attach: wires the real Chrome's authenticated page into the scraper.
    await sc.attach_cdp(CDP_URL, session)
    print(f"  Attached to real Chrome tab: {sc._page.url}")
    try:
        cases = await sc.get_my_cases(session, max_pages=max_pages)
        if with_detail:
            target = next((c for c in cases if getattr(c, "case_token", None)), None)
            if target is None:
                print("  (no case with a token to fetch detail)")
            else:
                try:
                    detail = await sc.get_case_detail(session=session, case_token=target.case_token)
                    print(f"  detail[{target.rol}]: {len(detail.movements)} movimientos · "
                          f"{len(detail.litigantes)} litigantes · "
                          f"{len(getattr(detail, 'escritos', []))} escritos · "
                          f"{len(getattr(detail, 'exhortos', []))} exhortos  ✓")
                    if with_download:
                        await _try_download(sc, session, detail)
                except Exception as e:
                    print(f"  get_case_detail over CDP raised: {type(e).__name__}: {str(e)[:140]}")
        return cases
    finally:
        await sc.stop()  # CDP-aware: disconnects, leaves the real Chrome open


async def persist_via_cdp(
    rut: str,
    name: str = "",
    max_pages: int = 2,
    batch: int = 20,
    dry_run: bool = True,
    reserved_first: bool = False,
) -> None:
    """Scrape a lawyer's cases over CDP and persist them to the database.

    Reuses the proven sync path: SyncService.sync_cases for case rows then
    detect_and_sync_movements for detail/movements.  The scraper attaches to the
    real Chrome exactly like scrape_via_cdp — no session move, F5-Shape stays happy.

    DRY-RUN SAFETY:
      The entire operation is wrapped in a SQLAlchemy SAVEPOINT (begin_nested).
      sync_cases' internal db.commit() releases only the SAVEPOINT — the outer
      transaction stays open.  detect_and_sync_movements does not commit (the
      caller is responsible, per the freshness_sync.py pattern).  At the end:
        dry_run=True  → db.rollback()  — outer transaction rolled back, nothing persisted
        dry_run=False → db.commit()   — outer transaction committed, all rows land

    Args:
        rut:       Lawyer's RUT (with or without check digit; normalized internally).
        name:      Display name for the Lawyer row (falls back to rut when empty).
        max_pages: Max pages passed to get_my_cases.
        batch:     Number of cases forwarded to the detail/movement rotation.
        dry_run:   Default True — safe for a first test; set False for a real sync.
    """
    from datetime import datetime

    from sqlalchemy.orm import Session as SASession

    from app.core.database import SessionLocal, engine
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.civil import CivilScraper
    from app.services.pjud_session import PJUDSession
    from app.services.sync_service import (
        ScrapedCase,
        SyncService,
        detect_and_sync_movements,
        _select_cases_for_detail_rotation,
    )

    rut = normalize_rut(rut)
    print(f"\n{'='*60}")
    print(f"  persist_via_cdp · rut={rut} · dry_run={dry_run}")
    print(f"{'='*60}")

    # DRY-RUN SAFETY: begin_nested() does NOT hold here — sync_cases' internal
    # db.commit() commits the WHOLE transaction (verified empirically: the row
    # persisted through a later rollback). Use the canonical external-transaction
    # pattern: bind the Session to a connection that has an outer transaction +
    # join_transaction_mode="create_savepoint", so each internal commit() becomes a
    # savepoint release; _outer.rollback() at the end undoes EVERYTHING.
    if dry_run:
        _conn = engine.connect()
        _outer = _conn.begin()
        db = SASession(bind=_conn, join_transaction_mode="create_savepoint")
    else:
        db = SessionLocal()
        _conn = _outer = None

    sc = CivilScraper(headless=False)
    session = PJUDSession.create(rut=rut, cookies=[], local_storage="{}", auth_method="captcha")

    try:

        # 1. Get-or-create Lawyer row ----------------------------------------
        lawyer = db.query(Lawyer).filter(Lawyer.rut == rut).first()
        if lawyer is None:
            lawyer = Lawyer(
                rut=rut,
                name=name or rut,
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(lawyer)
            db.flush()
            print(f"  Lawyer created : id={lawyer.id}  rut={rut}")
        else:
            print(f"  Lawyer found   : id={lawyer.id}  rut={rut}")
        lawyer_id = int(lawyer.id)

        # 2. Attach scraper to real Chrome over CDP --------------------------
        await sc.attach_cdp(CDP_URL, session)
        print(f"  Attached to Chrome: {sc._page.url}")

        # 3. Fetch cases from PJUD -------------------------------------------
        api_cases = await sc.get_my_cases(session, max_pages=max_pages)
        print(f"  get_my_cases → {len(api_cases)} cases")

        # 4. Sync case rows --------------------------------------------------
        # sync_cases calls db.commit() internally; inside begin_nested() that
        # releases the SAVEPOINT and keeps the outer transaction open.
        # get_my_cases returns PJUDCase dataclasses (attributes), NOT dicts, so
        # build ScrapedCase by attribute (convert_api_cases_to_scraped expects dicts).
        scraped = [
            ScrapedCase(
                rol=c.rol,
                tribunal=c.tribunal,
                caratulado=c.caratulado,
                fecha_ingreso=c.fecha_ingreso,
                estado_cuaderno=c.estado_cuaderno or "",
                cuaderno=c.cuaderno or "",
                institucion=c.institucion,
            )
            for c in api_cases
        ]
        sync = SyncService(db)
        sync_result = sync.sync_cases(lawyer_id, scraped, competencia="civil")
        print(
            f"  sync_cases → total={sync_result.cases_total}  "
            f"new={sync_result.cases_new}  updated={sync_result.cases_updated}  "
            f"errors={len(sync_result.errors)}"
        )

        # 5. Detail / movement sync ------------------------------------------
        selected = _select_cases_for_detail_rotation(db, lawyer_id, "civil", api_cases, batch, reserved_first=reserved_first)
        print(f"  selected for detail rotation: {len(selected)} cases")
        created, updated, errors = await detect_and_sync_movements(
            db=db,
            scraper=sc,
            pjud_session=session,
            lawyer_id=lawyer_id,
            api_cases=api_cases,
            selected_cases=selected,
            delay_between_fetches=2.0,
        )

        # 6. Commit or roll back --------------------------------------------
        if dry_run:
            print("\n  DRY RUN — will roll back (nothing persists)")
        else:
            db.commit()
            print("\n  COMMITTED — persisted ✓")

        # 7. Report ----------------------------------------------------------
        print(f"\n{'='*60}")
        print(f"  REPORT  (dry_run={dry_run})")
        print(f"    lawyer_id         : {lawyer_id}")
        print(f"    api_cases fetched : {len(api_cases)}")
        print(f"    cases new         : {sync_result.cases_new}")
        print(f"    cases updated     : {sync_result.cases_updated}")
        print(f"    selected detail   : {len(selected)}")
        print(f"    movements created : {created}")
        print(f"    movements updated : {updated}")
        print(f"    movement errors   : {len(errors)}")
        if errors:
            for err in errors[:5]:
                print(f"      - {err}")
        print(f"{'='*60}\n")

    finally:
        await sc.stop()   # CDP-aware: disconnects, leaves real Chrome open
        db.close()
        if dry_run and _outer is not None:
            _outer.rollback()   # undo EVERYTHING — canonical dry-run
            _conn.close()


async def main() -> int:
    rut = os.environ.get("TEST_ABOGADO_RUT", "").strip()
    if not rut:
        print("ERROR: TEST_ABOGADO_RUT not set (the lawyer's RUT).")
        return 2

    # PERSIST=1 → run the full DB sync instead of the read-only scrape check.
    # DRY_RUN defaults to "1" (safe); set DRY_RUN=0 to actually commit.
    if os.environ.get("PERSIST", "").strip() == "1":
        dry_run = os.environ.get("DRY_RUN", "1").strip() != "0"
        try:
            await persist_via_cdp(
                rut=rut, name=os.environ.get("TEST_ABOGADO_NAME", ""), dry_run=dry_run
            )
        except Exception as e:
            print(f"persist_via_cdp failed: {type(e).__name__}: {str(e)[:160]}")
            return 1
        return 0

    try:
        cases = await scrape_via_cdp(rut)
    except Exception as e:
        print(f"scrape_via_cdp failed: {type(e).__name__}: {str(e)[:160]}")
        return 1

    print(f"\n{'='*58}")
    print(f"  get_my_cases via CDP → {len(cases)} cases")
    for c in cases[:10]:
        print(f"    • {c.rol:<18} {(c.caratulado or '')[:50]}")
    print("=" * 58)
    if cases:
        print("  ✅ SCRAPER-OVER-CDP WORKS — reusing existing logic in the real Chrome.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
