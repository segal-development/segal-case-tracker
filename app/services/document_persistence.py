"""Document persistence service — token capture and DB upsert (Slice 1).

Responsibilities (Slice 1):
- document_identity_hash: stable business identity key (ADR-1)
- DocumentPersistenceService: upsert Document rows from PJUDCaseDetail

Out of scope (Slice 2): StorageService, downloads, GCS.
"""

import hashlib
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _parse_movement_date(value: Optional[str]) -> Optional[datetime]:
    """Parse a PJUD movement ``fecha`` (``DD/MM/YYYY``) into a ``datetime``.

    Returns ``None`` when the value is missing or unparseable — a missing
    document_date is tolerable (it is metadata, not identity).
    """
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y")
    except (ValueError, AttributeError):
        return None


def document_identity_hash(
    doc_type: str,
    case_rol: str,
    scope_key: str = "",
) -> str:
    """Compute a stable 64-char hex identity for a document slot.

    ADR-1: Idempotency key = sha256(doc_type|case_rol|scope_key).
    The JWT (pjud_token) is NOT included — it changes every sync due to
    per-load iat/exp claims and rotating encrypted data.  Using the JWT
    as the key would force a re-download on every sync even if the document
    has not changed.

    Args:
        doc_type:  Normalized document type, e.g. "resolution", "cert_envio".
        case_rol:  Case ROL, e.g. "C-0001-2026".
        scope_key: Movement natural key (folio) for movement-level docs;
                   empty string for case-level docs.

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    raw = f"{doc_type}|{case_rol}|{scope_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


class DocumentPersistenceService:
    """Upsert Document rows for all parsed document tokens in a PJUDCaseDetail.

    Idempotency: keyed on pjud_token_hash (stable identity, not the JWT).
    - Already stored → skip.
    - Pending/failed → refresh pjud_token (JWT rotated, status unchanged).
    - Not found → insert with status=pending (or unavailable when doc.available=False).

    Does NOT commit — caller owns the transaction.
    """

    def persist_from_detail(
        self,
        detail: "PJUDCaseDetail",  # type: ignore[name-defined]  # noqa: F821
        case_id: int,
        db: Session,
    ) -> List["Document"]:  # type: ignore[name-defined]  # noqa: F821
        """Upsert all Document rows from parsed case detail.

        Iterates:
        - detail.case_documents  (texto_demanda, cert_envio, ebook)
        - movement.documentos for each movement in detail.movements

        Also sets Movement.document_token = pjud_movement.documento_token
        (the bug fix: tokens were parsed but never written to the DB).

        Returns list of created/updated Document ORM objects.
        """
        from app.models.document import Document
        from app.models.movement import Movement

        case_rol = detail.case.rol
        results: List[Document] = []

        # Flush any pending movement/entity changes from earlier in this case's sync
        # into the OUTER transaction BEFORE the document savepoint opens, so a
        # document-phase failure can only roll back document work — never the
        # movement/entity upserts. (This also means the prefetch SELECTs below have
        # nothing pending to autoflush, so an autoflush failure can't escape the
        # savepoint uncaught.) If this flush fails it is bad movement/entity data,
        # not a document problem, so let it propagate to the caller's handler.
        db.flush()

        # Prefetch this case's existing rows ONCE (N+1 → 1). Over Cloud SQL from a
        # residential IP each round-trip is ~135ms, and this loop otherwise issued a
        # SELECT per document and per movement. Key documents by their stable
        # token_hash (which embeds case_rol, so it's already case-scoped) and
        # movements by folio, exactly matching the per-item lookups below.
        existing_docs: dict = {
            d.pjud_token_hash: d
            for d in db.query(Document).filter(Document.case_id == case_id).all()
        }
        movements_by_folio: dict = {
            m.folio: m
            for m in db.query(Movement).filter(Movement.case_id == case_id).all()
        }

        # ONE savepoint around the whole document phase (was one per document = a
        # SAVEPOINT/RELEASE round-trip each). A failure here rolls back only document
        # rows and the per-movement document_token writes below — the movement/entity
        # upserts were already flushed to the outer transaction above and stay
        # healthy. Documents are non-critical and retried next cycle. The prefetch
        # also removes the main flush-failure cause (duplicate token_hash) since
        # inserts update the in-memory map.
        savepoint = db.begin_nested()
        try:
            # ------------------------------------------------------------
            # 1. Case-level documents (texto_demanda, cert_envio, ebook)
            # ------------------------------------------------------------
            for pjud_doc in detail.case_documents:
                if not pjud_doc.doc_type:
                    continue
                token_hash = document_identity_hash(
                    pjud_doc.doc_type,
                    case_rol,
                    scope_key="",
                )
                doc = self._upsert_document(
                    db=db,
                    case_id=case_id,
                    movement_id=None,
                    pjud_doc=pjud_doc,
                    token_hash=token_hash,
                    existing_by_hash=existing_docs,
                )
                if doc is not None:
                    results.append(doc)

            # ------------------------------------------------------------
            # 2. Movement-level documents
            # ------------------------------------------------------------
            for pjud_movement in detail.movements:
                # In-memory lookup (prefetched above) instead of a per-movement SELECT.
                db_movement: Optional[Movement] = movements_by_folio.get(
                    pjud_movement.folio
                )

                # Bug fix: write primary document token to Movement row. No per-item
                # flush — deferred to the single flush after the loops.
                if db_movement is not None and pjud_movement.documento_token:
                    db_movement.document_token = pjud_movement.documento_token

                movement_id = db_movement.id if db_movement is not None else None

                # Guard: skip movement docs when folio is falsy to avoid hash collision.
                # Two movements with folio=None would share the same stable_hash and
                # silently overwrite each other.
                if not pjud_movement.folio:
                    logger.warning(
                        "Skipping document persistence for movement with falsy folio "
                        "(case_id=%s, doc_count=%d): cannot build stable identity hash.",
                        case_id,
                        len(pjud_movement.documentos),
                    )
                    continue

                document_date = _parse_movement_date(pjud_movement.fecha)

                for pjud_doc in pjud_movement.documentos:
                    if not pjud_doc.doc_type:
                        continue
                    token_hash = document_identity_hash(
                        pjud_doc.doc_type,
                        case_rol,
                        scope_key=pjud_movement.folio,
                    )
                    doc = self._upsert_document(
                        db=db,
                        case_id=case_id,
                        movement_id=movement_id,
                        pjud_doc=pjud_doc,
                        token_hash=token_hash,
                        existing_by_hash=existing_docs,
                        document_date=document_date,
                    )
                    if doc is not None:
                        results.append(doc)

            # Single flush for all document inserts/updates + movement token writes
            # (was a flush per document/movement — one round-trip each over the proxy).
            db.flush()
            savepoint.commit()
            return results

        except Exception as exc:
            savepoint.rollback()
            logger.warning(
                "Failed to persist documents for case_id=%s: %s", case_id, exc
            )
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _upsert_document(
        db: Session,
        case_id: int,
        movement_id: Optional[int],
        pjud_doc: "PJUDDocument",  # type: ignore[name-defined]  # noqa: F821
        token_hash: str,
        existing_by_hash: dict,
        document_date: Optional[datetime] = None,
    ) -> Optional["Document"]:  # type: ignore[name-defined]  # noqa: F821
        """INSERT-or-UPDATE a single Document row using a prefetched existence map.

        ``existing_by_hash`` ({token_hash: Document}) is prefetched once by the
        caller so this is an in-memory lookup, not a SELECT per document. Fault
        isolation (savepoint) and flushing are handled once by the caller around the
        whole document phase, so this method neither flushes nor swallows errors.

        Idempotency rules:
        - status=stored → skip entirely (already downloaded; do not overwrite)
        - status=pending or failed → refresh pjud_token (JWT rotated)
        - not found → insert

        Returns the Document ORM object (inserted or existing).
        """
        from app.models.document import Document

        existing: Optional[Document] = existing_by_hash.get(token_hash)

        if existing is not None:
            if existing.status == "stored":
                # Already downloaded — idempotent skip
                return existing
            # Refresh the live JWT; preserve status (flush deferred to caller)
            existing.pjud_token = pjud_doc.token
            return existing

        # New document row
        status = "unavailable" if not pjud_doc.available else "pending"
        doc = Document(
            case_id=case_id,
            movement_id=movement_id,
            doc_type=pjud_doc.doc_type,
            pjud_endpoint=pjud_doc.endpoint,
            pjud_token=pjud_doc.token,
            pjud_token_hash=token_hash,
            status=status,
            document_date=document_date,
        )
        db.add(doc)
        # Keep the map consistent so a duplicate token_hash later in the same detail
        # resolves to this just-added row instead of a second INSERT (unique on hash).
        existing_by_hash[token_hash] = doc
        return doc
