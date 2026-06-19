"""Authenticated read-only SMOKE for the PJUD pipeline. One-shot, observable.

Runs the full authenticated path and emits a structured JSON status with a clear
exit code, so it can be scheduled and alerted on:
  login_ok · list_ok · detail_ok · parse_ok  + counts + timestamps.

Exit 0 = all checks passed. Non-zero = a check failed (1) or misconfig (2).
On failure it dumps a redacted screenshot + HTML to SMOKE_FAILURE_DIR for triage.

Headful + residential (PJUD's F5 Shape). Run with the scraper PAUSED — one
browser at a time. Env from .env.backfill (CU_RUT, CU_PASSWORD, DATABASE_URL).
"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone

os.environ.setdefault("ENVIRONMENT", "production")

LAWYER_RUT = os.environ.get("LAWYER_RUT", "16021492-9")
CU_RUT = os.environ.get("CU_RUT", "")
CU_PASSWORD = os.environ.get("CU_PASSWORD", "")
HEADLESS = os.environ.get("BACKFILL_HEADLESS", "false").lower() == "true"
FAILURE_DIR = os.environ.get("SMOKE_FAILURE_DIR", "/tmp")

_JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


def _finish(status: dict, code: int) -> int:
    status["finished_at"] = datetime.now(timezone.utc).isoformat()
    status["exit_code"] = code
    print(json.dumps(status, ensure_ascii=False))
    return code


async def main() -> int:
    status = {
        "login_ok": False, "list_ok": False, "detail_ok": False, "parse_ok": False,
        "case_count": 0, "movement_count": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None, "error": None,
    }

    from app.scrapper.pjud_civil import PJUDCivilScraper
    from app.core.database import SessionLocal
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.browser import BrowserFactory
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials

    sc = PJUDCivilScraper(headless=HEADLESS)
    sc.reuse_context = True

    async def dump_failure(tag: str) -> None:
        try:
            if sc._page and not sc._page.is_closed():
                await sc._page.screenshot(path=f"{FAILURE_DIR}/smoke_fail_{tag}.png")
                html = _JWT_RE.sub("[REDACTED_JWT]", await sc._page.content())
                with open(f"{FAILURE_DIR}/smoke_fail_{tag}.html", "w") as fh:
                    fh.write(html)
        except Exception:
            pass

    try:
        if not (CU_RUT and CU_PASSWORD):
            status["error"] = "CU_RUT/CU_PASSWORD not set"
            return _finish(status, 2)
        lawyer = SessionLocal().query(Lawyer).filter(Lawyer.rut == LAWYER_RUT).first()
        if not lawyer:
            status["error"] = f"no lawyer for {LAWYER_RUT}"
            return _finish(status, 2)
        lawyer_id = int(lawyer.id)

        # 1) login
        creds = ClaveUnicaCredentials(rut=CU_RUT, password=CU_PASSWORD)
        async with BrowserFactory(headless=HEADLESS) as factory:
            page = await factory.new_page()
            session = await ClaveUnicaAuth().login(page, creds, lawyer_id)
        status["login_ok"] = True

        # 2) list (Mis Causas)
        api_cases = await sc.get_my_cases(session=session, max_pages=0)
        status["case_count"] = len(api_cases)
        status["list_ok"] = len(api_cases) > 0

        # 3) detail on a known case (first with a token)
        target = next((c for c in api_cases if getattr(c, "case_token", None)), None)
        if target is None:
            status["error"] = "no case with a token to fetch detail"
            await dump_failure("detail")
            return _finish(status, 1)
        detail = await sc.get_case_detail(session=session, case_token=target.case_token)
        status["detail_ok"] = detail is not None

        # 4) parse (movements came back structured)
        movements = getattr(detail, "movements", []) if detail else []
        status["movement_count"] = len(movements)
        status["parse_ok"] = detail is not None and isinstance(movements, list)

        all_ok = all(status[k] for k in ("login_ok", "list_ok", "detail_ok", "parse_ok"))
        if not all_ok:
            await dump_failure("checks")
        return _finish(status, 0 if all_ok else 1)
    except Exception as e:
        status["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        await dump_failure("exception")
        return _finish(status, 1)
    finally:
        try:
            await sc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
