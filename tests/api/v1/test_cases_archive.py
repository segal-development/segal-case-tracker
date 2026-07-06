"""Tests for DELETE /{case_id} (archive_case) authorization.

Approach C makes Case.lawyer_id the firm's single bookkeeping owner, so it
no longer distinguishes which lawyer may archive a case. Authorization is
litigante-based instead: the acting lawyer may archive a case iff they are
an abogado-of-record (AB./AP.) litigante on it. Auditor/admin retain
firm-wide access unaffected by this change. Non-litigante lawyers get 404
(not 403) to avoid leaking case existence, matching the endpoint's existing
shape.
"""

import pytest
from datetime import datetime, timedelta

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer

SANDY_RUT = "17171717-1"
CARLA_RUT = "16021492-9"
PEDRO_RUT = "18181818-1"
AUDITOR_RUT = "77777777-7"
ADMIN_RUT = "19191919-1"


@pytest.fixture
def sandy(db):
    obj = Lawyer(rut=SANDY_RUT, name="Sandy", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def carla(db):
    obj = Lawyer(rut=CARLA_RUT, name="Carla", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def pedro(db):
    obj = Lawyer(rut=PEDRO_RUT, name="Pedro", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Admin", role="admin")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-ARCH", name="Juzgado Archive", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def case(db, carla, court):
    """Case owned (Case.lawyer_id) by the firm account, Sandy is AB.DDO."""
    obj = Case(
        lawyer_id=carla.id,
        court_id=court.id,
        rol="C-4501-2025",
        status="active",
        competencia="civil",
        plaintiff="P",
        defendant="D",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    db.add(CaseLitigante(
        case_id=obj.id,
        participante="AB.DDO",
        rut=SANDY_RUT,
        persona_type="NATURAL",
        nombre="Sandy",
        natural_key=f"{obj.id}-sandy",
    ))
    db.commit()
    return obj


def _headers(rut: str) -> dict:
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


class TestArchiveCaseAuthz:
    def test_litigante_abogado_can_archive_case(self, client, case, sandy):
        resp = client.delete(f"/api/v1/cases/{case.id}", headers=_headers(SANDY_RUT))
        assert resp.status_code == 204

    def test_non_litigante_lawyer_gets_404_not_403(self, client, case, pedro):
        resp = client.delete(f"/api/v1/cases/{case.id}", headers=_headers(PEDRO_RUT))
        assert resp.status_code == 404

    def test_auditor_can_archive_any_case(self, client, case, auditor):
        resp = client.delete(f"/api/v1/cases/{case.id}", headers=_headers(AUDITOR_RUT))
        assert resp.status_code == 204

    def test_admin_can_archive_any_case(self, client, case, admin):
        resp = client.delete(f"/api/v1/cases/{case.id}", headers=_headers(ADMIN_RUT))
        assert resp.status_code == 204
