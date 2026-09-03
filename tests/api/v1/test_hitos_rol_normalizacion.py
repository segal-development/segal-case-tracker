"""ROL normalization for hitos.

A hito's ``descripcion`` carries the causa ROL (``C-6147-2026``). A bare ROL
typed without the letter prefix (``6147-2026``) must be stored as ``C-6147-2026``
and must dedup against the prefixed form — otherwise the same causa can be paid
twice (found in the August 2026 audit: hito 457 was stored as ``6147-2026``).
"""

import pytest

from app.api.v1.hitos import _causa_key, _normalize_rol_text


class TestNormalizeRolText:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("6147-2026", "C-6147-2026"),        # bare civil ROL → prefixed
            ("  6147-2026 ", "C-6147-2026"),     # whitespace trimmed first
            ("C-6147-2026", "C-6147-2026"),      # already prefixed → untouched
            ("c-6147-2026", "C-6147-2026"),      # prefix upper-cased
            ("V-123-2025", "V-123-2025"),        # other letter prefixes are kept as-is
            ("Causa C-6147-2026 dilatoria", "Causa C-6147-2026 dilatoria"),  # free text untouched
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_prefixes_only_a_bare_rol(self, raw, expected):
        assert _normalize_rol_text(raw) == expected


class TestCausaKeyToleratesBareRol:
    def test_bare_and_prefixed_rol_share_the_dedup_key(self):
        assert _causa_key("6147-2026") == _causa_key("C-6147-2026") == "C-6147-2026"

    def test_bare_rol_with_whitespace(self):
        assert _causa_key("  6147-2026 ") == "C-6147-2026"

    def test_prefixed_rol_embedded_in_text_still_wins(self):
        assert _causa_key("dilatoria causa c-9960-2026") == "C-9960-2026"

    def test_free_text_without_rol_is_unchanged_behaviour(self):
        assert _causa_key("texto libre sin rol") == "TEXTO LIBRE SIN ROL"
        assert _causa_key(None) is None
        assert _causa_key("") is None


# ---------------------------------------------------------------------------
# Endpoint behaviour: stored form + dedup across bare/prefixed spellings
# ---------------------------------------------------------------------------

from app.core.security import create_access_token  # noqa: E402
from app.models.hito import Hito, HitoTipo  # noqa: E402
from app.models.lawyer import Lawyer  # noqa: E402

ADMIN_RUT = "16021492-9"
CLIENT_RUT = "12.302.937-2"
TRIBUNAL = "1º Juzgado Civil de Santiago"


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def tipo(db):
    t = HitoTipo(
        code="dilatoria", label="Excepción dilatoria acogida", nivel="basico",
        valor_bruto=808, etapa_tramite="EXCEPCIONES", orden=1,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _post(client, tipo_id, descripcion):
    return client.post(
        "/api/v1/hitos", headers=_h(ADMIN_RUT),
        data={
            "hito_tipo_id": tipo_id, "fecha_hito": "2026-08-04",
            "rol_causa": CLIENT_RUT, "descripcion": descripcion, "tribunal": TRIBUNAL,
        },
        files={"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")},
    )


class TestCreateNormalizesRol:
    def test_bare_rol_is_stored_with_prefix(self, client, db, admin, tipo):
        r = _post(client, tipo.id, "6147-2026")
        assert r.status_code in (200, 201), r.text
        assert db.query(Hito).one().descripcion == "C-6147-2026"

    def test_bare_then_prefixed_is_a_duplicate(self, client, db, admin, tipo):
        assert _post(client, tipo.id, "6147-2026").status_code in (200, 201)
        r = _post(client, tipo.id, "C-6147-2026")
        assert r.status_code == 409, r.text
        assert db.query(Hito).count() == 1

    def test_prefixed_then_bare_is_a_duplicate(self, client, db, admin, tipo):
        assert _post(client, tipo.id, "C-6147-2026").status_code in (200, 201)
        r = _post(client, tipo.id, "6147-2026")
        assert r.status_code == 409, r.text
        assert db.query(Hito).count() == 1

    def test_different_causa_same_client_is_allowed(self, client, db, admin, tipo):
        assert _post(client, tipo.id, "6147-2026").status_code in (200, 201)
        assert _post(client, tipo.id, "6924-2026").status_code in (200, 201)
        assert sorted(h.descripcion for h in db.query(Hito).all()) == ["C-6147-2026", "C-6924-2026"]
