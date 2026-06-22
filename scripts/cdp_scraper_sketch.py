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


async def main() -> int:
    rut = os.environ.get("TEST_ABOGADO_RUT", "").strip()
    if not rut:
        print("ERROR: TEST_ABOGADO_RUT not set (the lawyer's RUT).")
        return 2
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
