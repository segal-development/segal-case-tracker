"""Tests de la orquestación del detector (Fase 1 · Slice 3).

Siembra un escenario completo (causa + abogado de récord + movimiento resolución
+ documento stored) y verifica que HitoDetectorService.detectar crea el hito
sugerido correcto, con idempotencia, ventana temporal, cierre y atribución.
"""
import datetime as dt

import pytest

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
