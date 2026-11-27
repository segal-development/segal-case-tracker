"""run_busqueda: public date search → DemandaPublica rows + per-(fecha, corte) runs."""

from datetime import date
from pathlib import Path

import pytest

from app.config import settings
from app.models.busqueda_fecha_run import BusquedaFechaRun
from app.models.court import Court
from app.models.demanda_publica import DemandaPublica
from app.scrapper.pjud import publico_fecha as pf
from app.services import pjud_busqueda_fecha as svc

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "pjud_publico"
TRIB = "1º Juzgado Civil de Santiago"

# row → modal fixture
MODALES = {
    "C-1001-2026": "modal_con_abogado.html",
    "C-1002-2026": "modal_sin_abogado.html",
    "C-1003-2026": "modal_dos_demandados_sin_abogado.html",
    "C-1004-2026": "modal_con_apoderado_ddo.html",
}


class FakeDriver:
    def __init__(self, *, pdf: "bytes | Exception" = b"%PDF", fail_modal=(), fail_search=False, tribunales=None):
        self.rows = pf.parse_resultados((FIX / "resultados_fecha.html").read_text(encoding="utf-8"))
        self.pdf = pdf
        self.fail_modal = set(fail_modal)
        self.fail_search = fail_search
        self.tribunales = tribunales if tribunales is not None else [("259", TRIB)]
        self.modales_abiertos: list[str] = []
        self.pdfs: list[str] = []

    async def listar_tribunales(self, corte):
        return list(self.tribunales)

    async def buscar(self, corte, tribunal, desde, hasta):
        if self.fail_search:
            raise RuntimeError("search failed")
        return [r for r in self.rows if desde <= r.fecha <= hasta]

    async def abrir_detalle(self, fila):
        self.modales_abiertos.append(fila.rol)
        if fila.rol in self.fail_modal:
            raise RuntimeError("modal failed")
        return (FIX / MODALES[fila.rol]).read_text(encoding="utf-8")

    async def descargar_demanda(self, valor):
        self.pdfs.append(valor)
        if isinstance(self.pdf, Exception):
            raise self.pdf
        return self.pdf


@pytest.fixture(autouse=True)
def _text(monkeypatch):
    monkeypatch.setattr(svc, "extraer_texto_pdf", lambda data: "TEXTO DE LA DEMANDA" if data else "")
    monkeypatch.setattr(settings, "PUBLICO_FECHA_MAX_DETALLES", 150, raising=False)


def _basic(c):
    return {k: v for k, v in c.items() if k != "detalles_por_dia"}


D = date(2026, 11, 24)
H = date(2026, 11, 26)


HOY = date(2026, 11, 27)


async def _run(db, driver, desde=D, hasta=H, cortes=("90",), hoy=HOY):
    return await svc.run_busqueda(db, driver, desde, hasta, list(cortes), hoy=hoy)


@pytest.mark.asyncio
async def test_stores_only_causas_without_defendant_lawyer(db):
    drv = FakeDriver()
    await _run(db, drv)
    roles = sorted(r.rol for r in db.query(DemandaPublica).all())
    assert roles == ["C-1002-2026", "C-1003-2026"]
    # E-/V- rows never open a modal
    assert "E-967-2026" not in drv.modales_abiertos and "V-77-2026" not in drv.modales_abiertos


@pytest.mark.asyncio
async def test_stored_fields_pdf_text_and_no_rut(db):
    await _run(db, FakeDriver())
    d = db.query(DemandaPublica).filter_by(rol="C-1002-2026").one()
    assert d.tribunal == TRIB
    assert d.corte == "C.A. de Santiago"
    assert d.caratulado == "BANCO EJEMPLO S.A./FICTICIA TRES"
    assert d.fecha_ingreso == date(2026, 11, 25)
    assert d.texto_demanda == "TEXTO DE LA DEMANDA"
    assert {x["participante"] for x in d.litigantes} == {"DTE.", "AB.DTE", "AP.DTE", "DDO."}
    for x in d.litigantes:
        assert set(x) == {"participante", "persona", "nombre"}
    assert "66666666" not in str(d.litigantes)


@pytest.mark.asyncio
async def test_empty_or_failed_pdf_text_is_null(db):
    await _run(db, FakeDriver(pdf=b""))
    assert all(r.texto_demanda is None for r in db.query(DemandaPublica).all())
    assert db.query(DemandaPublica).count() == 2


@pytest.mark.asyncio
async def test_pdf_download_error_still_stores_the_causa_with_null_text(db):
    await _run(db, FakeDriver(pdf=RuntimeError("boom")))
    rows = db.query(DemandaPublica).all()
    assert len(rows) == 2 and all(r.texto_demanda is None for r in rows)


@pytest.mark.asyncio
async def test_matches_court_by_tribunal_name(db):
    c = Court(code="259", name=TRIB, region="RM", type="civil")
    db.add(c)
    db.commit()
    await _run(db, FakeDriver())
    assert {r.court_id for r in db.query(DemandaPublica).all()} == {c.id}


@pytest.mark.asyncio
async def test_unknown_court_leaves_court_id_null(db):
    await _run(db, FakeDriver())
    assert {r.court_id for r in db.query(DemandaPublica).all()} == {None}


@pytest.mark.asyncio
async def test_already_stored_rol_tribunal_is_skipped_without_opening_the_modal(db):
    await _run(db, FakeDriver())
    drv = FakeDriver()
    await _run(db, drv)
    assert db.query(DemandaPublica).count() == 2
    assert "C-1002-2026" not in drv.modales_abiertos
    assert drv.pdfs == []


@pytest.mark.asyncio
async def test_row_error_does_not_abort_the_run(db):
    await _run(db, FakeDriver(fail_modal={"C-1002-2026"}))
    assert [r.rol for r in db.query(DemandaPublica).all()] == ["C-1003-2026"]
    run = db.query(BusquedaFechaRun).filter_by(fecha=date(2026, 11, 25)).one()
    assert run.contadores["errores"] == 1
    assert run.status == "partial"


@pytest.mark.asyncio
async def test_run_counters_per_fecha_and_corte(db):
    await _run(db, FakeDriver())
    runs = {r.fecha: r for r in db.query(BusquedaFechaRun).filter_by(corte="90").all()}
    assert set(runs) == {date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)}
    r25 = runs[date(2026, 11, 25)]
    assert r25.status == "done" and r25.finished_at is not None
    assert _basic(r25.contadores) == {"encontradas": 2, "c": 2, "con_abogado": 0, "guardadas": 2, "errores": 0, "detalles": 2}
    r26 = runs[date(2026, 11, 26)]
    assert _basic(r26.contadores) == {
        "encontradas": 2, "c": 1, "con_abogado": 1, "guardadas": 0, "errores": 0, "detalles": 1,
    }
    r24 = runs[date(2026, 11, 24)]
    assert r24.contadores["con_abogado"] == 1 and r24.contadores["c"] == 1


@pytest.mark.asyncio
async def test_day_without_results_is_recorded_done(db):
    await _run(db, FakeDriver(), desde=date(2026, 11, 20), hasta=date(2026, 11, 21))
    runs = db.query(BusquedaFechaRun).order_by(BusquedaFechaRun.fecha).all()
    assert [(r.fecha, r.status) for r in runs] == [
        (date(2026, 11, 20), "done"), (date(2026, 11, 21), "done"),
    ]


@pytest.mark.asyncio
async def test_daily_cap_stops_cleanly_and_marks_partial(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLICO_FECHA_MAX_DETALLES", 2, raising=False)
    drv = FakeDriver()
    await _run(db, drv)
    assert len(drv.modales_abiertos) == 2
    statuses = {r.fecha: r.status for r in db.query(BusquedaFechaRun).all()}
    assert "partial" in statuses.values()
    assert statuses[date(2026, 11, 24)] == "partial"  # its C-1004 row was left for the next run


@pytest.mark.asyncio
async def test_search_failure_makes_the_corte_failed_not_done(db):
    await _run(db, FakeDriver(fail_search=True))
    statuses = {r.status for r in db.query(BusquedaFechaRun).all()}
    assert statuses == {"failed"}


@pytest.mark.asyncio
async def test_rerun_updates_the_existing_run_row(db):
    await _run(db, FakeDriver(fail_search=True))
    await _run(db, FakeDriver())
    runs = db.query(BusquedaFechaRun).filter_by(corte="90").all()
    assert len(runs) == 3 and {r.status for r in runs} == {"done"}


def _used_today(db, hoy=HOY):
    return svc.detalles_usados_hoy(db, hoy)


@pytest.mark.asyncio
async def test_counters_are_persisted_per_tribunal_while_the_run_progresses(db):
    seen = []

    class Spy(FakeDriver):
        async def buscar(self, corte, tribunal, desde, hasta):
            # at the start of the 2nd tribunal the 1st one's work is already persisted
            seen.append(_used_today(db))
            if tribunal != "259":
                return []
            return await super().buscar(corte, tribunal, desde, hasta)

    await _run(db, Spy(tribunales=[("259", TRIB), ("260", "2º Juzgado Civil de Santiago")]))
    assert seen == [0, 4]  # the 4 C rows of the 1st tribunal were already counted


@pytest.mark.asyncio
async def test_second_run_same_day_after_cap_opens_no_modal_and_is_partial(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLICO_FECHA_MAX_DETALLES", 2, raising=False)
    await _run(db, FakeDriver())
    assert _used_today(db) == 2
    drv = FakeDriver()
    await _run(db, drv)
    assert drv.modales_abiertos == []
    assert _used_today(db) == 2
    assert {r.status for r in db.query(BusquedaFechaRun).all()} == {"partial"}
    assert db.query(BusquedaFechaRun).filter_by(fecha=date(2026, 11, 24)).one().status == "partial"


@pytest.mark.asyncio
async def test_cap_counts_across_cortes_in_the_same_day(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLICO_FECHA_MAX_DETALLES", 3, raising=False)
    drv = FakeDriver()
    await _run(db, drv, cortes=("90", "91"))
    assert len(drv.modales_abiertos) == 3
    assert _used_today(db) == 3


@pytest.mark.asyncio
async def test_next_day_starts_with_a_fresh_budget(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLICO_FECHA_MAX_DETALLES", 2, raising=False)
    await _run(db, FakeDriver())
    drv = FakeDriver()
    await _run(db, drv, hoy=date(2026, 11, 28))
    assert len(drv.modales_abiertos) == 2
    assert _used_today(db, date(2026, 11, 28)) == 2
    assert _used_today(db, HOY) == 2


def test_budget_remaining_is_cap_minus_used_today(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLICO_FECHA_MAX_DETALLES", 150, raising=False)
    assert svc.presupuesto_restante(db, HOY) == 150
    from datetime import datetime
    db.add(BusquedaFechaRun(
        fecha=date(2026, 11, 26), corte="90", status="partial", started_at=datetime.utcnow(),
        contadores={"detalles_por_dia": {HOY.isoformat(): 150}},
    ))
    db.commit()
    assert svc.presupuesto_restante(db, HOY) == 0
    assert svc.presupuesto_restante(db, date(2026, 11, 28)) == 150
