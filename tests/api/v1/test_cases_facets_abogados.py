"""GET /cases/facets also lists the firm's lawyers, for the Causas 'Abogado' filter.

Only a viewer with firm-wide scope (admin or auditor) gets the list: it exists to
look at another lawyer's caseload, which a plain lawyer cannot do.
"""
import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.lawyer import Lawyer

URL = "/api/v1/cases/facets"


def _lawyer(db, rut, name, role="lawyer", **kw):
    obj = Lawyer(rut=rut, name=name, role=role, **kw)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _login_as(lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.pop(get_current_lawyer, None)


@pytest.fixture
def firm(db):
    return {
        "admin": _lawyer(db, "11111111-1", "Carla Admin", role="admin", is_firm_lawyer=True),
        "auditor": _lawyer(db, "12121212-1", "Aldo Auditor", role="auditor", is_firm_lawyer=False),
        "pablo": _lawyer(db, "19456852-5", "PABLO ISMAEL ACEVEDO LÓPEZ", is_firm_lawyer=True),
        "ana": _lawyer(db, "18248270-6", "ANA ABOGADA", is_firm_lawyer=True),
        "inactive": _lawyer(db, "13131313-1", "ZOE INACTIVA", is_firm_lawyer=True, is_active=False),
        "outsider": _lawyer(db, "14141414-1", "NO ES DEL ESTUDIO", is_firm_lawyer=False),
    }


def test_admin_gets_the_active_firm_lawyers_sorted_by_name(client, db, firm):
    _login_as(firm["admin"])

    body = client.get(URL).json()

    assert body["abogados"] == [
        {"rut": "18248270-6", "nombre": "ANA ABOGADA"},
        {"rut": "11111111-1", "nombre": "Carla Admin"},
        {"rut": "19456852-5", "nombre": "PABLO ISMAEL ACEVEDO LÓPEZ"},
    ]


def test_inactive_and_non_firm_lawyers_are_left_out(client, db, firm):
    _login_as(firm["admin"])

    nombres = [a["nombre"] for a in client.get(URL).json()["abogados"]]

    assert "ZOE INACTIVA" not in nombres
    assert "NO ES DEL ESTUDIO" not in nombres


def test_auditor_gets_the_list_too(client, db, firm):
    _login_as(firm["auditor"])

    assert len(client.get(URL).json()["abogados"]) == 3


def test_a_plain_lawyer_gets_no_list(client, db, firm):
    _login_as(firm["pablo"])

    body = client.get(URL).json()

    assert body["abogados"] == []
    assert {"tribunals", "materias"} <= set(body)


def test_tribunals_and_materias_are_unchanged(client, db, firm):
    _login_as(firm["admin"])

    body = client.get(URL).json()

    assert body["tribunals"] == [] and body["materias"] == []
