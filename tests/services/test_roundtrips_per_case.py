"""Round-trip budget for the per-case sync work (``detect_and_sync_movements``).

Every statement against Cloud SQL from the station costs ~137 ms of network
round-trip while the server executes it in ~0.07 ms, so the number of
statements per case IS the cost of the sync.  These tests drive the real
per-case pipeline (movements, entities, documents, deadlines, matriz) against
SQLite and count statements with a ``before_cursor_execute`` listener on the
session's engine (plus ``commit``/``rollback``, which are round-trips too),
grouped by verb + table.

Two case shapes are measured:

* ``no_new``   - a re-sync of a case whose movements are all already stored.
                 The PJUD document JWTs rotate on every load, so every token
                 in the scraped detail differs from the stored one.
* ``five_new`` - the same case when 5 new movements (with documents) arrive,
                 plus entities.

Besides the budget, each test asserts that NOTHING is lost or duplicated.
"""

import re
from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from app.models.alert import Alert
from app.models.case import Case
from app.models.case_escrito import CaseEscrito
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.scrapper.pjud.base import (
    PJUDCase,
    PJUDCaseDetail,
    PJUDDocument,
    PJUDEscrito,
    PJUDExhorto,
    PJUDLitigante,
    PJUDMovement,
    PJUDNotificacion,
)

ROL = "C-1234-2024"
FIRM_RUT = "12345678-5"

_TABLE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE)\s+\"?([a-z_]+)\"?", re.IGNORECASE
)


def _verb_table(statement: str) -> str:
    verb = statement.strip().split(None, 1)[0].upper()
    if verb in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        match = _TABLE_RE.search(statement)
        return f"{verb} {match.group(1) if match else '?'}"
    return verb


class RoundTrips:
    """Counts every statement (and commit/rollback) sent through an engine."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.by_kind: Counter = Counter()

    def _on_execute(self, conn, cursor, statement, params, context, many):
        kind = _verb_table(statement)
        # psycopg2 runs ``executemany`` for UPDATE/DELETE as one network
        # round-trip PER PARAMETER SET (only INSERT is batched, through
        # SQLAlchemy's insertmanyvalues), so the listener's single event
        # undercounts what Cloud SQL actually sees.
        weight = len(params) if many and kind.split()[0] in ("UPDATE", "DELETE") else 1
        self.by_kind[kind] += weight

    def _on_commit(self, conn):
        self.by_kind["COMMIT"] += 1

    def _on_rollback(self, conn):
        self.by_kind["ROLLBACK"] += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on_execute)
        event.listen(self.engine, "commit", self._on_commit)
        event.listen(self.engine, "rollback", self._on_rollback)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._on_execute)
        event.remove(self.engine, "commit", self._on_commit)
        event.remove(self.engine, "rollback", self._on_rollback)

    @property
    def total(self) -> int:
        return sum(self.by_kind.values())

    def report(self, label: str) -> str:
        lines = [f"[roundtrips] {label}: total={self.total}"]
        for kind, n in sorted(self.by_kind.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {n:4d}  {kind}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scraped-detail builders
# ---------------------------------------------------------------------------

BASE_DATE = datetime(2024, 3, 1)


def _movement(folio: int, gen: str) -> PJUDMovement:
    """Movement with a main document + an anexo. ``gen`` rotates every JWT,
    exactly like a real PJUD load does."""
    return PJUDMovement(
        folio=str(folio),
        fecha=(BASE_DATE + timedelta(days=folio)).strftime("%d/%m/%Y"),
        tipo_tramite="Resolucion",
        descripcion=f"Movimiento {folio}",
        etapa="Notificacion demanda",
        documento_token=f"mov-{folio}-{gen}",
        documentos=[
            PJUDDocument(
                token=f"doc-{folio}-{gen}",
                tipo="principal",
                url_type="docuS",
                doc_type="resolution",
                endpoint="documentos/docuS.php",
                param_name="dtaDoc",
            ),
            PJUDDocument(
                token=f"anx-{folio}-{gen}",
                tipo="anexo",
                url_type="docuS",
                doc_type="resolution_anexo",
                endpoint="documentos/docuS.php",
                param_name="dtaDoc",
            ),
        ],
        tiene_documento=True,
    )


def _detail(n_movements: int, gen: str) -> PJUDCaseDetail:
    return PJUDCaseDetail(
        case=PJUDCase(
            rol=ROL,
            tribunal="1er Juzgado Civil de Santiago",
            caratulado="BANCO / DEUDOR",
            fecha_ingreso="01/01/2024",
            case_token="case-token",
        ),
        movements=[_movement(i, gen) for i in range(1, n_movements + 1)],
        litigantes=[
            PJUDLitigante("DTE.", "76000000-0", "JURIDICA", "BANCO SA"),
            PJUDLitigante("DDO.", "11111111-1", "NATURAL", "DEUDOR UNO"),
            PJUDLitigante("AB.DDO", FIRM_RUT, "NATURAL", "ABOGADO FIRMA"),
        ],
        notificaciones=[
            PJUDNotificacion(
                rol=ROL,
                estado_notif="Notificada",
                tipo_notif="Personal",
                fecha_tramite="05/03/2024",
                tipo_participante="DDO.",
                nombre="DEUDOR UNO",
                tramite="Demanda",
                obs_fallida="",
            )
        ],
        escritos=[
            PJUDEscrito(
                fecha_ingreso=f"{d:02d}/03/2024",
                tipo_escrito=f"Escrito {d}",
                solicitante="DEUDOR UNO",
                tiene_documento=True,
                tiene_anexo=False,
                doc_token=f"esc-{d}-{gen}",
            )
            for d in range(10, 14)
        ],
        exhortos=[
            PJUDExhorto(
                rol_origen=ROL,
                tipo_exhorto="Notificacion",
                rol_destino="C-9-2024",
                fecha_ordena="05/03/2024",
                fecha_ingreso="06/03/2024",
                tribunal_destino="2do Juzgado",
                estado="Pendiente",
            )
        ],
        case_documents=[
            PJUDDocument(
                token=f"txt-{gen}",
                tipo="caso",
                url_type="texto_demanda",
                doc_type="texto_demanda",
                endpoint="documentos/docuN.php",
                param_name="dtaDoc",
            )
        ],
    )


class _FakeScraper:
    def __init__(self, detail: PJUDCaseDetail) -> None:
        self.detail = detail
        self._panel_loaded = False

    async def get_case_detail(self, session, case_token):
        return self.detail


@pytest.fixture
def seeded(db, monkeypatch):
    from app.config import settings
    import app.services.shape_cooldown as shape_cooldown_module
    from app.services.shape_cooldown import ShapeCooldown

    monkeypatch.setattr(settings, "DOC_DOWNLOAD_ENABLED", False)
    monkeypatch.setattr(
        shape_cooldown_module,
        "_shape_cooldown",
        ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=lambda: 0.0),
    )

    lawyer = Lawyer(rut=FIRM_RUT, name="Firm Lawyer", is_active=True)
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


async def _run_case(db, lawyer, detail: PJUDCaseDetail) -> None:
    from app.services.sync_service import detect_and_sync_movements

    api_case = PJUDCase(
        rol=ROL,
        tribunal="1er Juzgado Civil de Santiago",
        caratulado="BANCO / DEUDOR",
        fecha_ingreso="01/01/2024",
        case_token="case-token",
    )
    _, _, errors = await detect_and_sync_movements(
        db,
        _FakeScraper(detail),
        object(),
        lawyer.id,
        [api_case],
        selected_cases=[api_case],
    )
    assert errors == []


def _snapshot(db) -> dict:
    """Everything persisted that the optimisation must not disturb, except the
    two rotating tokens (compared separately)."""
    db.expire_all()
    return {
        "movements": sorted(
            (m.folio, m.description, m.stage, m.procedure, m.movement_date)
            for m in db.query(Movement).all()
        ),
        "alerts": sorted(
            (a.type, a.movement_id, a.title, a.lawyer_id) for a in db.query(Alert).all()
        ),
        "documents": sorted(
            (d.pjud_token_hash, d.doc_type, d.status, d.movement_id)
            for d in db.query(Document).all()
        ),
        "litigantes": sorted(
            (x.natural_key, x.participante, x.rut) for x in db.query(CaseLitigante).all()
        ),
        "escritos": sorted(
            (e.tipo_escrito, e.solicitante) for e in db.query(CaseEscrito).all()
        ),
    }


@pytest.mark.asyncio
async def test_roundtrips_no_new_movements(db, seeded, capsys):
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(8, "g1"))  # populate

    before = _snapshot(db)
    with RoundTrips(db.get_bind()) as rt:
        await _run_case(db, lawyer, _detail(8, "g2"))  # tokens rotated
    with capsys.disabled():
        print("\n" + rt.report("no_new"))

    assert _snapshot(db) == before  # nothing lost, duplicated or altered
    assert rt.total <= NO_NEW_BUDGET


@pytest.mark.asyncio
async def test_roundtrips_five_new_movements(db, seeded, capsys):
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(3, "g1"))  # populate with 3

    before = _snapshot(db)
    with RoundTrips(db.get_bind()) as rt:
        await _run_case(db, lawyer, _detail(8, "g2"))  # 5 new, tokens rotated
    with capsys.disabled():
        print("\n" + rt.report("five_new"))

    after = _snapshot(db)
    assert len(after["movements"]) == len(before["movements"]) + 5
    assert len(after["alerts"]) == len(before["alerts"]) + 5
    # 5 new movements x (main + anexo) documents
    assert len(after["documents"]) == len(before["documents"]) + 10
    # old rows untouched
    for row in before["movements"]:
        assert row in after["movements"]
    assert after["litigantes"] == before["litigantes"]
    assert after["escritos"] == before["escritos"]
    assert rt.total <= FIVE_NEW_BUDGET


# Measured on origin/main (8ffc966): no_new = 68, five_new = 80 round-trips.
# What is left is deliberate: 17/7 pending-document JWT refreshes (a download
# needs the live token), 4 escrito doc_token refreshes (the API still serves
# that column), and the commit/savepoint boundaries that protect the movements.
#
# five_new went 72 -> 73 when last_movement_at started being derived from the
# case's rows instead of from scraped_movements[0]. The added round-trip is NOT
# a read: deriving it costs no query (the N+1 prefetch already holds every
# movement). It is the UPDATE on ``cases`` that now happens because the column
# actually changes. The old code was cheaper here only because it was leaving
# the column wrong -- measured on QA, 2.115 cases were behind by 373 days on
# average. no_new stays at 56: there the value is already correct, the helper
# writes nothing, and the case is never dirtied.
#
# Both budgets went up by 4 when escritos with a PDF started getting their own
# ``documents`` row (linked via escrito_id). The fixture carries 4 such escritos.
# The added trips are 4x ``UPDATE documents``: each pending escrito document gets
# its live JWT refreshed on every pass, exactly like the movement documents do
# (a download needs the live token). No SELECT was added: persist_from_detail
# reuses the case_escritos rows _sync_entities already loaded
# (``SELECT case_escritos`` stays at 1; see test_escrito_documents_add_no_select).
NO_NEW_BUDGET = 60
FIVE_NEW_BUDGET = 77


@pytest.mark.asyncio
async def test_escrito_documents_add_no_select(db, seeded):
    """Linking escrito PDFs reuses the rows _sync_entities loaded: no 2nd SELECT."""
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(3, "g1"))
    with RoundTrips(db.get_bind()) as rt:
        await _run_case(db, lawyer, _detail(3, "g2"))
    assert rt.by_kind["SELECT case_escritos"] == 1


# ---------------------------------------------------------------------------
# A - Movement.document_token is not rewritten on every sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_movement_document_token_is_written_once_not_rewritten(db, seeded):
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(3, "g1"))
    db.expire_all()
    assert {m.folio: m.document_token for m in db.query(Movement).all()} == {
        "1": "mov-1-g1",
        "2": "mov-2-g1",
        "3": "mov-3-g1",
    }

    await _run_case(db, lawyer, _detail(3, "g2"))  # JWTs rotated
    db.expire_all()
    # The first token stays: nothing reads the column, so rewriting it on every
    # pass only costs one UPDATE round-trip per movement.
    assert {m.folio: m.document_token for m in db.query(Movement).all()} == {
        "1": "mov-1-g1",
        "2": "mov-2-g1",
        "3": "mov-3-g1",
    }


@pytest.mark.asyncio
async def test_movement_document_token_fills_a_null(db, seeded):
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(2, "g1"))
    db.query(Movement).update({Movement.document_token: None})
    db.commit()

    await _run_case(db, lawyer, _detail(2, "g2"))
    db.expire_all()
    assert {m.folio: m.document_token for m in db.query(Movement).all()} == {
        "1": "mov-1-g2",
        "2": "mov-2-g2",
    }


@pytest.mark.asyncio
async def test_pending_document_jwt_is_still_refreshed(db, seeded):
    """A pending document needs a LIVE JWT to be downloaded - that refresh is
    NOT part of this optimisation and must keep happening."""
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(2, "g1"))
    await _run_case(db, lawyer, _detail(2, "g2"))
    db.expire_all()
    tokens = {d.pjud_token for d in db.query(Document).all()}
    assert "doc-1-g2" in tokens and "anx-2-g2" in tokens and "txt-g2" in tokens
    assert not any(t.endswith("g1") for t in tokens)


@pytest.mark.asyncio
async def test_no_movement_rewrite_when_nothing_changed(db, seeded):
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(8, "g1"))
    with RoundTrips(db.get_bind()) as rt:
        await _run_case(db, lawyer, _detail(8, "g2"))
    assert rt.by_kind["UPDATE movements"] == 0


# ---------------------------------------------------------------------------
# B - expire_on_commit is off while a case is processed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_on_commit_is_off_during_case_and_restored(db, seeded, monkeypatch):
    from app.services.document_persistence import DocumentPersistenceService

    lawyer, case = seeded
    seen = []
    real = DocumentPersistenceService.persist_from_detail

    def spy(self, detail, case_id, session, **kwargs):
        seen.append(session.expire_on_commit)
        return real(self, detail, case_id, session, **kwargs)

    monkeypatch.setattr(DocumentPersistenceService, "persist_from_detail", spy)
    assert db.expire_on_commit is True
    await _run_case(db, lawyer, _detail(2, "g1"))
    assert seen == [False]
    assert db.expire_on_commit is True


@pytest.mark.asyncio
async def test_expire_on_commit_restored_when_the_case_fails(db, seeded, monkeypatch):
    from app.services.document_persistence import DocumentPersistenceService
    from app.services.sync_service import detect_and_sync_movements

    lawyer, case = seeded

    def boom(self, detail, case_id, session, **kwargs):
        raise RuntimeError("document phase exploded")

    monkeypatch.setattr(DocumentPersistenceService, "persist_from_detail", boom)
    api_case = PJUDCase(rol=ROL, tribunal="t", caratulado="c", fecha_ingreso="01/01/2024",
                        case_token="case-token")
    _, _, errors = await detect_and_sync_movements(
        db, _FakeScraper(_detail(2, "g1")), object(), lawyer.id, [api_case],
        selected_cases=[api_case],
    )
    assert any("document phase exploded" in e for e in errors)  # cycle survived
    assert db.expire_on_commit is True


@pytest.mark.asyncio
async def test_session_started_with_expire_on_commit_off_stays_off(db, seeded):
    lawyer, case = seeded
    db.expire_on_commit = False
    try:
        await _run_case(db, lawyer, _detail(2, "g1"))
        assert db.expire_on_commit is False
    finally:
        db.expire_on_commit = True


# ---------------------------------------------------------------------------
# D - the firm side is resolved once per recompute
# ---------------------------------------------------------------------------


def test_firm_side_resolved_once_per_recompute(db, seeded, monkeypatch):
    import app.services.deadline_engine as de

    lawyer, case = seeded
    db.add(CaseLitigante(case_id=case.id, participante="AB.DDO", rut=FIRM_RUT,
                         persona_type="NATURAL", nombre="X", natural_key="k1"))
    db.commit()

    calls = []
    real = de._firm_side

    def spy(session, c):
        calls.append(c.id)
        return real(session, c)

    monkeypatch.setattr(de, "_firm_side", spy)
    de.DeadlineEngine.recompute_case(db, case)
    assert calls == [case.id]


def test_plaintiff_side_still_withholds_defense_recommendation(db, seeded):
    import app.services.deadline_engine as de

    lawyer, case = seeded
    db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=FIRM_RUT,
                         persona_type="NATURAL", nombre="X", natural_key="k1"))
    case.recommended_action_code = "SOMETHING"
    db.commit()
    de.DeadlineEngine.recompute_case(db, case)
    assert case.recommended_action_code is None


# ---------------------------------------------------------------------------
# E - the latest movement for the matriz comes from the loaded list
# ---------------------------------------------------------------------------


def _matriz_cache(db):
    from app.models.matriz_clasificacion import MatrizClasificacion
    from app.models.matriz_pjud_mapeo import MATCH_TIPO_STAGE, MatrizPjudMapeo
    from app.services.matriz_classifier import DEFAULT_PROC_SIMPLE, MatrizMappingCache

    db.add_all([
        MatrizPjudMapeo(pjud_stage="STAGE-LOW", matriz_etapa="ETAPA-LOW", activo=True,
                        match_tipo=MATCH_TIPO_STAGE, orden=0),
        MatrizPjudMapeo(pjud_stage="STAGE-HIGH", matriz_etapa="ETAPA-HIGH", activo=True,
                        match_tipo=MATCH_TIPO_STAGE, orden=0),
        MatrizClasificacion(proc_simple=DEFAULT_PROC_SIMPLE, etapa="ETAPA-LOW",
                            matriz="M1", proc_antiguo="X"),
        MatrizClasificacion(proc_simple=DEFAULT_PROC_SIMPLE, etapa="ETAPA-HIGH",
                            matriz="M2", proc_antiguo="X"),
    ])
    db.commit()
    return MatrizMappingCache.load(db)


def test_matriz_latest_from_list_matches_query_including_id_tiebreak(db, seeded):
    from app.services.matriz_classifier import classify_case

    lawyer, case = seeded
    same_day = datetime(2026, 5, 1)
    older = Movement(case_id=case.id, folio="1", stage="STAGE-LOW", description="a",
                     movement_date=same_day)
    newer_id = Movement(case_id=case.id, folio="2", stage="STAGE-HIGH", description="b",
                        movement_date=same_day)
    earlier_date = Movement(case_id=case.id, folio="3", stage="STAGE-LOW", description="c",
                            movement_date=datetime(2026, 4, 1))
    db.add_all([older, newer_id, earlier_date])
    db.commit()
    cache = _matriz_cache(db)

    expected = classify_case(db, case, mapping_cache=cache)  # the DB query path
    assert expected.matriz == "M2"  # same date: the HIGHER id wins

    for m in (older, newer_id, earlier_date):  # commit() expired them; reload once
        db.refresh(m)
    for order in ([older, newer_id, earlier_date], [newer_id, earlier_date, older],
                  [earlier_date, older, newer_id]):
        with RoundTrips(db.get_bind()) as rt:
            got = classify_case(db, case, mapping_cache=cache, movements=order)
        assert got == expected
        assert rt.by_kind["SELECT movements"] == 0


def test_matriz_with_empty_loaded_list_is_sin_detalle(db, seeded):
    from app.services.matriz_classifier import ORIGEN_SIN_DETALLE, classify_case

    lawyer, case = seeded
    cache = _matriz_cache(db)
    with RoundTrips(db.get_bind()) as rt:
        got = classify_case(db, case, mapping_cache=cache, movements=[])
    assert got.origen == ORIGEN_SIN_DETALLE
    assert rt.by_kind["SELECT movements"] == 0


def test_matriz_without_a_list_still_queries(db, seeded):
    from app.services.matriz_classifier import classify_case

    lawyer, case = seeded
    db.add(Movement(case_id=case.id, folio="1", stage="STAGE-LOW", description="a",
                    movement_date=datetime(2026, 5, 1)))
    db.commit()
    cache = _matriz_cache(db)
    with RoundTrips(db.get_bind()) as rt:
        got = classify_case(db, case, mapping_cache=cache)
    assert got.matriz == "M1"
    assert rt.by_kind["SELECT movements"] == 1


@pytest.mark.asyncio
async def test_pipeline_classifies_matriz_from_the_synced_movements(db, seeded):
    lawyer, case = seeded
    await _run_case(db, lawyer, _detail(4, "g1"))
    db.expire_all()
    case = db.get(Case, case.id)
    assert case.matriz_computed_at is not None
    assert case.matriz_origen is not None


@pytest.mark.parametrize(
    "role, expected",
    [("AB.DDO", "oponer_prescripcion"), ("AB.DTE", None)],
)
def test_recompute_case_decision_follows_the_side_it_resolved(db, seeded, role, expected):
    """End to end through ``recompute_case``: the side resolved ONCE must still
    drive the decision step (defense is withheld only for a confirmed plaintiff)."""
    import app.services.deadline_engine as de

    lawyer, case = seeded
    case.filed_at = datetime(2015, 1, 1)  # prescribed long ago
    db.add(CaseLitigante(case_id=case.id, participante=role, rut=FIRM_RUT,
                         persona_type="NATURAL", nombre="X", natural_key="k1"))
    db.commit()
    de.DeadlineEngine.recompute_case(db, case)
    assert case.recommended_action_code == expected
