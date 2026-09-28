"""Tests for app.services.poda — poda de causas sin cobertura comercial.

Comportamiento cubierto (no implementación):
  - la regla de podabilidad exacta (todas las partes evaluables caducado)
  - exclusión de representantes (AB./ABG./AP.) al evaluar partes
  - exhaustividad de las categorías de clasificar_cartera
  - dry_run no escribe nada; dry_run=False marca y es idempotente
  - guarda de movimiento reciente (default excluye, incluir_con_movimiento incluye)
  - limite acota la cantidad efectivamente podada
"""

from datetime import datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.poda import (
    DIAS_MOVIMIENTO_RECIENTE,
    MOTIVO_SIN_COBERTURA,
    aplicar_poda,
    clasificar_cartera,
)

ACTOR_RUT = "11111111-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lawyer(db) -> Lawyer:
    lw = Lawyer(rut="99999999-9", name="Firma Test", is_active=True)
    db.add(lw)
    db.flush()
    return lw


def _court(db) -> Court:
    court = db.query(Court).first()
    if court:
        return court
    court = Court(code="TEST-COURT", name="Tribunal Test", region="RM", type="civil")
    db.add(court)
    db.flush()
    return court


def _case(db, lawyer: Lawyer, court: Court, rol: str, **kwargs) -> Case:
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        competencia="civil",
        status="active",
        **kwargs,
    )
    db.add(case)
    db.flush()
    return case


def _litigante(db, case: Case, rut: str, participante: str = "DDO.") -> CaseLitigante:
    lit = CaseLitigante(
        case_id=case.id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre="Fulano de Tal",
        natural_key=f"{case.id}-{participante}-{rut}",
    )
    db.add(lit)
    db.flush()
    return lit


def _estado(db, rut: str, estado_codigo: str, vigencia_hasta=None) -> ClienteSysgalEstado:
    estado = ClienteSysgalEstado(
        rut=rut,
        encontrado=True,
        estado_codigo=estado_codigo,
        vigencia_hasta=vigencia_hasta,
    )
    db.add(estado)
    db.flush()
    return estado


# ---------------------------------------------------------------------------
# Regla de podabilidad
# ---------------------------------------------------------------------------

class TestReglaDePodabilidad:
    def test_causa_con_todas_las_partes_caducadas_es_podable(self, db):
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-1-2024")
        _litigante(db, case, "11111111-1")
        _estado(db, "11111111-1", "TERMINADO")
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id in clasificacion.podables

    def test_causa_con_una_parte_activa_no_es_podable(self, db):
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-2-2024")
        _litigante(db, case, "11111111-1")
        _litigante(db, case, "22222222-2", participante="DTE.")
        _estado(db, "11111111-1", "TERMINADO")
        _estado(db, "22222222-2", "ACTIVO")
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id not in clasificacion.podables
        assert clasificacion.con_cliente_activo == 1

    def test_causa_con_una_parte_morosa_no_es_podable(self, db):
        """Un moroso SIGUE siendo cliente: no es podable."""
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-3-2024")
        _litigante(db, case, "11111111-1")
        _litigante(db, case, "22222222-2", participante="DTE.")
        _estado(db, "11111111-1", "TERMINADO")
        _estado(db, "22222222-2", "MOROSO_INACTIVO")
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id not in clasificacion.podables
        assert clasificacion.con_cliente_moroso == 1

    def test_causa_con_parte_ausente_del_cache_no_es_podable(self, db):
        """Desconocido nunca es podable, aunque el resto esté caducado."""
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-4-2024")
        _litigante(db, case, "11111111-1")
        _litigante(db, case, "33333333-3", participante="DTE.")
        _estado(db, "11111111-1", "TERMINADO")
        # 33333333-3 nunca fue consultado en Sysgal — no hay fila en el cache.
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id not in clasificacion.podables

    def test_representantes_se_ignoran_al_evaluar_partes(self, db):
        """AB./ABG./AP. no cuentan como parte evaluable."""
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-5-2024")
        _litigante(db, case, "11111111-1", participante="DDO.")
        _litigante(db, case, "44444444-4", participante="AB.DDO")
        _litigante(db, case, "55555555-5", participante="ABG.DTE")
        _litigante(db, case, "66666666-6", participante="AP.DTE")
        _estado(db, "11111111-1", "TERMINADO")
        # Los representantes NUNCA tienen fila en el cache (no se les consulta),
        # y aun así la causa debe ser podable porque no cuentan como partes.
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id in clasificacion.podables

    def test_causa_sin_litigantes_con_rut_cae_en_sin_partes_evaluables(self, db):
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-6-2024")
        # Litigante con RUT vacío (sin detalle scrapeado aún).
        _litigante(db, case, "", participante="DDO.")
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id not in clasificacion.podables
        assert clasificacion.sin_partes_evaluables == 1

    def test_causa_con_parte_caducada_y_parte_desconocida_cuenta_como_sin_parte_conocida(self, db):
        """Diseño: una mezcla caducado+desconocido NO es podable (regla 3) y cae en
        sin_parte_conocida, no en podables — el desconocido domina sobre el caducado."""
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-8-2024")
        _litigante(db, case, "11111111-1")
        _litigante(db, case, "77777777-7", participante="DTE.")
        _estado(db, "11111111-1", "TERMINADO")
        # 77777777-7 nunca fue consultado — ausente del cache.
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id not in clasificacion.podables
        assert clasificacion.sin_parte_conocida == 1

    def test_causa_totalmente_sin_litigantes_cae_en_sin_partes_evaluables(self, db):
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-7-2024")
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert case.id not in clasificacion.podables
        assert clasificacion.sin_partes_evaluables == 1


# ---------------------------------------------------------------------------
# clasificar_cartera — exhaustividad
# ---------------------------------------------------------------------------

class TestClasificarCartera:
    def test_categorias_suman_total_causas(self, db):
        lw, court = _lawyer(db), _court(db)

        # podable
        c1 = _case(db, lw, court, "C-10-2024")
        _litigante(db, c1, "11111111-1")
        _estado(db, "11111111-1", "TERMINADO")

        # activo
        c2 = _case(db, lw, court, "C-11-2024")
        _litigante(db, c2, "22222222-2")
        _estado(db, "22222222-2", "ACTIVO")

        # moroso
        c3 = _case(db, lw, court, "C-12-2024")
        _litigante(db, c3, "33333333-3")
        _estado(db, "33333333-3", "MOROSO_INACTIVO")

        # sin_parte_conocida (ausente del cache)
        c4 = _case(db, lw, court, "C-13-2024")
        _litigante(db, c4, "44444444-4")

        # sin_partes_evaluables
        c5 = _case(db, lw, court, "C-14-2024")
        db.commit()

        clasificacion = clasificar_cartera(db)

        total = (
            len(clasificacion.podables)
            + clasificacion.con_cliente_activo
            + clasificacion.con_cliente_moroso
            + clasificacion.sin_parte_conocida
            + clasificacion.sin_partes_evaluables
        )
        assert total == clasificacion.total_causas
        assert clasificacion.total_causas == 5

    def test_causas_ya_podadas_no_entran_al_reparto(self, db):
        """Ya podadas quedan afuera de clasificar_cartera (no se re-evalúan)."""
        lw, court = _lawyer(db), _court(db)
        case = _case(db, lw, court, "C-20-2024")
        _litigante(db, case, "11111111-1")
        _estado(db, "11111111-1", "TERMINADO")
        case.poda_at = datetime.utcnow()
        case.poda_motivo = MOTIVO_SIN_COBERTURA
        case.poda_por_rut = ACTOR_RUT
        db.commit()

        clasificacion = clasificar_cartera(db)

        assert clasificacion.total_causas == 0
        assert case.id not in clasificacion.podables


# ---------------------------------------------------------------------------
# aplicar_poda — dry_run / escritura / idempotencia
# ---------------------------------------------------------------------------

class TestAplicarPoda:
    def _causa_podable(self, db, lw, court, rol="C-30-2024", last_movement_at=None):
        case = _case(db, lw, court, rol, last_movement_at=last_movement_at)
        rut = f"{abs(hash(rol)) % 90000000 + 10000000}-1"
        _litigante(db, case, rut)
        _estado(db, rut, "TERMINADO")
        return case

    def test_dry_run_no_escribe_nada(self, db):
        lw, court = _lawyer(db), _court(db)
        case = self._causa_podable(db, lw, court)
        db.commit()

        resultado = aplicar_poda(db, actor_rut=ACTOR_RUT, dry_run=True)

        db.refresh(case)
        assert case.poda_at is None
        assert resultado["dry_run"] is True
        assert resultado["podadas"] == 1

    def test_dry_run_false_marca_poda_at_motivo_y_actor(self, db):
        lw, court = _lawyer(db), _court(db)
        case = self._causa_podable(db, lw, court)
        db.commit()

        resultado = aplicar_poda(db, actor_rut=ACTOR_RUT, dry_run=False)

        db.refresh(case)
        assert case.poda_at is not None
        assert case.poda_motivo == MOTIVO_SIN_COBERTURA
        assert case.poda_por_rut == ACTOR_RUT
        assert resultado["dry_run"] is False
        assert resultado["podadas"] == 1

    def test_aplicar_poda_es_idempotente(self, db):
        lw, court = _lawyer(db), _court(db)
        case = self._causa_podable(db, lw, court)
        db.commit()

        aplicar_poda(db, actor_rut=ACTOR_RUT, dry_run=False)
        db.refresh(case)
        primera_poda_at = case.poda_at

        resultado_segunda = aplicar_poda(db, actor_rut=ACTOR_RUT, dry_run=False)

        db.refresh(case)
        assert case.poda_at == primera_poda_at, "una causa ya podada no se vuelve a podar"
        assert resultado_segunda["podadas"] == 0

    def test_movimiento_reciente_se_saltea_por_default(self, db):
        lw, court = _lawyer(db), _court(db)
        reciente = datetime.utcnow() - timedelta(days=5)
        case = self._causa_podable(db, lw, court, last_movement_at=reciente)
        db.commit()

        resultado = aplicar_poda(db, actor_rut=ACTOR_RUT, dry_run=False)

        db.refresh(case)
        assert case.poda_at is None, "causa con movimiento reciente no debe podarse por default"
        assert resultado["podadas"] == 0
        assert resultado["salteadas_por_movimiento"] == 1

    def test_movimiento_reciente_se_incluye_con_flag_explicito(self, db):
        lw, court = _lawyer(db), _court(db)
        reciente = datetime.utcnow() - timedelta(days=5)
        case = self._causa_podable(db, lw, court, last_movement_at=reciente)
        db.commit()

        resultado = aplicar_poda(
            db, actor_rut=ACTOR_RUT, dry_run=False, incluir_con_movimiento=True
        )

        db.refresh(case)
        assert case.poda_at is not None
        assert resultado["podadas"] == 1
        assert resultado["salteadas_por_movimiento"] == 0

    def test_movimiento_fuera_de_la_ventana_no_se_saltea(self, db):
        """Movimiento más viejo que dias_movimiento_reciente sí se poda por default."""
        lw, court = _lawyer(db), _court(db)
        viejo = datetime.utcnow() - timedelta(days=DIAS_MOVIMIENTO_RECIENTE + 30)
        case = self._causa_podable(db, lw, court, last_movement_at=viejo)
        db.commit()

        resultado = aplicar_poda(db, actor_rut=ACTOR_RUT, dry_run=False)

        db.refresh(case)
        assert case.poda_at is not None
        assert resultado["salteadas_por_movimiento"] == 0

    def test_limite_acota_la_cantidad_podada(self, db):
        lw, court = _lawyer(db), _court(db)
        for i in range(5):
            self._causa_podable(db, lw, court, rol=f"C-LIM-{i}-2024")
        db.commit()

        resultado = aplicar_poda(db, actor_rut=ACTOR_RUT, dry_run=False, limite=2)

        assert resultado["podadas"] == 2
        podadas_reales = db.query(Case).filter(Case.poda_at.isnot(None)).count()
        assert podadas_reales == 2
