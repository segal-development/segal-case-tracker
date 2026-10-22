"""Document persistence service — token capture and DB upsert (Slice 1).

Responsibilities (Slice 1):
- document_identity_hash: stable business identity key (ADR-1)
- DocumentPersistenceService: upsert Document rows from PJUDCaseDetail

Out of scope (Slice 2): StorageService, downloads, GCS.
"""

import hashlib
import logging
from collections import Counter
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
    court_id: Optional[int] = None,
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
        court_id:  Optional qualifier for causas that share a ROL across courts.
                   ``None`` keeps the legacy formula byte-for-byte (the extension
                   ingest path relies on it); otherwise ``|court:<id>`` is appended.

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    raw = f"{doc_type}|{case_rol}|{scope_key}"
    if court_id is not None:
        raw = f"{raw}|court:{court_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _effective_hash(
    doc_type: str,
    case_rol: str,
    scope_key: str,
    case_id: int,
    court_id: Optional[int],
    existing_docs: dict,
    case_courts: dict,
) -> str:
    """Resolve the identity hash a document of ``case_id`` must be stored under.

    The legacy hash is kept unless a Document that belongs to ANOTHER case in a
    DIFFERENT court already owns it (same ROL, different causa): then the
    court-qualified hash is used. Documents already owned by this case, not stored
    yet, or owned by a sibling Case row of the same court (the per-lawyer duplicate
    rows, still shared on purpose until they are merged) keep the legacy hash, so
    existing rows are never rehashed. Pure in-memory lookup on the prefetched maps
    (``case_courts`` is ``{case_id: court_id}`` for the prefetched owners).
    """
    legacy = document_identity_hash(doc_type, case_rol, scope_key)
    owner = existing_docs.get(legacy)
    if owner is None or owner.case_id == case_id or court_id is None:
        return legacy
    owner_court = case_courts.get(owner.case_id)
    if owner_court is None or owner_court == court_id:
        return legacy
    return document_identity_hash(doc_type, case_rol, scope_key, court_id=court_id)


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
        escrito_rows: Optional[list] = None,
    ) -> List["Document"]:  # type: ignore[name-defined]  # noqa: F821
        """Upsert all Document rows from parsed case detail.

        Iterates:
        - detail.case_documents  (texto_demanda, cert_envio, ebook)
        - movement.documentos for each movement in detail.movements
        - detail.escritos that carry a document (linked via ``Document.escrito_id``)

        ``escrito_rows`` is the ``(CaseEscrito, is_new)`` list that
        ``_sync_entities(..., SPEC_ESCRITO)`` already returned for this case. Passing
        it saves a SELECT; when omitted the case's escritos are queried (only if the
        detail actually has an escrito with a document).

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

        # Prefetch existing rows ONCE (N+1 → 1). Over Cloud SQL from a residential IP
        # each round-trip is ~135ms, and this loop otherwise issued a SELECT per
        # document and per movement.
        #
        # Documents MUST be prefetched by pjud_token_hash GLOBALLY, not by case_id:
        # ix_documents_pjud_token_hash is a global unique index, and the same PJUD
        # rol can appear under multiple lawyers (uq_cases_lawyer_rol is unique per
        # (lawyer, rol), not per rol) → multiple Case rows sharing a token_hash. A
        # case_id-scoped prefetch would miss a sibling case's already-persisted row
        # and then INSERT a duplicate token_hash, rolling back the whole document
        # phase. Compute the detail's token_hashes up front (pure Python) and fetch
        # exactly those rows — mirroring the old per-doc SELECT, which filtered on
        # token_hash alone.
        #
        # The same ROL can also belong to different causas (different courts): those
        # hold their documents under a court-qualified hash (see ``_effective_hash``),
        # so BOTH variants are prefetched in the same single query.
        from app.models.case import Case

        # identity-map hit in the sync path (the Case is already loaded): no SELECT.
        case_row = db.get(Case, case_id) if case_id is not None else None
        court_id = case_row.court_id if case_row is not None else None
        wanted_hashes: set = set()

        def _want(doc_type: str, scope_key: str) -> None:
            wanted_hashes.add(document_identity_hash(doc_type, case_rol, scope_key))
            if court_id is not None:
                wanted_hashes.add(
                    document_identity_hash(
                        doc_type, case_rol, scope_key, court_id=court_id
                    )
                )

        for pjud_doc in detail.case_documents:
            if pjud_doc.doc_type:
                _want(pjud_doc.doc_type, "")
        for pjud_movement in detail.movements:
            if not pjud_movement.folio:
                continue
            for pjud_doc in pjud_movement.documentos:
                if pjud_doc.doc_type:
                    _want(pjud_doc.doc_type, pjud_movement.folio)
        # Escritos live in their own PJUD pane and have no folio, so their identity
        # is scoped by the escrito's own natural key. The same PDF arriving later
        # as a movement (once the court resolves the escrito) hashes under the
        # movement's folio instead, so it is a separate row: there is no key the
        # two panes share.
        escrito_docs = self._escritos_with_documents(detail, case_id, db, escrito_rows)
        for escrito_row, pjud_doc, _scope in escrito_docs:
            _want(pjud_doc.doc_type, _scope)
        # The owner's court rides on the same query (join), so telling a sibling row
        # of the same court from a different causa costs no extra round-trip.
        existing_docs: dict = {}
        case_courts: dict = {}
        if wanted_hashes:
            for d, owner_court in (
                db.query(Document, Case.court_id)
                .outerjoin(Case, Case.id == Document.case_id)
                .filter(Document.pjud_token_hash.in_(wanted_hashes))
                .all()
            ):
                existing_docs[d.pjud_token_hash] = d
                case_courts[d.case_id] = owner_court
        # Movements ARE case-scoped (a movement row belongs to exactly one case), so
        # the folio lookup is correctly scoped by case_id.
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
                token_hash = _effective_hash(
                    pjud_doc.doc_type,
                    case_rol,
                    "",
                    case_id,
                    court_id,
                    existing_docs,
                    case_courts,
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

            # Escrito-linked rows a movement may adopt (in memory; see
            # _adopt_escrito_document). Built once, before the movement loop.
            adoptable: dict = {}
            for d in existing_docs.values():
                if (
                    d.doc_type == "escrito_doc"
                    and d.escrito_id is not None
                    and d.movement_id is None
                    and d.case_id == case_id
                    and d.document_date is not None
                    and d not in adoptable.setdefault(d.document_date.date(), [])
                ):
                    adoptable[d.document_date.date()].append(d)

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
                # Only when the column is still empty: the JWT rotates on every
                # PJUD load, so an unconditional write dirtied EVERY movement
                # with a document on EVERY pass (one UPDATE round-trip each,
                # ~137 ms over Cloud SQL). Nothing reads this column; the live
                # JWT used to download a document is Document.pjud_token, which
                # is still refreshed below.
                if (
                    db_movement is not None
                    and pjud_movement.documento_token
                    and not db_movement.document_token
                ):
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
                    token_hash = _effective_hash(
                        pjud_doc.doc_type,
                        case_rol,
                        pjud_movement.folio,
                        case_id,
                        court_id,
                        existing_docs,
                        case_courts,
                    )
                    if pjud_doc.doc_type == "escrito_doc" and movement_id is not None:
                        self._adopt_escrito_document(
                            case_id=case_id,
                            movement_id=movement_id,
                            token_hash=token_hash,
                            document_date=document_date,
                            existing_by_hash=existing_docs,
                            adoptable=adoptable,
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

            # ------------------------------------------------------------
            # 3. Escrito-level documents (Escritos pane, no movement yet)
            # ------------------------------------------------------------
            same_date_docs = self._escrito_doc_counts_by_date(existing_docs)
            escritos_per_date = Counter(
                row.fecha_ingreso.date()
                for row, _doc, _scope in escrito_docs
                if row.fecha_ingreso is not None
            )
            for escrito_row, pjud_doc, scope in escrito_docs:
                token_hash = _effective_hash(
                    pjud_doc.doc_type,
                    case_rol,
                    scope,
                    case_id,
                    court_id,
                    existing_docs,
                    case_courts,
                )
                # Same-date guard (a deliberate HEURISTIC, not a real dedup).
                # When the court resolves an escrito, the same PDF reappears as a
                # movement `escrito_doc`. The two panes share no key:
                # pjud_token_hash is sha256(doc_type|rol|scope_key) and an escrito
                # has no folio, so the movement row can never match ours. Queueing
                # ours too would download the PDF twice and show two identical rows
                # in the case Documentos tab. So: do not CREATE an escrito document
                # when the case already holds at least as many movement-borne
                # escrito_doc documents dated that day as it has escritos dated that
                # day (two escritos + one same-day doc still creates: one is
                # uncovered). Measured on QA: 510 duplicates avoided vs 28 of 1.135
                # tokened escritos sharing a date with another escrito. Rows that
                # already exist are never skipped, so they keep refreshing their JWT.
                day = (
                    escrito_row.fecha_ingreso.date()
                    if escrito_row.fecha_ingreso is not None
                    else None
                )
                if (
                    token_hash not in existing_docs
                    and day is not None
                    and same_date_docs.get(day, 0) >= escritos_per_date[day]
                ):
                    continue
                doc = self._upsert_document(
                    db=db,
                    case_id=case_id,
                    movement_id=None,
                    pjud_doc=pjud_doc,
                    token_hash=token_hash,
                    existing_by_hash=existing_docs,
                    document_date=escrito_row.fecha_ingreso,
                    escrito_id=escrito_row.id,
                )
                if doc is not None:
                    results.append(doc)

            # Single flush for all document inserts/updates + movement token writes
            # (was a flush per document/movement — one round-trip each over the proxy).
            db.flush()
            savepoint.commit()
            return results

        except Exception as exc:
            # Log the original error BEFORE rolling back: if the connection died,
            # the rollback can raise too and would otherwise hide the real cause.
            logger.warning(
                "Failed to persist documents for case_id=%s: %s",
                case_id,
                exc,
                exc_info=True,
            )
            savepoint.rollback()
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _adopt_escrito_document(
        case_id: int,
        movement_id: int,
        token_hash: str,
        document_date: Optional[datetime],
        existing_by_hash: dict,
        adoptable: dict,
    ) -> None:
        """Let a movement ``escrito_doc`` reuse the row already queued for its escrito.

        Order "escrito first, movement later": the escrito pane queued the PDF, then
        the court resolved the escrito and the movement carries the same PDF. The two
        hashes can never match (see the same-date guard in ``persist_from_detail``),
        so without this the movement would insert a second row. Instead the movement
        takes over the escrito's row: one row, both ``escrito_id`` and ``movement_id``.

        Only called for ``escrito_doc`` documents of a persisted movement, so no other
        document type can be affected. Works purely on the prefetched map, no query:
        it registers the chosen row under the movement's hash in ``existing_by_hash``
        so ``_upsert_document`` applies its usual rules (stored rows are untouched,
        pending/failed refresh the JWT). A row is adopted at most once: it leaves
        ``adoptable`` and, once it has a ``movement_id``, is never a candidate again.
        """
        if token_hash in existing_by_hash:
            return  # this movement already has its own row: normal path

        # Later cycles: the movement's document already lives on an adopted row
        # (hash of the escrito, movement_id of this movement).
        for d in existing_by_hash.values():
            if (
                d.doc_type == "escrito_doc"
                and d.escrito_id is not None
                and d.movement_id == movement_id
            ):
                existing_by_hash[token_hash] = d
                return

        if document_date is None:
            return
        candidates = adoptable.get(document_date.date())
        if not candidates:
            return
        row = candidates.pop(0)
        row.movement_id = movement_id
        existing_by_hash[token_hash] = row

    @staticmethod
    def _escrito_doc_counts_by_date(existing_by_hash: dict) -> Counter:
        """Count movement-borne ``escrito_doc`` documents per date, from memory.

        ``existing_by_hash`` already holds every movement document of this detail
        (prefetched by hash, plus rows added earlier in this pass), so no query is
        needed. Rows linked to an escrito are excluded: they are ours, not the
        movement-side copy.
        """
        return Counter(
            d.document_date.date()
            for d in existing_by_hash.values()
            if d.doc_type == "escrito_doc"
            and d.escrito_id is None
            and d.document_date is not None
        )

    @staticmethod
    def _escritos_with_documents(
        detail: "PJUDCaseDetail",  # type: ignore[name-defined]  # noqa: F821
        case_id: int,
        db: Session,
        escrito_rows: Optional[list],
    ) -> list:
        """Return ``(CaseEscrito, PJUDDocument, scope_key)`` for each escrito with a PDF.

        Only escritos flagged ``tiene_documento`` with a ``doc_token`` qualify. The
        escrito pane does not expose which PHP handler serves the file; escritos are
        unsigned documents, so they are served by ``docuN.php``. Verified against the
        existing corpus: all 91.051 stored ``escrito_doc`` rows use exactly this
        endpoint.
        """
        candidates = [e for e in detail.escritos if e.tiene_documento and e.doc_token]
        if not candidates:
            return []

        # Lazy import: sync_service imports this module at load time.
        from app.models.case_escrito import CaseEscrito
        from app.scrapper.pjud.base import PJUDDocument
        from app.services.sync_service import escrito_natural_key

        if escrito_rows is not None:
            rows_by_key = {row.natural_key: row for row, _is_new in escrito_rows}
        else:
            rows_by_key = {
                row.natural_key: row
                for row in db.query(CaseEscrito)
                .filter(CaseEscrito.case_id == case_id)
                .all()
            }

        out: list = []
        for escrito in candidates:
            key = escrito_natural_key(escrito)
            row = rows_by_key.get(key)
            if row is None or row.id is None:
                continue
            pjud_doc = PJUDDocument(
                token=escrito.doc_token,
                tipo="principal",
                url_type="docuN",
                doc_type="escrito_doc",
                endpoint="documentos/docuN.php",
                param_name="dtaDoc",
                available=True,
            )
            out.append((row, pjud_doc, f"escrito:{key}"))
        return out

    @staticmethod
    def _upsert_document(
        db: Session,
        case_id: int,
        movement_id: Optional[int],
        pjud_doc: "PJUDDocument",  # type: ignore[name-defined]  # noqa: F821
        token_hash: str,
        existing_by_hash: dict,
        document_date: Optional[datetime] = None,
        escrito_id: Optional[int] = None,
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

        ``escrito_id`` links a document that originated in the Escritos pane (it
        has no movement yet); ``movement_id`` and ``escrito_id`` are independent.

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
            escrito_id=escrito_id,
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
