"""GET/PUT /sysgal/incorporaciones/ritmo — admin only, DB-stored pace."""

from datetime import timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.services import app_settings

ADMIN_RUT, LAWYER_RUT = "11111111-1", "22222222-2"
URL = "/api/v1/sysgal/incorporaciones/ritmo"


def _headers(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))}


@pytest.fixture
def people(db):
    db.add_all([Lawyer(rut=ADMIN_RUT, name="Admin", role="admin"),
                Lawyer(rut=LAWYER_RUT, name="Lawyer", role="lawyer")])
    db.commit()


def test_both_verbs_require_admin(client, people):
    assert client.get(URL).status_code in (401, 403)
    assert client.get(URL, headers=_headers(LAWYER_RUT)).status_code == 403
    assert client.put(URL, json={"por_dia": 5}, headers=_headers(LAWYER_RUT)).status_code == 403


def test_get_defaults_to_off(client, people):
    response = client.get(URL, headers=_headers(ADMIN_RUT))

    assert response.status_code == 200
    body = response.json()
    assert (body["por_dia"], body["por_hora"], body["frenada_hoy"]) == (0, 10, False)


def test_put_sets_and_get_reads_back_and_partial_update_keeps_the_other(client, db, people):
    admin = _headers(ADMIN_RUT)

    assert client.put(URL, json={"por_dia": 100}, headers=admin).status_code == 200
    assert client.put(URL, json={"por_hora": 12}, headers=admin).json()["por_hora"] == 12

    body = client.get(URL, headers=admin).json()
    assert (body["por_dia"], body["por_hora"]) == (100, 12)
    assert app_settings.get_ritmo(db) == {"por_dia": 100, "por_hora": 12}


def test_put_rejects_negative_and_stores_nothing(client, db, people):
    response = client.put(URL, json={"por_dia": -5}, headers=_headers(ADMIN_RUT))

    assert response.status_code == 422
    assert app_settings.get_ritmo(db) == {"por_dia": 0, "por_hora": 10}
