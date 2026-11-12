"""Tests for the short-lived per-account case-scope cache (odd/tasks/case-scope-cache.md).

Security first (S3): the scope decides which cases a person sees, so these
tests pin that two accounts never share an entry and cached values are
immutable.
"""

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from app.api.deps import ALL_CASES, get_current_lawyer, resolve_case_scope
from app.config import settings
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services import lawyer_roster
from app.services.lawyer_roster import case_ids_for_abogado

SANDY_RUT = "17171717-1"
CARLA_RUT = "16021492-9"


def _lawyer(db, rut, name, role="lawyer"):
    obj = Lawyer(rut=rut, name=name, role=role)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def sandy(db):
    return _lawyer(db, SANDY_RUT, "Sandy")


@pytest.fixture
def carla(db):
    return _lawyer(db, CARLA_RUT, "Carla")


@pytest.fixture
def court(db):
    obj = Court(code="T1-CACHE", name="Juzgado Cache", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _case(db, owner, court, rol, litigante_rut=None):
    obj = Case(
        lawyer_id=owner.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        plaintiff="P",
        defendant="D",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    if litigante_rut:
        db.add(
            CaseLitigante(
                case_id=obj.id,
                participante="AB.DDO",
                rut=litigante_rut,
                persona_type="NATURAL",
                nombre="X",
                natural_key=f"{obj.id}-{litigante_rut}",
            )
        )
        db.commit()
    return obj


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(lawyer_roster, "_scope_clock", c)
    return c


@pytest.fixture
def spy(monkeypatch):
    """Count calls of the expensive firm-wide litigante load."""
    calls = {"n": 0}
    real = lawyer_roster._abogado_litigantes_by_case

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(lawyer_roster, "_abogado_litigantes_by_case", counting)
    return calls


def _ids(client, sub):
    async def _who():
        return {"sub": sub}

    app.dependency_overrides[get_current_lawyer] = _who
    try:
        resp = client.get("/api/v1/cases")
    finally:
        app.dependency_overrides.pop(get_current_lawyer, None)
    assert resp.status_code == 200
    return {item["id"] for item in resp.json()["items"]}


def test_same_lawyer_within_ttl_computes_once(db, sandy, carla, court, clock, spy):
    case = _case(db, carla, court, "C-5001-2025", SANDY_RUT)

    first = resolve_case_scope(db, {"sub": SANDY_RUT})
    second = resolve_case_scope(db, {"sub": SANDY_RUT})

    assert first == second == {case.id}
    assert spy["n"] == 1


def test_recomputes_after_ttl(db, sandy, carla, court, clock, spy):
    _case(db, carla, court, "C-5002-2025", SANDY_RUT)

    resolve_case_scope(db, {"sub": SANDY_RUT})
    clock.now += settings.CASE_SCOPE_CACHE_SECONDS + 1
    resolve_case_scope(db, {"sub": SANDY_RUT})

    assert spy["n"] == 2


def test_different_lawyers_never_share(client, db, sandy, carla, court, clock, spy):
    sandy_case = _case(db, carla, court, "C-5003-2025", SANDY_RUT)
    carla_case = _case(db, sandy, court, "C-5004-2025", CARLA_RUT)

    # Warm A's entry first, then ask as B: B must not see A's cases.
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {sandy_case.id}
    assert resolve_case_scope(db, {"sub": CARLA_RUT}) == {carla_case.id}
    assert _ids(client, SANDY_RUT) == {sandy_case.id}
    assert _ids(client, CARLA_RUT) == {carla_case.id}
    # Numeric-id subject resolves to the same account, not to another entry.
    assert resolve_case_scope(db, {"sub": str(carla.id)}) == {carla_case.id}


def test_cached_equals_uncached_result(db, sandy, carla, court, clock, monkeypatch):
    _case(db, carla, court, "C-5005-2025", SANDY_RUT)
    _case(db, sandy, court, "C-5006-2025")  # bootstrap-window case
    cached = resolve_case_scope(db, {"sub": SANDY_RUT})

    monkeypatch.setattr(settings, "CASE_SCOPE_CACHE_SECONDS", 0)
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == cached


def test_different_abogado_rut_is_a_different_entry(db, sandy, carla, court, clock, spy):
    sandy_case = _case(db, carla, court, "C-5007-2025", SANDY_RUT)
    carla_case = _case(db, sandy, court, "C-5008-2025", CARLA_RUT)

    assert case_ids_for_abogado(db, SANDY_RUT, SANDY_RUT) == {sandy_case.id}
    assert case_ids_for_abogado(db, SANDY_RUT, CARLA_RUT) == {carla_case.id}
    assert spy["n"] == 2
    # Repeats are served from cache, per (account, abogado) pair.
    assert case_ids_for_abogado(db, SANDY_RUT, CARLA_RUT) == {carla_case.id}
    assert spy["n"] == 2
    # Same abogado asked by another account is a separate entry.
    assert case_ids_for_abogado(db, CARLA_RUT, CARLA_RUT) == {carla_case.id}
    assert spy["n"] == 3


def test_zero_ttl_disables_cache(db, sandy, carla, court, clock, spy, monkeypatch):
    _case(db, carla, court, "C-5009-2025", SANDY_RUT)
    monkeypatch.setattr(settings, "CASE_SCOPE_CACHE_SECONDS", 0)

    resolve_case_scope(db, {"sub": SANDY_RUT})
    resolve_case_scope(db, {"sub": SANDY_RUT})

    assert spy["n"] == 2


def test_firm_wide_roles_unaffected(db, clock, spy):
    auditor = _lawyer(db, "77777777-7", "Auditor", role="auditor")
    admin = _lawyer(db, "88888888-8", "Admin", role="admin")

    for _ in range(2):
        assert resolve_case_scope(db, {"sub": auditor.rut}) is ALL_CASES
        assert resolve_case_scope(db, {"sub": admin.rut}) is ALL_CASES
    assert spy["n"] == 0


def test_mutating_returned_scope_does_not_change_cache(db, sandy, carla, court, clock):
    case = _case(db, carla, court, "C-5010-2025", SANDY_RUT)

    first = resolve_case_scope(db, {"sub": SANDY_RUT})
    assert isinstance(first, set)
    first.add(999999)
    first.discard(case.id)
    direct = case_ids_for_abogado(db, SANDY_RUT, SANDY_RUT)
    direct.clear()

    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {case.id}
    assert case_ids_for_abogado(db, SANDY_RUT, SANDY_RUT) == {case.id}


def test_exception_is_not_cached(db, sandy, clock):
    with pytest.raises(HTTPException):
        resolve_case_scope(db, {"sub": "00000000-0"})  # unknown RUT -> 404
    _lawyer(db, "00000000-0", "Late")
    assert resolve_case_scope(db, {"sub": "00000000-0"}) == set()


def test_cache_is_bounded_and_evicts_oldest(clock, monkeypatch):
    monkeypatch.setattr(lawyer_roster, "_SCOPE_CACHE_MAX_ENTRIES", 3)
    for i in range(5):
        lawyer_roster._scope_cache_put(("k", i), frozenset({i}))
    assert lawyer_roster._scope_cache_get(("k", 0)) is None
    assert lawyer_roster._scope_cache_get(("k", 1)) is None
    assert lawyer_roster._scope_cache_get(("k", 4)) == frozenset({4})


def test_reassignment_invalidates_cached_scope(db, sandy, carla, court, clock, spy):
    case = _case(db, sandy, court, "C-5011-2025")  # bootstrap-window, owned by sandy
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {case.id}

    case.assigned_lawyer_id = carla.id
    db.commit()

    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == set()
    assert resolve_case_scope(db, {"sub": CARLA_RUT}) == {case.id}


def test_new_case_and_new_litigante_invalidate_cached_scope(db, sandy, carla, court, clock):
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == set()
    first = _case(db, carla, court, "C-5012-2025", SANDY_RUT)
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {first.id}
    second = _case(db, carla, court, "C-5013-2025", SANDY_RUT)
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {first.id, second.id}


def test_role_change_invalidates_cached_scope(db, sandy, clock):
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == set()
    sandy.role = "admin"
    db.commit()
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) is ALL_CASES


def test_unrelated_case_update_keeps_cache(db, sandy, carla, court, clock, spy):
    case = _case(db, carla, court, "C-5014-2025", SANDY_RUT)
    resolve_case_scope(db, {"sub": SANDY_RUT})
    case.plaintiff = "Other"
    db.commit()
    resolve_case_scope(db, {"sub": SANDY_RUT})
    assert spy["n"] == 1


def test_patrocinio_pendiente_change_invalidates_cached_scope(db, sandy, carla, court, clock, spy):
    # The flag puts an incorporated case in its owner's scope; clearing it must not be served stale.
    case = _case(db, carla, court, "C-5015-2025", SANDY_RUT)
    resolve_case_scope(db, {"sub": SANDY_RUT})
    case.patrocinio_pendiente = True
    db.commit()
    resolve_case_scope(db, {"sub": SANDY_RUT})
    assert spy["n"] == 2


def test_rolled_back_change_is_not_served(db, sandy, carla, court, clock):
    case = _case(db, sandy, court, "C-5015-2025")
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {case.id}
    case.assigned_lawyer_id = carla.id
    db.flush()
    db.rollback()
    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {case.id}


def test_role_is_part_of_the_key(db, sandy, carla, court, clock):
    _case(db, carla, court, "C-5015-2025", SANDY_RUT)
    resolve_case_scope(db, {"sub": SANDY_RUT})

    # A Core update bypasses the invalidation events: only the key can separate roles.
    db.execute(update(Lawyer).where(Lawyer.id == sandy.id).values(role="viewer"))
    db.commit()
    db.expire_all()
    resolve_case_scope(db, {"sub": SANDY_RUT})

    scope_keys = {k for k in lawyer_roster._scope_cache if k[0] == "scope"}
    assert scope_keys == {
        ("scope", sandy.id, SANDY_RUT, "lawyer"),
        ("scope", sandy.id, SANDY_RUT, "viewer"),
    }


def test_computation_error_is_not_cached(db, sandy, carla, court, clock, monkeypatch):
    case = _case(db, carla, court, "C-5016-2025", SANDY_RUT)
    real = lawyer_roster._abogado_litigantes_by_case

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(lawyer_roster, "_abogado_litigantes_by_case", boom)
    with pytest.raises(RuntimeError):
        resolve_case_scope(db, {"sub": SANDY_RUT})
    monkeypatch.setattr(lawyer_roster, "_abogado_litigantes_by_case", real)

    assert resolve_case_scope(db, {"sub": SANDY_RUT}) == {case.id}
