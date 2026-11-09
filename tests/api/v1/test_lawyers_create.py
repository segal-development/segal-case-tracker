"""POST /api/v1/lawyers: an admin adds a person to the firm (a junior lawyer who must register hitos).

Until now lawyers only appeared when the PJUD scraper detected them. The new person has no
password, no hito link and no PJUD credential: those are separate admin actions that already exist.
"""
import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.lawyer import Lawyer

URL = "/api/v1/lawyers"
VALID_RUT = "12345678-5"          # valid check digit
OTHER_VALID_RUT = "11111111-1"    # valid check digit


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
def admin(db):
    return _lawyer(db, "16021492-9", "Carla Admin", role="admin")


def _body(**over):
    data = {"rut": "12.345.678-5", "name": "  Ana Pérez Soto  ", "email": "ana@segal.cl", "nivel": "junior"}
    data.update(over)
    return data


class TestAuthorization:
    @pytest.mark.parametrize("role", ["lawyer", "auditor"])
    def test_only_an_admin_can_create(self, client, db, role):
        other = _lawyer(db, "22222222-2", "Not admin", role=role)
        _login_as(other)

        r = client.post(URL, json=_body())

        assert r.status_code == 403
        assert db.query(Lawyer).filter(Lawyer.rut == VALID_RUT).first() is None

    def test_anonymous_is_rejected(self, client, db):
        assert client.post(URL, json=_body()).status_code in (401, 403)


class TestCreate:
    def test_creates_a_junior_firm_lawyer(self, client, db, admin):
        _login_as(admin)

        r = client.post(URL, json=_body())

        assert r.status_code == 201, r.text
        body = r.json()
        assert body["rut"] == VALID_RUT                      # normalized (no dots)
        assert body["name"] == "Ana Pérez Soto"              # trimmed
        assert body["email"] == "ana@segal.cl"
        assert body["nivel"] == "junior"
        assert body["role"] == "lawyer"
        assert body["has_password"] is False
        row = db.query(Lawyer).filter(Lawyer.rut == VALID_RUT).one()
        assert row.is_firm_lawyer is True and row.is_active is True
        assert row.password_hash is None and row.hito_form_token is None
        assert row.encrypted_pjud_password is None

    def test_email_is_optional_and_stored_lowercase(self, client, db, admin):
        _login_as(admin)

        r = client.post(URL, json=_body(email=None))
        assert r.status_code == 201 and r.json()["email"] is None

        r2 = client.post(URL, json=_body(rut=OTHER_VALID_RUT, email="  Bea@Segal.CL "))
        assert r2.json()["email"] == "bea@segal.cl"

    @pytest.mark.parametrize("nivel", ["junior", "pleno", None])
    def test_accepted_levels(self, client, db, admin, nivel):
        _login_as(admin)

        assert client.post(URL, json=_body(nivel=nivel)).status_code == 201


class TestValidation:
    @pytest.mark.parametrize("field,value", [
        ("rut", "12345678-9"),          # wrong check digit
        ("rut", "abc"),
        ("name", "   "),
        ("name", ""),
        ("nivel", "senior"),
        ("email", "not-an-email"),
    ])
    def test_invalid_input_is_422_and_saves_nothing(self, client, db, admin, field, value):
        _login_as(admin)

        r = client.post(URL, json=_body(**{field: value}))

        assert r.status_code == 422
        assert db.query(Lawyer).filter(Lawyer.rut == VALID_RUT).first() is None

    def test_name_longer_than_the_column_is_422(self, client, db, admin):
        _login_as(admin)

        assert client.post(URL, json=_body(name="x" * 256)).status_code == 422

    def test_existing_rut_is_409_naming_the_person(self, client, db, admin):
        _lawyer(db, VALID_RUT, "ANA PEREZ YA EXISTE")
        _login_as(admin)

        r = client.post(URL, json=_body())

        assert r.status_code == 409
        assert "ANA PEREZ YA EXISTE" in r.json()["detail"]

    def test_existing_email_is_409(self, client, db, admin):
        _lawyer(db, "33333333-3", "Otra", email="ana@segal.cl")
        _login_as(admin)

        r = client.post(URL, json=_body())

        assert r.status_code == 409
        assert db.query(Lawyer).filter(Lawyer.rut == VALID_RUT).first() is None


class TestAppearsWhereItShould:
    def test_shows_up_in_the_accounts_list_and_the_hito_lawyer_selector(self, client, db, admin):
        _login_as(admin)
        client.post(URL, json=_body())

        accounts = client.get("/api/v1/lawyers/accounts").json()
        assert any(a["rut"] == VALID_RUT and a["has_password"] is False for a in accounts)
        selector = client.get("/api/v1/hitos/abogados").json()
        assert any(a["nombre"] == "Ana Pérez Soto" for a in selector)


def test_accounts_list_says_who_is_a_firm_lawyer(client, db, admin):
    """The traspaso wizard lists only the firm's lawyers (not admins without cartera, auditors
    or procuradores): the accounts list must expose is_firm_lawyer."""
    _lawyer(db, "11111111-1", "Abogada Firma", is_firm_lawyer=True)
    _lawyer(db, "22222222-2", "Auditor Externo", role="auditor", is_firm_lawyer=False)
    _lawyer(db, "33333333-3", "Sin Marca")
    _login_as(admin)
    by_name = {a["name"]: a for a in client.get("/api/v1/lawyers/accounts").json()}
    assert by_name["Abogada Firma"]["is_firm_lawyer"] is True
    assert by_name["Auditor Externo"]["is_firm_lawyer"] is False
    assert by_name["Sin Marca"]["is_firm_lawyer"] is False
