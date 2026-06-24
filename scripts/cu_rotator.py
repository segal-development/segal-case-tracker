"""CU rotator — runs on the residential Mac with Carla PAUSED (one PJUD session per IP).
Uses each lawyer's stored Clave Única to scrape their full Mis Causas including reserved cases.

Env vars:
  DRY_RUN          "1" (default) = nothing persists; "0" = persist
  LAWYER           optional rut|id — rotate just that one lawyer
  LAWYER_BATCH     number of cases per detail rotation (default 200)
  FIRM_LAWYER_RUT  exclude this rut from rotation (defaults to LAWYER_RUT, then "16021492-9")
  LAWYER_RUT       existing env used as firm rut default (like freshness_sync.py)

Run (dry-run by default — nothing persists):
    PYTHONPATH=. <venv> python scripts/cu_rotator.py
Real sync:
    DRY_RUN=0 PYTHONPATH=. <venv> python scripts/cu_rotator.py
Single lawyer:
    LAWYER=12345678-9 DRY_RUN=0 PYTHONPATH=. <venv> python scripts/cu_rotator.py
"""
import asyncio
import logging
import os

os.environ.setdefault("ENVIRONMENT", "production")

DRY_RUN: bool = os.environ.get("DRY_RUN", "1").strip() != "0"
LAWYER_ONLY: str | None = os.environ.get("LAWYER", "").strip() or None
LAWYER_BATCH: int = int(os.environ.get("LAWYER_BATCH", "200"))
# Firm rut: prefer FIRM_LAWYER_RUT, fall back to LAWYER_RUT (Carla's default).
FIRM_LAWYER_RUT: str | None = (
    os.environ.get("FIRM_LAWYER_RUT", "").strip()
    or os.environ.get("LAWYER_RUT", "16021492-9").strip()
    or None
)

logger = logging.getLogger(__name__)


def _select_cu_lawyers(db, firm_rut: str | None, only: str | None = None) -> list:
    """Return lawyers that have a stored Clave Única credential.

    - Excludes the lawyer whose rut == firm_rut (the firm account kept fresh
      by the normal Carla session — no per-lawyer CU needed).
    - If `only` is set (rut or integer id), returns just that one lawyer;
      logs a warning and returns [] if the lawyer has no CU credential stored.
    """
    from app.models.lawyer import Lawyer

    if only is not None:
        # Resolve by id or rut.
        try:
            lawyer_id = int(only)
            target = db.query(Lawyer).filter(Lawyer.id == lawyer_id).first()
        except ValueError:
            target = db.query(Lawyer).filter(Lawyer.rut == only).first()

        if target is None:
            logger.warning("_select_cu_lawyers: no lawyer found for only=%r", only)
            return []
        if not target.encrypted_clave_unica_password:
            logger.warning(
                "_select_cu_lawyers: lawyer %r has no Clave Única stored — skipping",
                only,
            )
            return []
        return [target]

    q = db.query(Lawyer).filter(Lawyer.encrypted_clave_unica_password.isnot(None))
    if firm_rut:
        q = q.filter(Lawyer.rut != firm_rut)
    return q.all()


async def main() -> int:
    from sqlalchemy.orm import Session as SASession

    from app.core.database import SessionLocal, engine
    from app.core.security import decrypt_pjud_password
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
    from app.scrapper.pjud.civil import CivilScraper
    from app.services.sync_service import (
        ScrapedCase,
        SyncService,
        _select_cases_for_detail_rotation,
        detect_and_sync_movements,
    )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    dry_run = DRY_RUN
    batch = LAWYER_BATCH

    # Resolve the lawyer list using a short-lived query session.
    db_query = SessionLocal()
    try:
        lawyers = _select_cu_lawyers(db_query, FIRM_LAWYER_RUT, LAWYER_ONLY)
    finally:
        db_query.close()

    if not lawyers:
        print("No lawyers with Clave Única found — nothing to do.")
        return 0

    print(
        f"CU rotator · {len(lawyers)} lawyer(s) · dry_run={dry_run} · batch={batch}"
    )

    results: list[tuple[str, str]] = []

    for lawyer in lawyers:
        label = f"{lawyer.name} ({lawyer.rut})"
        print(f"\n{'#' * 62}\n#  {label}\n{'#' * 62}")

        sc: CivilScraper | None = None

        try:
            cu_password = decrypt_pjud_password(lawyer.encrypted_clave_unica_password)
            clave_rut: str = lawyer.clave_unica_rut or lawyer.rut
            lawyer_id = int(lawyer.id)

            sc = CivilScraper(headless=False)
            sc.reuse_context = True

            held: dict = {"session": None}

            async def cu_login(
                _sc=sc,
                _clave_rut=clave_rut,
                _cu_password=cu_password,
                _lawyer_id=lawyer_id,
                _held=held,
            ):
                """Perform a fresh Clave Única login and wire it into the scraper."""
                if not _sc._browser:
                    await _sc.start()
                ctx = await _sc._browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                page = await ctx.new_page()
                session = await ClaveUnicaAuth().login(
                    page,
                    ClaveUnicaCredentials(rut=_clave_rut, password=_cu_password),
                    _lawyer_id,
                )
                await ctx.close()
                _sc._page = None
                _held["session"] = session
                return session

            # Initial CU login.
            try:
                session = await cu_login()
            except Exception as exc:
                msg = (
                    f"CU login failed for {label}: "
                    f"{type(exc).__name__}: {str(exc)[:120]}"
                )
                print(f"  {msg}")
                try:
                    from scripts.freshness_monitor import send_telegram
                    send_telegram(f"⚠️ CU Rotator — {msg}")
                except Exception:
                    pass
                results.append((label, f"error:{type(exc).__name__}"))
                continue

            # Per-lawyer DB session with dry-run SAVEPOINT safety
            # (mirrors persist_via_cdp in cdp_scraper_sketch.py).
            if dry_run:
                _conn = engine.connect()
                _outer = _conn.begin()
                db = SASession(bind=_conn, join_transaction_mode="create_savepoint")
            else:
                db = SessionLocal()
                _conn = _outer = None

            try:
                # Fetch full Mis Causas (max_pages=0 → all pages).
                api_cases = await sc.get_my_cases(session, max_pages=0)
                print(f"  get_my_cases → {len(api_cases)} cases")

                # Build ScrapedCase list from PJUDCase dataclass attributes.
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
                sync_result = SyncService(db).sync_cases(
                    lawyer_id, scraped, competencia="civil"
                )
                print(
                    f"  sync_cases → total={sync_result.cases_total} "
                    f"new={sync_result.cases_new} "
                    f"updated={sync_result.cases_updated} "
                    f"errors={len(sync_result.errors)}"
                )

                # Detail / movement rotation — reserved cases first.
                selected = _select_cases_for_detail_rotation(
                    db, lawyer_id, "civil", api_cases, batch, reserved_first=True
                )
                print(f"  selected for detail rotation: {len(selected)} cases")

                created, updated, errors = await detect_and_sync_movements(
                    db=db,
                    scraper=sc,
                    pjud_session=session,
                    lawyer_id=lawyer_id,
                    api_cases=api_cases,
                    selected_cases=selected,
                    delay_between_fetches=2.0,
                    reauth_callback=cu_login,
                )

                if dry_run:
                    print("  DRY RUN — rolled back (nothing persisted)")
                else:
                    db.commit()
                    print("  COMMITTED ✓")

                status = f"ok  movements+{created} updated={updated} errors={len(errors)}"
                results.append((label, status))
                print(f"  {label}: {status}")

            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                msg = (
                    f"scrape/persist failed for {label}: "
                    f"{type(exc).__name__}: {str(exc)[:120]}"
                )
                print(f"  {msg}")
                try:
                    from scripts.freshness_monitor import send_telegram
                    send_telegram(f"⚠️ CU Rotator — {msg}")
                except Exception:
                    pass
                results.append((label, f"error:{type(exc).__name__}"))

            finally:
                try:
                    db.close()
                except Exception:
                    pass
                if dry_run and _outer is not None:
                    _outer.rollback()
                    _conn.close()

        except Exception as exc:
            print(
                f"  unexpected error for {label}: "
                f"{type(exc).__name__}: {str(exc)[:120]}"
            )
            results.append((label, f"error:{type(exc).__name__}"))

        finally:
            # Close the browser between lawyers — fresh context per lawyer.
            if sc is not None:
                try:
                    await sc.stop()
                except Exception:
                    pass

    # Summary.
    print(f"\n{'=' * 62}\n  CU ROTATOR SUMMARY (dry_run={dry_run})")
    for label, status in results:
        print(f"    • {label}: {status}")
    print("=" * 62)

    # End-of-run Telegram alert for any failures (mirrors rotate_per_abogado.py).
    failed = [(lbl, st) for lbl, st in results if not st.startswith("ok")]
    if failed:
        body = "\n".join(f"• {lbl}: {st}" for lbl, st in failed)
        try:
            from scripts.freshness_monitor import send_telegram
            send_telegram(
                f"⚠️ CU Rotator: {len(failed)}/{len(results)} con errores "
                f"(revisar / reintentar):\n{body}"
            )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
