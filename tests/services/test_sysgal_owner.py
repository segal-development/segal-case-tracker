"""Sysgal-as-owner resolver, kill switch and change report (odd/tasks/sysgal-fuente-principal.md S1, S3, S5)."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.goal import Goal
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import app_settings, sysgal_owner
from app.services.sysgal_owner import reporte_cambio_duenio, sysgal_duenio_enabled, sysgal_primary_owner_by_case

T0 = datetime(2026, 11, 1)


@pytest.fixture
def world(db):
    court = Court(code="T-SO", name="Juzgado SO", region="RM", type="civil")
    pjud = Lawyer(rut="11111111-1", name="Pjud Abogado", role="lawyer", is_active=True)
    eduardo = Lawyer(rut="22222222-2", name="Eduardo", role="lawyer", is_active=True)
    luis = Lawyer(rut="33333333-3", name="Luis", role="lawyer", is_active=True)
    inactive = Lawyer(rut="44444444-4", name="Inactivo", role="lawyer", is_active=False)
    db.add_all([court, pjud, eduardo, luis, inactive])
    db.flush()
    db.add_all([
        SysgalUsuario(sysgal_id=10, nombre="Eduardo", lawyer_id=eduardo.id),
        SysgalUsuario(sysgal_id=20, nombre="Luis", lawyer_id=luis.id),
        SysgalUsuario(sysgal_id=30, nombre="Inactivo", lawyer_id=inactive.id),
        SysgalUsuario(sysgal_id=40, nombre="Sin mapear", lawyer_id=None),
    ])
    db.commit()
    return {"court": court, "pjud": pjud, "eduardo": eduardo, "luis": luis, "inactive": inactive}


def make_case(db, w, rol, litigante=None, competencia="civil"):
    case = Case(lawyer_id=w["pjud"].id, court_id=w["court"].id, rol=rol, competencia=competencia, status="active")
    db.add(case)
    db.flush()
    if litigante is not None:
        db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=litigante.rut, nombre=litigante.name,
                             persona_type="NATURAL", natural_key=f"{case.id}-{litigante.rut}"))
    db.commit()
    return case


_seq = iter(range(1000, 10_000))


def link(db, case, abogado_sysgal_id, *, estado="activa", eliminada=False, updated=T0):
    n = next(_seq)
    row = SysgalCausa(sysgal_id=n, rol=f"R-{n}", rol_norm=f"R-{n}", tribunal="J", estado=estado,
                      eliminada=eliminada, abogado_sysgal_id=abogado_sysgal_id, sysgal_updated_at=updated,
                      case_id=case.id if case else None)
    db.add(row)
    db.commit()
    return row


class TestSwitch:
    def test_default_is_off_and_key_name(self, db):
        assert app_settings.KEY_SYSGAL_DUENIO == "sysgal_es_fuente_duenio"
        assert app_settings.DEFAULT_SYSGAL_DUENIO == 0
        assert sysgal_duenio_enabled(db) is False

    def test_on_only_when_value_is_1(self, db):
        app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, 1)
        db.commit()
        assert sysgal_duenio_enabled(db) is True
        app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, 0)
        db.commit()
        assert sysgal_duenio_enabled(db) is False
        app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, 2)
        db.commit()
        assert sysgal_duenio_enabled(db) is False

    def test_read_is_cached_briefly_across_bulk_writes(self, db, monkeypatch):
        now = [100.0]
        monkeypatch.setattr(sysgal_owner, "_clock", lambda: now[0])
        assert sysgal_duenio_enabled(db) is False
        db.add(Goal(key=app_settings.KEY_SYSGAL_DUENIO, value=1))
        db.commit()  # ORM flush of the switch itself invalidates the local cache
        assert sysgal_duenio_enabled(db) is True
        db.query(Goal).filter_by(key=app_settings.KEY_SYSGAL_DUENIO).update({"value": 0})  # bulk: no event
        db.commit()
        assert sysgal_duenio_enabled(db) is True  # still cached
        now[0] += sysgal_owner.SWITCH_CACHE_SECONDS + 1
        assert sysgal_duenio_enabled(db) is False  # TTL bounds the staleness


class TestPrimaryOwner:
    def test_active_mapped_row_decides(self, db, world):
        c = make_case(db, world, "C-1-2026")
        link(db, c, 10)
        owner = sysgal_primary_owner_by_case(db)[c.id]
        assert (owner.lawyer_id, owner.rut, owner.nombre) == (world["eduardo"].id, "22222222-2", "Eduardo")

    @pytest.mark.parametrize("kwargs,abogado", [
        ({}, 40),  # usuario without lawyer mapping
        ({}, 999),  # abogado not in sysgal_usuarios
        ({}, 30),  # mapped lawyer inactive
        ({}, None),  # no abogado at all
        ({"estado": "terminada"}, 10),
        ({"estado": "Terminada"}, 10),
        ({"eliminada": True}, 10),
    ])
    def test_fall_through_cases_have_no_sysgal_owner(self, db, world, kwargs, abogado):
        c = make_case(db, world, "C-2-2026")
        link(db, c, abogado, **kwargs)
        assert c.id not in sysgal_primary_owner_by_case(db)

    def test_estado_is_trimmed_and_case_insensitive(self, db, world):
        c = make_case(db, world, "C-3-2026")
        link(db, c, 10, estado="  Activa ")
        assert c.id in sysgal_primary_owner_by_case(db)

    def test_unlinked_row_is_ignored(self, db, world):
        link(db, None, 10)
        assert sysgal_primary_owner_by_case(db) == {}

    def test_several_rows_most_recently_updated_wins(self, db, world):
        c = make_case(db, world, "C-4-2026")
        link(db, c, 10, updated=T0 + timedelta(days=2))
        link(db, c, 20, updated=T0)
        assert sysgal_primary_owner_by_case(db)[c.id].lawyer_id == world["eduardo"].id

    def test_tie_on_update_picks_highest_id(self, db, world):
        c = make_case(db, world, "C-5-2026")
        link(db, c, 10)
        link(db, c, 20)  # created later -> higher id
        assert sysgal_primary_owner_by_case(db)[c.id].lawyer_id == world["luis"].id

    def test_unmapped_newest_row_is_not_a_candidate_older_mapped_row_decides(self, db, world):
        c = make_case(db, world, "C-6-2026")
        link(db, c, 10, updated=T0)
        link(db, c, 40, updated=T0 + timedelta(days=1))
        assert sysgal_primary_owner_by_case(db)[c.id].lawyer_id == world["eduardo"].id

    def test_terminated_newer_row_does_not_hide_an_older_active_one(self, db, world):
        c = make_case(db, world, "C-7-2026")
        link(db, c, 10, updated=T0)
        link(db, c, 20, estado="terminada", updated=T0 + timedelta(days=1))
        assert sysgal_primary_owner_by_case(db)[c.id].lawyer_id == world["eduardo"].id

    @pytest.mark.parametrize("attrs", [{"role": "procurador", "is_firm_lawyer": False}, {"is_firm_lawyer": False}])
    def test_non_firm_or_procurador_lawyer_never_owns(self, db, world, attrs):
        c = make_case(db, world, "C-9-2026")
        link(db, c, 10)
        for k, v in attrs.items():
            setattr(world["eduardo"], k, v)
        db.commit()
        assert c.id not in sysgal_primary_owner_by_case(db)

    def test_no_sibling_inheritance_only_own_link(self, db, world):
        a = make_case(db, world, "C-8-2026")
        sibling = Case(lawyer_id=world["pjud"].id, court_id=world["court"].id, rol="C-8-2026",
                       competencia="civil", status="active", procedure="Apremio")  # another cuaderno
        db.add(sibling)
        db.commit()
        link(db, a, 10)
        assert set(sysgal_primary_owner_by_case(db)) == {a.id}
        assert sibling.id not in sysgal_primary_owner_by_case(db)

    def test_competencia_filter(self, db, world):
        c = make_case(db, world, "C-9-2026", competencia="cobranza")
        link(db, c, 10)
        assert sysgal_primary_owner_by_case(db, competencia="civil") == {}
        assert c.id in sysgal_primary_owner_by_case(db, competencia="cobranza")
        assert c.id in sysgal_primary_owner_by_case(db)

    def test_case_id_filter(self, db, world):
        a, b = make_case(db, world, "C-10-2026"), make_case(db, world, "C-11-2026")
        link(db, a, 10)
        link(db, b, 20)
        assert set(sysgal_primary_owner_by_case(db, case_id=b.id)) == {b.id}

    def test_one_query_regardless_of_case_count(self, db, world):
        def count(n):
            for i in range(n):
                link(db, make_case(db, world, f"C-{n}{i}-2027"), 10)
            seen = []
            fn = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
            event.listen(db.get_bind(), "before_cursor_execute", fn)
            try:
                sysgal_primary_owner_by_case(db)
            finally:
                event.remove(db.get_bind(), "before_cursor_execute", fn)
            return len(seen)

        assert count(2) == count(25) == 1


class TestReporteCambioDuenio:
    def test_gana_pierde_per_lawyer_without_turning_the_switch_on(self, db, world):
        w = world
        # PJUD says Pjud; Sysgal says Eduardo -> Eduardo gana, Pjud pierde.
        link(db, make_case(db, w, "C-20-2026", litigante=w["pjud"]), 10)
        link(db, make_case(db, w, "C-21-2026", litigante=w["pjud"]), 10)
        # Same owner on both sides -> no change.
        link(db, make_case(db, w, "C-22-2026", litigante=w["luis"]), 20)
        # Unmapped Sysgal lawyer -> falls through, no change.
        link(db, make_case(db, w, "C-23-2026", litigante=w["pjud"]), 40)
        # Override wins -> no change even though Sysgal disagrees.
        ov = make_case(db, w, "C-24-2026", litigante=w["pjud"])
        ov.assigned_lawyer_id = w["luis"].id
        db.commit()
        link(db, ov, 10)
        # Sysgal owner on a case with no PJUD litigante at all -> pure gain.
        link(db, make_case(db, w, "C-25-2026"), 20)

        report = reporte_cambio_duenio(db)

        assert report == {
            "Eduardo": {"gana": 2, "pierde": 0},
            "Pjud Abogado": {"gana": 0, "pierde": 3},  # C-25 was Pjud's bootstrap case (lawyer_id, no litigantes)
            "Luis": {"gana": 1, "pierde": 0},
        }
        assert sysgal_duenio_enabled(db) is False
        assert db.query(Goal).count() == 0

    def test_report_equals_real_scope_delta_with_bootstrap_and_patrocinio_pendiente(self, db, world):
        from app.api.deps import resolve_case_scope
        from app.services import lawyer_roster

        w = world
        # Bootstrap-owned (no litigantes, lawyer_id = Luis) and Sysgal says Eduardo.
        for n in range(3):
            c = make_case(db, w, f"C-3{n}-2026")
            c.lawyer_id = w["luis"].id
            db.commit()
            link(db, c, 10)
        # Bootstrap-owned by Eduardo, Sysgal says Luis.
        c = make_case(db, w, "C-39-2026")
        c.lawyer_id = w["eduardo"].id
        db.commit()
        link(db, c, 20)
        # patrocinio_pendiente incorporated cases owned by Luis (lawyer_id), Sysgal says Eduardo / unchanged.
        for n, sysgal_id in enumerate((10, 10, 20)):
            c = make_case(db, w, f"C-4{n}-2026")
            c.lawyer_id = w["luis"].id
            c.patrocinio_pendiente = True
            db.commit()
            link(db, c, sysgal_id)
        # Plain PJUD litigante case moving Pjud -> Luis.
        link(db, make_case(db, w, "C-50-2026", litigante=w["pjud"]), 20)

        report = reporte_cambio_duenio(db)

        assert db.query(Goal).filter_by(key=app_settings.KEY_SYSGAL_DUENIO).count() == 0
        assert sysgal_duenio_enabled(db) is False

        def scopes():
            lawyer_roster.clear_case_scope_cache()
            return {k: set(resolve_case_scope(db, {"sub": w[k].id})) for k in ("pjud", "eduardo", "luis")}

        before = scopes()
        app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, 1)
        db.commit()
        after = scopes()
        names = {"pjud": "Pjud Abogado", "eduardo": "Eduardo", "luis": "Luis"}
        expected = {
            names[k]: {"gana": len(after[k] - before[k]), "pierde": len(before[k] - after[k])}
            for k in names
            if after[k] != before[k]
        }
        assert expected["Luis"]["pierde"] >= 5  # sanity: the old litigante-only report would have missed these
        assert report == expected
