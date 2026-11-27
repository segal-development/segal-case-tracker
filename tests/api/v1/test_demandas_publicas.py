"""Public, no-auth GET /api/v1/publico/demandas/{token}[/{id}]."""
import hashlib
from datetime import date, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.demanda_publica import DemandaPublica
from app.models.lawyer import Lawyer
from app.models.public_link_token import PublicLinkToken

BASE = "/api/v1/publico/demandas/"
TOKEN = "tok-secret-value-for-tests"
INVALID = {"detail": "Link no válido."}


def _link(db, token=TOKEN, revoked=False, purpose="demandas_publicas"):
    row = PublicLinkToken(
        purpose=purpose,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        revoked_at=datetime.utcnow() if revoked else None,
    )
    db.add(row)
    db.commit()
    return row


def _dem(db, rol, fecha, tribunal="1º Juzgado Civil de Santiago", caratulado="A / B", texto="texto"):
    d = DemandaPublica(
        rol=rol, tribunal=tribunal, corte="C.A. de Santiago", caratulado=caratulado,
        fecha_ingreso=fecha, texto_demanda=texto, litigantes=[],
    )
    db.add(d)
    db.commit()
    return d


@pytest.fixture
def link(db):
    return _link(db)


def test_list_shape_ordering_and_headers(client, db, link):
    a = _dem(db, "C-1-2026", date(2026, 3, 1))
    b = _dem(db, "C-2-2026", date(2026, 3, 5), texto=None)
    c = _dem(db, "C-3-2026", date(2026, 3, 3))
    r = client.get(BASE + TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3 and body["page"] == 1 and body["per_page"] == 50
    assert [i["id"] for i in body["items"]] == [b.id, c.id, a.id]
    item = body["items"][0]
    assert set(item) == {"id", "rol", "tribunal", "caratulado", "fecha_ingreso",
                         "encontrada_at", "tiene_texto"}
    assert item["fecha_ingreso"] == "2026-03-05"
    assert item["tiene_texto"] is False and body["items"][1]["tiene_texto"] is True
    assert "texto_demanda" not in item
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert r.headers["cache-control"] == "no-store"


def test_empty_text_counts_as_no_text(client, db, link):
    _dem(db, "C-1-2026", date(2026, 3, 1), texto="")
    assert client.get(BASE + TOKEN).json()["items"][0]["tiene_texto"] is False


def test_filter_desde_hasta_inclusive(client, db, link):
    for i, d in enumerate([date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]):
        _dem(db, f"C-{i}-2026", d)
    r = client.get(BASE + TOKEN, params={"desde": "2026-03-02", "hasta": "2026-03-03"})
    assert {i["fecha_ingreso"] for i in r.json()["items"]} == {"2026-03-02", "2026-03-03"}
    assert r.json()["total"] == 2
    assert client.get(BASE + TOKEN, params={"desde": "2026-03-04"}).json()["total"] == 1
    assert client.get(BASE + TOKEN, params={"hasta": "2026-03-01"}).json()["total"] == 1


def test_filter_q_ilike_over_rol_caratulado_tribunal(client, db, link):
    _dem(db, "C-777-2026", date(2026, 3, 1), caratulado="X / Y", tribunal="Tribunal Uno")
    _dem(db, "C-2-2026", date(2026, 3, 2), caratulado="BANCO FOO / PEREZ", tribunal="Tribunal Dos")
    _dem(db, "C-3-2026", date(2026, 3, 3), caratulado="Z / W", tribunal="JUZGADO SAN MIGUEL")
    q = lambda s: {i["rol"] for i in client.get(BASE + TOKEN, params={"q": s}).json()["items"]}  # noqa: E731
    assert q("c-777") == {"C-777-2026"}
    assert q("banco foo") == {"C-2-2026"}
    assert q("san miguel") == {"C-3-2026"}
    assert q("nada") == set()


def test_pagination(client, db, link):
    for i in range(5):
        _dem(db, f"C-{i}-2026", date(2026, 3, 1) + timedelta(days=i))
    p1 = client.get(BASE + TOKEN, params={"per_page": 2, "page": 1}).json()
    p3 = client.get(BASE + TOKEN, params={"per_page": 2, "page": 3}).json()
    p9 = client.get(BASE + TOKEN, params={"per_page": 2, "page": 9}).json()
    assert p1["total"] == 5 and p1["per_page"] == 2 and len(p1["items"]) == 2
    assert [i["fecha_ingreso"] for i in p1["items"]] == ["2026-03-05", "2026-03-04"]
    assert p3["page"] == 3 and [i["fecha_ingreso"] for i in p3["items"]] == ["2026-03-01"]
    assert p9["items"] == [] and p9["total"] == 5


@pytest.mark.parametrize("params", [{"page": 0}, {"per_page": 0}, {"per_page": 1000}, {"desde": "no-date"}])
def test_invalid_params_422(client, link, params):
    assert client.get(BASE + TOKEN, params=params).status_code == 422


def test_detail_shape_and_headers(client, db, link):
    d = _dem(db, "C-1-2026", date(2026, 3, 1), texto="DEMANDA COMPLETA")
    r = client.get(f"{BASE}{TOKEN}/{d.id}")
    assert r.status_code == 200
    assert r.json() == {
        "id": d.id, "rol": "C-1-2026", "tribunal": "1º Juzgado Civil de Santiago",
        "caratulado": "A / B", "fecha_ingreso": "2026-03-01",
        "encontrada_at": r.json()["encontrada_at"], "texto_demanda": "DEMANDA COMPLETA",
    }
    assert r.json()["encontrada_at"]
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert r.headers["cache-control"] == "no-store"


def test_detail_missing_id_is_404_link_text(client, db, link):
    r = client.get(f"{BASE}{TOKEN}/99999")
    assert r.status_code == 404 and r.json() == INVALID


def test_unknown_token_404_exact_with_headers(client, db, link):
    for url in (BASE + "otro-token", BASE + "otro-token/1"):
        r = client.get(url)
        assert r.status_code == 404 and r.json() == INVALID
        assert r.headers["x-robots-tag"] == "noindex, nofollow"
        assert r.headers["cache-control"] == "no-store"


def test_revoked_token_same_404(client, db):
    _link(db, revoked=True)
    d = _dem(db, "C-1-2026", date(2026, 3, 1))
    unknown = client.get(BASE + "zzz")
    for url in (BASE + TOKEN, f"{BASE}{TOKEN}/{d.id}"):
        r = client.get(url)
        assert r.status_code == 404 and r.json() == INVALID
        assert r.content == unknown.content


def test_other_purpose_token_is_not_valid(client, db):
    _link(db, purpose="otra")
    assert client.get(BASE + TOKEN).status_code == 404


def test_last_used_at_updated_only_on_valid_use(client, db, link):
    assert link.last_used_at is None
    client.get(BASE + "otro")
    db.refresh(link)
    assert link.last_used_at is None
    client.get(BASE + TOKEN)
    db.refresh(link)
    assert link.last_used_at is not None


def test_no_auth_needed_and_procurador_not_blocked(client, db, link):
    assert client.get(BASE + TOKEN).status_code == 200
    db.add(Lawyer(rut="33333333-3", name="p", email="p@segal.cl", role="procurador"))
    db.commit()
    h = {"Authorization": "Bearer " + create_access_token({"sub": "33333333-3"}, expires_delta=timedelta(minutes=5))}
    assert client.get(BASE + TOKEN, headers=h).status_code == 200


def test_token_not_in_activity_audit(client, db, link):
    from app.models.activity_event import ActivityEvent
    client.get(BASE + TOKEN)
    assert not any(TOKEN in (str(getattr(e, "path", "")) + str(getattr(e, "target", "")))
                   for e in db.query(ActivityEvent).all())
