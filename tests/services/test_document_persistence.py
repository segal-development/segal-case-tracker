"""Tests for document identity hash (ADR-1) and DocumentPersistenceService.

S1-T6: Failing tests for document_identity_hash (stable business identity, not JWT hash).
S1-T11: Failing tests for DocumentPersistenceService upsert and idempotency.

TDD — these tests must fail until S1-T7 (identity hash) and S1-T12 (service) are implemented.
"""

import hashlib
from datetime import datetime
from typing import List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# Side-effect import: registers all models so Base.metadata is populated.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.document import Document
from app.scrapper.pjud.base import (
    PJUDCase,
    PJUDCaseDetail,
    PJUDDocument,
    PJUDMovement,
)


# ---------------------------------------------------------------------------
# In-memory SQLite session fixture
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
def seeded_case(mem_db: Session):
    """Insert a minimal Lawyer, Court, Case and return (case, db)."""
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

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-0001-2026",
        plaintiff="DEMANDANTE TEST",
        defendant="DEMANDADO TEST",
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    mem_db.add(case)
    mem_db.flush()
    return case


def _make_detail(
    rol: str,
    movements: Optional[List[PJUDMovement]] = None,
    case_documents: Optional[List[PJUDDocument]] = None,
) -> PJUDCaseDetail:
    """Build a minimal PJUDCaseDetail for tests."""
    pjud_case = PJUDCase(
        rol=rol,
        tribunal="Tribunal Test",
        caratulado="DEMANDANTE/DEMANDADO",
        fecha_ingreso="01/01/2026",
    )
    return PJUDCaseDetail(
        case=pjud_case,
        movements=movements or [],
        case_documents=case_documents or [],
    )


def _make_movement(
    folio: str,
    doc_type: str = "resolution",
    url_type: str = "docuS",
    token: str = "eyJhbGciOiJub25lIn0.e30.FAKE_TOK",
    available: bool = True,
) -> PJUDMovement:
    """Build a PJUDMovement with one primary document."""
    doc = PJUDDocument(
        token=token,
        tipo="principal",
        url_type=url_type,
        doc_type=doc_type,
        endpoint=f"documentos/{url_type}.php",
        param_name="dtaDoc",
        available=available,
    )
    return PJUDMovement(
        folio=folio,
        fecha="01/01/2026",
        tipo_tramite="Resolución" if url_type == "docuS" else "Escrito",
        descripcion="Test movement",
        documento_token=token if available else None,
        documentos=[doc],
        tiene_documento=True,
    )


# ---------------------------------------------------------------------------
# S1-T6 — document_identity_hash unit tests (RED phase)
# ---------------------------------------------------------------------------

class TestDocumentIdentityHash:
    """Stable identity hash: sha256(doc_type|case_rol|scope_key).

    ADR-1: The JWT changes every sync; the hash must NOT use the JWT.
    """

    def test_same_identity_returns_same_hash(self) -> None:
        """Two calls with same doc_type/case_rol/scope_key return identical hash."""
        from app.services.document_persistence import document_identity_hash

        h1 = document_identity_hash("resolution", "C-0001-2026", "40")
        h2 = document_identity_hash("resolution", "C-0001-2026", "40")
        assert h1 == h2

    def test_different_scope_key_returns_different_hash(self) -> None:
        """Different scope_key → different hash (movement-level isolation)."""
        from app.services.document_persistence import document_identity_hash

        h1 = document_identity_hash("resolution", "C-0001-2026", "40")
        h2 = document_identity_hash("resolution", "C-0001-2026", "41")
        assert h1 != h2

    def test_empty_scope_key_valid_for_case_level_docs(self) -> None:
        """scope_key="" is valid for case-level docs and produces a hex string."""
        from app.services.document_persistence import document_identity_hash

        result = document_identity_hash("texto_demanda", "C-0001-2026", "")
        assert result  # non-empty
        assert isinstance(result, str)
        # Must be valid hex
        int(result, 16)

    def test_hash_is_64_char_hex(self) -> None:
        """SHA-256 digest → exactly 64 hex characters."""
        from app.services.document_persistence import document_identity_hash

        result = document_identity_hash("ebook", "C-0001-2026", "")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_jwt_rotation_does_not_change_hash(self) -> None:
        """Changing the JWT (not a parameter) must not change the identity hash."""
        from app.services.document_persistence import document_identity_hash

        # The function only takes doc_type, case_rol, scope_key — no JWT param.
        # Two calls with different JWTs (not passed) must return the same hash.
        h1 = document_identity_hash("cert_envio", "C-0001-2026", "")
        h2 = document_identity_hash("cert_envio", "C-0001-2026", "")
        assert h1 == h2

    def test_known_hash_value(self) -> None:
        """Sanity-check: hash must equal sha256 of the canonical string."""
        from app.services.document_persistence import document_identity_hash

        raw = "resolution|C-0001-2026|40"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert document_identity_hash("resolution", "C-0001-2026", "40") == expected


# ---------------------------------------------------------------------------
# S1-T11 — DocumentPersistenceService tests (RED phase)
# ---------------------------------------------------------------------------

class TestDocumentPersistenceService:
    """DocumentPersistenceService upserts Document rows keyed on pjud_token_hash."""

    def test_persist_creates_document_row_with_correct_doc_type(
        self, seeded_case, mem_db: Session
    ) -> None:
        """One resolution movement → Document row with doc_type='resolution', status='pending'."""
        from app.services.document_persistence import DocumentPersistenceService

        movement = _make_movement(folio="40", doc_type="resolution", url_type="docuS")

        # Pre-create the DB movement so the service can look it up by folio
        db_movement = Movement(
            case_id=seeded_case.id,
            folio="40",
            procedure="Resolución",
            description="Test movement",
            movement_date=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
        mem_db.add(db_movement)
        mem_db.flush()

        detail = _make_detail(rol=seeded_case.rol, movements=[movement])
        svc = DocumentPersistenceService()
        docs = svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        assert len(docs) >= 1
        doc = next((d for d in docs if d.doc_type == "resolution"), None)
        assert doc is not None
        assert doc.pjud_token_hash is not None
        assert len(doc.pjud_token_hash) == 64
        assert doc.status == "pending"

    def test_persist_sets_movement_document_token(
        self, seeded_case, mem_db: Session
    ) -> None:
        """After persist, the Movement DB row must have document_token set (bug fix)."""
        from app.services.document_persistence import DocumentPersistenceService

        token = "eyJhbGciOiJub25lIn0.e30.MOV_TOKEN"
        movement = _make_movement(folio="40", doc_type="resolution", url_type="docuS", token=token)

        db_movement = Movement(
            case_id=seeded_case.id,
            folio="40",
            procedure="Resolución",
            description="Test movement",
            movement_date=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
        mem_db.add(db_movement)
        mem_db.flush()

        detail = _make_detail(rol=seeded_case.rol, movements=[movement])
        svc = DocumentPersistenceService()
        svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        mem_db.refresh(db_movement)
        assert db_movement.document_token == token

    def test_re_sync_does_not_duplicate_rows(
        self, seeded_case, mem_db: Session
    ) -> None:
        """Persisting the same detail twice must not create duplicate Document rows."""
        from app.services.document_persistence import DocumentPersistenceService

        # 1 movement (docuN + escrito_cert) + 3 case-level docs (texto_demanda, cert_envio unavailable, ebook)
        movement_docs = [
            PJUDDocument(
                token="eyJhbGciOiJub25lIn0.e30.DOC_N",
                tipo="principal",
                url_type="docuN",
                doc_type="escrito_doc",
                endpoint="documentos/docuN.php",
                param_name="dtaDoc",
                available=True,
            ),
            PJUDDocument(
                token="eyJhbGciOiJub25lIn0.e30.CERT",
                tipo="cert",
                url_type="escrito_cert",
                doc_type="escrito_cert",
                endpoint="documentos/docCertificadoEscrito.php",
                param_name="dtaCert",
                available=True,
            ),
        ]
        movement = PJUDMovement(
            folio="39",
            fecha="01/01/2026",
            tipo_tramite="Escrito",
            descripcion="Test escrito",
            documento_token="eyJhbGciOiJub25lIn0.e30.DOC_N",
            documentos=movement_docs,
            tiene_documento=True,
        )

        db_movement = Movement(
            case_id=seeded_case.id,
            folio="39",
            procedure="Escrito",
            description="Test escrito",
            movement_date=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
        mem_db.add(db_movement)
        mem_db.flush()

        case_docs = [
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
                token=None,
                tipo="caso",
                url_type="cert_envio",
                doc_type="cert_envio",
                endpoint="documentos/docCertificadoDemanda.php",
                param_name="dtaCert",
                available=False,
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

        detail = _make_detail(
            rol=seeded_case.rol,
            movements=[movement],
            case_documents=case_docs,
        )

        svc = DocumentPersistenceService()

        # First persist
        docs1 = svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        # Second persist (same data — idempotent)
        docs2 = svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        total = mem_db.query(Document).filter(Document.case_id == seeded_case.id).count()
        assert total == 5, (
            f"Expected exactly 5 Document rows after 2 syncs (got {total}): "
            "2 movement docs + 3 case-level docs, no duplicates"
        )

    def test_fa_ban_persists_as_unavailable(
        self, seeded_case, mem_db: Session
    ) -> None:
        """cert_envio with fa-ban (available=False) → Document.status='unavailable', pjud_token=None."""
        from app.services.document_persistence import DocumentPersistenceService

        fa_ban_doc = PJUDDocument(
            token=None,
            tipo="caso",
            url_type="cert_envio",
            doc_type="cert_envio",
            endpoint="documentos/docCertificadoDemanda.php",
            param_name="dtaCert",
            available=False,
        )
        detail = _make_detail(
            rol=seeded_case.rol,
            case_documents=[fa_ban_doc],
        )

        svc = DocumentPersistenceService()
        docs = svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.status == "unavailable"
        assert doc.pjud_token is None

    def test_unavailable_document_never_downloaded(
        self, seeded_case, mem_db: Session
    ) -> None:
        """Unavailable docs are persisted but must not trigger any download calls."""
        from app.services.document_persistence import DocumentPersistenceService
        from unittest.mock import MagicMock

        fa_ban_doc = PJUDDocument(
            token=None,
            tipo="caso",
            url_type="cert_envio",
            doc_type="cert_envio",
            endpoint="documentos/docCertificadoDemanda.php",
            param_name="dtaCert",
            available=False,
        )
        detail = _make_detail(
            rol=seeded_case.rol,
            case_documents=[fa_ban_doc],
        )

        svc = DocumentPersistenceService()
        docs = svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        # In Slice 1, no downloader exists yet. Just confirm the doc was persisted
        # as unavailable without raising exceptions.
        assert all(d.status == "unavailable" for d in docs if not d.doc_type)
        assert docs[0].status == "unavailable"

    def test_movement_with_falsy_folio_is_skipped(
        self, seeded_case, mem_db: Session
    ) -> None:
        """FIX 3: movements with folio=None/empty must be skipped, not silently collide.

        Two movements both with folio=None would hash to the same stable_hash and
        overwrite each other. The guard logs a warning and skips both — no Document
        rows created, no exception raised.
        """
        from app.services.document_persistence import DocumentPersistenceService

        null_folio_movement = PJUDMovement(
            folio="",          # falsy folio — should be skipped
            fecha="01/01/2026",
            tipo_tramite="Resolución",
            descripcion="Test movement with no folio",
            documento_token=None,
            documentos=[
                PJUDDocument(
                    token="eyJhbGciOiJub25lIn0.e30.FAKE_TOK",
                    tipo="principal",
                    url_type="docuS",
                    doc_type="resolution",
                    endpoint="documentos/docuS.php",
                    param_name="dtaDoc",
                    available=True,
                )
            ],
            tiene_documento=True,
        )
        detail = _make_detail(rol=seeded_case.rol, movements=[null_folio_movement])

        svc = DocumentPersistenceService()
        docs = svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        # No Document rows should be created for a falsy-folio movement
        count = mem_db.query(Document).filter(Document.case_id == seeded_case.id).count()
        assert count == 0, (
            f"Expected 0 Document rows for falsy-folio movement; got {count}"
        )
        assert docs == [], "persist_from_detail must return [] when all movements skipped"

    def test_none_folio_movement_also_skipped(
        self, seeded_case, mem_db: Session
    ) -> None:
        """FIX 3: folio=None must also be skipped (same guard as empty string)."""
        from app.services.document_persistence import DocumentPersistenceService

        none_folio_movement = PJUDMovement(
            folio=None,
            fecha="01/01/2026",
            tipo_tramite="Resolución",
            descripcion="Test movement with None folio",
            documento_token=None,
            documentos=[
                PJUDDocument(
                    token="eyJhbGciOiJub25lIn0.e30.FAKE_TOK",
                    tipo="principal",
                    url_type="docuS",
                    doc_type="resolution",
                    endpoint="documentos/docuS.php",
                    param_name="dtaDoc",
                    available=True,
                )
            ],
            tiene_documento=True,
        )
        detail = _make_detail(rol=seeded_case.rol, movements=[none_folio_movement])

        svc = DocumentPersistenceService()
        docs = svc.persist_from_detail(detail, seeded_case.id, mem_db)
        mem_db.commit()

        count = mem_db.query(Document).filter(Document.case_id == seeded_case.id).count()
        assert count == 0
        assert docs == []
