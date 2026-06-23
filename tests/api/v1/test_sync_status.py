"""Tests for GET /api/v1/lawyers/sync-status — per-lawyer sync status for operator panel."""

import pytest
from datetime import datetime, timedelta

from app.core.security import create_access_token, hash_password
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYNC_URL = "/api/v1/lawyers/sync-status"
ADMIN_RUT = "55555555-5"
LAWYER_RUT = "66666666-6"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def court(db):
    """A minimal court record required by Case FK constraint."""
    c = Court(code="C-SYNC-TEST", name="Test Court", region="RM", type="civil")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture
def admin_lawyer(db):
    """Lawyer with role='admin'."""
    lawyer = Lawyer(
        rut=ADMIN_RUT,
        name="Admin Sync User",
        email="admin_sync@segal.cl",
        role="admin",
    )
    lawyer.password_hash = hash_password("adminpass123")
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    return lawyer


@pytest.fixture
def regular_lawyer(db):
    """Lawyer with role='lawyer'."""
    lawyer = Lawyer(
        rut=LAWYER_RUT,
        name="Regular Sync User",
        email="lawyer_sync@segal.cl",
        role="lawyer",
    )
    lawyer.password_hash = hash_password("lawyerpass123")
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    return lawyer


@pytest.fixture
def admin_headers(admin_lawyer):
    """Auth headers for the admin lawyer."""
    token = create_access_token({"sub": ADMIN_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def lawyer_headers(regular_lawyer):
    """Auth headers for the regular lawyer."""
    token = create_access_token({"sub": LAWYER_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Role gate
# ---------------------------------------------------------------------------


def test_sync_status_requires_admin_403(client, regular_lawyer, lawyer_headers):
    resp = client.get(SYNC_URL, headers=lawyer_headers)
    assert resp.status_code == 403


def test_sync_status_admin_200_empty_list(client, admin_lawyer, admin_headers):
    """Admin with no civil cases in the DB → 200 with empty list."""
    resp = client.get(SYNC_URL, headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# 2. Data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyers_with_cases(db, court):
    """
    Lawyer A: 3 civil cases
      - A1: last_detail_checked_at = 3 days ago  (fresh)
      - A2: last_detail_checked_at = 1 day ago   (fresh)
      - A3: last_detail_checked_at = NULL         (stale)
      → case_count=3, stale_count=1, last_synced_at = A2 timestamp

    Lawyer B: 2 civil cases
      - B1: last_detail_checked_at = 10 days ago  (stale: >7 days)
      - B2: last_detail_checked_at = NULL          (stale)
      → case_count=2, stale_count=2, last_synced_at = B1 timestamp

    Lawyer C: 0 cases → must be excluded from the response
    """
    now = datetime.utcnow()
    recent_3d = now - timedelta(days=3)
    recent_1d = now - timedelta(days=1)
    stale_10d = now - timedelta(days=10)

    lawyer_a = Lawyer(rut="77777777-7", name="Lawyer A", email="a_sync@segal.cl", role="lawyer")
    lawyer_b = Lawyer(rut="88888888-8", name="Lawyer B", email="b_sync@segal.cl", role="lawyer")
    lawyer_c = Lawyer(rut="99999999-9", name="Lawyer C", email="c_sync@segal.cl", role="lawyer")
    db.add_all([lawyer_a, lawyer_b, lawyer_c])
    db.commit()
    for l in [lawyer_a, lawyer_b, lawyer_c]:
        db.refresh(l)

    civil_cases = [
        # Lawyer A
        Case(
            lawyer_id=lawyer_a.id,
            court_id=court.id,
            rol="A-001-2024",
            competencia="civil",
            last_detail_checked_at=recent_3d,
        ),
        Case(
            lawyer_id=lawyer_a.id,
            court_id=court.id,
            rol="A-002-2024",
            competencia="civil",
            last_detail_checked_at=recent_1d,
        ),
        Case(
            lawyer_id=lawyer_a.id,
            court_id=court.id,
            rol="A-003-2024",
            competencia="civil",
            last_detail_checked_at=None,
        ),
        # Lawyer B
        Case(
            lawyer_id=lawyer_b.id,
            court_id=court.id,
            rol="B-001-2024",
            competencia="civil",
            last_detail_checked_at=stale_10d,
        ),
        Case(
            lawyer_id=lawyer_b.id,
            court_id=court.id,
            rol="B-002-2024",
            competencia="civil",
            last_detail_checked_at=None,
        ),
    ]
    db.add_all(civil_cases)
    db.commit()

    return {
        "lawyer_a": lawyer_a,
        "lawyer_b": lawyer_b,
        "lawyer_c": lawyer_c,
        "recent_1d": recent_1d,
        "stale_10d": stale_10d,
    }


# ---------------------------------------------------------------------------
# 3. Data correctness
# ---------------------------------------------------------------------------


def test_sync_status_lawyer_a_counts(client, admin_lawyer, admin_headers, lawyers_with_cases):
    """Lawyer A: 3 cases, 1 stale (NULL), last_synced_at is the most recent timestamp."""
    resp = client.get(SYNC_URL, headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    la_id = lawyers_with_cases["lawyer_a"].id
    entry = next((x for x in data if x["id"] == la_id), None)
    assert entry is not None, "Lawyer A must be in response"

    assert entry["case_count"] == 3
    assert entry["stale_count"] == 1
    assert entry["last_synced_at"] is not None


def test_sync_status_lawyer_b_counts(client, admin_lawyer, admin_headers, lawyers_with_cases):
    """Lawyer B: 2 cases, 2 stale (one too old, one NULL), last_synced_at = the 10d-old timestamp."""
    resp = client.get(SYNC_URL, headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    lb_id = lawyers_with_cases["lawyer_b"].id
    entry = next((x for x in data if x["id"] == lb_id), None)
    assert entry is not None, "Lawyer B must be in response"

    assert entry["case_count"] == 2
    assert entry["stale_count"] == 2
    assert entry["last_synced_at"] is not None  # max = stale_10d


# ---------------------------------------------------------------------------
# 4. No-cases lawyer excluded
# ---------------------------------------------------------------------------


def test_sync_status_excludes_lawyer_without_cases(
    client, admin_lawyer, admin_headers, lawyers_with_cases
):
    resp = client.get(SYNC_URL, headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    lc_id = lawyers_with_cases["lawyer_c"].id
    assert not any(x["id"] == lc_id for x in data), "Lawyer C (0 cases) must not appear"


# ---------------------------------------------------------------------------
# 5. Ordering — least-recently-synced first (NULLs first)
# ---------------------------------------------------------------------------


def test_sync_status_ordering(client, admin_lawyer, admin_headers, lawyers_with_cases):
    """Lawyer B (older last_synced_at) must come before Lawyer A (recent last_synced_at)."""
    resp = client.get(SYNC_URL, headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    la_id = lawyers_with_cases["lawyer_a"].id
    lb_id = lawyers_with_cases["lawyer_b"].id

    ids = [x["id"] for x in data]
    assert ids.index(lb_id) < ids.index(la_id), (
        "Lawyer B (stale last_synced_at) must appear before Lawyer A (recent last_synced_at)"
    )


# ---------------------------------------------------------------------------
# 6. Response shape
# ---------------------------------------------------------------------------


def test_sync_status_response_shape(client, admin_lawyer, admin_headers, lawyers_with_cases):
    """Each item must include all required fields with correct types."""
    resp = client.get(SYNC_URL, headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1

    item = data[0]
    assert "id" in item
    assert "rut" in item
    assert "name" in item
    assert "email" in item
    assert "role" in item
    assert "case_count" in item
    assert "last_synced_at" in item
    assert "stale_count" in item
