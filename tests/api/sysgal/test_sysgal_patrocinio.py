"""Tests for GET /api/sysgal/v1/patrocinio."""

import hashlib
from datetime import datetime

import pytest

from app.config import settings
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sysgal_api_key import SysgalApiKey
from app.models.sysgal_causa import SysgalCausa

URL = "/api/sysgal/v1/patrocinio"
VALID_KEY = "sysgal-patrocinio-key"
ALLOWED_IP = "35.225.238.115"


@pytest.fixture(autouse=True)
def _no_ip_restriction(monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_API_ALLOWED_IPS", "")


def _auth():
    return {"X-API-Key": VALID_KEY}


@pytest.fixture
def seeded(db):
    db.add(
        SysgalApiKey(
            label="patrocinio-test",
            key_hash=hashlib.sha256(VALID_KEY.encode()).hexdigest(),
            is_active=True,
        )
    )
    lawyer = Lawyer(rut="16021492-9", name="Firm", is_active=True)
    court = Court(code="PA1", name="Juzgado PA", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()

    def make_case(rol, **kw):
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol=rol, status="active", **kw
        )
        db.add(case)
        db.commit()
        return case

    def make_causa(sysgal_id, rol, case=None, **kw):
        row = SysgalCausa(
            sysgal_id=sysgal_id,
            rol=rol,
            rol_norm=rol,
            tribunal=kw.pop("tribunal", "Juzgado PA"),
            case_id=case.id if case else None,
            synced_at=kw.pop("synced_at", datetime(2026, 11, 1)),
            **kw,
        )
        db.add(row)
        db.commit()
        return row

    # 101: escrito presentado (linked, pending patrocinio, no obligation)
    c1 = make_case("C-1-2026", patrocinio_pendiente=True)
    make_causa(101, "C-1-2026", c1)
    # 102: poder pendiente (open Previo a proveer)
    c2 = make_case("C-2-2026", patrocinio_pendiente=True)
    for day, desc, proc in (
        (1, "Opone excepciones", "Escrito"),
        (10, "[760]Previo a proveer", "Resolución"),
    ):
        db.add(
            Movement(
                case_id=c2.id,
                movement_date=datetime(2026, 10, day),
                procedure=proc,
                description=desc,
            )
        )
    db.commit()
    make_causa(102, "C-2-2026", c2)
    # 103: poder acreditado (firm abogado litigante, patrocinio cleared)
    c3 = make_case("C-3-2026")
    db.add(
        CaseLitigante(
            case_id=c3.id,
            participante="AB.DDO",
            rut="16.021.492-9",
            persona_type="NATURAL",
            nombre="Firm",
            natural_key="k3",
        )
    )
    db.commit()
    make_causa(103, "C-3-2026", c3)
    # 104: not in PJUD
    make_causa(104, "C-4-2026", None, synced_at=datetime(2026, 11, 10))
    return db


def _estados(body):
    return {i["sysgal_causa_id"]: i["estado"] for i in body["items"]}


def test_eliminada_causas_are_excluded(client, seeded):
    # A causa deleted in Sysgal is not reported, neither in the listing nor by id.
    case = seeded.query(Case).filter(Case.rol == "C-1-2026").one()
    seeded.add(
        SysgalCausa(
            sysgal_id=199, rol="C-1-2026", rol_norm="C-1-2026", tribunal="Juzgado PA",
            case_id=case.id, synced_at=datetime(2026, 11, 1), eliminada=True,
        )
    )
    seeded.commit()
    listing = client.get(URL, headers=_auth()).json()
    assert 199 not in _estados(listing)
    assert listing["total"] == 4
    by_id = client.get(URL, params={"sysgal_causa_id": "199"}, headers=_auth()).json()
    assert by_id["items"] == [] and by_id["total"] == 0


def test_requires_api_key(client, seeded):
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"X-API-Key": "wrong"}).status_code == 401


def test_each_state_and_item_shape(client, seeded):
    r = client.get(URL, headers=_auth(), params={"sysgal_causa_id": "101,102,103,104"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4 and body["page"] == 1 and body["per_page"] == 50
    assert _estados(body) == {
        101: "escrito_presentado",
        102: "poder_pendiente",
        103: "poder_acreditado",
        104: "sin_causa_pjud",
    }
    by_id = {i["sysgal_causa_id"]: i for i in body["items"]}
    assert set(by_id[102]) == {
        "sysgal_causa_id",
        "case_id",
        "rol",
        "tribunal",
        "estado",
        "bloqueado_por_mora",
        "poder_acreditado_at",
        "escrito_presentado_at",
        "actualizado_at",
    }
    assert by_id[102]["rol"] == "C-2-2026" and by_id[102]["tribunal"] == "Juzgado PA"
    assert by_id[104]["case_id"] is None
    assert all(i["bloqueado_por_mora"] is False for i in body["items"])


def test_repeatable_param_and_unknown_id(client, seeded):
    r = client.get(
        URL,
        headers=_auth(),
        params=[("sysgal_causa_id", "101"), ("sysgal_causa_id", "102"), ("sysgal_causa_id", "999")],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and set(_estados(body)) == {101, 102}


def test_unknown_id_only_returns_empty(client, seeded):
    r = client.get(URL, headers=_auth(), params={"sysgal_causa_id": "999"})
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["total"] == 0


def test_invalid_id_is_422(client, seeded):
    r = client.get(URL, headers=_auth(), params={"sysgal_causa_id": "abc"})
    assert r.status_code == 422


def test_rol_and_tribunal(client, seeded):
    r = client.get(URL, headers=_auth(), params={"rol": "C-3-2026", "tribunal": "juzgado pa"})
    assert r.status_code == 200
    assert _estados(r.json()) == {103: "poder_acreditado"}


def test_rol_without_tribunal_is_422(client, seeded):
    assert client.get(URL, headers=_auth(), params={"rol": "C-3-2026"}).status_code == 422


def test_actualizado_desde(client, seeded):
    r = client.get(URL, headers=_auth(), params={"actualizado_desde": "2026-11-05T00:00:00"})
    assert r.status_code == 200
    # Only 104 (synced 11-10) is newer; the cases' updated_at is "now" in tests, so
    # restrict by id to isolate the causa timestamp for the unlinked one.
    r2 = client.get(
        URL,
        headers=_auth(),
        params={"sysgal_causa_id": "104", "actualizado_desde": "2026-11-11T00:00:00"},
    )
    assert r2.json()["items"] == []
    r3 = client.get(
        URL,
        headers=_auth(),
        params={"sysgal_causa_id": "104", "actualizado_desde": "2026-11-10T00:00:00"},
    )
    assert _estados(r3.json()) == {104: "sin_causa_pjud"}
    assert r3.json()["items"][0]["actualizado_at"].startswith("2026-11-10")


def test_pagination(client, seeded):
    p = {"sysgal_causa_id": "101,102,103,104", "per_page": 2}
    first = client.get(URL, headers=_auth(), params={**p, "page": 1}).json()
    second = client.get(URL, headers=_auth(), params={**p, "page": 2}).json()
    assert first["total"] == second["total"] == 4
    assert [i["sysgal_causa_id"] for i in first["items"]] == [101, 102]
    assert [i["sysgal_causa_id"] for i in second["items"]] == [103, 104]


def test_ip_allowed_via_forwarded_for(client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_API_ALLOWED_IPS", f"1.2.3.4, {ALLOWED_IP}")
    h = {**_auth(), "X-Forwarded-For": f"{ALLOWED_IP}, 10.0.0.1"}
    assert client.get(URL, headers=h).status_code == 200


def test_ip_denied(client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_API_ALLOWED_IPS", ALLOWED_IP)
    r = client.get(URL, headers={**_auth(), "X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 403
    # No forwarded header at all: the ASGI peer is not allow-listed either.
    assert client.get(URL, headers=_auth()).status_code == 403


def test_ip_allowed_still_requires_key(client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_API_ALLOWED_IPS", ALLOWED_IP)
    assert client.get(URL, headers={"X-Forwarded-For": ALLOWED_IP}).status_code == 401


def test_existing_routers_not_ip_restricted(client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "SYSGAL_API_ALLOWED_IPS", ALLOWED_IP)
    r = client.get("/api/sysgal/v1/causas", headers=_auth(), params={"cliente_rut": "1-9"})
    assert r.status_code == 200
