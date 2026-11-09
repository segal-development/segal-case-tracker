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
                         "calzadas": 1, "sin_calce": 1, "ambiguas": 0}
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
                           abogado_sysgal_id=1, eliminada=eliminada, case_id=case.id, synced_at=datetime.utcnow()))
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
                           case_id=case.id, sysgal_updated_at=datetime(2026, 11, 2), synced_at=datetime.utcnow()))
        db.commit()
        assert sysgal_owner_by_case(db) == {case.id: other.id}


def test_columns_fit_the_real_sysgal_data():
    """First real sync (2026-11-09) died on a 60-char free-text ROL ('DEMANDA CUIDADO PERSONAL Y
    MODIFICACION VISITAS ROMERO'). Real maxima over 110,064 causas: rol 60, tribunal 49, rut 9."""
    from app.models.sysgal_causa import SysgalCausa

    cols = SysgalCausa.__table__.c
    assert cols.rol.type.length >= 255
    assert cols.rol_norm.type.length >= 255
    assert cols.rut_cliente.type.length >= 30
