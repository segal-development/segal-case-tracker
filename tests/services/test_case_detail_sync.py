"""Tests for Slice 1b persistence helpers (S1-T10 + S1-T11 — RED first).

Coverage:
- normalize_cell: strips, collapses whitespace, casefolds
- natural_key functions: litigante (rut path + fallback), exhorto, notificacion, escrito
- upsert_*: first run inserts, second run is idempotent (is_new=False), new-row returns is_new=True
- _sync_entities: end-to-end parse→persist with HTML fixture; 0 Alert rows; no NotificationService call
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Side-effect import: registers all models so Base.metadata is populated.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.scrapper.pjud.base import (
    PJUDEscrito,
    PJUDExhorto,
    PJUDLitigante,
    PJUDNotificacion,
)
from app.scrapper.pjud.civil import CivilScraper
from app.services.sync_service import (
    SPEC_ESCRITO,
    SPEC_EXHORTO,
    SPEC_LITIGANTE,
    SPEC_NOTIFICACION,
    NotifyBudget,
    SyncService,
    _sync_entities,
    detect_and_sync_movements,
    escrito_natural_key,
    exhorto_natural_key,
    litigante_natural_key,
    normalize_cell,
    notificacion_natural_key,
    sync_case_detail,
    upsert_escritos,
    upsert_exhortos,
    upsert_litigantes,
    upsert_notificaciones,
)

# ---------------------------------------------------------------------------
# Path to HTML fixtures from Slice 1a
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures/pjud"
_RICH_HTML = _FIXTURE_DIR / "detail_civil_rich.html"
_SYNTHETIC_HTML = _FIXTURE_DIR / "detail_civil_synthetic.html"


# ---------------------------------------------------------------------------
# Shared in-memory SQLite fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def sqlite_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="function")
def seeded_case(sqlite_db):
    """Lawyer + Court + Case pre-seeded.

    Returns {"db", "case", "lawyer"} — the "lawyer" key was added in Slice 2 so
    that S2 tests can pass the lawyer object to _sync_entities / detect_and_sync.
    Existing tests that only access "db" and "case" are unaffected.
    """
    db = sqlite_db
    lawyer = Lawyer(
        rut="11111111-1",
        name="Test Lawyer",
        email="test@example.com",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.flush()

    court = Court(code="T1-SYNC", name="Juzgado Sync Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-9999-2025",
        plaintiff="BANCO ITAU",
        defendant="FERNANDEZ",
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    return {"db": db, "case": case, "lawyer": lawyer}


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _lit(rut="11111111-1", participante="DTE.", nombre="JUAN PEREZ") -> PJUDLitigante:
    return PJUDLitigante(participante=participante, rut=rut, persona_type="NATURAL", nombre=nombre)


def _exh(rol_origen="C-1253-2015", tipo="ACTIVO", rol_destino="E-355-2026") -> PJUDExhorto:
    return PJUDExhorto(
        rol_origen=rol_origen,
        tipo_exhorto=tipo,
        rol_destino=rol_destino,
        fecha_ordena="01/01/2026",
        fecha_ingreso="02/01/2026",
        tribunal_destino="JUZGADO TEST",
        estado="PENDIENTE",
    )


def _notif(nombre="FERNANDEZ") -> PJUDNotificacion:
    return PJUDNotificacion(
        rol="C-9999-2025",
        estado_notif="REALIZADA",
        tipo_notif="PERSONAL",
        fecha_tramite="10/06/2026",
        tipo_participante="DDO.",
        nombre=nombre,
        tramite="Demanda",
        obs_fallida="",
    )


def _escrito(solicitante="BANCO ITAU") -> PJUDEscrito:
    return PJUDEscrito(
        fecha_ingreso="10/06/2026",
        tipo_escrito="DEMANDA",
        solicitante=solicitante,
        tiene_documento=True,
        tiene_anexo=False,
    )


# ---------------------------------------------------------------------------
# S1-T10a: normalize_cell
# ---------------------------------------------------------------------------


class TestNormalizeCell:
    def test_strips_leading_and_trailing_whitespace(self):
        assert normalize_cell("  hello  ") == "hello"

    def test_collapses_internal_whitespace(self):
        assert normalize_cell("hello   world") == "hello world"

    def test_casefolds(self):
        assert normalize_cell("BANCO ITAÚ") == "banco itaú"

    def test_pjud_padded_string_cleans_to_single_word(self):
        padded = "  JURIDICA   "
        result = normalize_cell(padded)
        assert result == "juridica"
        assert "  " not in result

    def test_empty_string(self):
        assert normalize_cell("") == ""

    def test_already_clean_string_unchanged(self):
        assert normalize_cell("banco itau") == "banco itau"


# ---------------------------------------------------------------------------
# S1-T10b: litigante_natural_key
# ---------------------------------------------------------------------------


class TestLitiganteNaturalKey:
    def test_with_rut_returns_64_char_hex(self):
        lit = _lit(rut="81826800-9", participante="DTE.")
        key = litigante_natural_key(lit)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_with_rut_is_deterministic(self):
        lit = _lit(rut="81826800-9", participante="DTE.")
        assert litigante_natural_key(lit) == litigante_natural_key(lit)

    def test_with_rut_matches_expected_hash(self):
        lit = _lit(rut="81826800-9", participante="DTE.")
        expected = hashlib.sha256(
            f"{normalize_cell('81826800-9')}|{normalize_cell('DTE.')}".encode()
        ).hexdigest()
        assert litigante_natural_key(lit) == expected

    def test_fallback_no_rut_uses_participante_nombre(self):
        lit = _lit(rut="", participante="AB.DTE", nombre="PEREZ GONZALEZ")
        key = litigante_natural_key(lit)
        expected = hashlib.sha256(
            f"{normalize_cell('AB.DTE')}|{normalize_cell('PEREZ GONZALEZ')}".encode()
        ).hexdigest()
        assert key == expected

    def test_different_ruts_produce_different_keys(self):
        lit1 = _lit(rut="11111111-1", participante="DTE.")
        lit2 = _lit(rut="22222222-2", participante="DTE.")
        assert litigante_natural_key(lit1) != litigante_natural_key(lit2)

    def test_same_rut_different_participante_produces_different_keys(self):
        lit1 = _lit(rut="11111111-1", participante="DTE.")
        lit2 = _lit(rut="11111111-1", participante="AB.DTE")
        assert litigante_natural_key(lit1) != litigante_natural_key(lit2)


# ---------------------------------------------------------------------------
# S1-T10c: exhorto_natural_key
# ---------------------------------------------------------------------------


class TestExhortoNaturalKey:
    def test_returns_64_char_hex(self):
        exh = _exh()
        assert len(exhorto_natural_key(exh)) == 64

    def test_matches_expected_sha256(self):
        exh = _exh(rol_origen="C-1253-2015", tipo="ACTIVO", rol_destino="E-355-2026")
        expected = hashlib.sha256(
            f"{normalize_cell('C-1253-2015')}|{normalize_cell('E-355-2026')}|{normalize_cell('ACTIVO')}".encode()
        ).hexdigest()
        assert exhorto_natural_key(exh) == expected

    def test_different_rol_destino_produces_different_key(self):
        exh1 = _exh(rol_destino="E-355-2026")
        exh2 = _exh(rol_destino="E-999-2026")
        assert exhorto_natural_key(exh1) != exhorto_natural_key(exh2)


# ---------------------------------------------------------------------------
# S1-T10d: notificacion_natural_key + escrito_natural_key
# ---------------------------------------------------------------------------


class TestNotificacionNaturalKey:
    def test_same_row_same_hash(self):
        n = _notif()
        assert notificacion_natural_key(n) == notificacion_natural_key(n)

    def test_different_nombre_different_hash(self):
        n1 = _notif(nombre="FERNANDEZ")
        n2 = _notif(nombre="GONZALEZ")
        assert notificacion_natural_key(n1) != notificacion_natural_key(n2)

    def test_returns_64_char_hex(self):
        assert len(notificacion_natural_key(_notif())) == 64


class TestEscritoNaturalKey:
    def test_same_row_same_hash(self):
        e = _escrito()
        assert escrito_natural_key(e) == escrito_natural_key(e)

    def test_different_solicitante_different_hash(self):
        e1 = _escrito(solicitante="BANCO ITAU")
        e2 = _escrito(solicitante="BANCO BCI")
        assert escrito_natural_key(e1) != escrito_natural_key(e2)


# ---------------------------------------------------------------------------
# S1-T11a: upsert_litigantes
# ---------------------------------------------------------------------------


class TestUpsertLitigantes:
    def test_first_run_inserts_all(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        items = [_lit("11111111-1", "DTE."), _lit("22222222-2", "DDO.")]
        results = upsert_litigantes(db, case.id, items)
        assert len(results) == 2
        assert all(is_new for _, is_new in results)
        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == case.id).count() == 2

    def test_idempotent_no_duplicates(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        items = [_lit("11111111-1", "DTE.")]
        r1 = upsert_litigantes(db, case.id, items)
        assert r1[0][1] is True  # is_new
        r2 = upsert_litigantes(db, case.id, items)
        assert r2[0][1] is False  # not new
        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == case.id).count() == 1

    def test_new_row_is_new_true(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        results = upsert_litigantes(db, case.id, [_lit("33333333-3", "DTE.")])
        assert results[0][1] is True


# ---------------------------------------------------------------------------
# S1-T11b: upsert_exhortos
# ---------------------------------------------------------------------------


class TestUpsertExhortos:
    def test_first_run_inserts(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        results = upsert_exhortos(db, case.id, [_exh()])
        assert len(results) == 1
        assert results[0][1] is True
        assert db.query(CaseExhorto).count() == 1

    def test_idempotent(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        upsert_exhortos(db, case.id, [_exh()])
        r2 = upsert_exhortos(db, case.id, [_exh()])
        assert r2[0][1] is False
        assert db.query(CaseExhorto).count() == 1


# ---------------------------------------------------------------------------
# S1-T11c: upsert_notificaciones + upsert_escritos
# ---------------------------------------------------------------------------


class TestUpsertNotificaciones:
    def test_first_run_inserts(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        results = upsert_notificaciones(db, case.id, [_notif(), _notif(nombre="OTRO")])
        assert len(results) == 2
        assert all(is_new for _, is_new in results)

    def test_idempotent(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        upsert_notificaciones(db, case.id, [_notif()])
        r2 = upsert_notificaciones(db, case.id, [_notif()])
        assert r2[0][1] is False
        assert db.query(CaseNotificacion).count() == 1


class TestUpsertEscritos:
    def test_first_run_inserts(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        results = upsert_escritos(db, case.id, [_escrito(), _escrito("BCI")])
        assert len(results) == 2

    def test_idempotent(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        upsert_escritos(db, case.id, [_escrito()])
        r2 = upsert_escritos(db, case.id, [_escrito()])
        assert r2[0][1] is False
        assert db.query(CaseEscrito).count() == 1


# ---------------------------------------------------------------------------
# S1-T11d: _sync_entities engine constraints (Slice 1 mode)
# ---------------------------------------------------------------------------


class TestSyncEntitiesSlice1Mode:
    def test_no_alerts_created(self, seeded_case):
        """_sync_entities must NOT create Alert rows when creates_alert=False."""
        db = seeded_case["db"]
        case = seeded_case["case"]
        _sync_entities(db, case.id, [_lit()], SPEC_LITIGANTE)
        assert db.query(Alert).count() == 0

    def test_no_notification_service_called(self, seeded_case):
        """_sync_entities must NOT touch NotificationService when notify=False."""
        db = seeded_case["db"]
        case = seeded_case["case"]
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            _sync_entities(db, case.id, [_lit()], SPEC_LITIGANTE)
        MockNotif.assert_not_called()

    def test_spec_litigante_creates_alert_false(self):
        """Litigantes are permanently silent (ADR-004)."""
        assert SPEC_LITIGANTE.creates_alert is False

    # S2-T01: flags flipped in Slice 2 — notificacion/escrito/exhorto now alert.
    def test_spec_notificacion_creates_alert_true(self):
        assert SPEC_NOTIFICACION.creates_alert is True

    def test_spec_escrito_creates_alert_true(self):
        assert SPEC_ESCRITO.creates_alert is True

    def test_spec_exhorto_creates_alert_true(self):
        assert SPEC_EXHORTO.creates_alert is True


# ---------------------------------------------------------------------------
# S1-T11e: end-to-end parse → persist using Slice 1a HTML fixtures
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _RICH_HTML.exists(), reason="Rich HTML fixture not found")
class TestSyncEntitiesEndToEnd:
    def _parse_rich(self):
        html = _RICH_HTML.read_text(encoding="utf-8")
        return CivilScraper()._parse_case_detail_html(html, "test-token")

    def _parse_synthetic(self):
        html = _SYNTHETIC_HTML.read_text(encoding="utf-8")
        return CivilScraper()._parse_case_detail_html(html, "test-token")

    def test_rich_fixture_inserts_6_litigantes(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_rich()
        _sync_entities(db, case.id, detail.litigantes, SPEC_LITIGANTE)
        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == case.id).count() == 6

    def test_litigantes_idempotent_on_second_run(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_rich()
        _sync_entities(db, case.id, detail.litigantes, SPEC_LITIGANTE)
        results2 = _sync_entities(db, case.id, detail.litigantes, SPEC_LITIGANTE)
        assert all(not is_new for _, is_new in results2)
        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == case.id).count() == 6

    def test_rich_fixture_inserts_1_exhorto(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_rich()
        _sync_entities(db, case.id, detail.exhortos, SPEC_EXHORTO)
        assert db.query(CaseExhorto).filter(CaseExhorto.case_id == case.id).count() == 1

    def test_exhorto_idempotent_on_second_run(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_rich()
        _sync_entities(db, case.id, detail.exhortos, SPEC_EXHORTO)
        results2 = _sync_entities(db, case.id, detail.exhortos, SPEC_EXHORTO)
        assert all(not is_new for _, is_new in results2)
        assert db.query(CaseExhorto).filter(CaseExhorto.case_id == case.id).count() == 1

    def test_rich_fixture_0_notificaciones_no_rows(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_rich()
        assert detail.notificaciones == []
        _sync_entities(db, case.id, detail.notificaciones, SPEC_NOTIFICACION)
        assert db.query(CaseNotificacion).count() == 0

    def test_synthetic_fixture_inserts_2_notificaciones(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_synthetic()
        _sync_entities(db, case.id, detail.notificaciones, SPEC_NOTIFICACION)
        assert db.query(CaseNotificacion).filter(CaseNotificacion.case_id == case.id).count() == 2

    def test_synthetic_fixture_inserts_2_escritos(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_synthetic()
        _sync_entities(db, case.id, detail.escritos, SPEC_ESCRITO)
        assert db.query(CaseEscrito).filter(CaseEscrito.case_id == case.id).count() == 2

    def test_no_alerts_when_called_without_context(self, seeded_case):
        """_sync_entities called without case/lawyer kwargs creates 0 alerts.

        Even though creates_alert=True on notificacion/escrito/exhorto specs,
        alert creation requires both 'case' and 'lawyer' to be passed explicitly.
        Callers that omit them (e.g. simple storage tests) remain safe.
        """
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._parse_rich()
        for items, spec in [
            (detail.litigantes, SPEC_LITIGANTE),
            (detail.notificaciones, SPEC_NOTIFICACION),
            (detail.escritos, SPEC_ESCRITO),
            (detail.exhortos, SPEC_EXHORTO),
        ]:
            _sync_entities(db, case.id, items, spec)
        assert db.query(Alert).count() == 0


# ---------------------------------------------------------------------------
# FIX 2 — updatable_fields: volatile fields are refreshed on re-sync
# ---------------------------------------------------------------------------


def _escrito_with_token(token: str) -> "PJUDEscrito":
    return PJUDEscrito(
        fecha_ingreso="10/06/2026",
        tipo_escrito="DEMANDA",
        solicitante="BANCO ITAU",
        tiene_documento=True,
        tiene_anexo=False,
        doc_token=token,
    )


class TestUpdatableFields:
    def test_escrito_doc_token_refreshed_on_resync(self, seeded_case):
        """Re-syncing an escrito with a new doc_token updates the stored token.
        The natural_key is unchanged (same fecha/tipo/solicitante/flags), so
        is_new must be False and the row's doc_token must reflect the new value.
        """
        db = seeded_case["db"]
        case = seeded_case["case"]

        r1 = upsert_escritos(db, case.id, [_escrito_with_token("token-v1")])
        assert r1[0][1] is True  # new row

        r2 = upsert_escritos(db, case.id, [_escrito_with_token("token-v2")])
        assert r2[0][1] is False  # same row, not new

        row = db.query(CaseEscrito).filter(CaseEscrito.case_id == case.id).one()
        assert row.doc_token == "token-v2"

    def test_exhorto_estado_refreshed_on_resync(self, seeded_case):
        """Re-syncing an exhorto with a changed estado updates estado in-place.
        The natural_key is unchanged (same rol_origen/rol_destino/tipo_exhorto),
        so is_new must be False and the row's estado must reflect the new value.
        """
        db = seeded_case["db"]
        case = seeded_case["case"]

        r1 = upsert_exhortos(db, case.id, [_exh()])  # estado="PENDIENTE"
        assert r1[0][1] is True

        exh_updated = PJUDExhorto(
            rol_origen="C-1253-2015",
            tipo_exhorto="ACTIVO",
            rol_destino="E-355-2026",
            fecha_ordena="01/01/2026",
            fecha_ingreso="02/01/2026",
            tribunal_destino="JUZGADO TEST",
            estado="CUMPLIDO",
        )
        r2 = upsert_exhortos(db, case.id, [exh_updated])
        assert r2[0][1] is False  # same row

        row = db.query(CaseExhorto).filter(CaseExhorto.case_id == case.id).one()
        assert row.estado == "CUMPLIDO"

    def test_notificacion_estado_notif_refreshed_on_resync(self, seeded_case):
        """Re-syncing a notificacion with a changed estado_notif updates the
        existing row.  estado_notif and obs_fallida are excluded from the natural
        key so state changes don't create duplicate rows.
        """
        db = seeded_case["db"]
        case = seeded_case["case"]

        r1 = upsert_notificaciones(db, case.id, [_notif()])  # estado_notif="REALIZADA"
        assert r1[0][1] is True

        notif_updated = PJUDNotificacion(
            rol="C-9999-2025",
            estado_notif="FALLIDA",
            tipo_notif="PERSONAL",
            fecha_tramite="10/06/2026",
            tipo_participante="DDO.",
            nombre="FERNANDEZ",
            tramite="Demanda",
            obs_fallida="Dirección no encontrada",
        )
        r2 = upsert_notificaciones(db, case.id, [notif_updated])
        assert r2[0][1] is False  # same row

        row = db.query(CaseNotificacion).filter(CaseNotificacion.case_id == case.id).one()
        assert row.estado_notif == "FALLIDA"
        assert row.obs_fallida == "Dirección no encontrada"

    def test_litigante_has_no_updatable_fields(self):
        """Litigantes are static — SPEC_LITIGANTE.updatable_fields must be empty."""
        assert SPEC_LITIGANTE.updatable_fields == []


# ---------------------------------------------------------------------------
# FIX 1 — sync_case_detail: atomicity contract
# ---------------------------------------------------------------------------


class TestSyncCaseDetailAtomicity:
    def _make_detail(self) -> Any:
        from app.scrapper.pjud.base import PJUDCase, PJUDCaseDetail

        case_meta = PJUDCase(
            rol="C-9999-2025",
            tribunal="TEST",
            caratulado="A/B",
            fecha_ingreso="01/01/2026",
        )
        return PJUDCaseDetail(
            case=case_meta,
            litigantes=[_lit()],
            notificaciones=[_notif()],
            escritos=[_escrito()],
            exhortos=[_exh()],
        )

    def test_sync_case_detail_inserts_all_entities(self, seeded_case):
        """Happy path: sync_case_detail persists all four entity types in one commit."""
        db = seeded_case["db"]
        case = seeded_case["case"]
        counts = sync_case_detail(db, case.id, self._make_detail())

        assert counts["litigantes_new"] == 1
        assert counts["notificaciones_new"] == 1
        assert counts["escritos_new"] == 1
        assert counts["exhortos_new"] == 1
        assert db.query(CaseLitigante).count() == 1
        assert db.query(CaseNotificacion).count() == 1
        assert db.query(CaseEscrito).count() == 1
        assert db.query(CaseExhorto).count() == 1

    def test_sync_case_detail_rollback_on_failure(self, seeded_case):
        """If any entity upsert raises after others have flushed, rolling back
        leaves the DB empty — litigantes/notificaciones/escritos that were flushed
        must NOT survive when the commit is never reached.
        """
        db = seeded_case["db"]
        case = seeded_case["case"]
        detail = self._make_detail()

        with patch(
            "app.services.sync_service.upsert_exhortos",
            side_effect=RuntimeError("simulated failure"),
        ):
            with pytest.raises(RuntimeError, match="simulated failure"):
                sync_case_detail(db, case.id, detail)

        db.rollback()

        assert db.query(CaseLitigante).count() == 0
        assert db.query(CaseNotificacion).count() == 0
        assert db.query(CaseEscrito).count() == 0
        assert db.query(CaseExhorto).count() == 0


# ---------------------------------------------------------------------------
# S2-T01: Alert entity_type / entity_id wiring
# ---------------------------------------------------------------------------


class TestSyncEntitiesSlice2AlertWiring:
    """S2-T01 — _sync_entities creates polymorphic Alert rows when
    spec.creates_alert=True AND case+lawyer context is provided."""

    def test_new_exhorto_creates_alert_with_entity_type(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]

        results = _sync_entities(
            db, case.id, [_exh()], SPEC_EXHORTO,
            case=case, lawyer=lawyer,
        )
        alerts = db.query(Alert).all()

        assert len(alerts) == 1
        assert alerts[0].type == "new_exhorto"
        assert alerts[0].entity_type == "exhorto"
        assert alerts[0].entity_id == results[0][0].id
        assert alerts[0].lawyer_id == lawyer.id
        assert alerts[0].case_id == case.id

    def test_existing_exhorto_creates_no_additional_alert(self, seeded_case):
        """Re-syncing an existing exhorto must not create a duplicate alert."""
        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]

        _sync_entities(db, case.id, [_exh()], SPEC_EXHORTO, case=case, lawyer=lawyer)
        _sync_entities(db, case.id, [_exh()], SPEC_EXHORTO, case=case, lawyer=lawyer)

        assert db.query(Alert).count() == 1  # only first insert triggered an alert

    def test_new_notificacion_creates_alert(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]

        _sync_entities(
            db, case.id, [_notif("FERNANDEZ"), _notif("GONZALEZ")], SPEC_NOTIFICACION,
            case=case, lawyer=lawyer,
        )
        alerts = db.query(Alert).all()

        assert len(alerts) == 2
        for a in alerts:
            assert a.entity_type == "notificacion"
            assert a.type == "new_notificacion"

    def test_new_escrito_creates_alert(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]

        _sync_entities(
            db, case.id, [_escrito("BANCO ITAU"), _escrito("BANCO BCI")], SPEC_ESCRITO,
            case=case, lawyer=lawyer,
        )
        alerts = db.query(Alert).all()

        assert len(alerts) == 2
        for a in alerts:
            assert a.entity_type == "escrito"
            assert a.type == "new_escrito"

    def test_litigante_creates_no_alert(self, seeded_case):
        """ADR-004: litigantes are SILENT — no alerts, no notifications ever."""
        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]

        items = [_lit(rut=f"1111111{i}-{i}", participante="DTE.") for i in range(6)]
        _sync_entities(
            db, case.id, items, SPEC_LITIGANTE,
            case=case, lawyer=lawyer,
        )

        assert db.query(Alert).count() == 0


# ---------------------------------------------------------------------------
# S2-T03: NotifyBudget + shared budget across entity types
# ---------------------------------------------------------------------------


class TestNotifyBudget:
    def test_budget_starts_with_full_remaining(self):
        b = NotifyBudget(5)
        assert b.remaining == 5

    def test_decrement_reduces_remaining(self):
        b = NotifyBudget(3)
        b.decrement()
        assert b.remaining == 2

    def test_exhausted_when_remaining_zero(self):
        b = NotifyBudget(1)
        b.decrement()
        assert b.exhausted() is True

    def test_decrement_below_zero_stays_at_zero(self):
        b = NotifyBudget(1)
        b.decrement()
        b.decrement()  # should be a no-op
        assert b.remaining == 0

    def test_not_exhausted_when_remaining_positive(self):
        b = NotifyBudget(2)
        assert b.exhausted() is False


class TestSyncEntitiesBudgetDispatch:
    """S2-T03 — _sync_entities dispatches notifications through the shared budget."""

    def test_sync_entities_calls_notify_fn_per_new_row(self, seeded_case):
        """3 new exhortos → notify_new_exhorto called 3 times (budget ≥ 3)."""
        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]
        budget = NotifyBudget(10)
        mock_notif_svc = MagicMock()

        exhortos = [
            _exh(rol_origen=f"C-{i}-2015", rol_destino=f"E-{i}-2026")
            for i in range(1, 4)
        ]
        _sync_entities(
            db, case.id, exhortos, SPEC_EXHORTO,
            case=case, lawyer=lawyer, webhooks=[],
            notification_svc=mock_notif_svc, budget=budget,
        )

        assert mock_notif_svc.notify_new_exhorto.call_count == 3
        assert budget.remaining == 7  # 10 - 3

    def test_litigante_notify_never_called_even_when_new(self, seeded_case):
        """ADR-004: litigantes SILENT — no notify call regardless of context."""
        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]
        budget = NotifyBudget(10)
        mock_notif_svc = MagicMock()

        items = [_lit(rut=f"1111111{i}-{i}", participante="DTE.") for i in range(6)]
        _sync_entities(
            db, case.id, items, SPEC_LITIGANTE,
            case=case, lawyer=lawyer, webhooks=[],
            notification_svc=mock_notif_svc, budget=budget,
        )

        # No alerts, no notify calls, budget untouched
        assert db.query(Alert).count() == 0
        assert budget.remaining == 10
        mock_notif_svc.notify_new_litigante.assert_not_called()

    def test_budget_cap_across_entity_types(self, seeded_case, caplog):
        """ADR-005: shared cap of 2 across notificacion + escrito types.

        3 new items total → 2 dispatches (cap), all 3 alerts persisted.
        A cap-reached warning is logged.
        """
        import logging

        db = seeded_case["db"]
        case = seeded_case["case"]
        lawyer = seeded_case["lawyer"]
        budget = NotifyBudget(2)
        mock_notif_svc = MagicMock()

        # 1 notificacion (1 dispatch)
        _sync_entities(
            db, case.id, [_notif()], SPEC_NOTIFICACION,
            case=case, lawyer=lawyer, webhooks=[],
            notification_svc=mock_notif_svc, budget=budget,
        )
        # 2 escritos — budget has 1 remaining → only 1 more dispatch
        _sync_entities(
            db, case.id, [_escrito("BCI"), _escrito("ITAU")], SPEC_ESCRITO,
            case=case, lawyer=lawyer, webhooks=[],
            notification_svc=mock_notif_svc, budget=budget,
        )

        # 3 alerts persisted (always, regardless of budget)
        assert db.query(Alert).count() == 3
        # Only 2 dispatches
        total_dispatches = (
            mock_notif_svc.notify_new_notificacion.call_count
            + mock_notif_svc.notify_new_escrito.call_count
        )
        assert total_dispatches == 2
        assert budget.exhausted()

    def test_sync_movements_accepts_budget_kwarg(self, sqlite_db, caplog):
        """sync_movements accepts budget as keyword-only arg; backward compat preserved."""
        from app.models.webhook import Webhook
        from app.services.sync_service import ScrapedMovement

        db = sqlite_db

        lawyer = Lawyer(
            rut="12345678-9",
            name="Test Lawyer",
            email="t@t.cl",
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(lawyer)
        db.flush()
        court = Court(code="T-BUDGET", name="Budget Test Court", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id,
            court_id=court.id,
            rol="C-BUDGET-2025",
            status="active",
            competencia="civil",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(case)
        db.commit()

        budget = NotifyBudget(5)
        sync = SyncService(db)

        with patch("app.services.sync_service.NotificationService"):
            new_count, alert_count = sync.sync_movements(
                case.id,
                [ScrapedMovement(
                    folio="1",
                    fecha="11/06/2026",
                    tipo_tramite="Resolución",
                    descripcion="Test",
                )],
                budget=budget,
            )

        assert new_count == 1
        assert alert_count == 1
        # Budget was decremented by one movement dispatch
        assert budget.remaining == 4


# ---------------------------------------------------------------------------
# S1-T12: detect_and_sync_movements persists all entity types
# ---------------------------------------------------------------------------


_RICH_HTML_PATH = _FIXTURE_DIR / "detail_civil_rich.html"


@pytest.mark.skipif(
    not _RICH_HTML_PATH.exists(),
    reason="Rich HTML fixture not found",
)
class TestDetectAndSyncStoresAllEntityTypes:
    """S1-T12 — detect_and_sync_movements calls entity sync after movement sync.

    Uses the real rich fixture (6 litigantes, 3 movements, 1 exhorto).
    Scraper is mocked to return the parsed PJUDCaseDetail without a live fetch.
    """

    async def test_detect_and_sync_stores_all_entity_types(self, seeded_case):
        """After detect_and_sync_movements: 6 litigantes + 1 exhorto + 0 notif + 0 escritos."""
        db = seeded_case["db"]
        case = seeded_case["case"]

        # Parse the real fixture (no live network call)
        rich_html = _RICH_HTML_PATH.read_text(encoding="utf-8")
        detail = CivilScraper()._parse_case_detail_html(rich_html, "token")

        # Build a minimal PJUDCase stub matching the seeded case ROL
        api_case = MagicMock()
        api_case.rol = case.rol  # "C-9999-2025"
        api_case.case_token = "test-token"
        # detail.movements = list of PJUDMovement (3 items in rich fixture)
        for m in detail.movements:
            api_case.folio = getattr(m, "folio", "")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=detail)
        mock_pjud_session = MagicMock()

        with patch("app.services.sync_service.NotificationService"):
            movements_new, alerts_created, errors = await detect_and_sync_movements(
                db=db,
                scraper=mock_scraper,
                pjud_session=mock_pjud_session,
                lawyer_id=case.lawyer_id,
                api_cases=[api_case],
            )

        assert errors == [], f"Unexpected errors: {errors}"
        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == case.id).count() == 6
        assert db.query(CaseExhorto).filter(CaseExhorto.case_id == case.id).count() == 1
        assert db.query(CaseNotificacion).filter(CaseNotificacion.case_id == case.id).count() == 0
        assert db.query(CaseEscrito).filter(CaseEscrito.case_id == case.id).count() == 0

    async def test_detect_and_sync_idempotent_on_second_run(self, seeded_case):
        """Running detect_and_sync_movements twice creates no duplicate entities."""
        db = seeded_case["db"]
        case = seeded_case["case"]

        rich_html = _RICH_HTML_PATH.read_text(encoding="utf-8")
        detail = CivilScraper()._parse_case_detail_html(rich_html, "token")

        api_case = MagicMock()
        api_case.rol = case.rol
        api_case.case_token = "test-token"

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=detail)

        with patch("app.services.sync_service.NotificationService"):
            await detect_and_sync_movements(
                db=db, scraper=mock_scraper, pjud_session=MagicMock(),
                lawyer_id=case.lawyer_id, api_cases=[api_case],
            )
            await detect_and_sync_movements(
                db=db, scraper=mock_scraper, pjud_session=MagicMock(),
                lawyer_id=case.lawyer_id, api_cases=[api_case],
            )

        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == case.id).count() == 6
        assert db.query(CaseExhorto).filter(CaseExhorto.case_id == case.id).count() == 1
