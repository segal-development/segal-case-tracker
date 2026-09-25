"""Motor de asignación automática de cartera por nivel.

Reparte causas sin abogado asignado entre los abogados de la firma cuyo
``nivel`` satisface lo que exige la matriz de cada causa, balanceando la carga.
Escribe SOLO el override ``assigned_*``; ``Case.lawyer_id`` es procedencia de
scraping y no se toca nunca.
"""

from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.asignacion_engine import (
    MOTIVO_AUTOMATICO,
    asignar_automatico,
)

FECHA = datetime(2026, 9, 1, 12, 0, 0)


@pytest.fixture
def court(db):
    obj = Court(code="TASIG", name="Juzgado Asignación", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _lawyer(db, rut, name, nivel, *, activo=True, de_la_firma=True):
    obj = Lawyer(rut=rut, name=name, nivel=nivel, role="lawyer",
                 is_firm_lawyer=de_la_firma, is_active=activo)
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _causa(db, court, rol, *, matriz, origen="pjud_etapa", owner=None, asignado=None):
    obj = Case(
        lawyer_id=owner.id if owner else _firma(db).id,
        court_id=court.id, rol=rol, status="active", competencia="civil",
        matriz=matriz, matriz_origen=origen,
        assigned_lawyer_id=asignado.id if asignado else None,
        created_at=FECHA, updated_at=FECHA,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _firma(db):
    lw = db.query(Lawyer).filter(Lawyer.rut == "99999999-9").first()
    if lw is None:
        lw = _lawyer(db, "99999999-9", "Cuenta scraping", None)
    return lw


class TestNivelRequerido:
    def test_una_m1_baja_va_a_un_junior(self, db, court):
        junior = _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja")

        plan = asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(causa)
        assert causa.assigned_lawyer_id == junior.id
        assert plan.asignadas == 1

    def test_nunca_asigna_a_un_nivel_que_no_corresponde(self, db, court):
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M3")  # M3 exige senior

        plan = asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(causa)
        assert causa.assigned_lawyer_id is None
        assert plan.asignadas == 0
        assert plan.omitidas.get("sin abogado del nivel requerido") == 1

    def test_matriz_sin_regla_se_omite(self, db, court):
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        sin_matriz = _causa(db, court, "C-1-2026", matriz=None)
        rara = _causa(db, court, "C-2-2026", matriz="M9 inventada")

        plan = asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(sin_matriz); db.refresh(rara)
        assert sin_matriz.assigned_lawyer_id is None
        assert rara.assigned_lawyer_id is None
        assert plan.omitidas.get("matriz sin regla de nivel") == 2


class TestClasificacionProvisoria:
    """El 51,3% de la cartera está clasificada por defecto, sin un solo
    movimiento scrapeado. Repartir sobre eso es repartir un valor inventado."""

    def test_por_defecto_no_asigna_clasificaciones_provisorias(self, db, court):
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja", origen="sin_detalle")

        plan = asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(causa)
        assert causa.assigned_lawyer_id is None
        assert plan.omitidas.get("clasificación provisoria") == 1

    def test_se_pueden_incluir_a_pedido(self, db, court):
        junior = _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja", origen="sin_detalle")

        asignar_automatico(db, actor_rut="admin", dry_run=False, incluir_provisorias=True)

        db.refresh(causa)
        assert causa.assigned_lawyer_id == junior.id


class TestNoPisaDecisionesHumanas:
    def test_una_causa_ya_asignada_no_se_toca(self, db, court):
        otro = _lawyer(db, "22222222-2", "Otro Junior", "junior")
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja", asignado=otro)

        plan = asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(causa)
        assert causa.assigned_lawyer_id == otro.id
        assert plan.omitidas.get("ya tiene abogado asignado") == 1

    def test_nunca_toca_lawyer_id(self, db, court):
        """``Case.lawyer_id`` es procedencia de scraping: tocarlo rompe la
        deduplicación del sync (``existing_by_rol``)."""
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja")
        original = causa.lawyer_id

        asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(causa)
        assert causa.lawyer_id == original


class TestBalanceDeCarga:
    def test_reparte_al_menos_cargado(self, db, court):
        a = _lawyer(db, "11111111-1", "Junior A", "junior")
        b = _lawyer(db, "22222222-2", "Junior B", "junior")
        # A arranca con una causa ya asignada a mano.
        _causa(db, court, "C-0-2026", matriz="M1 Baja", asignado=a)
        c1 = _causa(db, court, "C-1-2026", matriz="M1 Baja")
        c2 = _causa(db, court, "C-2-2026", matriz="M1 Baja")

        asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(c1); db.refresh(c2)
        # B estaba en 0 y A en 1: la primera va a B, la segunda empata y va a A.
        assert {c1.assigned_lawyer_id, c2.assigned_lawyer_id} == {a.id, b.id}

    def test_solo_abogados_activos_de_la_firma(self, db, court):
        _lawyer(db, "33333333-3", "Inactivo", "junior", activo=False)
        _lawyer(db, "44444444-4", "Externo", "junior", de_la_firma=False)
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja")

        plan = asignar_automatico(db, actor_rut="admin", dry_run=False)

        db.refresh(causa)
        assert causa.assigned_lawyer_id is None
        assert plan.asignadas == 0


class TestDryRun:
    def test_dry_run_no_escribe_nada(self, db, court):
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja")

        plan = asignar_automatico(db, actor_rut="admin", dry_run=True)

        db.refresh(causa)
        assert causa.assigned_lawyer_id is None
        assert plan.asignadas == 1  # el plan igual lo cuenta
        assert plan.aplicado is False

    def test_el_plan_detalla_cada_asignacion(self, db, court):
        junior = _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja")

        plan = asignar_automatico(db, actor_rut="admin", dry_run=True)

        assert len(plan.detalle) == 1
        item = plan.detalle[0]
        assert item.case_id == causa.id
        assert item.rol == "C-1-2026"
        assert item.matriz == "M1 Baja"
        assert item.lawyer_id == junior.id
        assert item.nivel == "junior"


class TestTrazabilidad:
    def test_marca_la_asignacion_como_automatica_y_reversible(self, db, court):
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        causa = _causa(db, court, "C-1-2026", matriz="M1 Baja")

        asignar_automatico(db, actor_rut="12345678-9", dry_run=False)

        db.refresh(causa)
        assert causa.assigned_motivo == MOTIVO_AUTOMATICO
        assert causa.assigned_by_rut == "12345678-9"
        assert causa.assigned_at is not None
        # Las automáticas se pueden identificar y deshacer en bloque.
        automaticas = (
            db.query(Case).filter(Case.assigned_motivo == MOTIVO_AUTOMATICO).count()
        )
        assert automaticas == 1


class TestLimite:
    def test_respeta_el_limite_pedido(self, db, court):
        _lawyer(db, "11111111-1", "Juana Junior", "junior")
        for i in range(5):
            _causa(db, court, f"C-{i}-2026", matriz="M1 Baja")

        plan = asignar_automatico(db, actor_rut="admin", dry_run=False, limite=2)

        assert plan.asignadas == 2
        assert db.query(Case).filter(Case.assigned_lawyer_id.isnot(None)).count() == 2
