"""Tests for the case lifecycle timeline aggregation (req #8, slice 1).

Pure unit tests: model rows are constructed directly (no DB session needed)
and fed straight into the mapper helpers / aggregator. This module is
read-only aggregation — it must never write to the DB.
"""

from datetime import datetime

from app.models.alert import Alert
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_notificacion import CaseNotificacion
from app.models.document import Document
from app.models.movement import Movement
from app.services.timeline_service import (
    build_case_timeline,
    map_alert,
    map_deadline,
    map_document,
    map_escrito,
    map_exhorto,
    map_movement,
    map_notificacion,
)


# ---------------------------------------------------------------------------
# Per-source mappers
# ---------------------------------------------------------------------------


class TestMapMovement:
    def test_maps_kind_title_date_ref_id(self):
        m = Movement(
            id=1,
            case_id=10,
            stage="Primera instancia",
            procedure="Notificación",
            description="Resolución con fecha 29 de mayo de 2026",
            movement_date=datetime(2026, 5, 29, 10, 0, 0),
        )
        event = map_movement(m)
        assert event.kind == "movimiento"
        assert event.title == "Resolución con fecha 29 de mayo de 2026"
        assert event.description == "Primera instancia / Notificación"
        assert event.date == datetime(2026, 5, 29, 10, 0, 0)
        assert event.ref_id == 1
        assert event.status is None


class TestMapDocument:
    def test_uses_stored_at_when_present(self):
        d = Document(
            id=2,
            case_id=10,
            doc_type="resolution",
            status="stored",
            downloaded_at=datetime(2026, 5, 1, 0, 0, 0),
            stored_at=datetime(2026, 5, 2, 0, 0, 0),
            failed_at=None,
        )
        event = map_document(d)
        assert event.kind == "documento"
        assert event.date == datetime(2026, 5, 2, 0, 0, 0)
        assert event.status == "stored"
        assert event.ref_id == 2

    def test_falls_back_to_downloaded_at_when_no_stored_or_failed_at(self):
        """TDD scenario: doc with no stored_at falls back to downloaded_at."""
        d = Document(
            id=3,
            case_id=10,
            doc_type="cert_envio",
            status="pending",
            downloaded_at=datetime(2026, 4, 1, 0, 0, 0),
            stored_at=None,
            failed_at=None,
        )
        event = map_document(d)
        assert event.date == datetime(2026, 4, 1, 0, 0, 0)

    def test_uses_failed_at_over_downloaded_at(self):
        d = Document(
            id=4,
            case_id=10,
            doc_type="resolution",
            status="failed",
            downloaded_at=datetime(2026, 4, 1, 0, 0, 0),
            stored_at=None,
            failed_at=datetime(2026, 4, 3, 0, 0, 0),
        )
        event = map_document(d)
        assert event.date == datetime(2026, 4, 3, 0, 0, 0)
        assert event.status == "failed"


class TestMapDeadline:
    def test_title_includes_label_and_status(self):
        dl = CaseDeadline(
            id=5,
            case_id=10,
            deadline_type="excepciones_8d",
            due_date=datetime(2026, 6, 10).date(),
            triggered_at=datetime(2026, 6, 1).date(),
            status="active",
            created_at=datetime(2026, 6, 1, 8, 0, 0),
        )
        event = map_deadline(dl)
        assert event.kind == "plazo"
        assert event.title == "Plazo para oponer excepciones — active"
        assert event.status == "active"
        assert event.date == datetime(2026, 6, 1, 0, 0, 0)


class TestMapAlert:
    def test_maps_title_and_created_at(self):
        a = Alert(
            id=6,
            lawyer_id=1,
            case_id=10,
            type="new_movement",
            title="Nuevo movimiento detectado",
            message="Se detectó un nuevo movimiento",
            created_at=datetime(2026, 5, 15, 9, 30, 0),
        )
        event = map_alert(a)
        assert event.kind == "alerta"
        assert event.title == "Nuevo movimiento detectado"
        assert event.description == "Se detectó un nuevo movimiento"
        assert event.date == datetime(2026, 5, 15, 9, 30, 0)


class TestMapEscrito:
    def test_maps_tipo_and_fecha_ingreso(self):
        e = CaseEscrito(
            id=7,
            case_id=10,
            fecha_ingreso=datetime(2026, 5, 20, 0, 0, 0),
            tipo_escrito="Escrito de contestación",
            solicitante="Demandado",
            natural_key="key-1",
            created_at=datetime(2026, 5, 19, 0, 0, 0),
        )
        event = map_escrito(e)
        assert event.kind == "escrito"
        assert event.title == "Escrito de contestación"
        assert event.date == datetime(2026, 5, 20, 0, 0, 0)

    def test_falls_back_to_created_at_when_no_fecha_ingreso(self):
        e = CaseEscrito(
            id=8,
            case_id=10,
            fecha_ingreso=None,
            tipo_escrito="Escrito",
            solicitante="Demandante",
            natural_key="key-2",
            created_at=datetime(2026, 5, 19, 0, 0, 0),
        )
        event = map_escrito(e)
        assert event.date == datetime(2026, 5, 19, 0, 0, 0)


class TestMapNotificacion:
    def test_maps_tipo_notif_and_fecha_tramite(self):
        n = CaseNotificacion(
            id=9,
            case_id=10,
            rol="C-1-2026",
            estado_notif="notificada",
            tipo_notif="Personal",
            fecha_tramite=datetime(2026, 5, 18, 0, 0, 0),
            tipo_participante="demandado",
            nombre="Juan Perez",
            tramite="Notificación de demanda",
            natural_key="key-3",
            created_at=datetime(2026, 5, 17, 0, 0, 0),
        )
        event = map_notificacion(n)
        assert event.kind == "notificacion"
        assert event.title == "Personal"
        assert event.status == "notificada"
        assert event.date == datetime(2026, 5, 18, 0, 0, 0)


class TestMapExhorto:
    def test_maps_tipo_exhorto_and_fecha_ingreso(self):
        ex = CaseExhorto(
            id=10,
            case_id=10,
            rol_origen="C-1-2026",
            tipo_exhorto="ACTIVO",
            rol_destino="E-355-2026",
            fecha_ordena=datetime(2026, 5, 10, 0, 0, 0),
            fecha_ingreso=datetime(2026, 5, 12, 0, 0, 0),
            tribunal_destino="1er Juzgado Civil",
            estado="tramitado",
            natural_key="key-4",
            created_at=datetime(2026, 5, 9, 0, 0, 0),
        )
        event = map_exhorto(ex)
        assert event.kind == "exhorto"
        assert event.title == "ACTIVO"
        assert event.status == "tramitado"
        assert event.date == datetime(2026, 5, 12, 0, 0, 0)
        assert "C-1-2026" in event.description
        assert "E-355-2026" in event.description


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class TestBuildCaseTimeline:
    def test_empty_case_returns_empty_page(self):
        result = build_case_timeline()
        assert result.items == []
        assert result.total == 0
        assert result.pages == 0

    def test_merges_and_sorts_most_recent_first(self):
        movement = Movement(
            id=1, case_id=10, description="Movimiento",
            movement_date=datetime(2026, 5, 1, 0, 0, 0),
        )
        # Linked to the movement → inherits its date (05-01), NOT stored_at (05-05).
        document = Document(
            id=2, case_id=10, movement_id=1, doc_type="resolution", status="stored",
            downloaded_at=datetime(2026, 5, 2, 0, 0, 0),
            stored_at=datetime(2026, 5, 5, 0, 0, 0),
        )

        result = build_case_timeline(
            movements=[movement], documents=[document],
            movement_dates={1: datetime(2026, 5, 1, 0, 0, 0)},
        )

        # Alerts are no longer part of the timeline (they duplicate movements).
        assert result.total == 2
        assert {e.kind for e in result.items} == {"documento", "movimiento"}
        doc = next(e for e in result.items if e.kind == "documento")
        assert doc.date == datetime(2026, 5, 1, 0, 0, 0)

    def test_pagination_total_and_pages(self):
        movements = [
            Movement(
                id=i, case_id=10, description=f"Movimiento {i}",
                movement_date=datetime(2026, 5, i, 0, 0, 0),
            )
            for i in range(1, 6)
        ]

        result = build_case_timeline(movements=movements, page=1, per_page=2)

        assert result.total == 5
        assert result.pages == 3
        assert len(result.items) == 2
        # Most recent (day 5) first
        assert result.items[0].date == datetime(2026, 5, 5, 0, 0, 0)

        page2 = build_case_timeline(movements=movements, page=2, per_page=2)
        assert len(page2.items) == 2
        assert page2.items[0].date == datetime(2026, 5, 3, 0, 0, 0)
