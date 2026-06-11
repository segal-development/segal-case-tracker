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
from unittest.mock import patch

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
    _sync_entities,
    escrito_natural_key,
    exhorto_natural_key,
    litigante_natural_key,
    normalize_cell,
    notificacion_natural_key,
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
    """Lawyer + Court + Case pre-seeded."""
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
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    return {"db": db, "case": case}


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
        assert SPEC_LITIGANTE.creates_alert is False

    def test_spec_notificacion_creates_alert_false(self):
        assert SPEC_NOTIFICACION.creates_alert is False

    def test_spec_escrito_creates_alert_false(self):
        assert SPEC_ESCRITO.creates_alert is False

    def test_spec_exhorto_creates_alert_false(self):
        assert SPEC_EXHORTO.creates_alert is False


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

    def test_no_alerts_after_full_sync(self, seeded_case):
        """After syncing all entity types, Alert table must remain empty."""
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
