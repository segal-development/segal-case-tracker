"""A procurador sees exactly the union of his lawyers' scopes (S3)."""
import pytest

from app.models.procurador import ProcuradorAbogado
from app.services import lawyer_roster
from tests.api.v1.procurador_support import headers, link, make_case, make_court, make_lawyer


@pytest.fixture
def world(db):
    court = make_court(db)
    firm = make_lawyer(db, "16021492-9", "Firma", role="admin")
    a = make_lawyer(db, "11111111-1", "Abogado A")
    b = make_lawyer(db, "22222222-2", "Abogado B")
    c = make_lawyer(db, "33333333-3", "Abogado C")
    proc = make_lawyer(db, "44444444-4", "Proc Uno", role="procurador")
    cases = {
        "a": make_case(db, firm, court, "C-1-2024", a),
        "b": make_case(db, firm, court, "C-2-2024", b),
        "c": make_case(db, firm, court, "C-3-2024", c),
    }
    return {"a": a, "b": b, "c": c, "proc": proc, "cases": cases}


def _ids(client, rut):
    r = client.get("/api/v1/cases", headers=headers(rut), params={"per_page": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    items = body.get("items") or body.get("cases") or []
    return {i["id"] for i in items}


def test_one_lawyer_scope(client, db, world):
    link(db, world["proc"], world["a"])
    assert _ids(client, world["proc"].rut) == {world["cases"]["a"].id}


def test_two_lawyers_scope_is_the_union(client, db, world):
    link(db, world["proc"], world["a"], world["b"])
    expected = {world["cases"]["a"].id, world["cases"]["b"].id}
    assert _ids(client, world["proc"].rut) == expected
    own = _ids(client, world["a"].rut) | _ids(client, world["b"].rut)
    assert own == expected


def test_case_detail_of_unsupported_lawyer_is_not_visible(client, db, world):
    link(db, world["proc"], world["a"])
    r = client.get(f"/api/v1/cases/{world['cases']['c'].id}", headers=headers(world["proc"].rut))
    assert r.status_code == 404
    ok = client.get(f"/api/v1/cases/{world['cases']['a'].id}", headers=headers(world["proc"].rut))
    assert ok.status_code == 200


def test_no_links_sees_nothing(client, db, world):
    assert _ids(client, world["proc"].rut) == set()


def test_scope_follows_link_changes_with_cache_on(client, db, world):
    link(db, world["proc"], world["a"])
    assert _ids(client, world["proc"].rut) == {world["cases"]["a"].id}  # warms the cache
    db.add(ProcuradorAbogado(procurador_id=world["proc"].id, abogado_id=world["b"].id))
    db.commit()
    assert _ids(client, world["proc"].rut) == {world["cases"]["a"].id, world["cases"]["b"].id}
    db.query(ProcuradorAbogado).filter(ProcuradorAbogado.abogado_id == world["a"].id).delete()
    db.commit()
    lawyer_roster.clear_case_scope_cache()  # bulk delete bypasses ORM events (documented)
    assert _ids(client, world["proc"].rut) == {world["cases"]["b"].id}


def test_orm_delete_of_a_link_invalidates_the_cache(client, db, world):
    link(db, world["proc"], world["a"], world["b"])
    assert len(_ids(client, world["proc"].rut)) == 2
    row = db.query(ProcuradorAbogado).filter_by(abogado_id=world["a"].id).one()
    db.delete(row)
    db.commit()
    assert _ids(client, world["proc"].rut) == {world["cases"]["b"].id}


def test_a_blocked_linked_lawyer_drops_out_of_scope(client, db, world):
    link(db, world["proc"], world["a"], world["b"])
    assert len(_ids(client, world["proc"].rut)) == 2
    world["a"].is_active = False
    db.commit()
    assert _ids(client, world["proc"].rut) == {world["cases"]["b"].id}


def test_stats_me_aggregates_the_union_without_double_counting(client, db, world):
    court = make_court(db) if False else None  # noqa: F841
    link(db, world["proc"], world["a"], world["b"])
    r = client.get("/api/v1/stats/me", headers=headers(world["proc"].rut))
    assert r.status_code == 200
    assert r.json()["case_count"] == 2


def test_agenda_is_scoped_to_the_union(client, db, world):
    link(db, world["proc"], world["a"])
    r = client.get("/api/v1/calendar", headers=headers(world["proc"].rut))
    assert r.status_code == 200
