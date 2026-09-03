"""Excel import: TRIBUNAL column + tribunal-aware dedup.

Hito identity is (abogado, RUT cliente, ROL, tribunal) — the same ROL in two
courts is two causas (migration 048). Until now the importer ignored the
tribunal: imported hitos were stored without one and a same-ROL-other-court
row was dropped as a duplicate. These tests pin the new behaviour, including
the tolerant rule for legacy rows stored without a tribunal.
"""

import io
from datetime import date, datetime

import openpyxl
import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo
from app.models.lawyer import Lawyer

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19813311-6"
CLIENT_RUT = "16.086.088-k"
ROL = "C-6924-2026"
TRIB_A = "26º Juzgado Civil de Santiago"
TRIB_B = "4º Juzgado Civil de Santiago"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Eduardo Andrés Venegas Prado", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def tipo(db):
    t = HitoTipo(code="pleno_excepcion_dilatoria", label="Excepción dilatoria acogida", nivel="pleno",
                 valor_bruto=808, etapa_tramite="EXCEPCIONES", orden=1)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _import(client, rows):
    """Build a NEW-format sheet (header-based) with a TRIBUNAL column and import it.
    Each row: (tribunal, rol) — same lawyer, same client RUT, same tipo."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HITOS"
    ws.append(["ABOGADO", "FECHA", "RUT", "ROL", "TIPO DE HITO", "TRIBUNAL", "APROBADO"])
    for tribunal, rol in rows:
        ws.append(["Eduardo Venegas", datetime(2026, 8, 12), CLIENT_RUT, rol, "dilatoria", tribunal, "SI"])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/v1/hitos/importar", headers=_h(ADMIN_RUT),
                    files={"archivo": ("h.xlsx", buf.getvalue(), _XLSX_MIME)})
    assert r.status_code == 200, r.text
    return r.json()


def _seed(db, lawyer, tipo, tribunal):
    """A pre-existing hito for the same (abogado, RUT, ROL), with or without tribunal."""
    db.add(Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=808, fecha_hito=date(2026, 8, 1),
                rol_causa=CLIENT_RUT, descripcion=ROL, tribunal=tribunal))
    db.commit()


class TestImportReadsTribunal:
    def test_tribunal_is_stored_on_the_imported_hito(self, client, db, admin, lawyer, tipo):
        r = _import(client, [(TRIB_A, ROL)])
        assert r["creadas"] == 1
        assert db.query(Hito).one().tribunal == TRIB_A

    def test_blank_tribunal_is_stored_as_null(self, client, db, admin, lawyer, tipo):
        _import(client, [(None, ROL)])
        assert db.query(Hito).one().tribunal is None


class TestImportDedupUsesTribunal:
    def test_same_rol_in_two_courts_keeps_both(self, client, db, admin, lawyer, tipo):
        r = _import(client, [(TRIB_A, ROL), (TRIB_B, ROL)])
        assert r["creadas"] == 2 and r["omitidas_duplicadas"] == 0
        assert {h.tribunal for h in db.query(Hito).all()} == {TRIB_A, TRIB_B}

    def test_same_rol_same_court_is_a_duplicate(self, client, db, admin, lawyer, tipo):
        r = _import(client, [(TRIB_A, ROL), (TRIB_A, ROL)])
        assert r["creadas"] == 1 and r["omitidas_duplicadas"] == 1

    def test_tribunal_match_is_case_and_space_insensitive(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, "26º JUZGADO CIVIL DE SANTIAGO ")
        r = _import(client, [(TRIB_A, ROL)])
        assert r["creadas"] == 0 and r["omitidas_duplicadas"] == 1


class TestLegacyRowsWithoutTribunal:
    """Bulk import is where a duplicate multiplies into real money, so the importer
    is deliberately MORE tolerant than the strict create/edit rule: a hito stored
    without a tribunal is treated as the same causa whatever tribunal the sheet
    brings, and a sheet row without a tribunal matches an existing hito that has one."""

    def test_existing_hito_without_tribunal_blocks_reimport_with_tribunal(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=None)
        r = _import(client, [(TRIB_A, ROL)])
        assert r["creadas"] == 0 and r["omitidas_duplicadas"] == 1
        assert db.query(Hito).count() == 1

    def test_row_without_tribunal_matches_existing_hito_with_tribunal(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=TRIB_A)
        r = _import(client, [(None, ROL)])
        assert r["creadas"] == 0 and r["omitidas_duplicadas"] == 1

    def test_within_file_blank_then_tribunal_is_a_duplicate(self, client, db, admin, lawyer, tipo):
        r = _import(client, [(None, ROL), (TRIB_A, ROL)])
        assert r["creadas"] == 1 and r["omitidas_duplicadas"] == 1

    def test_within_file_tribunal_then_blank_is_a_duplicate(self, client, db, admin, lawyer, tipo):
        r = _import(client, [(TRIB_A, ROL), (None, ROL)])
        assert r["creadas"] == 1 and r["omitidas_duplicadas"] == 1

    def test_different_rol_is_never_a_duplicate(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=None)
        r = _import(client, [(TRIB_A, "C-6147-2026")])
        assert r["creadas"] == 1 and r["omitidas_duplicadas"] == 0
