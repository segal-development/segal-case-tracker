"""Document persistence service — token capture and DB upsert (Slice 1).

Responsibilities (Slice 1):
- document_identity_hash: stable business identity key (ADR-1)
- DocumentPersistenceService: upsert Document rows from PJUDCaseDetail

Out of scope (Slice 2): StorageService, downloads, GCS.
"""

import hashlib
import logging
from typing import List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


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

        # ----------------------------------------------------------------
        # 1. Case-level documents (texto_demanda, cert_envio, ebook)
        # ----------------------------------------------------------------
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
            )
            if doc is not None:
                results.append(doc)

        # ----------------------------------------------------------------
        # 2. Movement-level documents
        # ----------------------------------------------------------------
        for pjud_movement in detail.movements:
            # Look up the DB movement by folio so we can set movement_id + document_token
            db_movement: Optional[Movement] = (
                db.query(Movement)
                .filter(
                    Movement.case_id == case_id,
                    Movement.folio == pjud_movement.folio,
                )
                .first()
            )

            # Bug fix: write primary document token to Movement row
            if db_movement is not None and pjud_movement.documento_token:
                db_movement.document_token = pjud_movement.documento_token
                db.flush()

            movement_id = db_movement.id if db_movement is not None else None

            for pjud_doc in pjud_movement.documentos:
                if not pjud_doc.doc_type:
                    continue
                token_hash = document_identity_hash(
                    pjud_doc.doc_type,
                    case_rol,
                    scope_key=pjud_movement.folio or "",
                )
                doc = self._upsert_document(
                    db=db,
                    case_id=case_id,
                    movement_id=movement_id,
                    pjud_doc=pjud_doc,
                    token_hash=token_hash,
                )
                if doc is not None:
                    results.append(doc)

        return results

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
    ) -> Optional["Document"]:  # type: ignore[name-defined]  # noqa: F821
        """SELECT then INSERT-or-UPDATE a single Document row.

        Idempotency rules:
        - status=stored → skip entirely (already downloaded; do not overwrite)
        - status=pending or failed → refresh pjud_token (JWT rotated)
        - not found → insert

        Returns the Document ORM object (inserted or existing), or None on
        unexpected DB error (logged; never propagated to preserve outer tx).
        """
        from app.models.document import Document

        try:
            existing: Optional[Document] = (
                db.query(Document)
                .filter(Document.pjud_token_hash == token_hash)
                .first()
            )

            if existing is not None:
                if existing.status == "stored":
                    # Already downloaded — idempotent skip
                    return existing
                # Refresh the live JWT; preserve status
                existing.pjud_token = pjud_doc.token
                db.flush()
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
            )
            db.add(doc)
            db.flush()
            return doc

        except Exception as exc:
            logger.error(
                "Failed to upsert Document (case_id=%s, doc_type=%s, hash=%s): %s",
                case_id,
                pjud_doc.doc_type,
                token_hash[:8],
                exc,
            )
            return None
