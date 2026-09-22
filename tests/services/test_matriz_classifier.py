"""Tests for the matriz de clasificación classifier and seeder.

Covers: seeder idempotency, each precedence branch, trámite-override
priority, the "Accion de Prescripcion" documented exception, and
most-recent-movement selection.
"""
from datetime import datetime, timedelta

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.matriz_pjud_mapeo import MatrizPjudMapeo
from app.models.matriz_tramite_override import MatrizTramiteOverride
from app.models.movement import Movement
from app.services.matriz_classifier import (
    DEFAULT_PROC_SIMPLE,
    MatrizMappingCache,
    ORIGEN_ETAPA,
    ORIGEN_NO_MAPEADA,
    ORIGEN_PROCEDIMIENTO,
    ORIGEN_SIN_DETALLE,
    classify_case,
)
from app.services.matriz_seed import seed_matriz


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Test Lawyer", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-MTZ", name="Juzgado Matriz", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol="C-1-2025", **kwargs):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _make_movement(db, case, *, stage, description="mov", procedure=None, movement_date=None):
    obj = Movement(
        case_id=case.id,
        stage=stage,
        procedure=procedure,
        description=description,
        movement_date=movement_date or datetime(2026, 1, 1),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _mapeo(db, pjud_stage, matriz_etapa, activo=True):
    obj = MatrizPjudMapeo(pjud_stage=pjud_stage, matriz_etapa=matriz_etapa, activo=activo)
    db.add(obj); db.commit()
    return obj


def _clasificacion(db, proc_simple, etapa, matriz, proc_antiguo="JUICIO EJECUTIVO"):
    obj = MatrizClasificacion(
        proc_simple=proc_simple, proc_antiguo=proc_antiguo, etapa=etapa, matriz=matriz
    )
    db.add(obj); db.commit()
    return obj


def _override(db, proc_antiguo, etapa, nombre_tramite, matriz):
    obj = MatrizTramiteOverride(
        proc_antiguo=proc_antiguo, etapa=etapa, nombre_tramite=nombre_tramite, matriz=matriz
    )
    db.add(obj); db.commit()
    return obj


# ---------------------------------------------------------------------------
# Seeder idempotency
# ---------------------------------------------------------------------------


class TestSeederIdempotency:
    def test_seed_twice_same_counts(self, db):
        first = seed_matriz(db)
        assert first["clasificacion"]["created"] == 114
        assert first["tramite_override"]["created"] == 80
        assert first["pjud_mapeo"]["created"] == 33

        second = seed_matriz(db)
        assert second["clasificacion"]["created"] == 0
        assert second["clasificacion"]["updated"] == 114
        assert second["tramite_override"]["created"] == 0
        assert second["tramite_override"]["updated"] == 80
        assert second["pjud_mapeo"]["created"] == 0
        assert second["pjud_mapeo"]["updated"] == 33

        assert db.query(MatrizClasificacion).count() == 114
        assert db.query(MatrizTramiteOverride).count() == 80
        assert db.query(MatrizPjudMapeo).count() == 33

    def test_seed_preserves_manually_deactivated_mapeo(self, db):
        seed_matriz(db)
        row = db.query(MatrizPjudMapeo).filter_by(pjud_stage="Ingreso").first()
        row.activo = False
        db.commit()

        seed_matriz(db)
        db.refresh(row)
        assert row.activo is False


# ---------------------------------------------------------------------------
# Precedence branches
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_apremio_procedure_wins_regardless_of_last_stage(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, procedure="Juicio Ejecutivo en Apremio")
        _make_movement(db, case, stage="Notificación")  # would map to a non-M3 etapa
        _mapeo(db, "Notificación", "PENDIENTE DE NOTIFICACIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "PENDIENTE DE NOTIFICACIÓN", "M1 Baja")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M3"
        assert result.matriz_etapa == "APREMIO"
        assert result.origen == ORIGEN_PROCEDIMIENTO

    def test_terceria_procedure_wins(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, procedure="Solo Tercería de Dominio")
        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)
        assert result.matriz == "M3"
        assert result.origen == ORIGEN_PROCEDIMIENTO

    def test_mapped_stage_resolves_via_clasificacion(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Notificación demanda y su proveído")
        _mapeo(db, "Notificación demanda y su proveído", "DEMANDA NOTIFICADA")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "DEMANDA NOTIFICADA", "M2")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M2"
        assert result.matriz_etapa == "DEMANDA NOTIFICADA"
        assert result.origen == ORIGEN_ETAPA

    def test_no_movements_gives_provisional_m1_baja(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M1 Baja"
        assert result.origen == ORIGEN_SIN_DETALLE
        assert result.matriz_etapa == "ASIGNACIÓN / PENDIENTE DE NOTIFICACIÓN"

    def test_unmapped_pjud_stage_is_no_mapeada(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Una Etapa Desconocida")
        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz is None
        assert result.origen == ORIGEN_NO_MAPEADA
        assert result.detalle == "Una Etapa Desconocida"

    def test_mapped_stage_without_clasificacion_row_is_no_mapeada(self, db, lawyer, court):
        """Mirrors the real JUDICIAL etapa gap: a stage maps to a matriz etapa
        that has no row for the causa's (fallback) proc_simple."""
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Tramitación Liquidación")
        _mapeo(db, "Tramitación Liquidación", "JUDICIAL")
        # No MatrizClasificacion row for (DEFAULT_PROC_SIMPLE, "JUDICIAL").

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz is None
        assert result.matriz_etapa == "JUDICIAL"
        assert result.origen == ORIGEN_NO_MAPEADA

    def test_inactive_mapeo_row_treated_as_unmapped(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Ingreso")
        _mapeo(db, "Ingreso", "ASIGNACIÓN", activo=False)

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.origen == ORIGEN_NO_MAPEADA


# ---------------------------------------------------------------------------
# Trámite override
# ---------------------------------------------------------------------------


class TestTramiteOverride:
    def test_override_beats_etapa_level_matriz(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Ingreso", procedure="Asignación Abogado")
        _mapeo(db, "Ingreso", "ASIGNACIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "ASIGNACIÓN", "M1 Baja")
        _override(db, "JUICIO EJECUTIVO", "ASIGNACIÓN", "Asignación Abogado", "M3")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M3"  # override, not the M1 Baja etapa-level value
        assert result.origen == ORIGEN_ETAPA

    def test_no_matching_tramite_keeps_etapa_level_matriz(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Ingreso", procedure="Otro Trámite Cualquiera")
        _mapeo(db, "Ingreso", "ASIGNACIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "ASIGNACIÓN", "M1 Baja")
        _override(db, "JUICIO EJECUTIVO", "ASIGNACIÓN", "Asignación Abogado", "M3")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M1 Baja"


# ---------------------------------------------------------------------------
# Accion de Prescripcion exception
# ---------------------------------------------------------------------------


class TestAccionDePrescripcionException:
    def test_default_map_gives_m1_alta(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)  # matriz_proc_simple = None -> default JEC map
        _make_movement(db, case, stage="Excepción de Prescripción Alegada")
        _mapeo(db, "Excepción de Prescripción Alegada", "INGRESO EXCEPCIONES DE PRESCRIPCIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "INGRESO EXCEPCIONES DE PRESCRIPCIÓN", "M1 Alta")
        _clasificacion(db, "Accion de Prescripcion", "INGRESO EXCEPCIONES DE PRESCRIPCIÓN", "M1 Baja")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M1 Alta"

    def test_accion_de_prescripcion_gives_m1_baja(self, db, lawyer, court):
        case = _make_case(
            db, lawyer, court, matriz_proc_simple="Accion de Prescripcion"
        )
        _make_movement(db, case, stage="Excepción de Prescripción Alegada")
        _mapeo(db, "Excepción de Prescripción Alegada", "INGRESO EXCEPCIONES DE PRESCRIPCIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "INGRESO EXCEPCIONES DE PRESCRIPCIÓN", "M1 Alta")
        _clasificacion(db, "Accion de Prescripcion", "INGRESO EXCEPCIONES DE PRESCRIPCIÓN", "M1 Baja")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M1 Baja"


# ---------------------------------------------------------------------------
# Most-recent movement selection
# ---------------------------------------------------------------------------


class TestMostRecentMovement:
    def test_most_recent_movement_wins(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _mapeo(db, "Ingreso", "ASIGNACIÓN")
        _mapeo(db, "Notificación demanda y su proveído", "DEMANDA NOTIFICADA")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "ASIGNACIÓN", "M1 Baja")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "DEMANDA NOTIFICADA", "M2")

        _make_movement(db, case, stage="Ingreso", movement_date=datetime(2026, 1, 1))
        _make_movement(
            db, case, stage="Notificación demanda y su proveído",
            movement_date=datetime(2026, 2, 1),
        )

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M2"
        assert result.matriz_etapa == "DEMANDA NOTIFICADA"

    def test_blank_stage_movement_is_skipped(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _mapeo(db, "Ingreso", "ASIGNACIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "ASIGNACIÓN", "M1 Baja")

        _make_movement(db, case, stage="Ingreso", movement_date=datetime(2026, 1, 1))
        _make_movement(db, case, stage=None, movement_date=datetime(2026, 2, 1))
        _make_movement(db, case, stage="", movement_date=datetime(2026, 3, 1))

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz_etapa == "ASIGNACIÓN"


# ---------------------------------------------------------------------------
# Crash-proofing
# ---------------------------------------------------------------------------


class TestWithRealSeedData:
    """End-to-end sanity over the ACTUAL committed CSVs (app/data/matriz/),
    not synthetic fixture rows — catches drift if the CSVs are regenerated."""

    def test_demanda_notificada_default_map(self, db, lawyer, court):
        seed_matriz(db)
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Notificación demanda y su proveído")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M2"
        assert result.matriz_etapa == "DEMANDA NOTIFICADA"
        assert result.origen == ORIGEN_ETAPA

    def test_terminada_gives_no_active_matriz(self, db, lawyer, court):
        """CAUSA ARCHIVADA legitimately has NO matriz assigned (not an error)."""
        seed_matriz(db)
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Terminada")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz is None
        assert result.matriz_etapa == "CAUSA ARCHIVADA"
        assert result.origen == ORIGEN_ETAPA  # resolved, just no matriz for this etapa

    def test_tramitacion_liquidacion_unmapped_for_default_proc_simple(self, db, lawyer, court):
        """Documented real gap: 'Tramitación Liquidación' maps to the JUDICIAL
        matriz-etapa, which only exists for Insolvencia procedures — a causa
        falling back to the default Juicio Ejecutivo map has no row for it."""
        seed_matriz(db)
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage="Tramitación Liquidación")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz is None
        assert result.matriz_etapa == "JUDICIAL"
        assert result.origen == ORIGEN_NO_MAPEADA


class TestCrashProof:
    def test_exception_returns_indeterminate_not_raise(self, db, lawyer, court, monkeypatch):
        case = _make_case(db, lawyer, court)

        import app.services.matriz_classifier as mod

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "_classify_case", _boom)
        cache = MatrizMappingCache.load(db)
        result = mod.classify_case(db, case, mapping_cache=cache)

        assert result.matriz is None
        assert result.origen == "error"
