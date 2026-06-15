"""Backfill full case detail (movements + entities + documents) for every case
that has not been detail-scraped yet, using the live PJUD session in Redis.

Manual-session model (no 2Captcha): seed a session via POST /pjud/login first,
then run this inside the api container:

    docker compose -f docker-compose.qa.yml exec -T api python scripts/backfill_detail.py

It fetches the case list (for the per-case tokens), then scrapes the detail of
ALL not-yet-checked cases until the PJUD session expires (~25 min), committing
and marking each case as it goes. RESUMABLE: re-run after a fresh login and it
continues with whatever is still unchecked.
"""
import asyncio
import os

os.environ.setdefault("ENVIRONMENT", "production")

LAWYER_RUT = "16021492-9"
COMPETENCIA = "civil"


async def main() -> None:
    from app.services.session_store import get_session_store
    from app.api.v1.pjud import get_scraper
    from app.core.database import SessionLocal
    from app.models.case import Case
    from app.models.lawyer import Lawyer
    from app.services.sync_service import (
        detect_and_sync_movements,
        _select_cases_for_detail_rotation,
    )
    from app.scrapper.pjud.base import SessionExpiredError, SessionNotAuthenticatedError

    db = SessionLocal()
    lawyer = db.query(Lawyer).filter(Lawyer.rut == LAWYER_RUT).first()
    if not lawyer:
        print(f"No lawyer for rut {LAWYER_RUT} — run a sync/login first.")
        return
    lawyer_id = int(lawyer.id)

    session = await get_session_store().get_session_by_lawyer(lawyer_id)
    if not session:
        print("NO live PJUD session — seed one via POST /pjud/login, then re-run.")
        return

    def coverage() -> tuple[int, int]:
        total = (
            db.query(Case)
            .filter(Case.lawyer_id == lawyer_id, Case.competencia == COMPETENCIA)
            .count()
        )
        remaining = (
            db.query(Case)
            .filter(
                Case.lawyer_id == lawyer_id,
                Case.competencia == COMPETENCIA,
                Case.last_detail_checked_at.is_(None),
            )
            .count()
        )
        return total, remaining

    total, remaining_before = coverage()
    print(f"coverage before: {total - remaining_before}/{total} checked, {remaining_before} remaining")

    sc = get_scraper(COMPETENCIA)
    print("fetching case list (per-case tokens)...")
    api_cases = await sc.get_my_cases(session=session, max_pages=0)
    print(f"  list size: {len(api_cases)}")

    # Select ALL still-unchecked cases (big batch); process until the session dies.
    batch = _select_cases_for_detail_rotation(
        db, lawyer_id, COMPETENCIA, api_cases, batch_size=100000
    )
    print(f"processing up to {len(batch)} unchecked cases this session...")

    created = updated = 0
    errors: list = []
    try:
        created, updated, errors = await detect_and_sync_movements(
            db=db,
            scraper=sc,
            pjud_session=session,
            lawyer_id=lawyer_id,
            api_cases=api_cases,
            selected_cases=batch,
            delay_between_fetches=1.0,
        )
        db.commit()
    except (SessionExpiredError, SessionNotAuthenticatedError) as exc:
        db.commit()
        print(f"\nSession ended mid-backfill ({exc}). Progress so far is saved.")
    finally:
        try:
            await sc.stop()
        except Exception:
            pass

    total, remaining_after = coverage()
    done_this_run = remaining_before - remaining_after
    print(f"\n=== SESSION DONE ===")
    print(f"  cases detailed this run: {done_this_run}")
    print(f"  movements created: {created}  | errors: {len(errors)}")
    print(f"  coverage after: {total - remaining_after}/{total} checked, {remaining_after} remaining")
    if remaining_after:
        print(f"  -> re-login (/pjud/login) and re-run to continue the remaining {remaining_after}.")
    else:
        print("  -> 100% of cases now have detail. ✅")
    for e in errors[:3]:
        print("  sample error:", e)


if __name__ == "__main__":
    asyncio.run(main())
