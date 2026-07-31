"""Tests del motor de detección de hitos (Fase 1 · Slice 2).

Lógica pura: matching de reglas por ETAPA/TRÁMITE + clasificador de resultado
favorable sobre el texto del PDF. Sin DB ni GCS.
"""
from app.services.hito_detector import (
    clasificar_favorable,
    evaluar_movimiento,
    extraer_texto_pdf,
    regla_para_movimiento,
    REGLAS,
)


def _regla(code):
    return next(r for r in REGLAS if r.code == code)


# --- Etapa A: matching por metadata ---------------------------------------- #

class TestReglaPorMovimiento:
    def test_excepciones_resolucion_matchea_prescripcion(self):
        r = regla_para_movimiento("Excepciones", "Resolución")
        assert r is not None and r.code == "prescripcion"

    def test_tolera_tildes_y_mayusculas(self):
        assert regla_para_movimiento("EXCEPCIÓN", "RESOLUCIÓN") is not None

    def test_prejudicial_matchea_exhibicion(self):
        r = regla_para_movimiento("Presentación de la Medida Prejudicial", "Resolución")
        assert r is not None and r.code == "exhibicion"

    def test_escrito_no_matchea(self):
        # un TRÁMITE "Escrito" no es una resolución → no dispara
        assert regla_para_movimiento("Excepciones", "Escrito") is None

    def test_notificacion_no_matchea(self):
        assert regla_para_movimiento("Notificación demanda y su proveído", "Resolución") is None

    def test_stage_o_procedure_vacios(self):
        assert regla_para_movimiento("", "Resolución") is None
        assert regla_para_movimiento("Excepciones", None) is None


# --- Etapa B: clasificador de resultado favorable -------------------------- #

class TestClasificarFavorable:
    def test_favorable_con_firmeza_alta(self):
        r = _regla("prescripcion")
        cl = clasificar_favorable(r, "Se acoge la excepción de prescripción. Resolución firme y ejecutoriada.")
        assert cl.es_candidato and cl.confianza == "alta"

    def test_favorable_sin_firmeza_media(self):
        r = _regla("prescripcion")
        cl = clasificar_favorable(r, "Ha lugar a la excepción de prescripción opuesta.")
        assert cl.es_candidato and cl.confianza == "media"  # falta firmeza

    def test_veto_rechazo_no_es_candidato(self):
        r = _regla("prescripcion")
        cl = clasificar_favorable(r, "No ha lugar a la excepción de prescripción. Se rechaza.")
        assert cl.es_candidato is False

    def test_sin_keyword_no_es_candidato(self):
        r = _regla("prescripcion")
        cl = clasificar_favorable(r, "Téngase presente. Autos para resolver.")
        assert cl.es_candidato is False

    def test_pdf_ilegible_es_candidato_baja(self):
        r = _regla("prescripcion")
        cl = clasificar_favorable(r, "")
        assert cl.es_candidato and cl.confianza == "baja"
        assert "revisar" in (cl.frase or "").lower()

    def test_exhibicion_no_requiere_firmeza(self):
        r = _regla("exhibicion")
        cl = clasificar_favorable(r, "Ha lugar a la exhibición de documentos solicitada.")
        assert cl.es_candidato and cl.confianza == "alta"  # exhibición no exige firmeza

    def test_abandono_declarado_con_firmeza(self):
        r = _regla("abandono_3a")
        cl = clasificar_favorable(r, "Se declara el abandono del procedimiento. Resolución ejecutoriada.")
        assert cl.es_candidato and cl.confianza == "alta"


# --- Pipeline completo ----------------------------------------------------- #

class TestEvaluarMovimiento:
    def test_prescripcion_favorable_firme(self):
        d = evaluar_movimiento("Excepciones", "Resolución",
                               "Se acoge la prescripción. Sentencia firme.")
        assert d is not None
        assert d.hito_tipo_code == "pleno_prescripcion"
        assert d.regla_code == "prescripcion"
        assert d.confianza == "alta"

    def test_sin_regla_devuelve_none(self):
        assert evaluar_movimiento("Notificación", "Escrito", "cualquier cosa") is None

    def test_regla_matchea_pero_resultado_rechazado(self):
        # califica por metadata pero la resolución rechaza → no es hito
        assert evaluar_movimiento("Excepciones", "Resolución", "No ha lugar. Se rechaza.") is None


# --- Extracción de PDF (robustez) ------------------------------------------ #

class TestExtraerTextoPdf:
    def test_bytes_vacios(self):
        assert extraer_texto_pdf(b"") == ""

    def test_bytes_invalidos_no_lanza(self):
        assert extraer_texto_pdf(b"esto no es un pdf") == ""
