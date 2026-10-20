"""Escrito PDFs must enter the document pipeline.

PJUD's case detail exposes the Escritos pane separately from the cuaderno
(movements). The escrito's ``doc_token`` was stored on ``case_escritos`` but no
``Document`` row was ever created for it, so the PDF was never queued. These
tests pin the fix: one ``Document`` per escrito carrying a document, linked via
``escrito_id``, reusing ``_upsert_document`` and the existing download gating.
"""

from datetime import datetime
from typing import List

import pytest

from app.config import settings
from app.models.case import Case
from app.models.case_escrito import CaseEscrito
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


def _escrito(day: int, token: str | None = "esc-tok-1", has_doc: bool = True) -> PJUDEscrito:
    return PJUDEscrito(
        fecha_ingreso=f"{day:02d}/03/2024",
        tipo_escrito=f"Escrito {day}",
        solicitante="DEUDOR UNO",
        tiene_documento=has_doc,
        tiene_anexo=False,
        doc_token=token,
    )


def _detail(escritos: List[PJUDEscrito], movements=None, rol: str = ROL) -> PJUDCaseDetail:
    return PJUDCaseDetail(
        case=PJUDCase(
            rol=rol,
            tribunal="1er Juzgado Civil de Santiago",
            caratulado="BANCO / DEUDOR",
            fecha_ingreso="01/01/2024",
            case_token="case-token",
        ),
        movements=movements or [],
        escritos=escritos,
    )


@pytest.fixture
def seeded(db):
    lawyer = Lawyer(rut="12345678-5", name="Firm Lawyer", is_active=True)
    db.add(lawyer)
    court = Court(code="C-1", name="1er Juzgado Civil de Santiago", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=ROL,
        competencia="civil",
        status="active",
        filed_at=datetime(2024, 1, 1),
    )
    db.add(case)
    db.commit()
    return lawyer, case


def _persist(db, case, detail):
    _sync_entities(db, int(case.id), detail.escritos, SPEC_ESCRITO)
    return DocumentPersistenceService().persist_from_detail(detail, int(case.id), db)


def test_escrito_with_document_creates_a_linked_pending_document(db, seeded):
    _, case = seeded
    detail = _detail([_escrito(10, token="esc-tok-1")])

    docs = _persist(db, case, detail)

    escrito = db.query(CaseEscrito).one()
    rows = db.query(Document).all()
    assert len(rows) == 1
    doc = rows[0]
    assert doc.escrito_id == escrito.id
    assert doc.movement_id is None
    assert doc.case_id == case.id
    assert doc.doc_type == "escrito_doc"
    assert doc.pjud_endpoint == "documentos/docuN.php"
    assert doc.pjud_token == "esc-tok-1"
    assert doc.status == "pending"
    assert doc.document_date == datetime(2024, 3, 10)
    assert doc in docs  # returned so the download step receives it


def test_escrito_document_is_idempotent_and_refreshes_the_jwt(db, seeded):
    _, case = seeded
    _persist(db, case, _detail([_escrito(10, token="esc-tok-1")]))
    db.commit()

    _persist(db, case, _detail([_escrito(10, token="esc-tok-2")]))
    db.commit()

    rows = db.query(Document).all()
    assert len(rows) == 1
    assert rows[0].pjud_token == "esc-tok-2"


def test_two_escritos_get_two_distinct_documents(db, seeded):
    _, case = seeded
    _persist(db, case, _detail([_escrito(10, "a"), _escrito(11, "b")]))

    rows = db.query(Document).all()
    assert len(rows) == 2
    assert len({r.escrito_id for r in rows}) == 2
    assert len({r.pjud_token_hash for r in rows}) == 2


@pytest.mark.parametrize(
    "escrito",
    [
        _escrito(10, token=None, has_doc=False),  # no document at all
        _escrito(10, token=None, has_doc=True),   # flagged but no token
        _escrito(10, token="x", has_doc=False),   # token without the flag
    ],
)
def test_escrito_without_a_usable_document_creates_nothing(db, seeded, escrito):
    _, case = seeded
    _persist(db, case, _detail([escrito]))
    assert db.query(Document).count() == 0


def test_movement_documents_keep_working_next_to_escrito_documents(db, seeded):
    _, case = seeded
    movement = PJUDMovement(
        folio="1",
        fecha="12/03/2024",
        tipo_tramite="Resolucion",
        descripcion="m",
        documentos=[
            PJUDDocument(
                token="mov-tok",
                tipo="principal",
                url_type="docuS",
                doc_type="resolution",
                endpoint="documentos/docuS.php",
                param_name="dtaDoc",
            )
        ],
    )
    from app.models.movement import Movement

    db.add(Movement(
            case_id=case.id, folio="1", description="m", movement_date=datetime(2024, 3, 12)
        ))
    db.commit()

    _persist(db, case, _detail([_escrito(10)], movements=[movement]))

    by_type = {d.doc_type: d for d in db.query(Document).all()}
    assert by_type["resolution"].movement_id is not None
    assert by_type["resolution"].escrito_id is None
    assert by_type["escrito_doc"].movement_id is None
    assert by_type["escrito_doc"].escrito_id is not None


def test_escrito_identity_hash_does_not_collide_with_a_movement_hash(db, seeded):
    """The escrito pane has no folio; its identity must not reuse the empty
    case-level scope or a movement's folio scope."""
    _, case = seeded
    _persist(db, case, _detail([_escrito(10)]))
    doc = db.query(Document).one()
    assert doc.pjud_token_hash != document_identity_hash("escrito_doc", ROL, scope_key="")
    assert doc.pjud_token_hash != document_identity_hash("escrito_doc", ROL, scope_key="10")


class _CapturingDownloader:
    batches: list = []

    async def download_and_store(self, pending_docs, **kwargs):
        type(self).batches.append(list(pending_docs))


class _FakeScraper:
    def __init__(self, detail):
        self.detail = detail
        self._panel_loaded = False

    async def get_case_detail(self, session, case_token):
        return self.detail


async def _run_pipeline(db, lawyer, rol, monkeypatch):
    import app.services.document_downloader as dd
    import app.services.storage_service as ss
    import app.services.shape_cooldown as shape_cooldown_module
    from app.services.shape_cooldown import ShapeCooldown
    from app.services.sync_service import detect_and_sync_movements

    monkeypatch.setattr(settings, "DOC_DOWNLOAD_ENABLED", True)
    monkeypatch.setattr(settings, "DOC_DOWNLOAD_MIN_YEAR", 2024)
    monkeypatch.setattr(dd, "DocumentDownloader", _CapturingDownloader)
    monkeypatch.setattr(ss, "get_storage_backend", lambda s: object())
    monkeypatch.setattr(ss, "StorageService", lambda backend: object())
    monkeypatch.setattr(
        shape_cooldown_module,
        "_shape_cooldown",
        ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=lambda: 0.0),
    )
    _CapturingDownloader.batches = []
    api_case = PJUDCase(
        rol=rol,
        tribunal="1er Juzgado Civil de Santiago",
        caratulado="BANCO / DEUDOR",
        fecha_ingreso="01/01/2024",
        case_token="case-token",
    )
    _, _, errors = await detect_and_sync_movements(
        db,
        _FakeScraper(_detail([_escrito(10)], rol=rol)),
        object(),
        lawyer.id,
        [api_case],
        selected_cases=[api_case],
    )
    assert errors == []


@pytest.mark.asyncio
async def test_escrito_document_is_handed_to_the_downloader_for_recent_cases(
    db, seeded, monkeypatch
):
    lawyer, _ = seeded
    await _run_pipeline(db, lawyer, ROL, monkeypatch)

    handed = [d for batch in _CapturingDownloader.batches for d in batch]
    assert [d.doc_type for d in handed] == ["escrito_doc"]
    assert handed[0].escrito_id is not None


@pytest.mark.asyncio
async def test_pre_2024_case_escrito_is_not_downloaded(db, seeded, monkeypatch):
    """Same DOC_DOWNLOAD_MIN_YEAR floor as movement documents."""
    lawyer, case = seeded
    case.rol = "C-77-2023"
    db.commit()

    await _run_pipeline(db, lawyer, "C-77-2023", monkeypatch)

    assert [d for batch in _CapturingDownloader.batches for d in batch] == []


# ---------------------------------------------------------------------------
# Same PDF through both panes: count-aware same-date guard
# ---------------------------------------------------------------------------


def _movement_with_escrito_doc(folio: str, fecha: str) -> PJUDMovement:
    return PJUDMovement(
        folio=folio,
        fecha=fecha,
        tipo_tramite="Escrito",
        descripcion="m",
        documentos=[
            PJUDDocument(
                token=f"mov-tok-{folio}",
                tipo="principal",
                url_type="docuN",
                doc_type="escrito_doc",
                endpoint="documentos/docuN.php",
                param_name="dtaDoc",
            )
        ],
    )


def _add_movements(db, case, *folios_dates):
    from app.models.movement import Movement

    for folio, fecha in folios_dates:
        db.add(
            Movement(
                case_id=case.id,
                folio=folio,
                description="m",
                movement_date=datetime.strptime(fecha, "%d/%m/%Y"),
            )
        )
    db.commit()


def _docs_by_link(db):
    rows = db.query(Document).all()
    return (
        [d for d in rows if d.movement_id is not None],
        [d for d in rows if d.escrito_id is not None],
    )


def test_one_escrito_with_a_same_date_escrito_doc_is_not_duplicated(db, seeded):
    _, case = seeded
    _add_movements(db, case, ("1", "10/03/2024"))
    detail = _detail(
        [_escrito(10, "esc-a")],
        movements=[_movement_with_escrito_doc("1", "10/03/2024")],
    )

    _persist(db, case, detail)

    from_movements, from_escritos = _docs_by_link(db)
    assert len(from_movements) == 1
    assert from_escritos == []


def test_two_escritos_with_one_same_date_escrito_doc_still_create_rows(db, seeded):
    _, case = seeded
    _add_movements(db, case, ("1", "10/03/2024"))
    two_same_day = [
        PJUDEscrito("10/03/2024", "Escrito A", "DEUDOR UNO", True, False, "esc-a"),
        PJUDEscrito("10/03/2024", "Escrito B", "DEUDOR UNO", True, False, "esc-b"),
    ]
    detail = _detail(
        two_same_day, movements=[_movement_with_escrito_doc("1", "10/03/2024")]
    )

    _persist(db, case, detail)

    _, from_escritos = _docs_by_link(db)
    assert len(from_escritos) >= 1  # one of the two is genuinely uncovered
    assert {d.escrito_id for d in from_escritos} <= {e.id for e in db.query(CaseEscrito)}


def test_escrito_with_no_same_date_escrito_doc_is_created(db, seeded):
    _, case = seeded
    _add_movements(db, case, ("1", "09/03/2024"))
    detail = _detail(
        [_escrito(10, "esc-a")],
        movements=[_movement_with_escrito_doc("1", "09/03/2024")],  # other day
    )

    _persist(db, case, detail)

    _, from_escritos = _docs_by_link(db)
    assert len(from_escritos) == 1


def test_an_already_queued_escrito_document_keeps_refreshing(db, seeded):
    """The guard only decides whether to CREATE; an existing row is never dropped."""
    _, case = seeded
    _persist(db, case, _detail([_escrito(10, "esc-1")]))
    db.commit()
    _add_movements(db, case, ("1", "10/03/2024"))

    _persist(
        db,
        case,
        _detail(
            [_escrito(10, "esc-2")],
            movements=[_movement_with_escrito_doc("1", "10/03/2024")],
        ),
    )

    _, from_escritos = _docs_by_link(db)
    assert [d.pjud_token for d in from_escritos] == ["esc-2"]


# ---------------------------------------------------------------------------
# Escrito queued first, movement arrives later: the movement ADOPTS the row
# ---------------------------------------------------------------------------


def _two_escritos_same_day():
    return [
        PJUDEscrito("10/03/2024", "Escrito A", "DEUDOR UNO", True, False, "esc-a"),
        PJUDEscrito("10/03/2024", "Escrito B", "DEUDOR UNO", True, False, "esc-b"),
    ]


def test_movement_adopts_the_queued_escrito_document(db, seeded):
    _, case = seeded
    escritos = [_escrito(10, "esc-1")]
    _persist(db, case, _detail(escritos))
    db.commit()
    _add_movements(db, case, ("1", "10/03/2024"))
    movement = _movement_with_escrito_doc("1", "10/03/2024")

    _persist(db, case, _detail(escritos, movements=[movement]))
    db.commit()
    # A further cycle must stay at one row (adoption is stable, not re-inserted).
    _persist(db, case, _detail(escritos, movements=[movement]))
    db.commit()

    rows = db.query(Document).all()
    assert len(rows) == 1
    assert rows[0].escrito_id is not None
    assert rows[0].movement_id is not None


def test_adopting_a_stored_escrito_document_keeps_status_and_gcs_path(db, seeded):
    _, case = seeded
    escritos = [_escrito(10, "esc-1")]
    _persist(db, case, _detail(escritos))
    row = db.query(Document).one()
    row.status = "stored"
    row.gcs_path = "bucket/path.pdf"
    db.commit()
    _add_movements(db, case, ("1", "10/03/2024"))

    _persist(
        db,
        case,
        _detail(escritos, movements=[_movement_with_escrito_doc("1", "10/03/2024")]),
    )
    db.commit()

    row = db.query(Document).one()
    assert row.movement_id is not None and row.escrito_id is not None
    assert row.status == "stored"
    assert row.gcs_path == "bucket/path.pdf"
    assert row.pjud_token == "esc-1"  # stored rows are never touched


def test_movement_escrito_doc_without_a_matching_escrito_inserts_as_before(db, seeded):
    _, case = seeded
    _add_movements(db, case, ("1", "10/03/2024"))

    _persist(
        db,
        case,
        _detail([], movements=[_movement_with_escrito_doc("1", "10/03/2024")]),
    )

    row = db.query(Document).one()
    assert row.movement_id is not None
    assert row.escrito_id is None
    assert row.doc_type == "escrito_doc"


def test_two_same_day_escritos_with_one_movement_doc_adopt_exactly_one(db, seeded):
    _, case = seeded
    escritos = _two_escritos_same_day()
    _persist(db, case, _detail(escritos))
    db.commit()
    assert db.query(Document).count() == 2
    _add_movements(db, case, ("1", "10/03/2024"))
    movement = _movement_with_escrito_doc("1", "10/03/2024")

    _persist(db, case, _detail(escritos, movements=[movement]))
    db.commit()
    _persist(db, case, _detail(escritos, movements=[movement]))
    db.commit()

    rows = db.query(Document).all()
    assert len(rows) == 2
    assert sorted(d.movement_id is not None for d in rows) == [False, True]
