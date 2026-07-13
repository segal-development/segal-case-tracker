"""Regression guard for the N+1 query fix in
``DocumentPersistenceService.persist_from_detail``.

Before the fix, ``persist_from_detail`` issued, PER document, a
``SAVEPOINT`` + ``SELECT Document WHERE pjud_token_hash == ...`` + ``RELEASE``,
and PER movement a ``SELECT Movement WHERE case_id ==, folio ==``. A case with
25 movements (each with a document) therefore issued ~25 Document SELECTs, ~25
Movement SELECTs, and ~25 SAVEPOINT/RELEASE round-trips — brutal over Cloud SQL
where each round-trip is ~135ms.

It now prefetches ALL of the case's Documents (into ``{pjud_token_hash: Document}``)
and ALL of its Movements (into ``{folio: Movement}``) in ONE query each, wraps the
whole document phase in ONE ``begin_nested`` savepoint, and flushes once. So
SELECTs against ``documents`` and ``movements`` — and the number of opening
SAVEPOINT statements — must be CONSTANT, not O(number of documents/movements).

These tests lock that in by comparing an N=5 run against an N=25 run and
asserting each table's SELECT count (and the SAVEPOINT count) is a small
constant and EQUAL across both N — never O(N).

Counting technique mirrors ``tests/services/test_sync_movements_n1.py``:
an in-memory SQLite engine with a ``before_cursor_execute`` listener that
tallies statements per table. SQLAlchemy emits real ``SAVEPOINT`` statements on
SQLite too, so counting the opening SAVEPOINT works. Per the N+1 note, the
assertions target SCALING (constant vs O(N)) via the two-N comparison, not a
Postgres-exact absolute count.

No live DB / network: everything runs against ``sqlite:///:memory:``.
"""

import re
from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# Side-effect import: registers all models so Base.metadata is populated.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.scrapper.pjud.base import (
    PJUDCase,
    PJUDCaseDetail,
    PJUDDocument,
    PJUDMovement,
)
from app.services.document_persistence import DocumentPersistenceService


# ---------------------------------------------------------------------------
# In-memory SQLite fixtures (mirror test_document_persistence.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def mem_db() -> Session:
    """Fresh in-memory SQLite session, created and dropped per test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def lawyer_and_court(mem_db: Session):
    """Seed a shared Lawyer + Court (cases hang off these)."""
    lawyer = Lawyer(
        rut="11111111-1",
        email="test@example.com",
        name="Test Lawyer",
        created_at=datetime.utcnow(),
    )
    mem_db.add(lawyer)
    mem_db.flush()

    court = Court(code="TEST01", name="Tribunal Test", region="RM", type="civil")
    mem_db.add(court)
    mem_db.flush()
    return lawyer, court


# ---------------------------------------------------------------------------
# Seeding + detail-building helpers
# ---------------------------------------------------------------------------

def _seed_case(db: Session, lawyer, court, rol: str) -> Case:
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        plaintiff="DEMANDANTE TEST",
        defendant="DEMANDADO TEST",
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.flush()
    return case


def _seed_movements(db: Session, case: Case, n: int) -> None:
    """Seed N Movement rows (folios "0".."N-1") so the service can look them
    up by folio during persist."""
    for i in range(n):
        db.add(
            Movement(
                case_id=case.id,
                folio=str(i),
                procedure="Resolución",
                description=f"Movimiento {i}",
                movement_date=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
        )
    db.flush()


def _movement_doc(folio: str) -> PJUDMovement:
    """A PJUDMovement carrying one movement-level document. token_hash =
    sha256(doc_type|rol|folio), so a shared doc_type across movements still
    yields a distinct hash per folio."""
    token = f"eyJhbGciOiJub25lIn0.e30.MOV_{folio}"
    doc = PJUDDocument(
        token=token,
        tipo="principal",
        url_type="docuS",
        doc_type="resolution",
        endpoint="documentos/docuS.php",
        param_name="dtaDoc",
        available=True,
    )
    return PJUDMovement(
        folio=folio,
        fecha="01/01/2026",
        tipo_tramite="Resolución",
        descripcion=f"Test movement {folio}",
        documento_token=token,
        documentos=[doc],
        tiene_documento=True,
    )


def _case_docs() -> list:
    """A couple of case-level documents (distinct doc_types → distinct hashes)."""
    return [
        PJUDDocument(
            token="eyJhbGciOiJub25lIn0.e30.TEXTO",
            tipo="caso",
            url_type="texto_demanda",
            doc_type="texto_demanda",
            endpoint="documentos/docu.php",
            param_name="valorEncTxtDmda",
            available=True,
        ),
        PJUDDocument(
            token="eyJhbGciOiJub25lIn0.e30.EBOOK",
            tipo="caso",
            url_type="ebook",
            doc_type="ebook",
            endpoint="documentos/newebookcivil.php",
            param_name="dtaEbook",
            available=True,
        ),
    ]


def _build_detail(rol: str, n_movements: int) -> PJUDCaseDetail:
    """A detail with N movement-docs + 2 case-level docs."""
    pjud_case = PJUDCase(
        rol=rol,
        tribunal="Tribunal Test",
        caratulado="DEMANDANTE/DEMANDADO",
        fecha_ingreso="01/01/2026",
    )
    return PJUDCaseDetail(
        case=pjud_case,
        movements=[_movement_doc(str(i)) for i in range(n_movements)],
        case_documents=_case_docs(),
    )


# ---------------------------------------------------------------------------
# Statement counter
# ---------------------------------------------------------------------------

@contextmanager
def count_statements(engine):
    """Tally, while active:
      - ``documents`` SELECTs   (SELECT ... FROM documents ...)
      - ``movements`` SELECTs   (SELECT ... FROM movements ...)
      - opening SAVEPOINTs      (statements starting with SAVEPOINT)

    Only the OPENING SAVEPOINT is counted (not RELEASE/ROLLBACK TO), so the
    tally is exactly the number of nested transactions the phase opened — 1
    now, one-per-document before the fix.
    """
    doc_re = re.compile(r"\bFROM\s+documents\b", re.IGNORECASE)
    mov_re = re.compile(r"\bFROM\s+movements\b", re.IGNORECASE)
    stats = {"documents": 0, "movements": 0, "savepoint": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        s = statement.strip()
        head = s[:9].upper()
        if head.startswith("SELECT"):
            if doc_re.search(s):
                stats["documents"] += 1
            if mov_re.search(s):
                stats["movements"] += 1
        elif head.startswith("SAVEPOINT"):
            stats["savepoint"] += 1

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield stats
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


# ---------------------------------------------------------------------------
# (a) Query-count regression guard: O(1), not O(N)
# ---------------------------------------------------------------------------

class TestQueryCountBound:
    def test_document_and_movement_selects_do_not_scale_with_n(
        self, mem_db: Session, lawyer_and_court
    ) -> None:
        """N=5 vs N=25: SELECTs against documents AND movements — and the
        SAVEPOINT count — must be a small constant and EQUAL across both N."""
        lawyer, court = lawyer_and_court
        engine = mem_db.get_bind()

        # --- N=5 on its own case (clean all-insert) ---
        case_small = _seed_case(mem_db, lawyer, court, "C-0005-2026")
        _seed_movements(mem_db, case_small, 5)
        detail_small = _build_detail(case_small.rol, 5)
        with count_statements(engine) as small:
            svc = DocumentPersistenceService()
            docs_small = svc.persist_from_detail(detail_small, case_small.id, mem_db)
            mem_db.commit()

        # --- N=25 on a SEPARATE case (clean all-insert again) ---
        case_large = _seed_case(mem_db, lawyer, court, "C-0025-2026")
        _seed_movements(mem_db, case_large, 25)
        detail_large = _build_detail(case_large.rol, 25)
        with count_statements(engine) as large:
            svc = DocumentPersistenceService()
            docs_large = svc.persist_from_detail(detail_large, case_large.id, mem_db)
            mem_db.commit()

        # Sanity: the service actually persisted the expected number of rows,
        # otherwise "constant queries" would be a vacuous truth.
        assert len(docs_small) == 5 + 2   # 5 movement docs + 2 case docs
        assert len(docs_large) == 25 + 2

        # documents-table SELECTs: ONE prefetch, regardless of N.
        assert small["documents"] <= 2
        assert large["documents"] <= 2
        assert large["documents"] == small["documents"], (
            "documents SELECTs scaled with N — the per-document existence "
            f"SELECT was not batched (N=5 -> {small['documents']}, "
            f"N=25 -> {large['documents']})"
        )

        # movements-table SELECTs: ONE prefetch, regardless of N.
        assert small["movements"] <= 2
        assert large["movements"] <= 2
        assert large["movements"] == small["movements"], (
            "movements SELECTs scaled with N — the per-movement folio SELECT "
            f"was not batched (N=5 -> {small['movements']}, "
            f"N=25 -> {large['movements']})"
        )

        # ONE savepoint for the whole phase, not one per document.
        assert small["savepoint"] <= 1
        assert large["savepoint"] <= 1
        assert large["savepoint"] == small["savepoint"], (
            "SAVEPOINT count scaled with N — the phase is opening a nested "
            f"transaction per document (N=5 -> {small['savepoint']}, "
            f"N=25 -> {large['savepoint']})"
        )


# ---------------------------------------------------------------------------
# (b) Re-sync correctness + still constant-query on second pass
# ---------------------------------------------------------------------------

class TestResync:
    def test_second_pass_no_duplicates_and_constant_query(
        self, mem_db: Session, lawyer_and_court
    ) -> None:
        lawyer, court = lawyer_and_court
        case = _seed_case(mem_db, lawyer, court, "C-0100-2026")
        _seed_movements(mem_db, case, 12)
        detail = _build_detail(case.rol, 12)
        expected_rows = 12 + 2  # 12 movement docs + 2 case docs

        svc = DocumentPersistenceService()

        # First pass — all inserts.
        svc.persist_from_detail(detail, case.id, mem_db)
        mem_db.commit()
        after_first = (
            mem_db.query(Document).filter(Document.case_id == case.id).count()
        )
        assert after_first == expected_rows

        # Second pass — same detail. No new rows, and still O(1) queries even
        # though every row already exists (the pre-fix code SELECTed per doc here too).
        engine = mem_db.get_bind()
        with count_statements(engine) as second:
            svc.persist_from_detail(detail, case.id, mem_db)
            mem_db.commit()

        after_second = (
            mem_db.query(Document).filter(Document.case_id == case.id).count()
        )
        assert after_second == expected_rows, (
            f"Re-sync duplicated rows: {after_first} -> {after_second}"
        )
        assert second["documents"] <= 2
        assert second["movements"] <= 2
        assert second["savepoint"] <= 1


# ---------------------------------------------------------------------------
# (c) Duplicate token_hash within a single detail → one row
# ---------------------------------------------------------------------------

class TestDuplicateHashWithinDetail:
    def test_two_docs_same_hash_resolve_to_single_row(
        self, mem_db: Session, lawyer_and_court
    ) -> None:
        """Two case-level docs with the SAME doc_type hash to the SAME
        token_hash. The in-loop ``existing_by_hash[token_hash] = doc`` update
        must make the second resolve to the first's just-added row — one
        Document row, no duplicate INSERT, no unique-constraint crash."""
        lawyer, court = lawyer_and_court
        case = _seed_case(mem_db, lawyer, court, "C-0200-2026")

        # Same doc_type + same (empty) scope_key -> identical token_hash.
        dup_a = PJUDDocument(
            token="eyJhbGciOiJub25lIn0.e30.DUP_A",
            tipo="caso",
            url_type="texto_demanda",
            doc_type="texto_demanda",
            endpoint="documentos/docu.php",
            param_name="valorEncTxtDmda",
            available=True,
        )
        dup_b = PJUDDocument(
            token="eyJhbGciOiJub25lIn0.e30.DUP_B",
            tipo="caso",
            url_type="texto_demanda",
            doc_type="texto_demanda",
            endpoint="documentos/docu.php",
            param_name="valorEncTxtDmda",
            available=True,
        )
        pjud_case = PJUDCase(
            rol=case.rol,
            tribunal="Tribunal Test",
            caratulado="DEMANDANTE/DEMANDADO",
            fecha_ingreso="01/01/2026",
        )
        detail = PJUDCaseDetail(
            case=pjud_case,
            movements=[],
            case_documents=[dup_a, dup_b],
        )

        svc = DocumentPersistenceService()
        docs = svc.persist_from_detail(detail, case.id, mem_db)
        mem_db.commit()

        # Both PJUD docs collapse onto ONE Document row.
        row_count = (
            mem_db.query(Document).filter(Document.case_id == case.id).count()
        )
        assert row_count == 1, (
            f"Duplicate token_hash within one detail created {row_count} rows; "
            "the in-loop existing_by_hash map update should keep it at 1"
        )
        # And both returned references point at that single row.
        assert len({id(d) for d in docs}) == 1


# ---------------------------------------------------------------------------
# (d) Fault isolation: a flush failure rolls back only the document phase
# ---------------------------------------------------------------------------

class TestFaultIsolation:
    def test_flush_failure_returns_empty_but_session_stays_usable(
        self, mem_db: Session, lawyer_and_court
    ) -> None:
        """Force a controlled flush failure inside the document phase (Document
        rows built with a NULL case_id violate the NOT NULL constraint on
        flush). The savepoint must roll back ONLY the document changes, return
        [], and leave the outer session usable for follow-up queries."""
        lawyer, court = lawyer_and_court
        case = _seed_case(mem_db, lawyer, court, "C-0300-2026")
        mem_db.commit()

        detail = _build_detail(case.rol, 0)  # 2 case-level docs, no movements

        svc = DocumentPersistenceService()
        # case_id=None -> Document.case_id NOT NULL violation at flush().
        result = svc.persist_from_detail(detail, None, mem_db)  # type: ignore[arg-type]

        assert result == [], "flush failure must yield an empty result list"

        # Nothing persisted for either the bogus (None) or the real case.
        assert mem_db.query(Document).count() == 0

        # Outer session is still usable — the nested-savepoint rollback did not
        # poison the parent transaction.
        assert mem_db.query(Case).filter(Case.id == case.id).count() == 1
        # And it can still do more work afterwards.
        _seed_movements(mem_db, case, 1)
        mem_db.commit()
        assert (
            mem_db.query(Movement).filter(Movement.case_id == case.id).count() == 1
        )
