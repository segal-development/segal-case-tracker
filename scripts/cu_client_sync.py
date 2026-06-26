"""Per-client Clave Única sync runner.

PAUSE Carla before running — one PJUD session per IP.

Iterates active clients that have a stored Clave Única credential and an
assigned firm lawyer. Logs in as each client, fetches their PJUD Mis Causas,
and attributes the cases to their assigned lawyer.

Env vars:
  DRY_RUN   "1" (default) = nothing persists; "0" = real persist
  LIMIT     max clients to process per run (default 50)
  CLIENT    optional client rut — process just that one client

Run (dry-run by default):
    PYTHONPATH=. <venv> python scripts/cu_client_sync.py
Real sync:
    DRY_RUN=0 PYTHONPATH=. <venv> python scripts/cu_client_sync.py
Single client:
    CLIENT=22222222-2 DRY_RUN=0 PYTHONPATH=. <venv> python scripts/cu_client_sync.py
"""
import asyncio
import logging
import os

os.environ.setdefault("ENVIRONMENT", "production")

DRY_RUN: bool = os.environ.get("DRY_RUN", "1").strip() != "0"
LIMIT: int = int(os.environ.get("LIMIT", "50"))
CLIENT_ONLY: str | None = os.environ.get("CLIENT", "").strip() or None

logger = logging.getLogger(__name__)


def _select_cu_clients(db, only: str | None = None) -> list:
    """Return active clients that have a stored Clave Única and an assigned lawyer.

    If `only` is set (rut), returns just that one client if eligible.
    """
    from app.models.client import Client

    if only is not None:
        target = db.query(Client).filter(Client.rut == only).first()
        if target is None:
            logger.warning("_select_cu_clients: no client found for rut=%r", only)
            return []
        if not target.encrypted_clave_unica_password:
            logger.warning(
                "_select_cu_clients: client %r has no Clave Única stored — skipping",
                only,
            )
            return []
        if target.assigned_lawyer_id is None:
            logger.warning(
                "_select_cu_clients: client %r has no assigned_lawyer_id — skipping",
                only,
            )
            return []
        if not target.is_active:
            logger.warning(
                "_select_cu_clients: client %r is inactive — skipping",
                only,
            )
            return []
        return [target]

    return (
        db.query(Client)
        .filter(
            Client.is_active.is_(True),
            Client.encrypted_clave_unica_password.isnot(None),
            Client.assigned_lawyer_id.isnot(None),
        )
        .order_by(Client.id)
        .limit(LIMIT)
        .all()
    )


async def main() -> int:
    from sqlalchemy.orm import Session as SASession

    from app.core.database import SessionLocal, engine
    from app.scrapper.pjud.civil import CivilScraper
    from app.services.client_sync import sync_one_cu_client

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    dry_run = DRY_RUN

    # Resolve the client list using a short-lived query session.
    db_query = SessionLocal()
    try:
        clients = _select_cu_clients(db_query, CLIENT_ONLY)
    finally:
        db_query.close()

    if not clients:
        print("No eligible clients found — nothing to do.")
        return 0

    print(
        f"CU client sync · {len(clients)} client(s) · dry_run={dry_run}"
    )

    results: list[tuple[str, str]] = []

    for client in clients:
        label = f"{client.nombre or client.rut} (rut={client.rut})"
        print(f"\n{'#' * 62}\n#  {label}\n{'#' * 62}")

        sc: CivilScraper | None = None

        try:
            sc = CivilScraper(headless=False)
            sc.reuse_context = True

            if dry_run:
                _conn = engine.connect()
                _outer = _conn.begin()
                db = SASession(bind=_conn, join_transaction_mode="create_savepoint")
            else:
                db = SessionLocal()
                _conn = _outer = None

            try:
                created, updated, errors = await sync_one_cu_client(
                    sc, db, client, batch=200
                )

                if dry_run:
                    print("  DRY RUN — rolled back (nothing persisted)")
                else:
                    db.commit()
                    print("  COMMITTED ✓")

                status = f"ok  movements+{created} updated={updated} errors={errors}"
                results.append((label, status))
                print(f"  {label}: {status}")

            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                msg = (
                    f"failed for {label}: "
                    f"{type(exc).__name__}: {str(exc)[:120]}"
                )
                print(f"  {msg}")
                try:
                    from scripts.freshness_monitor import send_telegram
                    send_telegram(f"⚠️ CU Client Sync — {msg}")
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
            if sc is not None:
                try:
                    await sc.stop()
                except Exception:
                    pass

    # Summary.
    print(f"\n{'=' * 62}\n  CU CLIENT SYNC SUMMARY (dry_run={dry_run})")
    for label, status in results:
        print(f"    • {label}: {status}")
    print("=" * 62)

    # End-of-run Telegram alert for any failures.
    failed = [(lbl, st) for lbl, st in results if not st.startswith("ok")]
    if failed:
        body = "\n".join(f"• {lbl}: {st}" for lbl, st in failed)
        try:
            from scripts.freshness_monitor import send_telegram
            send_telegram(
                f"⚠️ CU Client Sync: {len(failed)}/{len(results)} con errores "
                f"(revisar / reintentar):\n{body}"
            )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
