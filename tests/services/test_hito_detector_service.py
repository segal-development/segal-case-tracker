"""Tests de la orquestación del detector (Fase 1 · Slice 3).

Siembra un escenario completo (causa + abogado de récord + movimiento resolución
+ documento stored) y verifica que HitoDetectorService.detectar crea el hito
sugerido correcto, con idempotencia, ventana temporal, cierre y atribución.
"""
import datetime as dt
import re
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.models.bono_cierre import BonoCierre, CIERRE_CERRADO
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.document import Document
from app.models.hito import HITO_SUGERIDO, Hito, HitoTipo
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services import hito_detector
from app.services.hito_detector import HitoDetectorService

PERIODO = "2026-07"
FECHA = dt.datetime(2026, 7, 15, 10, 0)
FAVORABLE = "Se acoge la excepción de prescripción. Sentencia firme y ejecutoriada."


class FakeStorage:
    def retrieve(self, path):
        return b"%PDF-fake"


@pytest.fixture
def esc(db):
    court = Court(code="TDET", name="Juzgado Detector", region="RM", type="civil")
    lw = Lawyer(rut="18888888-8", name="Pleno Detector", role="lawyer",
                is_firm_lawyer=True, is_active=True)
    tipo = HitoTipo(code="pleno_prescripcion", label="Prescripción", nivel="pleno",
                    valor_bruto=10000, orden=1)
    db.add_all([court, lw, tipo])
    db.commit()
    for o in (court, lw, tipo):
        db.refresh(o)
    case = Case(lawyer_id=lw.id, court_id=court.id, rol="C-500-2026", status="active",
                competencia="civil", created_at=FECHA, updated_at=FECHA)
    db.add(case)
    db.commit()
    db.refresh(case)
    db.add(CaseLitigante(case_id=case.id, participante="AB.DDO", rut=lw.rut,
                         persona_type="NATURAL", nombre=lw.name,
                         natural_key=f"{case.id}-{lw.rut}"))
    mv = Movement(case_id=case.id, stage="Excepciones", procedure="Resolución",
                  description="Se pronuncia sobre excepciones", movement_date=FECHA)
    db.add(mv)
    db.commit()
    db.refresh(mv)
    doc = Document(case_id=case.id, movement_id=mv.id, status="stored",
                   gcs_path="gs://b/cases/1/x.pdf", filename="x.pdf",
                   content_type="application/pdf")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return {"lw": lw, "tipo": tipo, "case": case, "mv": mv, "doc": doc}


def _run(db, monkeypatch, texto=FAVORABLE, periodo=PERIODO):
    monkeypatch.setattr(hito_detector, "extraer_texto_pdf", lambda b: texto)
    return HitoDetectorService(db, storage=FakeStorage()).detectar(periodo)


def test_detecta_prescripcion_favorable(db, esc, monkeypatch):
    res = _run(db, monkeypatch)
    assert res.creados == 1
    h = db.query(Hito).filter(Hito.origen == "detector").one()
    assert h.estado == HITO_SUGERIDO
    assert h.lawyer_id == esc["lw"].id
    assert h.hito_tipo_id == esc["tipo"].id
    assert h.movement_id == esc["mv"].id
    assert h.confianza == "alta"
    assert h.valor_bruto == 10000
    assert h.evidencia_storage_key == esc["doc"].gcs_path  # evidencia PJUD adjunta
    assert h.fecha_hito == FECHA.date()
    assert "C-500-2026" in (h.descripcion or "")


def test_idempotente_no_reduplica(db, esc, monkeypatch):
    _run(db, monkeypatch)
    res2 = _run(db, monkeypatch)
    assert res2.creados == 0 and res2.ya_existe == 1
    assert db.query(Hito).filter(Hito.origen == "detector").count() == 1


def test_periodo_cerrado_no_detecta(db, esc, monkeypatch):
    db.add(BonoCierre(periodo=PERIODO, estado=CIERRE_CERRADO))
    db.commit()
    res = _run(db, monkeypatch)
    assert res.cerrado is True and res.creados == 0
    assert db.query(Hito).filter(Hito.origen == "detector").count() == 0


class RaisingStorage:
    def retrieve(self, path):
        raise AssertionError("no debería bajar de GCS cuando documents.texto está presente")


def test_usa_documents_texto_sin_bajar_de_gcs(db, esc, monkeypatch):
    """Con el texto ya extraído (FTS backfill), el detector lo usa desde la
    columna y NO baja de GCS ni re-extrae el PDF."""
    esc["doc"].texto = FAVORABLE
    db.commit()

    def _boom(_b):
        raise AssertionError("no debería re-extraer cuando documents.texto está presente")
    monkeypatch.setattr(hito_detector, "extraer_texto_pdf", _boom)

    res = HitoDetectorService(db, storage=RaisingStorage()).detectar(PERIODO)
    assert res.creados == 1
    h = db.query(Hito).filter(Hito.origen == "detector").one()
    assert h.estado == HITO_SUGERIDO and h.confianza == "alta"


def test_resolucion_rechazada_no_crea(db, esc, monkeypatch):
    res = _run(db, monkeypatch, texto="No ha lugar a la excepción. Se rechaza.")
    assert res.creados == 0 and res.rechazados == 1


def test_sin_pdf_no_crea(db, esc, monkeypatch):
    db.query(Document).delete()
    db.commit()
    res = _run(db, monkeypatch)
    assert res.creados == 0 and res.sin_pdf == 1


def test_ventana_temporal_ignora_otro_mes(db, esc, monkeypatch):
    # Un movimiento de JUNIO no debe detectarse al correr JULIO (aunque se scrapee hoy).
    junio = Movement(case_id=esc["case"].id, stage="Excepciones", procedure="Resolución",
                     description="res junio", movement_date=dt.datetime(2026, 6, 15))
    db.add(junio)
    db.commit()
    db.refresh(junio)
    db.add(Document(case_id=esc["case"].id, movement_id=junio.id, status="stored",
                    gcs_path="gs://b/j.pdf", filename="j.pdf", content_type="application/pdf"))
    db.commit()
    res = _run(db, monkeypatch, periodo="2026-07")
    # solo el de julio; el de junio queda fuera de la ventana
    assert res.creados == 1
    assert db.query(Hito).filter(Hito.movement_id == junio.id).count() == 0


def test_no_atribuible_se_salta(db, esc, monkeypatch):
    # sin litigante AB.DDO → la causa no se atribuye a ningún abogado del estudio
    db.query(CaseLitigante).delete()
    db.commit()
    res = _run(db, monkeypatch)
    assert res.creados == 0 and res.sin_atribucion == 1


# --- Shadow-run / dry-run (Slice 5) ---------------------------------------- #

def test_dry_run_no_persiste_pero_cuenta(db, esc, monkeypatch):
    res = _run_dry(db, monkeypatch)
    assert res.dry_run is True
    assert res.creados == 0            # nada se creó
    assert res.would_create == 1       # pero se habría creado 1
    assert res.por_confianza["alta"] == 1
    # y confirmamos que la DB quedó limpia
    assert db.query(Hito).filter(Hito.origen == "detector").count() == 0


def test_dry_run_desglosa_por_confianza(db, esc, monkeypatch):
    # sin firmeza → confianza media
    res = _run_dry(db, monkeypatch, texto="Ha lugar a la excepción de prescripción.")
    assert res.would_create == 1
    assert res.por_confianza["media"] == 1 and res.por_confianza["alta"] == 0


def _run_dry(db, monkeypatch, texto=FAVORABLE, periodo=PERIODO):
    monkeypatch.setattr(hito_detector, "extraer_texto_pdf", lambda b: texto)
    return HitoDetectorService(db, storage=FakeStorage()).detectar(periodo, dry_run=True)


# ---------------------------------------------------------------------------
# Costo del mapa case → abogado
# ---------------------------------------------------------------------------


@contextmanager
def _count_selects(table: str):
    """Count SELECTs issued against *table* while the block runs."""
    from tests.conftest import engine

    pattern = re.compile(r"\bFROM\s+" + re.escape(table) + r"\b", re.IGNORECASE)
    counter = {"count": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        stripped = statement.strip()
        if stripped[:6].upper() == "SELECT" and pattern.search(stripped):
            counter["count"] += 1

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


def _abogado_con_causa(db, court, i):
    """Un abogado de la firma con una causa propia donde es abogado de récord."""
    lw = Lawyer(rut=f"1000000{i}-{i}", name=f"Abogado {i}", role="lawyer",
                is_firm_lawyer=True, is_active=True)
    db.add(lw); db.commit(); db.refresh(lw)
    case = Case(lawyer_id=lw.id, court_id=court.id, rol=f"C-70{i}-2026", status="active",
                competencia="civil", created_at=FECHA, updated_at=FECHA)
    db.add(case); db.commit(); db.refresh(case)
    db.add(CaseLitigante(case_id=case.id, participante="AB.DDO", rut=lw.rut,
                         persona_type="NATURAL", nombre=lw.name,
                         natural_key=f"{case.id}-{lw.rut}"))
    db.commit()
    return lw, case


class TestMapaCaseLawyerCost:
    """El detector corre en el cron diario. Nadie espera, pero compite por el
    mismo proxy que el scraping, y re-escanear la cartera entera una vez por
    abogado multiplica ese costo por la cantidad de abogados."""

    def test_un_solo_escaneo_para_todos_los_abogados(self, db):
        court = Court(code="TCOST", name="Juzgado Costo", region="RM", type="civil")
        db.add(court); db.commit(); db.refresh(court)
        esperados = {}
        for i in range(8):
            lw, case = _abogado_con_causa(db, court, i)
            esperados[case.id] = lw.id

        svc = HitoDetectorService(db, storage=FakeStorage())
        with _count_selects("case_litigantes") as counter:
            mapa = svc._mapa_case_lawyer()

        assert mapa == esperados
        assert counter["count"] <= 1, (
            f"la cartera se re-escanea una vez por abogado: {counter['count']} para 8 abogados"
        )

    def test_gana_el_primer_abogado_de_la_causa(self, db):
        """Dos abogados de récord en la misma causa cuentan una sola vez."""
        court = Court(code="TDOS", name="Juzgado Dos", region="RM", type="civil")
        db.add(court); db.commit(); db.refresh(court)
        lw_a, case = _abogado_con_causa(db, court, 1)
        lw_b = Lawyer(rut="20000002-2", name="Segundo", role="lawyer",
                      is_firm_lawyer=True, is_active=True)
        db.add(lw_b); db.commit(); db.refresh(lw_b)
        db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=lw_b.rut,
                             persona_type="NATURAL", nombre=lw_b.name,
                             natural_key=f"{case.id}-{lw_b.rut}"))
        db.commit()

        mapa = HitoDetectorService(db, storage=FakeStorage())._mapa_case_lawyer()

        assert list(mapa) == [case.id]
        assert mapa[case.id] in (lw_a.id, lw_b.id)
