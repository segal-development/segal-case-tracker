"""DOC_MAX_PER_CASE: the sweep downloads ALL documents of RECENT movements
(newer than DOC_RECENT_DAYS) and caps the historical remainder. Deferred
documents stay ``pending`` untouched so a later visit can pick them up.

"Recent" is judged by the movement date, not by "new to our DB": 98% of the
causas the rotation visits are first visits, where every movement is new.
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer

ROL = "C-1-2026"


def _detail():
    detail = MagicMock()
    detail.case_documents = []
    detail.movements = []
    detail.litigantes = []
    detail.notificaciones = []
    detail.escritos = []
    detail.exhortos = []
    detail.case = MagicMock()
    detail.case.rol = ROL
    return detail


def _setup(db, recent=0, old=0, case_level=0):
    """First-visit causa: no movements in the DB, only pending documents."""
    lawyer = Lawyer(rut="40000002-2", name="Lawyer Cap", is_active=True)
    db.add(lawyer)
    db.flush()
    court = Court(code="CAP-COURT", name="Cap Court", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(lawyer_id=lawyer.id, court_id=court.id, rol=ROL,
                competencia="civil", status="active")
    db.add(case)
    db.flush()
    now = datetime.utcnow()
    specs = (
        [("r", now - timedelta(days=2 + i)) for i in range(recent)]
        + [("o", now - timedelta(days=200 + i)) for i in range(old)]
        + [("c", None) for _ in range(case_level)]
    )
    docs = []
    for i, (kind, date) in enumerate(specs):
        d = Document(case_id=case.id, doc_type="resolution", pjud_token=f"{kind}{i}",
                     pjud_token_hash=f"h{i}", status="pending", document_date=date)
        db.add(d)
        docs.append(d)
    db.commit()
    return lawyer, docs


async def _run(db, lawyer, docs, cap, historical=True):
    from app.services import sync_service
    from app.services.sync_service import detect_and_sync_movements

    api_case = MagicMock()
    api_case.rol = ROL
    api_case.case_token = "tok"
    scraper = MagicMock()
    scraper.get_case_detail = AsyncMock(return_value=_detail())

    with patch.object(sync_service.settings, "DOC_DOWNLOAD_ENABLED", True), \
         patch.object(sync_service.settings, "DOC_MAX_PER_CASE", cap), \
         patch.object(sync_service.settings, "DOC_RECENT_DAYS", 30), \
         patch.object(sync_service.settings, "DOC_HISTORICAL_ENABLED", historical), \
         patch.object(sync_service.DocumentPersistenceService,
                      "persist_from_detail", return_value=docs), \
         patch("app.services.document_downloader.DocumentDownloader.download_and_store",
               new_callable=AsyncMock) as dl:
        await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(),
            lawyer_id=lawyer.id, api_cases=[api_case], selected_cases=[api_case],
        )
    return dl.await_args.kwargs["pending_docs"] if dl.await_args_list else []


@pytest.mark.asyncio
async def test_default_zero_downloads_everything_as_today(db):
    lawyer, docs = _setup(db, recent=2, old=10)
    assert len(await _run(db, lawyer, docs, cap=0)) == 12


@pytest.mark.asyncio
async def test_first_visit_downloads_recent_plus_cap_not_all(db):
    """12 movements on a first visit: 2 recent + 10 old -> 2 + cap(3), not 12."""
    lawyer, docs = _setup(db, recent=2, old=10)
    sent = await _run(db, lawyer, docs, cap=3)
    assert len(sent) == 5
    assert sum(d.pjud_token.startswith("r") for d in sent) == 2
    assert sum(d.pjud_token.startswith("o") for d in sent) == 3


@pytest.mark.asyncio
async def test_recent_documents_are_never_capped(db):
    lawyer, docs = _setup(db, recent=8, old=10)
    sent = await _run(db, lawyer, docs, cap=3)
    assert sum(d.pjud_token.startswith("r") for d in sent) == 8
    assert sum(d.pjud_token.startswith("o") for d in sent) == 3


@pytest.mark.asyncio
async def test_old_movements_and_case_level_docs_share_the_cap(db):
    """Old movements (even if new to our DB) and undated case-level docs are capped."""
    lawyer, docs = _setup(db, old=6, case_level=4)
    assert len(await _run(db, lawyer, docs, cap=3)) == 3


@pytest.mark.asyncio
async def test_deferred_documents_stay_pending_untouched(db):
    lawyer, docs = _setup(db, recent=1, old=10)
    sent = await _run(db, lawyer, docs, cap=3)
    sent_ids = {d.id for d in sent}
    deferred = [d for d in docs if d.id not in sent_ids]
    assert len(deferred) == 7
    for d in deferred:
        db.refresh(d)
        assert d.status == "pending"
        assert d.failed_at is None


@pytest.mark.asyncio
async def test_log_reports_how_many_were_deferred(db, caplog):
    lawyer, docs = _setup(db, recent=2, old=10)
    with caplog.at_level(logging.INFO, logger="app.services.sync_service"):
        await _run(db, lawyer, docs, cap=3)
    msgs = [r.getMessage() for r in caplog.records if "deferred" in r.getMessage()]
    assert msgs, "expected a log line about deferred documents"
    assert "downloading 5" in msgs[0] and "deferred 7" in msgs[0]


@pytest.mark.asyncio
async def test_dated_movement_doc_without_date_is_treated_as_recent(db):
    """A doc attached to a movement but with NO date must NOT be deferred.

    `document_date` is parsed from PJUD's DD/MM/YYYY and its own helper documents
    it as "tolerable to miss — metadata, not identity". That stopped being true
    once it started deciding what gets deferred: if PJUD changes the date format
    (it has changed field formats before), every document would look undated and
    the urgent ones would be deferred in silence. Undated-but-attached fails OPEN.

    A case-level document has no movement and no date BY DESIGN (texto_demanda,
    cert_envio, the multi-MB ebook), so it stays capped — covered by
    test_old_movements_and_case_level_docs_share_the_cap.
    """
    from app.models.movement import Movement

    lawyer, docs = _setup(db, old=10)
    case_id = docs[0].case_id
    mov = Movement(case_id=case_id, description="sin fecha parseable",
                   movement_date=datetime.utcnow())
    db.add(mov)
    db.flush()
    huerfano = Document(case_id=case_id, doc_type="resolution", pjud_token="nofecha",
                        pjud_token_hash="hnofecha", status="pending",
                        document_date=None, movement_id=mov.id)
    db.add(huerfano)
    db.commit()

    sent = await _run(db, lawyer, docs + [huerfano], cap=3)
    assert huerfano.id in {d.id for d in sent}, "un doc de movimiento sin fecha no debe diferirse"
    assert len(sent) == 4, "el sin-fecha va aparte del tope de 3 historicos"


# ---------------------------------------------------------------------------
# DOC_HISTORICAL_ENABLED: the daily station fetches only what is fresh.
#
# Measured 2026-10-07: documents eat 45-52% of the cycle while PJUD caps detail
# at ~60 min per account, so every minute on a PDF is a minute not spent on
# movements. The pending queue is 83% older than six months and only THIRTEEN
# documents are newer than a week. The historical sweep belongs to the
# dedicated station, not to the daily one.
#
# `cap <= 0` already means "no cap", so it cannot also mean "none" -- and 0 is
# the default. Hence a separate flag.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_historical_disabled_downloads_only_the_recent_ones(db):
    lawyer, docs = _setup(db, recent=2, old=10, case_level=4)
    sent = await _run(db, lawyer, docs, cap=3, historical=False)
    assert len(sent) == 2
    assert all(d.pjud_token.startswith("r") for d in sent)


@pytest.mark.asyncio
async def test_historical_disabled_with_nothing_recent_downloads_nothing(db):
    """No fresh documents means no download at all -- not a fallback to the
    cap. The whole point is to stop spending the PJUD window on history."""
    lawyer, docs = _setup(db, old=10, case_level=4)
    assert await _run(db, lawyer, docs, cap=3, historical=False) == []


@pytest.mark.asyncio
async def test_historical_enabled_is_the_default_and_unchanged(db):
    """The flag must not change behaviour for anyone who has not set it."""
    lawyer, docs = _setup(db, recent=2, old=10)
    assert len(await _run(db, lawyer, docs, cap=3, historical=True)) == 5


@pytest.mark.asyncio
async def test_the_capped_historical_ones_are_the_newest(db):
    """`historical[:cap]` used to take whatever order persist_from_detail
    returned. It happened to be newest-first, which is right by accident --
    make it explicit so a reordering upstream cannot silently start fetching
    the three oldest instead."""
    lawyer, docs = _setup(db, old=10)
    docs = list(reversed(docs))  # oldest first, the hostile order
    sent = await _run(db, lawyer, docs, cap=3, historical=True)
    assert [d.pjud_token for d in sent] == ["o0", "o1", "o2"]
