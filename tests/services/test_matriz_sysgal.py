"""Matriz precedence with Sysgal data: sysgal matriz > sysgal etapa > PJUD path.

Also covers the bulk preload (no N+1), the targeted reclassification used after the
daily Sysgal sync, and that cases without Sysgal are classified exactly as before.
"""
from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.matriz_pjud_mapeo import MatrizPjudMapeo
from app.models.matriz_tramite_override import MatrizTramiteOverride
from app.models.movement import Movement
from app.models.sysgal_causa import SysgalCausa
from app.services.matriz_classifier import MatrizMappingCache, classify_case
from app.services.matriz_sysgal import reclassify_cases

PS = "Juicio Ejecutivo Completo"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Test Lawyer", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-MSG", name="Juzgado Sysgal", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def taxonomy(db):
    """PJUD stage 'Ingreso' -> ASIGNACIÓN (M1 Baja); Sysgal-side etapas below."""
    db.add(MatrizPjudMapeo(pjud_stage="Ingreso", matriz_etapa="ASIGNACIÓN"))
    for etapa, matriz in [
        ("ASIGNACIÓN", "M1 Baja"),
        ("EXHIBICIÓN DOCUMENTOS", "M2"),
        ("INGRESO ABANDONO DE PROCEDIMIENTO 3 AÑOS", "M1 Alta"),
        ("APREMIO", "M3"),
    ]:
        db.add(MatrizClasificacion(proc_simple=PS, proc_antiguo="JUICIO EJECUTIVO", etapa=etapa, matriz=matriz))
    db.commit()


def _case(db, lawyer, court, rol="C-1-2025", **kw):
    obj = Case(lawyer_id=lawyer.id, court_id=court.id, rol=rol, status="active", competencia="civil",
               created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **kw)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _mov(db, case, stage="Ingreso"):
    db.add(Movement(case_id=case.id, stage=stage, description="mov", movement_date=datetime(2026, 1, 1)))
    db.commit()


_sid = iter(range(1000, 100000))


def _sysgal(db, case, etapa=None, tramite=None, matriz=None, estado="activa", eliminada=False,
            updated=datetime(2026, 11, 1)):
    row = SysgalCausa(sysgal_id=next(_sid), rol="C-1-2025", rol_norm="C-1-2025", tribunal="x",
                      etapa=etapa, tramite=tramite, matriz=matriz, estado=estado, eliminada=eliminada,
                      sysgal_updated_at=updated, case_id=case.id if case else None)
    db.add(row)
    db.commit()
    return row


def _classify(db, case):
    return classify_case(db, case, mapping_cache=MatrizMappingCache.load(db))


class TestPrecedence:
    def test_sysgal_etapa_beats_pjud(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _mov(db, case)  # PJUD would say M1 Baja
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS")

        r = _classify(db, case)

        assert (r.matriz, r.matriz_etapa, r.origen) == ("M2", "EXHIBICIÓN DOCUMENTOS", "sysgal_etapa")

    def test_sysgal_etapa_reaches_m1_alta(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="INGRESO ABANDONO DE PROCEDIMIENTO 3 AÑOS")

        assert _classify(db, case).matriz == "M1 Alta"

    def test_sysgal_matriz_beats_sysgal_etapa(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS", matriz="M3")

        r = _classify(db, case)

        assert (r.matriz, r.origen) == ("M3", "sysgal")

    def test_sysgal_matriz_alone_needs_no_taxonomy_row(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="JUDICIAL", matriz="M1 Alta")

        r = _classify(db, case)

        assert (r.matriz, r.origen) == ("M1 Alta", "sysgal")

    def test_invalid_sysgal_matriz_is_ignored(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS", matriz="M9")

        r = _classify(db, case)

        assert (r.matriz, r.origen) == ("M2", "sysgal_etapa")

    def test_sysgal_beats_apremio_procedure_signal(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court, procedure="Cuaderno de Apremio")
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS")

        assert _classify(db, case).origen == "sysgal_etapa"

    def test_etapa_matches_ignoring_case_accents_and_spaces(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="  exhibicion   documentos ")

        r = _classify(db, case)

        assert (r.matriz, r.matriz_etapa, r.origen) == ("M2", "EXHIBICIÓN DOCUMENTOS", "sysgal_etapa")

    @pytest.mark.parametrize("etapa", ["JUDICIAL", None, "", "   "])
    def test_unresolved_etapa_falls_back_to_pjud(self, db, lawyer, court, taxonomy, etapa):
        case = _case(db, lawyer, court)
        _mov(db, case)
        _sysgal(db, case, etapa=etapa)

        r = _classify(db, case)

        assert (r.matriz, r.origen) == ("M1 Baja", "pjud_etapa")

    @pytest.mark.parametrize("kw", [{"estado": "terminada"}, {"eliminada": True}])
    def test_inactive_or_deleted_sysgal_causa_is_ignored(self, db, lawyer, court, taxonomy, kw):
        case = _case(db, lawyer, court)
        _mov(db, case)
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS", matriz="M3", **kw)

        assert _classify(db, case).origen == "pjud_etapa"

    def test_unlinked_sysgal_causa_is_ignored(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _mov(db, case)
        _sysgal(db, None, etapa="EXHIBICIÓN DOCUMENTOS")

        assert _classify(db, case).origen == "pjud_etapa"


class TestTramiteOverride:
    def test_override_uses_sysgal_tramite(self, db, lawyer, court, taxonomy):
        db.add(MatrizTramiteOverride(proc_antiguo="JUICIO EJECUTIVO", etapa="EXHIBICIÓN DOCUMENTOS",
                                     nombre_tramite="TRAMITE X", matriz="M1 Alta"))
        db.commit()
        case = _case(db, lawyer, court)
        _mov(db, case)
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS", tramite="TRAMITE X")

        r = _classify(db, case)

        assert (r.matriz, r.origen) == ("M1 Alta", "sysgal_etapa")

    def test_override_ignores_the_pjud_movement_tramite(self, db, lawyer, court, taxonomy):
        db.add(MatrizTramiteOverride(proc_antiguo="JUICIO EJECUTIVO", etapa="EXHIBICIÓN DOCUMENTOS",
                                     nombre_tramite="TRAMITE X", matriz="M1 Alta"))
        db.commit()
        case = _case(db, lawyer, court)
        db.add(Movement(case_id=case.id, stage="Ingreso", procedure="TRAMITE X", description="m",
                        movement_date=datetime(2026, 1, 1)))
        db.commit()
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS", tramite="otro")

        assert _classify(db, case).matriz == "M2"


class TestMultipleRows:
    def test_latest_updated_wins(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="ASIGNACIÓN", updated=datetime(2026, 11, 5))
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS", updated=datetime(2026, 11, 1))

        assert _classify(db, case).matriz == "M1 Baja"

    def test_ties_break_on_highest_id(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="ASIGNACIÓN")
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS")

        assert _classify(db, case).matriz == "M2"

    def test_inactive_newer_row_does_not_win(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        _sysgal(db, case, etapa="ASIGNACIÓN", updated=datetime(2026, 11, 1))
        _sysgal(db, case, etapa="EXHIBICIÓN DOCUMENTOS", updated=datetime(2026, 11, 9), estado="terminada")

        assert _classify(db, case).matriz == "M1 Baja"


class TestUnlinkedUnchanged:
    def test_case_without_sysgal_matches_pjud_path(self, db, lawyer, court, taxonomy):
        linked = _case(db, lawyer, court, rol="C-1-2025")
        plain = _case(db, lawyer, court, rol="C-2-2025")
        _mov(db, plain)
        _sysgal(db, linked, etapa="EXHIBICIÓN DOCUMENTOS")

        r = _classify(db, plain)

        assert (r.matriz, r.matriz_etapa, r.origen, r.detalle) == ("M1 Baja", "ASIGNACIÓN", "pjud_etapa", None)

    def test_empty_cache_has_no_sysgal_inputs(self):
        assert MatrizMappingCache().sysgal_inputs == {}


class TestNoNPlusOne:
    def test_classifying_preloaded_cases_issues_no_query(self, db, lawyer, court, taxonomy):
        cases = [_case(db, lawyer, court, rol=f"C-{i}-2025") for i in range(4)]
        for c in cases[:2]:
            _sysgal(db, c, etapa="EXHIBICIÓN DOCUMENTOS")
        for c in cases[2:]:
            _mov(db, c)
        cache = MatrizMappingCache.load(db)
        cache.preload_latest_movements(db, [c.id for c in cases])

        calls = {"n": 0}
        original = db.query

        def counting(*a, **k):
            calls["n"] += 1
            return original(*a, **k)

        db.query = counting
        try:
            origenes = [classify_case(db, c, mapping_cache=cache).origen for c in cases]
        finally:
            db.query = original

        assert calls["n"] == 0
        assert origenes == ["sysgal_etapa"] * 2 + ["pjud_etapa"] * 2

    def test_load_reads_sysgal_in_one_query(self, db, lawyer, court, taxonomy):
        for i in range(5):
            _sysgal(db, _case(db, lawyer, court, rol=f"C-{i}-2025"), etapa="EXHIBICIÓN DOCUMENTOS")
        statements = []
        from sqlalchemy import event
        engine = db.get_bind()
        def on_exec(conn, cursor, statement, *a):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", on_exec)
        try:
            MatrizMappingCache.load(db)
        finally:
            event.remove(engine, "before_cursor_execute", on_exec)

        assert sum("sysgal_causas" in s for s in statements) == 1


class TestReclassifyCases:
    def test_only_given_cases_are_written(self, db, lawyer, court, taxonomy):
        a = _case(db, lawyer, court, rol="C-1-2025")
        b = _case(db, lawyer, court, rol="C-2-2025")
        for c in (a, b):
            _mov(db, c)
            _sysgal(db, c, etapa="EXHIBICIÓN DOCUMENTOS")

        n = reclassify_cases(db, {a.id})

        db.expire_all()
        assert n == 1
        assert (db.get(Case, a.id).matriz, db.get(Case, a.id).matriz_origen) == ("M2", "sysgal_etapa")
        assert db.get(Case, a.id).matriz_computed_at is not None
        assert db.get(Case, b.id).matriz is None and db.get(Case, b.id).matriz_computed_at is None

    def test_non_civil_and_unknown_ids_are_skipped(self, db, lawyer, court, taxonomy):
        c = _case(db, lawyer, court)
        c.competencia = "laboral"
        db.commit()

        assert reclassify_cases(db, {c.id, 999999}) == 0
        assert reclassify_cases(db, set()) == 0
