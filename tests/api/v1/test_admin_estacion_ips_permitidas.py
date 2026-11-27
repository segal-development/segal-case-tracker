"""GET/PUT /api/v1/admin/estacion/ips-permitidas — admin only."""
from datetime import timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.services import app_settings

URL = "/api/v1/admin/estacion/ips-permitidas"


def _headers(db, rut, role):
    db.add(Lawyer(rut=rut, name=role, email=f"{role}@segal.cl", role=role))
    db.commit()
    token = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db):
    return _headers(db, "11111111-1", "admin")


def test_get_empty_by_default(client, admin):
    r = client.get(URL, headers=admin)
    assert r.status_code == 200 and r.json() == {"ips": []}


def test_put_then_get_round_trip(client, admin):
    r = client.put(URL, json={"ips": ["186.9.145.83", "10.0.0.0/8", "2001:db8::/32"]}, headers=admin)
    assert r.status_code == 200
    assert r.json() == {"ips": ["186.9.145.83", "10.0.0.0/8", "2001:db8::/32"]}
    assert client.get(URL, headers=admin).json() == r.json()


def test_put_empty_list_allowed(client, admin, db):
    client.put(URL, json={"ips": ["1.2.3.4"]}, headers=admin)
    r = client.put(URL, json={"ips": []}, headers=admin)
    assert r.status_code == 200 and r.json() == {"ips": []}
    assert client.get(URL, headers=admin).json() == {"ips": []}
    assert app_settings.get_text(db, app_settings.KEY_IPS_PERMITIDAS) == ""


def test_invalid_entry_422_names_it_and_saves_nothing(client, admin, db):
    client.put(URL, json={"ips": ["1.2.3.4"]}, headers=admin)
    r = client.put(URL, json={"ips": ["5.6.7.8", "not-an-ip"]}, headers=admin)
    assert r.status_code == 422
    assert "not-an-ip" in r.text
    assert client.get(URL, headers=admin).json() == {"ips": ["1.2.3.4"]}


def test_blank_or_comma_entry_is_invalid(client, admin):
    assert client.put(URL, json={"ips": [" "]}, headers=admin).status_code == 422
    assert client.put(URL, json={"ips": ["1.1.1.1,2.2.2.2"]}, headers=admin).status_code == 422


@pytest.mark.parametrize("role", ["lawyer", "auditor"])
def test_non_admin_403_and_nothing_saved(client, db, role):
    h = _headers(db, "22222222-2", role)
    assert client.get(URL, headers=h).status_code == 403
    assert client.put(URL, json={"ips": ["1.2.3.4"]}, headers=h).status_code == 403
    assert app_settings.get_text(db, app_settings.KEY_IPS_PERMITIDAS) is None


def test_anonymous_rejected(client):
    assert client.get(URL).status_code in (401, 403)
    assert client.put(URL, json={"ips": []}).status_code in (401, 403)


@pytest.mark.parametrize("bad", ["186.9.145.83/24", "0.0.0.0/0", "::/0", "1.2.3.4,5.6.7.8"])
def test_host_bits_zero_prefix_and_commas_are_422_nothing_saved(client, admin, bad):
    client.put(URL, json={"ips": ["1.2.3.4"]}, headers=admin)
    r = client.put(URL, json={"ips": ["5.6.7.8", bad]}, headers=admin)
    assert r.status_code == 422
    assert bad in r.text
    assert client.get(URL, headers=admin).json() == {"ips": ["1.2.3.4"]}
