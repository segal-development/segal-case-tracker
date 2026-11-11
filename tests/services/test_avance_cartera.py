"""Avance de cartera service: snapshot take/get, month rules, bono cobertura map (Sysgal mocked)."""

from datetime import date, datetime

import pytest

from app.models.avance_cartera import AvanceCarteraSnapshot
from app.models.bono_cierre import CIERRE_CERRADO, BonoCierre
from app.models.lawyer import Lawyer
from app.models.sysgal_usuario import SysgalUsuario
from app.services import avance_cartera as svc
from app.services.sysgal_client import SysgalError


def _stats(desde="2026-10-01", hasta="2026-10-31", abogados=((238, 32.26),)):
    return {
        "desde": desde, "hasta": hasta, "total_activas": 10, "cobertura": 40.0, "por_matriz": [],
        "por_abogado": [{"abogado_id": a, "nombre": f"A{a}", "total": 10, "gestionadas": 3, "cobertura": c}
                        for a, c in abogados],
    }


class FakeClient:
    def __init__(self, stats=None, error=None):
        self.stats, self.error, self.calls = stats, error, []

    def estadisticas_cartera(self, desde, hasta):
        self.calls.append((desde, hasta))
        if self.error:
            raise self.error
        return self.stats or _stats(desde.isoformat(), hasta.isoformat())


TODAY = date(2026, 11, 10)


class TestPeriodoBounds:
    def test_whole_calendar_month(self):
        assert svc.periodo_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
        assert svc.periodo_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))

    @pytest.mark.parametrize("bad", ["2026-13", "26-01", "abc", "2026-1", ""])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            svc.periodo_bounds(bad)


class TestTakeSnapshot:
    def test_creates_with_payload_as_received(self, db):
        client = FakeClient()
        snap = svc.take_snapshot(db, client, "2026-10", today=TODAY)
        assert client.calls == [(date(2026, 10, 1), date(2026, 10, 31))]
        assert snap.periodo == "2026-10" and snap.desde == date(2026, 10, 1) and snap.hasta == date(2026, 10, 31)
        assert snap.payload == _stats()
        assert snap.taken_by is None

    def test_retake_replaces_same_row(self, db):
        svc.take_snapshot(db, FakeClient(), "2026-10", today=TODAY)
        svc.take_snapshot(db, FakeClient(_stats(abogados=((238, 50.0),))), "2026-10", today=TODAY)
        rows = db.query(AvanceCarteraSnapshot).all()
        assert len(rows) == 1 and rows[0].payload["por_abogado"][0]["cobertura"] == 50.0

    def test_current_month_refused(self, db):
        with pytest.raises(svc.PeriodoNoTerminado):
            svc.take_snapshot(db, FakeClient(), "2026-11", today=date(2026, 11, 30))
        assert db.query(AvanceCarteraSnapshot).count() == 0

    def test_last_day_passed_is_ok(self, db):
        svc.take_snapshot(db, FakeClient(), "2026-10", today=date(2026, 11, 1))
        assert db.query(AvanceCarteraSnapshot).count() == 1

    def test_closed_liquidacion_refused(self, db):
        db.add(BonoCierre(periodo="2026-10", estado=CIERRE_CERRADO))
        db.commit()
        with pytest.raises(svc.PeriodoCerrado):
            svc.take_snapshot(db, FakeClient(), "2026-10", today=TODAY)
        assert db.query(AvanceCarteraSnapshot).count() == 0

    def test_sysgal_error_leaves_existing_snapshot(self, db):
        svc.take_snapshot(db, FakeClient(), "2026-10", today=TODAY)
        with pytest.raises(SysgalError):
            svc.take_snapshot(db, FakeClient(error=SysgalError("x")), "2026-10", today=TODAY)
        assert db.query(AvanceCarteraSnapshot).one().payload == _stats()

    def test_taken_by_recorded(self, db):
        lw = Lawyer(rut="1-9", name="Admin", role="admin")
        db.add(lw)
        db.commit()
        snap = svc.take_snapshot(db, FakeClient(), "2026-10", today=TODAY, taken_by=lw.id)
        assert snap.taken_by == lw.id


class TestGetSnapshot:
    def test_none_when_missing(self, db):
        assert svc.get_snapshot(db, "2026-10") is None

    def test_returns_row(self, db):
        svc.take_snapshot(db, FakeClient(), "2026-10", today=TODAY)
        assert svc.get_snapshot(db, "2026-10").periodo == "2026-10"


class TestPreviousMonth:
    def test_previous_periodo(self):
        assert svc.previous_periodo(date(2026, 11, 1)) == "2026-10"
        assert svc.previous_periodo(date(2026, 1, 1)) == "2025-12"


class TestMaybeTakePrevious:
    def test_takes_when_missing(self, db):
        client = FakeClient()
        assert svc.take_previous_month_if_missing(db, client, TODAY) == "taken"
        assert db.query(AvanceCarteraSnapshot).one().periodo == "2026-10"

    def test_skips_when_exists(self, db):
        svc.take_snapshot(db, FakeClient(), "2026-10", today=TODAY)
        client = FakeClient()
        assert svc.take_previous_month_if_missing(db, client, TODAY) == "exists"
        assert client.calls == []

    def test_skips_when_closed(self, db):
        db.add(BonoCierre(periodo="2026-10", estado=CIERRE_CERRADO))
        db.commit()
        client = FakeClient()
        assert svc.take_previous_month_if_missing(db, client, TODAY) == "closed"
        assert client.calls == []


class TestCoberturaByLawyer:
    def test_none_without_snapshot(self, db):
        assert svc.cobertura_by_lawyer(db, "2026-10") is None

    def test_maps_sysgal_ids_to_lawyers(self, db):
        lw = Lawyer(rut="1-9", name="L", role="lawyer", nivel="junior")
        db.add(lw)
        db.commit()
        db.add(SysgalUsuario(sysgal_id=238, nombre="L", lawyer_id=lw.id, synced_at=datetime.utcnow()))
        db.add(SysgalUsuario(sysgal_id=999, nombre="Sin mapa", synced_at=datetime.utcnow()))
        db.commit()
        svc.take_snapshot(db, FakeClient(_stats(abogados=((238, 91.5), (999, 10.0)))), "2026-10", today=TODAY)
        assert svc.cobertura_by_lawyer(db, "2026-10") == {lw.id: 91.5}


class TestCoberturaDuplicates:
    def _lawyer(self, db, *sysgal_ids):
        lw = Lawyer(rut="1-9", name="L", role="lawyer", nivel="junior")
        db.add(lw)
        db.commit()
        for sid in sysgal_ids:
            db.add(SysgalUsuario(sysgal_id=sid, nombre=f"U{sid}", lawyer_id=lw.id, synced_at=datetime.utcnow()))
        db.commit()
        return lw

    def _snap(self, db, rows):
        db.add(AvanceCarteraSnapshot(
            periodo="2026-10", desde=date(2026, 10, 1), hasta=date(2026, 10, 31),
            payload={"por_abogado": rows},
        ))
        db.commit()

    def test_two_sysgal_users_one_lawyer_combine_by_totals(self, db):
        lw = self._lawyer(db, 1, 2)
        self._snap(db, [
            {"abogado_id": 1, "total": 10, "gestionadas": 10, "cobertura": 100},
            {"abogado_id": 2, "total": 30, "gestionadas": 0, "cobertura": 0},
        ])
        assert svc.cobertura_by_lawyer(db, "2026-10") == {lw.id: 25.0}

    def test_combined_is_rounded_to_two_decimals(self, db):
        lw = self._lawyer(db, 1, 2)
        self._snap(db, [
            {"abogado_id": 1, "total": 1, "gestionadas": 1, "cobertura": 100},
            {"abogado_id": 2, "total": 2, "gestionadas": 0, "cobertura": 0},
        ])
        assert svc.cobertura_by_lawyer(db, "2026-10") == {lw.id: 33.33}

    def test_repeated_identical_abogado_row_counts_once(self, db):
        lw = self._lawyer(db, 1)
        row = {"abogado_id": 1, "total": 10, "gestionadas": 4, "cobertura": 40.0}
        self._snap(db, [row, dict(row)])
        assert svc.cobertura_by_lawyer(db, "2026-10") == {lw.id: 40.0}

    def test_combined_zero_total_is_zero(self, db):
        lw = self._lawyer(db, 1, 2)
        self._snap(db, [
            {"abogado_id": 1, "total": 0, "gestionadas": 0, "cobertura": 0},
            {"abogado_id": 2, "total": 0, "gestionadas": 0, "cobertura": 0},
        ])
        assert svc.cobertura_by_lawyer(db, "2026-10") == {lw.id: 0.0}

    def test_single_row_keeps_sysgal_cobertura_as_received(self, db):
        lw = self._lawyer(db, 1)
        self._snap(db, [{"abogado_id": 1, "total": 3, "gestionadas": 1, "cobertura": 33.3333}])
        assert svc.cobertura_by_lawyer(db, "2026-10") == {lw.id: 33.3333}


class TestRace:
    def test_insert_race_updates_the_row_that_won(self, db, monkeypatch):
        """The row appears between our lookup and our insert (the scheduler won): no error."""
        svc.take_snapshot(db, FakeClient(_stats(abogados=((238, 10.0),))), "2026-10", today=TODAY)
        real_get = svc.get_snapshot
        calls = {"n": 0}

        def stale_get(session, periodo):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get(session, periodo)

        monkeypatch.setattr(svc, "get_snapshot", stale_get)
        snap = svc.take_snapshot(db, FakeClient(_stats(abogados=((238, 77.0),))), "2026-10", today=TODAY)

        assert db.query(AvanceCarteraSnapshot).count() == 1
        assert snap.payload["por_abogado"][0]["cobertura"] == 77.0
        assert real_get(db, "2026-10").payload["por_abogado"][0]["cobertura"] == 77.0
