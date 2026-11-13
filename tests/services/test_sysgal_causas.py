"""sync_sysgal_causas (upserts + matching stats) and sysgal_owner_by_case. Sysgal is a fake client."""

from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services.sysgal_causas import sync_sysgal_causas, sysgal_owner_by_case
from app.services.sysgal_client import SysgalCausaDTO, SysgalUsuarioDTO


class FakeClient:
    def __init__(self, causas=(), usuarios=()):
        self._causas, self._usuarios = list(causas), list(usuarios)
        self.causas_kwargs = []

    def usuarios(self):
        return list(self._usuarios)

    def iter_causas(self, **kwargs):
        self.causas_kwargs.append(kwargs)
        yield from self._causas


def _causa(sid, rol="C-100-2026", tribunal="Juzgado Uno", abogado=1, updated=datetime(2026, 11, 1), **kw):
    return SysgalCausaDTO(sysgal_id=sid, rol=rol, tribunal=tribunal, abogado_sysgal_id=abogado,
                          abogado_nombre="Ana", updated_at=updated, **kw)


@pytest.fixture
def lawyer(db):
    lw = Lawyer(rut="16021492-9", name="Firm", email="ana@firm.cl")
    db.add(lw)
    db.flush()
    return lw


@pytest.fixture
def court(db):
    c = Court(code="A", name="Juzgado Uno", region="RM", type="civil")
    db.add(c)
    db.flush()
    return c


def _case(db, lawyer, court, rol="C-100-2026", procedure=None):
    case = Case(lawyer_id=lawyer.id, court_id=court.id, rol=rol, procedure=procedure, competencia="civil")
    db.add(case)
    db.flush()
    return case


class TestSync:
    def test_upserts_usuarios_keeping_the_human_mapping(self, db, lawyer):
        db.add(SysgalUsuario(sysgal_id=1, nombre="Old", lawyer_id=lawyer.id, mapped_by="1-9"))
        db.commit()
        client = FakeClient(usuarios=[SysgalUsuarioDTO(1, "Ana Nueva", "Abogado", "activo", "a@x.cl"),
                                      SysgalUsuarioDTO(2, "Beto")])

        stats = sync_sysgal_causas(db, client)

        assert stats["usuarios"] == 2
        u1 = db.query(SysgalUsuario).filter_by(sysgal_id=1).one()
        assert (u1.nombre, u1.correo, u1.lawyer_id, u1.mapped_by) == ("Ana Nueva", "a@x.cl", lawyer.id, "1-9")
        assert db.query(SysgalUsuario).count() == 2

    def test_inserts_and_matches_causas_with_stats(self, db, lawyer, court):
        case = _case(db, lawyer, court)
        client = FakeClient(causas=[_causa(10), _causa(11, rol="C-5-2026")])

        stats = sync_sysgal_causas(db, client)

        assert stats == {"usuarios": 0, "causas": 2, "nuevas": 2, "actualizadas": 0,
                         "calzadas": 1, "sin_calce": 1, "ambiguas": 0, "reenlazadas": 0}
        row = db.query(SysgalCausa).filter_by(sysgal_id=10).one()
        assert (row.case_id, row.match_method, row.rol_norm) == (case.id, "rol+tribunal", "C-100-2026")
        assert db.query(SysgalCausa).filter_by(sysgal_id=11).one().case_id is None

    def test_ambiguous_leaves_case_null_and_is_counted(self, db, lawyer, court):
        _case(db, lawyer, court, procedure="A")
        _case(db, lawyer, court, procedure="B")

        stats = sync_sysgal_causas(db, FakeClient(causas=[_causa(10)]))

        assert (stats["ambiguas"], stats["calzadas"]) == (1, 0)
        row = db.query(SysgalCausa).one()
        assert (row.case_id, row.match_method) == (None, "ambigua")

    def test_rerun_is_idempotent_and_updates_in_place(self, db, lawyer, court):
        _case(db, lawyer, court)
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="Prueba")]), full=True)

        stats = sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="Sentencia")]), full=True)

        assert (stats["nuevas"], stats["actualizadas"]) == (0, 1)
        assert db.query(SysgalCausa).count() == 1
        assert db.query(SysgalCausa).one().etapa == "Sentencia"

    def test_rematch_clears_a_stale_match(self, db, lawyer, court):
        case = _case(db, lawyer, court)
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10)]))
        db.delete(case)
        db.commit()

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10)]), full=True)

        row = db.query(SysgalCausa).one()
        assert (row.case_id, row.match_method) == (None, None)

    def test_incremental_uses_max_updated_and_full_does_not(self, db):
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, updated=datetime(2026, 11, 3)),
                                                  _causa(11, updated=datetime(2026, 11, 1))]))
        inc, full = FakeClient(), FakeClient()

        sync_sysgal_causas(db, inc)
        sync_sysgal_causas(db, full, full=True)

        assert inc.causas_kwargs[0]["actualizado_desde"] == datetime(2026, 11, 3)
        assert full.causas_kwargs[0].get("actualizado_desde") is None

    def test_first_incremental_run_has_no_filter(self, db):
        client = FakeClient()
        sync_sysgal_causas(db, client)
        assert client.causas_kwargs[0].get("actualizado_desde") is None

    def test_optional_extras_are_stored(self, db):
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, eliminada=True, estado="Activa", competencia="civil",
                                                         tribunal_codigo="1234")]))
        row = db.query(SysgalCausa).one()
        assert (row.eliminada, row.estado, row.competencia, row.tribunal_codigo) == (True, "Activa", "civil", "1234")


class TestOwnerByCase:
    def _setup(self, db, lawyer, court, *, eliminada=False, mapped=True):
        case = _case(db, lawyer, court)
        db.add(SysgalUsuario(sysgal_id=1, nombre="Ana", lawyer_id=lawyer.id if mapped else None))
        db.add(SysgalCausa(sysgal_id=10, rol="C-100-2026", rol_norm="C-100-2026", tribunal="T",
                           abogado_sysgal_id=1, eliminada=eliminada, estado="activa", case_id=case.id,
                           synced_at=datetime.utcnow()))
        db.commit()
        return case

    def test_matched_mapped_causa_gives_owner(self, db, lawyer, court):
        case = self._setup(db, lawyer, court)
        assert sysgal_owner_by_case(db) == {case.id: lawyer.id}

    def test_eliminada_is_skipped(self, db, lawyer, court):
        self._setup(db, lawyer, court, eliminada=True)
        assert sysgal_owner_by_case(db) == {}

    def test_unmapped_usuario_is_skipped(self, db, lawyer, court):
        self._setup(db, lawyer, court, mapped=False)
        assert sysgal_owner_by_case(db) == {}

    def test_unmatched_causa_is_skipped(self, db, lawyer, court):
        self._setup(db, lawyer, court)
        db.query(SysgalCausa).update({"case_id": None})
        db.commit()
        assert sysgal_owner_by_case(db) == {}

    def test_most_recently_updated_causa_wins_for_a_case(self, db, lawyer, court):
        case = self._setup(db, lawyer, court)
        other = Lawyer(rut="22222222-2", name="Other")
        db.add(other)
        db.flush()
        db.add(SysgalUsuario(sysgal_id=2, nombre="Beto", lawyer_id=other.id))
        db.query(SysgalCausa).update({"sysgal_updated_at": datetime(2026, 11, 1)})
        db.add(SysgalCausa(sysgal_id=11, rol="C-100-2026", rol_norm="C-100-2026", tribunal="T", abogado_sysgal_id=2,
                           estado="activa", case_id=case.id, sysgal_updated_at=datetime(2026, 11, 2), synced_at=datetime.utcnow()))
        db.commit()
        assert sysgal_owner_by_case(db) == {case.id: other.id}

    def test_terminated_causa_gives_no_owner(self, db, lawyer, court):
        self._setup(db, lawyer, court)
        db.query(SysgalCausa).update({"estado": "terminada"})
        db.commit()
        assert sysgal_owner_by_case(db) == {}

    def test_terminated_causa_does_not_override_an_active_one(self, db, lawyer, court):
        case = self._setup(db, lawyer, court)
        other = Lawyer(rut="22222222-2", name="Other")
        db.add(other)
        db.flush()
        db.add(SysgalUsuario(sysgal_id=2, nombre="Beto", lawyer_id=other.id))
        db.query(SysgalCausa).update({"sysgal_updated_at": datetime(2026, 11, 1)})
        db.add(SysgalCausa(sysgal_id=11, rol="C-100-2026", rol_norm="C-100-2026", tribunal="T", abogado_sysgal_id=2,
                           estado="terminada", case_id=case.id, sysgal_updated_at=datetime(2026, 11, 2),
                           synced_at=datetime.utcnow()))
        db.commit()
        assert sysgal_owner_by_case(db) == {case.id: lawyer.id}

    def test_owner_propagates_to_sibling_cuadernos_of_the_same_rol_and_court(self, db, lawyer, court):
        case = self._setup(db, lawyer, court)
        sibling = _case(db, lawyer, court, procedure="Administración Concursal")
        other_court = Court(code="B", name="Juzgado Dos", region="RM", type="civil")
        other_rol_court = Court(code="C", name="Juzgado Tres", region="RM", type="civil")
        db.add_all([other_court, other_rol_court])
        db.flush()
        elsewhere = _case(db, lawyer, other_court)
        other_rol = _case(db, lawyer, court, rol="C-999-2026")
        db.commit()

        owners = sysgal_owner_by_case(db)

        assert owners == {case.id: lawyer.id, sibling.id: lawyer.id}
        assert elsewhere.id not in owners and other_rol.id not in owners

    def test_sibling_with_its_own_sysgal_match_keeps_its_own_owner(self, db, lawyer, court):
        case = self._setup(db, lawyer, court)
        sibling = _case(db, lawyer, court, procedure="Administración Concursal")
        other = Lawyer(rut="22222222-2", name="Other")
        db.add(other)
        db.flush()
        db.add(SysgalUsuario(sysgal_id=2, nombre="Beto", lawyer_id=other.id))
        db.add(SysgalCausa(sysgal_id=11, rol="C-100-2026", rol_norm="C-100-2026", tribunal="T", abogado_sysgal_id=2,
                           estado="activa", case_id=sibling.id, synced_at=datetime.utcnow()))
        db.commit()

        assert sysgal_owner_by_case(db) == {case.id: lawyer.id, sibling.id: other.id}

    def test_sibling_propagation_does_not_scale_with_cases(self, db, lawyer, court):
        from sqlalchemy import event

        case = self._setup(db, lawyer, court)
        for i in range(5):
            _case(db, lawyer, court, procedure=f"Inc {i}")
        db.commit()
        count = []
        engine = db.get_bind()
        listener = lambda *a, **k: count.append(1)  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            owners = sysgal_owner_by_case(db)
        finally:
            event.remove(engine, "before_cursor_execute", listener)

        assert len(owners) == 6 and case.id in owners
        assert len(count) <= 2


def test_columns_fit_the_real_sysgal_data():
    """First real sync (2026-11-09) died on a 60-char free-text ROL ('DEMANDA CUIDADO PERSONAL Y
    MODIFICACION VISITAS ROMERO'). Real maxima over 110,064 causas: rol 60, tribunal 49, rut 9."""
    from app.models.sysgal_causa import SysgalCausa

    cols = SysgalCausa.__table__.c
    assert cols.rol.type.length >= 255
    assert cols.rol_norm.type.length >= 255
    assert cols.rut_cliente.type.length >= 30


class TestMatrizReclassification:
    @pytest.fixture
    def taxonomy(self, db):
        from app.models.matriz_clasificacion import MatrizClasificacion
        for etapa, m in [("PRUEBA", "M2"), ("SENTENCIA", "M3")]:
            db.add(MatrizClasificacion(proc_simple="Juicio Ejecutivo Completo",
                                       proc_antiguo="JUICIO EJECUTIVO", etapa=etapa, matriz=m))
        db.commit()

    def test_new_link_classifies_only_the_linked_case(self, db, lawyer, court, taxonomy):
        linked = _case(db, lawyer, court)
        other = _case(db, lawyer, court, rol="C-7-2026")

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="PRUEBA", estado="activa")]))

        db.expire_all()
        assert (linked.matriz, linked.matriz_origen) == ("M2", "sysgal_etapa")
        assert other.matriz_computed_at is None

    def test_etapa_change_reclassifies_and_unchanged_is_untouched(self, db, lawyer, court, taxonomy):
        a = _case(db, lawyer, court)
        b = _case(db, lawyer, court, rol="C-7-2026")
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="PRUEBA", estado="activa"),
                                                  _causa(11, rol="C-7-2026", etapa="PRUEBA", estado="activa")]))
        db.expire_all()
        stamp_b = b.matriz_computed_at

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="SENTENCIA", estado="activa")]))

        db.expire_all()
        assert a.matriz == "M3"
        assert b.matriz_computed_at == stamp_b

    def test_sysgal_matriz_field_is_stored_and_used(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="PRUEBA", matriz="M1 Alta", estado="activa")]))

        db.expire_all()
        assert db.query(SysgalCausa).one().matriz == "M1 Alta"
        assert (case.matriz, case.matriz_origen) == ("M1 Alta", "sysgal")

    def test_causa_becoming_inactive_falls_back_to_pjud(self, db, lawyer, court, taxonomy):
        case = _case(db, lawyer, court)
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="PRUEBA", estado="activa")]))

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="PRUEBA", estado="terminada")]))

        db.expire_all()
        assert case.matriz_origen == "sin_detalle"

    def test_reclassification_failure_does_not_break_the_sync(self, db, lawyer, court, taxonomy, monkeypatch):
        _case(db, lawyer, court)
        monkeypatch.setattr("app.services.sysgal_causas.reclassify_cases",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        stats = sync_sysgal_causas(db, FakeClient(causas=[_causa(10, etapa="PRUEBA", estado="activa")]))

        assert stats["causas"] == 1
        assert db.query(SysgalCausa).count() == 1


class TestRematchUnlinked:
    """S7a: after each sync, ACTIVE unlinked causas are matched again (cases created later get linked)."""

    def _seed_unlinked(self, db, **kw):
        row = SysgalCausa(sysgal_id=kw.pop("sysgal_id", 50), rol="C-100-2026", rol_norm="C-100-2026",
                          tribunal="Juzgado Uno", estado=kw.pop("estado", "activa"),
                          eliminada=kw.pop("eliminada", False), sysgal_updated_at=datetime(2026, 11, 1),
                          etapa=kw.pop("etapa", None), **kw)
        db.add(row)
        db.commit()
        return row

    def test_links_a_case_created_after_the_first_sync(self, db, lawyer, court):
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="activa")]))
        assert db.query(SysgalCausa).one().case_id is None
        case = _case(db, lawyer, court)
        db.commit()

        stats = sync_sysgal_causas(db, FakeClient())  # incremental, nothing changed in Sysgal

        row = db.query(SysgalCausa).one()
        assert (row.case_id, row.match_method) == (case.id, "rol+tribunal")
        assert stats["reenlazadas"] == 1

    def test_newly_linked_case_is_reclassified(self, db, lawyer, court):
        from app.models.matriz_clasificacion import MatrizClasificacion

        db.add(MatrizClasificacion(proc_simple="Juicio Ejecutivo Completo", proc_antiguo="JUICIO EJECUTIVO",
                                   etapa="EXHIBICIÓN DOCUMENTOS", matriz="M2"))
        db.commit()
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="activa", etapa="EXHIBICIÓN DOCUMENTOS")]))
        case = _case(db, lawyer, court)
        db.commit()

        sync_sysgal_causas(db, FakeClient())

        db.refresh(case)
        assert (case.matriz, case.matriz_origen) == ("M2", "sysgal_etapa")

    def test_already_linked_rows_are_never_repointed(self, db, lawyer, court):
        first = _case(db, lawyer, court)
        row = self._seed_unlinked(db)
        row.case_id, row.match_method = first.id, "manual"
        db.commit()
        _case(db, lawyer, court, procedure="Principal")  # would now be ambiguous/other candidate
        db.commit()

        stats = sync_sysgal_causas(db, FakeClient())

        db.refresh(row)
        assert (row.case_id, row.match_method) == (first.id, "manual")
        assert stats["reenlazadas"] == 0

    @pytest.mark.parametrize("kw", [{"estado": "terminada"}, {"eliminada": True}])
    def test_terminated_or_eliminated_rows_are_not_linked(self, db, lawyer, court, kw):
        _case(db, lawyer, court)
        row = self._seed_unlinked(db, **kw)

        stats = sync_sysgal_causas(db, FakeClient())

        db.refresh(row)
        assert row.case_id is None
        assert stats["reenlazadas"] == 0

    def test_ambiguous_rows_stay_unlinked(self, db, lawyer, court):
        _case(db, lawyer, court, procedure="A")
        _case(db, lawyer, court, procedure="B")
        row = self._seed_unlinked(db)

        sync_sysgal_causas(db, FakeClient())

        db.refresh(row)
        assert (row.case_id, row.match_method) == (None, "ambigua")

    def test_rematch_failure_is_not_fatal(self, db, lawyer, court, monkeypatch):
        import app.services.sysgal_causas as mod

        monkeypatch.setattr(mod, "rematch_unlinked", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))

        stats = sync_sysgal_causas(db, FakeClient(causas=[_causa(10)]))

        assert stats["causas"] == 1 and stats["reenlazadas"] == 0

    def test_query_count_is_flat_in_the_number_of_unlinked_rows(self, db, lawyer, court):
        from sqlalchemy import event

        from app.services.sysgal_causas import rematch_unlinked

        def run(n):
            for i in range(n):
                db.add(SysgalCausa(sysgal_id=1000 + i + 100 * n, rol=f"C-{i}-2026", rol_norm=f"C-{i}-2026",
                                   tribunal="Juzgado Uno", estado="activa", eliminada=False))
            db.commit()
            seen = []
            hook = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
            engine = db.get_bind()
            event.listen(engine, "before_cursor_execute", hook)
            try:
                rematch_unlinked(db, dry_run=True)
            finally:
                event.remove(engine, "before_cursor_execute", hook)
            return len(seen)

        small = run(3)
        large = run(40)

        assert large <= small + 2

    def test_dry_run_reports_and_writes_nothing(self, db, lawyer, court):
        from app.services.sysgal_causas import rematch_unlinked

        _case(db, lawyer, court)
        row = self._seed_unlinked(db)

        report = rematch_unlinked(db, dry_run=True)

        db.refresh(row)
        assert row.case_id is None
        assert (report["candidatas"], report["reenlazadas"], report["ambiguas"], report["sin_calce"]) == (1, 1, 0, 0)


class TestSiblingMatrizAfterSync:
    def test_sibling_is_reclassified_when_the_principal_etapa_changes(self, db, lawyer, court):
        from app.models.matriz_clasificacion import MatrizClasificacion

        for etapa, matriz in [("PRUEBA", "M2"), ("APREMIO", "M3")]:
            db.add(MatrizClasificacion(proc_simple="Juicio Ejecutivo Completo", proc_antiguo="JUICIO EJECUTIVO",
                                       etapa=etapa, matriz=matriz))
        principal = _case(db, lawyer, court, procedure="Principal")
        secondary = _case(db, lawyer, court, procedure="Incidente nulidad")
        db.commit()
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="activa", etapa="PRUEBA")]))
        db.refresh(secondary)
        assert (secondary.matriz, secondary.matriz_origen) == ("M2", "sysgal_etapa")

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="activa", etapa="APREMIO",
                                                         updated=datetime(2026, 11, 5))]))

        db.refresh(secondary)
        db.refresh(principal)
        assert (secondary.matriz, principal.matriz) == ("M3", "M3")


class TestTerminadaFlagAfterSync:
    def test_flag_follows_the_sysgal_state_and_unlinked_cases_are_untouched(self, db, lawyer, court):
        case = _case(db, lawyer, court)
        other = _case(db, lawyer, court, rol="C-7-2026")
        db.commit()

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="terminada")]))
        db.expire_all()
        assert case.terminada_en_sysgal is True
        assert other.terminada_en_sysgal is False and other.matriz_computed_at is None

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="activa", updated=datetime(2026, 11, 5))]))
        db.expire_all()
        assert case.terminada_en_sysgal is False

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="terminada", updated=datetime(2026, 11, 6))]))
        db.expire_all()
        assert case.terminada_en_sysgal is True

    def test_unchanged_terminated_case_is_not_rewritten(self, db, lawyer, court):
        case = _case(db, lawyer, court)
        db.commit()
        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="terminada")]))
        db.expire_all()
        stamp = case.matriz_computed_at

        sync_sysgal_causas(db, FakeClient(causas=[_causa(10, estado="terminada", updated=datetime(2026, 11, 5))]))

        db.expire_all()
        assert case.terminada_en_sysgal is True and case.matriz_computed_at == stamp
