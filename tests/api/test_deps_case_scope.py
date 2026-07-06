"""Tests for litigante-based ``resolve_case_scope`` / ``apply_case_scope``.

Approach C: Case.lawyer_id is the firm's single bookkeeping owner, so
non-auditor visibility can no longer be resolved via ``Case.lawyer_id ==
scope``. Non-auditor roles now resolve to the set of case ids where they are
an abogado-of-record litigante (``case_ids_for_abogado``). Auditor/admin
retain firm-wide ``ALL_CASES`` visibility, unaffected by this change.

Load-bearing: without this, PR1a's firm-wide attribution fix is inert — a
lawyer would still see zero cases for anything not owned via their own
``Case.lawyer_id``.
"""

import pytest
from datetime import datetime

from app.api.deps import resolve_case_scope, apply_case_scope, ALL_CASES, get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer

SANDY_RUT = "17171717-1"
CARLA_RUT = "16021492-9"


@pytest.fixture
def sandy(db):
    obj = Lawyer(rut=SANDY_RUT, name="Sandy", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def carla(db):
    obj = Lawyer(rut=CARLA_RUT, name="Carla", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut="77777777-7", name="Auditor", role="auditor")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-SCOPE", name="Juzgado Scope", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, owner, court, rol):
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
    return obj


def _add_litigante(db, case, rut, nombre, participante="AB.DDO"):
    lit = CaseLitigante(
        case_id=case.id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case.id}-{rut}",
    )
    db.add(lit)
    db.commit()


class TestResolveCaseScope:
    def test_resolve_case_scope_non_auditor_returns_litigante_case_ids(self, db, sandy, carla, court):
        """Case synced under Carla's account (firm id), Sandy is AB.DDO."""
        case = _make_case(db, carla, court, "C-4001-2025")
        _add_litigante(db, case, SANDY_RUT, "Sandy")

        scope = resolve_case_scope(db, {"sub": SANDY_RUT})

        assert scope is not ALL_CASES
        assert case.id in scope

    def test_apply_case_scope_filters_by_id_set(self, db, sandy, carla, court):
        case_visible = _make_case(db, carla, court, "C-4002-2025")
        case_hidden = _make_case(db, carla, court, "C-4003-2025")
        _add_litigante(db, case_visible, SANDY_RUT, "Sandy")
        _add_litigante(db, case_hidden, CARLA_RUT, "Carla")

        scope = resolve_case_scope(db, {"sub": SANDY_RUT})
        query = apply_case_scope(db.query(Case), scope)
        ids = {c.id for c in query.all()}

        assert case_visible.id in ids
        assert case_hidden.id not in ids

    def test_resolve_case_scope_auditor_still_all_cases(self, db, auditor):
        scope = resolve_case_scope(db, {"sub": auditor.rut})
        assert scope is ALL_CASES


class TestListCasesEndpointFirmWideAttribution:
    def test_list_cases_endpoint_shows_sandys_case_synced_under_firm_id(
        self, client, db, sandy, carla, court
    ):
        """Integration: Sandy sees a case synced by Carla's account where
        Sandy is the abogado-of-record, via GET /api/v1/cases."""
        case = _make_case(db, carla, court, "C-4004-2025")
        _add_litigante(db, case, SANDY_RUT, "Sandy")

        async def _mock_get_current_lawyer():
            return {"sub": SANDY_RUT}

        app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
        try:
            resp = client.get("/api/v1/cases")
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert case.id in ids


class TestListCasesEndpointBootstrapWindowFallback:
    """``_bootstrap_owned_case_ids`` is the load-bearing safety net for a
    just-synced case that has no ``CaseLitigante`` rows yet (see deps.py
    docstring). These tests exercise it through the real endpoint rather
    than by calling the helper directly, since the endpoint is what a
    regression would actually break.
    """

    def test_list_cases_endpoint_shows_own_case_with_zero_litigante_rows(
        self, client, db, sandy, court
    ):
        """Sandy owns a case (Case.lawyer_id == sandy.id) that has NO
        CaseLitigante rows at all yet — the bootstrap window. It must still
        appear in her GET /api/v1/cases."""
        case = _make_case(db, sandy, court, "C-4005-2025")

        async def _mock_get_current_lawyer():
            return {"sub": SANDY_RUT}

        app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
        try:
            resp = client.get("/api/v1/cases")
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert case.id in ids

    def test_list_cases_endpoint_hides_own_case_with_litigantes_when_not_abogado(
        self, client, db, sandy, carla, court
    ):
        """Sandy owns a case (Case.lawyer_id == sandy.id) that DOES have
        litigante rows, but Sandy is not among them — she is not an
        abogado-of-record. Once a case has litigantes, ownership via
        Case.lawyer_id alone must NOT grant visibility (the security rule
        the bootstrap fallback must not undermine)."""
        case = _make_case(db, sandy, court, "C-4006-2025")
        _add_litigante(db, case, CARLA_RUT, "Carla")

        async def _mock_get_current_lawyer():
            return {"sub": SANDY_RUT}

        app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
        try:
            resp = client.get("/api/v1/cases")
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert case.id not in ids
