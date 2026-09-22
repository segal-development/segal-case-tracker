"""Tests for the matriz de clasificación classifier and seeder.

Covers: seeder idempotency, each precedence branch, the description
fallback layer, trámite-override priority, the "Accion de Prescripcion"
documented exception, and most-recent-movement selection.
"""
from datetime import datetime, timedelta

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.matriz_pjud_mapeo import (
    MATCH_TIPO_DESCRIPCION,
    MATCH_TIPO_STAGE,
    MatrizPjudMapeo,
)
from app.models.matriz_tramite_override import MatrizTramiteOverride
from app.models.movement import Movement
from app.services.matriz_classifier import (
    DEFAULT_PROC_SIMPLE,
    MatrizMappingCache,
    ORIGEN_DESCRIPCION,
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


def _mapeo(db, pjud_stage, matriz_etapa, activo=True, match_tipo=MATCH_TIPO_STAGE, orden=0):
    obj = MatrizPjudMapeo(
        pjud_stage=pjud_stage,
        matriz_etapa=matriz_etapa,
        activo=activo,
        match_tipo=match_tipo,
        orden=orden,
    )
    db.add(obj); db.commit()
    return obj


def _descripcion_rule(db, substring, matriz_etapa, orden=0, activo=True):
    return _mapeo(db, substring, matriz_etapa, activo=activo, match_tipo=MATCH_TIPO_DESCRIPCION, orden=orden)


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
        assert first["pjud_mapeo"]["created"] == 39  # 33 stage + 6 descripcion fallback rules

        second = seed_matriz(db)
        assert second["clasificacion"]["created"] == 0
        assert second["clasificacion"]["updated"] == 114
        assert second["tramite_override"]["created"] == 0
        assert second["tramite_override"]["updated"] == 80
        assert second["pjud_mapeo"]["created"] == 0
        assert second["pjud_mapeo"]["updated"] == 39

        assert db.query(MatrizClasificacion).count() == 114
        assert db.query(MatrizTramiteOverride).count() == 80
        assert db.query(MatrizPjudMapeo).count() == 39
        assert (
            db.query(MatrizPjudMapeo).filter(MatrizPjudMapeo.match_tipo == MATCH_TIPO_DESCRIPCION).count()
            == 6
        )

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

    def test_truly_last_movement_used_even_if_blank_stage(self, db, lawyer, court):
        """The TRULY most recent movement decides classification — an older,
        properly-staged movement is never used as a fallback once a newer
        blank-stage movement exists. (When that newest movement's
        description ALSO fails to match, the causa is no_mapeada — see
        TestDescripcionFallback for the case where it succeeds.)"""
        case = _make_case(db, lawyer, court)
        _mapeo(db, "Ingreso", "ASIGNACIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "ASIGNACIÓN", "M1 Baja")

        _make_movement(db, case, stage="Ingreso", movement_date=datetime(2026, 1, 1))
        _make_movement(
            db, case, stage="", description="algo sin relevancia",
            movement_date=datetime(2026, 3, 1),
        )

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz is None
        assert result.origen == ORIGEN_NO_MAPEADA


# ---------------------------------------------------------------------------
# Description fallback layer
# ---------------------------------------------------------------------------


class TestDescripcionFallback:
    def test_fires_when_stage_is_blank(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _descripcion_rule(db, "Archivo del expediente en el Tribunal", "CAUSA ARCHIVADA", orden=1)
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "CAUSA ARCHIVADA", None)

        _make_movement(
            db, case, stage=None, description="Archivo del expediente en el Tribunal",
        )

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz_etapa == "CAUSA ARCHIVADA"
        assert result.origen == ORIGEN_DESCRIPCION

    def test_fires_when_stage_has_no_active_mapping(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _descripcion_rule(db, "Cita a Audiencia", "FASE DECLARATIVA", orden=1)
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "FASE DECLARATIVA", "M2")

        _make_movement(
            db, case, stage="Una Etapa Rara Sin Mapeo", description="Se Cita a Audiencia de rigor",
        )

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M2"
        assert result.origen == ORIGEN_DESCRIPCION

    def test_never_overrides_a_good_stage_match(self, db, lawyer, court):
        """A stage rule matching the description substring too must NOT
        change the result — the stage match wins outright, description is
        never even consulted."""
        case = _make_case(db, lawyer, court)
        _mapeo(db, "Ingreso", "ASIGNACIÓN")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "ASIGNACIÓN", "M1 Baja")
        # Would resolve to a totally different etapa if consulted — proves it's not.
        _descripcion_rule(db, "notificacion", "FASE DECLARATIVA", orden=1)
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "FASE DECLARATIVA", "M2")

        _make_movement(db, case, stage="Ingreso", description="Ingreso y notificación conjunta")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz == "M1 Baja"
        assert result.origen == ORIGEN_ETAPA

    def test_precedence_and_ordering(self, db, lawyer, court):
        """Two rules could both match the same description — the lower
        ``orden`` wins, deterministically."""
        case = _make_case(db, lawyer, court)
        _descripcion_rule(db, "prueba", "APREMIO", orden=1)
        _descripcion_rule(db, "recibe la causa a prueba", "FASE DECLARATIVA", orden=2)
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "APREMIO", "M3")
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "FASE DECLARATIVA", "M2")

        _make_movement(db, case, stage=None, description="Recibe la causa a prueba")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        # orden=1 ("prueba") is a substring too and evaluated first.
        assert result.matriz_etapa == "APREMIO"
        assert result.matriz == "M3"

    def test_accent_insensitive_matching(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _descripcion_rule(db, "prescripcion", "INGRESO EXCEPCIONES DE PRESCRIPCIÓN", orden=1)
        _clasificacion(db, DEFAULT_PROC_SIMPLE, "INGRESO EXCEPCIONES DE PRESCRIPCIÓN", "M1 Alta")

        _make_movement(db, case, stage=None, description="Se alega PRESCRIPCIÓN de la acción")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz_etapa == "INGRESO EXCEPCIONES DE PRESCRIPCIÓN"
        assert result.matriz == "M1 Alta"
        assert result.origen == ORIGEN_DESCRIPCION

    def test_no_match_falls_through_to_no_mapeada(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _descripcion_rule(db, "archivo del expediente", "CAUSA ARCHIVADA", orden=1)

        _make_movement(db, case, stage=None, description="Un evento cualquiera sin match")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz is None
        assert result.origen == ORIGEN_NO_MAPEADA
        assert result.detalle == "Un evento cualquiera sin match"

    def test_inactive_descripcion_rule_is_ignored(self, db, lawyer, court):
        case = _make_case(db, lawyer, court)
        _descripcion_rule(db, "archivo del expediente", "CAUSA ARCHIVADA", orden=1, activo=False)

        _make_movement(db, case, stage=None, description="Archivo del expediente en el Tribunal")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.origen == ORIGEN_NO_MAPEADA


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

    def test_real_descripcion_fallback_archivo_expediente(self, db, lawyer, court):
        """The measured top real-DB gap: blank stage + this description is
        314 causas (2026-09) — now resolved via the seeded fallback rule."""
        seed_matriz(db)
        case = _make_case(db, lawyer, court)
        _make_movement(db, case, stage=None, description="Archivo del expediente en el Tribunal")

        cache = MatrizMappingCache.load(db)
        result = classify_case(db, case, mapping_cache=cache)

        assert result.matriz_etapa == "CAUSA ARCHIVADA"
        assert result.origen == ORIGEN_DESCRIPCION

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


# ---------------------------------------------------------------------------
# Bulk preload of the latest movement (backfill performance)
# ---------------------------------------------------------------------------


def _preload_fixture(db, lawyer, court):
    """Three cases covering every branch the preload has to reproduce."""
    _mapeo(db, "Apremio", "APREMIO")
    _descripcion_rule(db, "Archivo del expediente", "CAUSA ARCHIVADA", orden=1)
    _clasificacion(db, DEFAULT_PROC_SIMPLE, "APREMIO", "M3")
    _clasificacion(db, DEFAULT_PROC_SIMPLE, "CAUSA ARCHIVADA", "M1 Baja")

    con_etapa = _make_case(db, lawyer, court, rol="C-100-2026")
    _make_movement(db, con_etapa, stage="Apremio", movement_date=datetime(2026, 1, 1))
    # A newer movement with a blank stage must win and fall through to the
    # description layer — the same tie-break the per-case query applies.
    _make_movement(
        db, con_etapa, stage="", description="Archivo del expediente en el Tribunal",
        movement_date=datetime(2026, 5, 1),
    )
    solo_etapa = _make_case(db, lawyer, court, rol="C-200-2026")
    _make_movement(db, solo_etapa, stage="Apremio", movement_date=datetime(2026, 2, 1))
    sin_movimientos = _make_case(db, lawyer, court, rol="C-300-2026")
    return [con_etapa, solo_etapa, sin_movimientos]


def test_preload_matches_per_case_query(db, lawyer, court):
    """Preloaded classification must equal the one-query-per-case result."""
    cases = _preload_fixture(db, lawyer, court)

    sin_preload = MatrizMappingCache.load(db)
    esperado = [classify_case(db, c, mapping_cache=sin_preload) for c in cases]

    con_preload = MatrizMappingCache.load(db)
    con_preload.preload_latest_movements(db, [c.id for c in cases])
    obtenido = [classify_case(db, c, mapping_cache=con_preload) for c in cases]

    assert obtenido == esperado
    assert [r.matriz for r in obtenido] == ["M1 Baja", "M3", "M1 Baja"]
    assert [r.origen for r in obtenido] == [
        ORIGEN_DESCRIPCION, ORIGEN_ETAPA, ORIGEN_SIN_DETALLE,
    ]


def test_preload_issues_no_per_case_query(db, lawyer, court):
    """A preloaded case must not hit the DB again — that was the backfill bug."""
    cases = _preload_fixture(db, lawyer, court)
    cache = MatrizMappingCache.load(db)
    cache.preload_latest_movements(db, [c.id for c in cases])

    calls = {"n": 0}
    original = db.query

    def contando(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    db.query = contando
    try:
        for case in cases:
            classify_case(db, case, mapping_cache=cache)
    finally:
        db.query = original

    assert calls["n"] == 0


def test_preload_without_ids_covers_every_case(db, lawyer, court):
    """``case_ids=None`` preloads the whole portfolio in one pass."""
    cases = _preload_fixture(db, lawyer, court)
    cache = MatrizMappingCache.load(db)
    cache.preload_latest_movements(db)

    assert {c.id for c in cases} <= cache.preloaded_case_ids
    # The case with no movements is marked as preloaded but holds no entry,
    # which is what lets the classifier tell "no movements" from "not loaded".
    assert cases[2].id not in cache.latest_movement
    assert classify_case(db, cases[2], mapping_cache=cache).origen == ORIGEN_SIN_DETALLE


def test_preload_empty_id_list_is_a_noop(db, lawyer, court):
    cache = MatrizMappingCache.load(db)
    cache.preload_latest_movements(db, [])
    assert cache.preloaded_case_ids == set()
    assert cache.latest_movement == {}
