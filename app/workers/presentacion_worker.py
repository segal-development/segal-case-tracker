"""Presentación worker (Slice 2a — SAFE SCAFFOLDING, no PJUD yet).

Standalone, station-side async worker that drains the ``presentaciones`` queue
built in Slice 1: it claims the oldest ``estado="en_cola"`` filing, downloads
its documents to temporary files, and hands them to :func:`presentar`.

CRITICAL — this slice is scaffolding only:

- It is gated by ``PRESENTACION_WORKER_ENABLED`` (default **OFF**). While OFF the
  worker only logs the current queue depth and claims/processes NOTHING.
- :func:`presentar` is a **Slice-2b STUB** that raises ``NotImplementedError`` —
  there is NO Playwright, NO browser, NO PJUD/OJV call anywhere in this module.
- No LaunchAgent is installed and this is NOT wired into the FastAPI lifespan.

Session discipline mirrors ``sync_scheduler``: every loop iteration opens a
fresh, short-lived ``SessionLocal()`` and closes it before sleeping. An idle
session must NEVER be held across the poll sleep — Cloud SQL / the auth proxy
silently kills idle connections and the next query then crashes the worker.

Usage:
    # Run as standalone process
    python -m app.workers.presentacion_worker
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.database import SessionLocal
from app.models.presentacion import (
    PRES_EN_COLA,
    PRES_ERROR,
    PRES_PRESENTANDO,
    Presentacion,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("presentacion_worker")

# Per-request timeout for document downloads (seconds).
_DOWNLOAD_TIMEOUT = 60.0


class TransientPresentacionError(Exception):
    """A retryable failure (e.g. a document download network blip).

    Raising this from :func:`_download_documents` or :func:`presentar` tells
    :func:`process_one` to put the filing BACK to ``en_cola`` for a later retry
    (until ``PRESENTACION_MAX_INTENTOS`` is reached), rather than marking it a
    hard ``error``.
    """


async def _download_documents(payload: dict) -> dict:
    """Download the filing's documents to temporary files.

    Fetches ``documento_principal.url`` and each ``documentos[].url`` from the
    ``payload`` via ``httpx.AsyncClient`` (GET, following redirects) to
    ``NamedTemporaryFile`` paths, preserving each entry's metadata.

    Any network/HTTP failure is treated as TRANSIENT and re-raised as
    :class:`TransientPresentacionError` so the caller can retry the filing.

    The CALLER owns cleanup of the returned temp files (see
    :func:`process_one`'s ``finally``).

    Returns a dict shaped like::

        {"documento_principal": {"path": str, "referencia": str},
         "documentos": [{"path": str, "referencia": str, "tipo": str,
                         "cantidad": ..., "original_papel": ...}, ...]}
    """
    import tempfile

    result: dict = {"documento_principal": None, "documentos": []}

    async def _fetch(client: httpx.AsyncClient, url: str) -> str:
        """GET ``url`` into a NamedTemporaryFile; return its path."""
        resp = await client.get(url)
        resp.raise_for_status()
        # delete=False so the file survives past this function; the caller
        # removes it once the filing has been processed.
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        try:
            tmp.write(resp.content)
        finally:
            tmp.close()
        return tmp.name

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT
        ) as client:
            principal = payload.get("documento_principal") or {}
            if principal.get("url"):
                path = await _fetch(client, principal["url"])
                result["documento_principal"] = {
                    "path": path,
                    "referencia": principal.get("referencia"),
                }

            for doc in payload.get("documentos") or []:
                if not doc.get("url"):
                    continue
                path = await _fetch(client, doc["url"])
                result["documentos"].append(
                    {
                        "path": path,
                        "referencia": doc.get("referencia"),
                        "tipo": doc.get("tipo"),
                        "cantidad": doc.get("cantidad"),
                        "original_papel": doc.get("original_papel"),
                    }
                )
    except httpx.HTTPError as exc:
        # Any download/HTTP error is transient — the network blipped, the
        # storage signed-URL expired, etc. Let the caller retry the filing.
        raise TransientPresentacionError(f"document download failed: {exc}") from exc

    return result


async def presentar(presentacion: Presentacion, files: dict) -> dict:
    """Present the filing at the OJV/PJUD — **Slice-2b STUB, raises for now**.

    Slice 2b implements the real Playwright flow: login → ingreso (Competencia/
    Tribunal/Rol/Año/Tipo) → adjuntar documentos → Bandeja. For ``modo="semiauto"``
    it stops at ``cargada_pendiente_envio`` (a human sends from the Bandeja);
    for ``modo="auto"`` it sends and returns the comprobante fields.

    On success it must return a dict like::

        {"estado": <final estado>, "rol_asignado": ..., "ruc": ...,
         "numero_identificador": ..., "fecha_envio": datetime,
         "certificado_url": ...}

    For now there is NO automation — it just raises.
    """
    raise NotImplementedError(
        "Slice 2b: automatización OJV/PJUD no implementada todavía"
    )


def claim_next(db: Session) -> Optional[Presentacion]:
    """Atomically claim the oldest queued filing, or return None if empty.

    Selects the oldest ``estado == en_cola`` row (``created_at`` then ``id``),
    flips it to ``presentando``, increments ``intentos``, stamps ``updated_at``,
    commits, and returns it. Returns None when the queue is empty.
    """
    presentacion = (
        db.query(Presentacion)
        .filter(Presentacion.estado == PRES_EN_COLA)
        .order_by(Presentacion.created_at.asc(), Presentacion.id.asc())
        .first()
    )
    if presentacion is None:
        return None

    presentacion.estado = PRES_PRESENTANDO
    presentacion.intentos = (presentacion.intentos or 0) + 1
    presentacion.updated_at = datetime.utcnow()
    db.commit()
    return presentacion


async def process_one(
    db: Session,
    presentacion: Presentacion,
    presentar_fn=presentar,
) -> None:
    """Process a single claimed filing end to end. Never lets an exception escape.

    Downloads the documents, calls ``presentar_fn`` and applies its outcome:

    - success            → set the returned ``estado`` + any result fields, commit.
    - TransientPresentacionError → retry: back to ``en_cola`` while
      ``intentos < PRESENTACION_MAX_INTENTOS``, else ``error``.
    - any other Exception (incl. ``NotImplementedError`` from the stub)
      → ``error`` with ``error_detail = str(e)[:1024]``, commit.

    Temp files downloaded for this filing are always removed in a ``finally``.
    """
    files: dict = {}
    try:
        files = await _download_documents(presentacion.payload or {})

        result = await presentar_fn(presentacion, files)

        # Success — apply the final estado and any comprobante/outcome fields.
        presentacion.estado = result.get("estado", presentacion.estado)
        for field in (
            "rol_asignado",
            "ruc",
            "numero_identificador",
            "fecha_envio",
            "certificado_url",
        ):
            if field in result and result[field] is not None:
                setattr(presentacion, field, result[field])
        presentacion.updated_at = datetime.utcnow()
        db.commit()

    except TransientPresentacionError as exc:
        # Retryable: put it back in the queue unless we've exhausted attempts.
        if (presentacion.intentos or 0) < settings.PRESENTACION_MAX_INTENTOS:
            presentacion.estado = PRES_EN_COLA
            logger.warning(
                "Presentación %s transient failure (intento %s/%s), re-encolada: %s",
                presentacion.id,
                presentacion.intentos,
                settings.PRESENTACION_MAX_INTENTOS,
                exc,
            )
        else:
            presentacion.estado = PRES_ERROR
            presentacion.error_detail = str(exc)[:1024]
            logger.error(
                "Presentación %s agotó los %s intentos: %s",
                presentacion.id,
                settings.PRESENTACION_MAX_INTENTOS,
                exc,
            )
        presentacion.updated_at = datetime.utcnow()
        db.commit()

    except Exception as exc:  # incl. NotImplementedError from the Slice-2b stub
        presentacion.estado = PRES_ERROR
        presentacion.error_detail = str(exc)[:1024]
        presentacion.updated_at = datetime.utcnow()
        logger.error("Presentación %s falló: %s", presentacion.id, exc)
        db.commit()

    finally:
        # Always clean up the temp files this filing downloaded.
        _cleanup_files(files)


def _cleanup_files(files: dict) -> None:
    """Best-effort removal of the temp files returned by _download_documents."""
    if not files:
        return
    paths = []
    principal = files.get("documento_principal")
    if principal and principal.get("path"):
        paths.append(principal["path"])
    for doc in files.get("documentos") or []:
        if doc.get("path"):
            paths.append(doc["path"])
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


async def run_once(db: Session) -> None:
    """Run one poll cycle against ``db``.

    When ``PRESENTACION_WORKER_ENABLED`` is False the worker is inert: it only
    logs the current queue depth and returns WITHOUT claiming anything. When
    enabled it claims the next filing (if any) and processes it.
    """
    if not settings.PRESENTACION_WORKER_ENABLED:
        pending = (
            db.query(Presentacion)
            .filter(Presentacion.estado == PRES_EN_COLA)
            .count()
        )
        logger.info(
            "%d presentaciones en cola; worker deshabilitado", pending
        )
        return

    presentacion = claim_next(db)
    if presentacion is not None:
        await process_one(db, presentacion)


async def run_loop() -> None:
    """Poll the queue forever, one fresh short-lived session per iteration.

    Each iteration opens a new ``SessionLocal()``, runs ``run_once``, and closes
    it BEFORE sleeping — an idle session is never held across the poll sleep
    (that idle-connection bug was just fixed in ``sync_scheduler``; do not
    reintroduce it here).
    """
    logger.info(
        "Presentación worker started; enabled=%s; poll=%ss",
        settings.PRESENTACION_WORKER_ENABLED,
        settings.PRESENTACION_POLL_INTERVAL_SECONDS,
    )
    while True:
        db = SessionLocal()
        try:
            await run_once(db)
        except Exception:  # a poll failure must never kill the loop
            logger.exception("Presentación poll failed (non-fatal)")
        finally:
            db.close()
        await asyncio.sleep(settings.PRESENTACION_POLL_INTERVAL_SECONDS)


async def main() -> None:
    """Run the presentación worker as a standalone process."""
    await run_loop()


if __name__ == "__main__":
    asyncio.run(main())
