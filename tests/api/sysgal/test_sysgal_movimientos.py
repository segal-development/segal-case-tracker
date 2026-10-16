"""Tests for GET /api/sysgal/v1/movimientos."""

import hashlib
from datetime import timedelta

from app.api.sysgal.movimientos import _encode_cursor, _previous_chilean_day_bounds
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sysgal_api_key import SysgalApiKey


URL = "/api/sysgal/v1/movimientos"
VALID_KEY = "sysgal-movimientos-key"


def _seed_key(db):
    db.add(
        SysgalApiKey(
            label="movimientos-test",
            key_hash=hashlib.sha256(VALID_KEY.encode()).hexdigest(),
            is_active=True,
        )
    )
    db.commit()


def _auth():
    return {"X-API-Key": VALID_KEY}


def _seed(db):
    start, end, target = _previous_chilean_day_bounds()
    lawyer = Lawyer(rut="16021492-9", name="Firm")
    court = Court(code="MOV1", name="Juzgado de prueba", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-123-2026",
        status="active",
        plaintiff="BANCO",
        defendant="CLIENTE",
    )
    db.add(case)
    db.commit()
    db.add(
        CaseLitigante(
            case_id=case.id,
            participante="DDO.",
            rut="18.765.432-1",
            persona_type="NATURAL",
            nombre="CLIENTE",
            natural_key="mov-client",
        )
    )

    yesterday_early = Movement(
        case_id=case.id,
        stage="Discusión",
        procedure="Ingreso",
        description="Primer movimiento detectado ayer",
        folio="1",
        movement_date=start - timedelta(days=30),
        created_at=start + timedelta(minutes=1),
    )
    yesterday_late = Movement(
        case_id=case.id,
        stage="Prueba",
        procedure="Resolución",
        description="Segundo movimiento detectado ayer",
        folio="2",
        movement_date=start - timedelta(days=20),
        created_at=end - timedelta(minutes=1),
        document_url="https://pjud.example/directo.pdf",
    )
    outside = Movement(
        case_id=case.id,
        description="Detectado hoy, no debe aparecer",
        movement_date=end,
        created_at=end + timedelta(minutes=1),
    )
    db.add_all([yesterday_early, yesterday_late, outside])
    db.commit()

    db.add_all(
        [
            Document(
                case_id=case.id,
                movement_id=yesterday_early.id,
                filename="resolucion.pdf",
                pjud_url="https://pjud.example/resolucion.pdf",
                gcs_path="gs://bucket/resolucion.pdf",
                status="stored",
            ),
            Document(
                case_id=case.id,
                movement_id=yesterday_late.id,
                filename="otra.pdf",
                pjud_url="https://pjud.example/otra.pdf",
                gcs_path="gs://bucket/otra.pdf",
                status="stored",
            ),
        ]
    )
    db.commit()
    return target


def test_requires_sysgal_api_key(client, db):
    _seed(db)
    assert client.get(URL).status_code == 401


def test_returns_only_movements_detected_yesterday_with_case_client_and_document(client, db):
    _seed_key(db)
    target = _seed(db)

    response = client.get(URL, headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["fecha_deteccion"] == target
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["per_page"] == 100
    assert payload["next_cursor"]
    assert payload["has_more"] is False
    assert [item["folio"] for item in payload["items"]] == ["2", "1"]
    assert all(item["rol"] == "C-123-2026" for item in payload["items"])
    assert all(item["rut_cliente"] == "18765432-1" for item in payload["items"])

    latest, earliest = payload["items"]
    assert latest["documento_url"] == "https://pjud.example/directo.pdf"
    assert latest["documento_ruta"] == "gs://bucket/otra.pdf"
    assert earliest["documento_url"] == "https://pjud.example/resolucion.pdf"
    assert earliest["documento_ruta"] == "gs://bucket/resolucion.pdf"

    resumed = client.get(
        URL, params={"desde": payload["next_cursor"]}, headers=_auth()
    )
    assert resumed.status_code == 200
    assert resumed.json()["total"] == 1
    assert resumed.json()["items"][0]["descripcion"] == "Detectado hoy, no debe aparecer"


def test_pagination_is_applied_bounded_and_reports_real_total(client, db):
    _seed_key(db)
    _seed(db)

    response = client.get(URL, params={"page": 1, "per_page": 1}, headers=_auth())
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["page"] == 1
    assert response.json()["per_page"] == 1
    assert response.json()["has_more"] is True
    assert response.json()["next_cursor"] is None
    assert len(response.json()["items"]) == 1

    second_page = client.get(
        URL, params={"page": 2, "per_page": 1}, headers=_auth()
    )
    assert second_page.status_code == 200
    assert second_page.json()["total"] == 2
    assert second_page.json()["page"] == 2
    assert second_page.json()["has_more"] is False
    assert second_page.json()["next_cursor"]
    assert second_page.json()["items"][0]["folio"] == "1"

    assert client.get(URL, params={"page": 0}, headers=_auth()).status_code == 422
    assert client.get(URL, params={"per_page": 0}, headers=_auth()).status_code == 422
    assert client.get(URL, params={"per_page": 1001}, headers=_auth()).status_code == 422


def test_cursor_resumes_after_exact_timestamp_and_recovers_later_days(client, db):
    _seed_key(db)
    _seed(db)
    movements = db.query(Movement).order_by(Movement.created_at, Movement.id).all()
    first, second, today = movements

    # Same timestamp exercises the id tiebreaker: only ids after the cursor
    # are returned, even when their created_at values are equal.
    second.created_at = first.created_at
    db.commit()
    cursor = _encode_cursor(first.created_at, first.id)

    response = client.get(
        URL, params={"desde": cursor, "per_page": 1}, headers=_auth()
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["fecha_deteccion"] is None
    assert payload["total"] == 2
    assert payload["has_more"] is True
    assert payload["items"][0]["movimiento_id"] == second.id
    assert payload["next_cursor"]

    resumed = client.get(
        URL,
        params={"desde": payload["next_cursor"], "per_page": 10},
        headers=_auth(),
    )
    assert resumed.status_code == 200
    assert resumed.json()["total"] == 1
    assert resumed.json()["has_more"] is False
    assert resumed.json()["items"][0]["movimiento_id"] == today.id


def test_invalid_cursor_returns_422(client, db):
    _seed_key(db)
    _seed(db)
    response = client.get(URL, params={"desde": "no-es-un-cursor"}, headers=_auth())
    assert response.status_code == 422
    assert response.json()["detail"] == "Cursor 'desde' inválido"
