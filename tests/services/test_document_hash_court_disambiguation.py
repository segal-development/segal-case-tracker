"""Same ROL in different courts must yield distinct Documents per causa.

``document_identity_hash`` is sha256(doc_type|rol|scope_key) and the prefetch is
global, so two Case rows with the same ROL in different courts used to share one
Document: the second causa adopted the first one's rows and never got its own.

Rule under test (worker path only): the legacy hash is kept unless ANOTHER case
already owns it; then the hash is qualified with ``|court:<court_id>``.
"""

import re
from datetime import datetime
from typing import List

import pytest
from sqlalchemy import event

from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.scrapper.pjud.base import (
    PJUDCase,
    PJUDCaseDetail,
    PJUDDocument,
    PJUDEscrito,
    PJUDMovement,
)
from app.services.document_persistence import (
    DocumentPersistenceService,
    document_identity_hash,
)
from app.services.sync_service import SPEC_ESCRITO, _sync_entities

ROL = "C-1234-2024"
LEGACY_RESOLUTION_7 = "6cd2b762aaab78106fbef24359bda9a1f19b6185e694769d0dcdb010fb3e49d5"


# ---------------------------------------------------------------------------
# Hash function
# ---------------------------------------------------------------------------


def test_hash_without_court_is_byte_identical_to_legacy_formula():
    assert document_identity_hash("resolution", ROL, "7") == LEGACY_RESOLUTION_7
    assert document_identity_hash("resolution", ROL, "7", court_id=None) == LEGACY_RESOLUTION_7


def test_hash_with_court_differs_and_is_stable():
    a = document_identity_hash("resolution", ROL, "7", court_id=1)
    assert a != LEGACY_RESOLUTION_7
    assert a == document_identity_hash("resolution", ROL, "7", court_id=1)
    assert a != document_identity_hash("resolution", ROL, "7", court_id=2)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture
def world(db):
    lawyer = Lawyer(rut="12345678-5", name="Firm Lawyer", is_active=True)
    db.add(lawyer)
    court_a = Court(code="C-A", name="1er Juzgado Civil de Santiago", region="RM", type="civil")
    court_b = Court(code="C-B", name="2do Juzgado Civil de Santiago", region="RM", type="civil")
    db.add_all([court_a, court_b])
    db.flush()

    def mk(court):
        c = Case(
            lawyer_id=lawyer.id,
            court_id=court.id,
            rol=ROL,
            competencia="civil",
            status="active",
            filed_at=datetime(2024, 1, 1),
        )
        db.add(c)
        db.flush()
        return c

    case_a, case_b = mk(court_a), mk(court_b)
    db.commit()
    return case_a, case_b


def _doc(doc_type: str, token: str) -> PJUDDocument:
    return PJUDDocument(
        token=token,
        tipo="principal",
        url_type="docuS",
        doc_type=doc_type,
        endpoint="documentos/docuS.php",
        param_name="dtaDoc",
        available=True,
    )


def _movement(folio: str, token: str) -> PJUDMovement:
    return PJUDMovement(
        folio=folio,
        fecha="01/01/2026",
        tipo_tramite="Resolución",
        descripcion=f"Movement {folio}",
        documento_token=token,
        documentos=[_doc("resolution", token)],
        tiene_documento=True,
    )


def _escrito(day: int, token: str) -> PJUDEscrito:
    return PJUDEscrito(
        fecha_ingreso=f"{day:02d}/03/2024",
        tipo_escrito=f"Escrito {day}",
        solicitante="DEUDOR UNO",
        tiene_documento=True,
        tiene_anexo=False,
        doc_token=token,
    )


def _detail(n_movements: int = 2, n_escritos: int = 1, tag: str = "t") -> PJUDCaseDetail:
    return PJUDCaseDetail(
        case=PJUDCase(
            rol=ROL,
            tribunal="Tribunal",
            caratulado="BANCO / DEUDOR",
            fecha_ingreso="01/01/2024",
            case_token="case-token",
        ),
        movements=[_movement(str(i), f"mov-{tag}-{i}") for i in range(n_movements)],
        case_documents=[_doc("texto_demanda", f"txt-{tag}"), _doc("ebook", f"ebk-{tag}")],
        escritos=[_escrito(10 + i, f"esc-{tag}-{i}") for i in range(n_escritos)],
    )


def _persist(db, case, detail) -> List[Document]:
    _sync_entities(db, int(case.id), detail.escritos, SPEC_ESCRITO)
    docs = DocumentPersistenceService().persist_from_detail(detail, int(case.id), db)
    db.commit()
    return docs


def _rows(db, case) -> List[Document]:
    return db.query(Document).filter(Document.case_id == case.id).all()


# ---------------------------------------------------------------------------
# Single causa: unchanged behaviour
# ---------------------------------------------------------------------------


def test_single_causa_uses_legacy_hashes_and_is_idempotent(db, world):
    case_a, _ = world
    _persist(db, case_a, _detail())
    rows = _rows(db, case_a)
    assert len(rows) == 2 + 2 + 1
    expected = {
        document_identity_hash("texto_demanda", ROL, ""),
        document_identity_hash("ebook", ROL, ""),
        document_identity_hash("resolution", ROL, "0"),
        document_identity_hash("resolution", ROL, "1"),
    }
    assert expected <= {r.pjud_token_hash for r in rows}
    assert document_identity_hash("texto_demanda", ROL, "") in {r.pjud_token_hash for r in rows}

    _persist(db, case_a, _detail(tag="t2"))
    assert len(_rows(db, case_a)) == 5
    assert db.query(Document).count() == 5


# ---------------------------------------------------------------------------
# Two causas, same ROL, different courts
# ---------------------------------------------------------------------------


def test_second_causa_gets_its_own_documents_and_first_is_untouched(db, world):
    case_a, case_b = world
    _persist(db, case_a, _detail(tag="a"))
    a_before = {r.id: (r.pjud_token_hash, r.pjud_token, r.case_id) for r in _rows(db, case_a)}
    assert len(a_before) == 5

    _persist(db, case_b, _detail(tag="b"))
    b_rows = _rows(db, case_b)
    assert len(b_rows) == 5, "case B must get its own case-level, movement and escrito docs"
    assert {r.pjud_token_hash for r in b_rows}.isdisjoint(
        {h for h, _, _ in a_before.values()}
    )
    assert {r.doc_type for r in b_rows} == {"texto_demanda", "ebook", "resolution", "escrito_doc"}
    assert {r.pjud_token for r in b_rows} >= {"txt-b", "ebk-b", "mov-b-0", "mov-b-1", "esc-b-0"}

    a_after = {r.id: (r.pjud_token_hash, r.pjud_token, r.case_id) for r in _rows(db, case_a)}
    assert a_after == a_before


def test_both_causas_resync_is_idempotent(db, world):
    case_a, case_b = world
    _persist(db, case_a, _detail(tag="a"))
    _persist(db, case_b, _detail(tag="b"))
    for i in range(2):
        _persist(db, case_a, _detail(tag=f"a{i}"))
        _persist(db, case_b, _detail(tag=f"b{i}"))
    assert len(_rows(db, case_a)) == 5
    assert len(_rows(db, case_b)) == 5
    assert db.query(Document).count() == 10
    hashes = [h for (h,) in db.query(Document.pjud_token_hash).all()]
    assert len(set(hashes)) == 10


def test_b_synced_first_then_a_keeps_legacy_for_first_comer(db, world):
    case_a, case_b = world
    _persist(db, case_b, _detail(tag="b"))
    _persist(db, case_a, _detail(tag="a"))
    assert len(_rows(db, case_a)) == 5 and len(_rows(db, case_b)) == 5
    legacy = document_identity_hash("texto_demanda", ROL, "")
    owner = db.query(Document).filter(Document.pjud_token_hash == legacy).one()
    assert owner.case_id == case_b.id


def test_legacy_rows_owned_by_a_are_never_rehashed(db, world):
    case_a, case_b = world
    _persist(db, case_a, _detail(tag="a"))
    legacy_hashes = {r.id: r.pjud_token_hash for r in _rows(db, case_a)}
    _persist(db, case_b, _detail(tag="b"))
    _persist(db, case_a, _detail(tag="a2"))
    _persist(db, case_b, _detail(tag="b2"))
    assert {r.id: r.pjud_token_hash for r in _rows(db, case_a)} == legacy_hashes
    court_b = case_b.court_id
    for r in _rows(db, case_b):
        assert r.pjud_token_hash.__len__() == 64
    assert document_identity_hash("texto_demanda", ROL, "", court_id=court_b) in {
        r.pjud_token_hash for r in _rows(db, case_b)
    }


def test_sibling_case_row_in_same_court_still_shares_documents(db, world):
    """Per-lawyer duplicate rows of ONE causa (same court) keep sharing, as before."""
    case_a, _ = world
    other = Lawyer(rut="22222222-2", name="Other", is_active=True)
    db.add(other)
    db.flush()
    sibling = Case(
        lawyer_id=other.id,
        court_id=case_a.court_id,
        rol=ROL,
        competencia="civil",
        status="active",
        filed_at=datetime(2024, 1, 1),
    )
    db.add(sibling)
    db.commit()
    _persist(db, case_a, _detail(tag="a"))
    _persist(db, sibling, _detail(tag="s"))
    assert db.query(Document).count() == 5
    assert _rows(db, sibling) == []


# ---------------------------------------------------------------------------
# Query-count guard
# ---------------------------------------------------------------------------


def _count_document_selects(db, fn) -> int:
    engine = db.get_bind()
    pattern = re.compile(r"\bFROM\s+documents\b", re.IGNORECASE)
    box = {"n": 0}

    def listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and pattern.search(statement):
            box["n"] += 1

    event.listen(engine, "before_cursor_execute", listener)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return box["n"]


def test_document_selects_do_not_scale_with_number_of_documents(db, world):
    case_a, case_b = world
    # A owns the legacy hashes, so B's runs exercise the court-qualified path.
    _persist(db, case_a, _detail(n_movements=25, n_escritos=5, tag="a"))
    small = _count_document_selects(
        db, lambda: _persist(db, case_b, _detail(n_movements=2, n_escritos=1, tag="b"))
    )
    large = _count_document_selects(
        db, lambda: _persist(db, case_b, _detail(n_movements=25, n_escritos=5, tag="b2"))
    )
    assert small == large
    assert large <= 3
