"""Backfill ``documents.texto`` (full-text search) for already-stored PDFs.

Slice 1 extracts text for NEW documents at download time. This one-shot,
resumable script fills in the backlog: every document that is already
``status='stored'`` but has no extracted text yet.

Standalone — own engine from ``DATABASE_URL`` (like scripts/freshness_monitor.py),
no app-server import. For each eligible doc it retrieves the stored PDF bytes via
the configured storage backend, runs ``extraer_texto_pdf``, and writes
``texto`` + ``text_extracted_at``. Commits per batch. Resumable: the
``texto IS NULL`` filter means a re-run only touches docs still missing text.

Failures are isolated (logged + counted, never abort the run). Keyset pagination
by id guarantees forward progress even when a doc yields empty/NULL text.

Usage:
    poetry run python scripts/backfill_document_text.py            # full backfill
    poetry run python scripts/backfill_document_text.py --limit 50 # test run
    poetry run python scripts/backfill_document_text.py --batch-size 100
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.document import Document
from app.services.pdf_text import extraer_texto_pdf
from app.services.storage_service import get_storage_backend


def _base_query(session):
    return session.query(Document).filter(
        Document.status == "stored",
        Document.texto.is_(None),
        Document.gcs_path.isnot(None),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill documents.texto for FTS")
    parser.add_argument(
        "--limit", type=int, default=None, help="Max docs to process (test run)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=200, help="Docs committed per batch"
    )
    args = parser.parse_args()

    db_url = getattr(settings, "DATABASE_URL", None) or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        return 2

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    backend = get_storage_backend(settings)

    total = _base_query(session).with_entities(func.count(Document.id)).scalar() or 0
    if args.limit is not None:
        total = min(total, args.limit)
    print(f"[backfill] {total} document(s) to process (batch={args.batch_size})")

    seen = 0
    ok = 0
    failed = 0
    last_id = 0

    while True:
        if args.limit is not None and seen >= args.limit:
            break
        remaining = args.batch_size
        if args.limit is not None:
            remaining = min(remaining, args.limit - seen)
        if remaining <= 0:
            break

        batch = (
            _base_query(session)
            .filter(Document.id > last_id)
            .order_by(Document.id)
            .limit(remaining)
            .all()
        )
        if not batch:
            break

        for doc in batch:
            last_id = doc.id
            seen += 1
            try:
                data = backend.retrieve(doc.gcs_path)
                doc.texto = extraer_texto_pdf(data) or None
                doc.text_extracted_at = datetime.utcnow()
                ok += 1
            except Exception as exc:  # noqa: BLE001 — isolate per-doc failures
                failed += 1
                print(f"  skip doc {doc.id} ({doc.gcs_path}): {exc}", file=sys.stderr)

        session.commit()
        print(f"[backfill] {seen}/{total}  (ok={ok}, failed={failed})")

    session.close()
    print(f"[backfill] done — processed {seen}, extracted {ok}, failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
