"""Preview step for the hitos Excel import (``POST /hitos/importar/preview``).

Real incident (2026-09): a free-text column ("PODER ACREDITADO EXH DOC.") was
stored as the causa ROL — part of the dedup key (abogado, RUT cliente, ROL,
tribunal) — so a garbage causa found no collisions and silently re-created 64
duplicate hitos across two lawyers, 35 already approved. The import report said
"40 creadas, 0 duplicadas" and looked perfectly healthy.

The preview must let an admin see how every row will be interpreted, and what
looks new vs duplicate, BEFORE anything is written — and it must be IMPOSSIBLE
for it to disagree with the real import, since both are built on the same
``_run_hito_import`` helper (see ``app/api/v1/hitos.py``).
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
LAWYER_NAME = "Eduardo Andrés Venegas Prado"
CLIENT_RUT = "16.086.088-k"
TRIBUNAL = "26º Juzgado Civil de Santiago"
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
    obj = Lawyer(rut=LAWYER_RUT, name=LAWYER_NAME, role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def tipo(db):
    t = HitoTipo(code="pleno_excepcion_dilatoria", label="Excepción dilatoria acogida",
                 nivel="pleno", valor_bruto=808, orden=1)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _build_wb(rows, headers=("ABOGADO", "FECHA", "RUT", "ROL", "TIPO DE HITO",
                              "TRIBUNAL", "DESCRIPCION", "APROBADO"), sheet_title="HITOS"):
    """A new-format hitos sheet: ABOGADO/FECHA/RUT/ROL/TIPO/TRIBUNAL/DESCRIPCION/APROBADO."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _preview(client, data, admin_rut=ADMIN_RUT, hoja=None):
    url = "/api/v1/hitos/importar/preview" + (f"?hoja={hoja}" if hoja else "")
    return client.post(url, headers=_h(admin_rut), files={"archivo": ("h.xlsx", data, _XLSX_MIME)})


def _import(client, data, admin_rut=ADMIN_RUT, hoja=None):
    url = "/api/v1/hitos/importar" + (f"?hoja={hoja}" if hoja else "")
    return client.post(url, headers=_h(admin_rut), files={"archivo": ("h.xlsx", data, _XLSX_MIME)})


class TestPreviewWritesNothing:
    def test_preview_does_not_create_hitos(self, client, db, admin, lawyer, tipo):
        data = _build_wb([
            ("Eduardo Venegas", datetime(2026, 8, 12), CLIENT_RUT, "C-8818-2026",
             "dilatoria", TRIBUNAL, None, "SI"),
        ])
        before = db.query(Hito).count()
        r = _preview(client, data)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resumen"]["nuevas"] == 1
        assert db.query(Hito).count() == before == 0


class TestRolShapeWarning:
    def test_fires_on_free_text_causa(self, client, db, admin, lawyer, tipo):
        data = _build_wb([
            ("Eduardo Venegas", datetime(2026, 9, 4), CLIENT_RUT, "PODER ACREDITADO EXH DOC.",
             "dilatoria", TRIBUNAL, None, "SI"),
        ])
        r = _preview(client, data)
        assert r.status_code == 200, r.text
        fila = r.json()["filas"][0]
        assert any("no tiene forma de ROL" in a for a in fila["advertencias"]), fila

    def test_does_not_fire_on_a_real_rol(self, client, db, admin, lawyer, tipo):
        data = _build_wb([
            ("Eduardo Venegas", datetime(2026, 9, 4), CLIENT_RUT, "C-8818-2026",
             "dilatoria", TRIBUNAL, None, "SI"),
        ])
        r = _preview(client, data)
        fila = r.json()["filas"][0]
        assert fila["advertencias"] == []


class TestRolDescripcionMismatchWarning:
    def test_general_warning_when_rol_and_descripcion_disagree(self, client, db, admin, lawyer, tipo):
        data = _build_wb([
            ("Eduardo Venegas", datetime(2026, 9, 4), CLIENT_RUT, "C-11449-2026", "dilatoria",
             TRIBUNAL, "PODER ACREDITADO EXH DOC.", "SI"),
        ])
        r = _preview(client, data)
        assert r.status_code == 200, r.text
        generales = r.json()["resumen"]["advertencias_generales"]
        assert generales, r.json()["resumen"]
        assert "ROL" in generales[0] and "DESCRIPCION" in generales[0]

    def test_no_general_warning_when_only_rol_is_present(self, client, db, admin, lawyer, tipo):
        data = _build_wb([
            ("Eduardo Venegas", datetime(2026, 9, 4), CLIENT_RUT, "C-8818-2026", "dilatoria",
             TRIBUNAL, None, "SI"),
        ])
        r = _preview(client, data)
        assert r.json()["resumen"]["advertencias_generales"] == []


class TestDuplicateRows:
    def test_duplicate_is_reported_and_not_created(self, client, db, admin, lawyer, tipo):
        db.add(Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=808,
                     fecha_hito=date(2026, 8, 1), rol_causa=CLIENT_RUT,
                     descripcion="C-6924-2026", tribunal=TRIBUNAL))
        db.commit()
        data = _build_wb([
            ("Eduardo Venegas", datetime(2026, 8, 12), CLIENT_RUT, "C-6924-2026",
             "dilatoria", TRIBUNAL, None, "SI"),
        ])
        r = _preview(client, data)
        body = r.json()
        fila = body["filas"][0]
        assert fila["resultado"] == "duplicada"
        assert fila["motivo"]
        assert body["resumen"]["duplicadas"] == 1
        assert body["resumen"]["nuevas"] == 0
        assert db.query(Hito).count() == 1  # nothing new was written


class TestSospechosoGuard:
    def test_fires_on_the_incident_shape(self, client, db, admin, lawyer, tipo):
        # Lawyer already has 5 hitos in 2026-09.
        for i in range(5):
            db.add(Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=808,
                         fecha_hito=date(2026, 9, 1), rol_causa=CLIENT_RUT,
                         descripcion=f"C-{9000 + i}-2026", tribunal=TRIBUNAL))
        db.commit()

        rows = [
            ("Eduardo Venegas", datetime(2026, 9, 10), CLIENT_RUT, f"C-{9100 + i}-2026",
             "dilatoria", TRIBUNAL, None, "SI")
            for i in range(11)
        ]
        r = _preview(client, _build_wb(rows))
        body = r.json()
        assert body["resumen"]["nuevas"] == 11
        assert body["resumen"]["duplicadas"] == 0
        assert body["resumen"]["sospechoso"] is True
        assert body["resumen"]["motivo_sospecha"]
        por = next(p for p in body["por_abogado"] if p["abogado"] == LAWYER_NAME)
        assert por["ya_tiene_en_el_mes"] == 5

    def test_stays_false_on_a_normal_small_import(self, client, db, admin, lawyer, tipo):
        rows = [
            ("Eduardo Venegas", datetime(2026, 9, 10), CLIENT_RUT, f"C-{9200 + i}-2026",
             "dilatoria", TRIBUNAL, None, "SI")
            for i in range(3)
        ]
        r = _preview(client, _build_wb(rows))
        body = r.json()
        assert body["resumen"]["sospechoso"] is False
        assert body["resumen"]["motivo_sospecha"] is None


class TestErrorRows:
    def test_unmatched_lawyer_unknown_tipo_and_bad_date_are_errors_with_motivo(
        self, client, db, admin, lawyer, tipo,
    ):
        rows = [
            ("Nadie Desconocido", datetime(2026, 9, 4), CLIENT_RUT, "C-1-2026",
             "dilatoria", TRIBUNAL, None, "SI"),
            ("Eduardo Venegas", datetime(2026, 9, 4), CLIENT_RUT, "C-2-2026",
             "un tipo que no existe en el catálogo", TRIBUNAL, None, "SI"),
            ("Eduardo Venegas", "no-es-una-fecha", CLIENT_RUT, "C-3-2026",
             "dilatoria", TRIBUNAL, None, "SI"),
        ]
        r = _preview(client, _build_wb(rows))
        body = r.json()
        assert body["resumen"]["errores"] == 3
        for fila in body["filas"]:
            assert fila["resultado"] == "error"
            assert fila["motivo"]


class TestRoleGuard:
    def test_requires_admin(self, client, db, lawyer):
        data = _build_wb([
            ("Eduardo Venegas", datetime(2026, 9, 4), CLIENT_RUT, "C-1-2026",
             "dilatoria", TRIBUNAL, None, "SI"),
        ])
        r = client.post("/api/v1/hitos/importar/preview", headers=_h(LAWYER_RUT),
                         files={"archivo": ("h.xlsx", data, _XLSX_MIME)})
        assert r.status_code == 403


class TestFilasTruncadas:
    def test_caps_at_500_rows(self, client, db, admin, lawyer, tipo):
        rows = [
            ("Eduardo Venegas", datetime(2026, 9, 4), CLIENT_RUT, f"C-{20000 + i}-2026",
             "dilatoria", TRIBUNAL, None, "SI")
            for i in range(501)
        ]
        r = _preview(client, _build_wb(rows))
        body = r.json()
        assert body["filas_truncadas"] is True
        assert len(body["filas"]) == 500
        assert body["resumen"]["total_leidas"] == 501
        assert body["resumen"]["nuevas"] == 501


class TestPreviewNeverDrifts:
    """The guarantee: preview and the real import must always agree."""

    def test_preview_and_import_report_the_same_counts(self, client, db, admin, lawyer, tipo):
        db.add(Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=808,
                     fecha_hito=date(2026, 8, 1), rol_causa=CLIENT_RUT,
                     descripcion="C-500-2026", tribunal=TRIBUNAL))
        db.commit()
        rows = [
            ("Eduardo Venegas", datetime(2026, 8, 12), CLIENT_RUT, "C-500-2026",
             "dilatoria", TRIBUNAL, None, "SI"),          # duplicada
            ("Eduardo Venegas", datetime(2026, 8, 13), CLIENT_RUT, "C-501-2026",
             "dilatoria", TRIBUNAL, None, "SI"),          # nueva
            ("Nadie Desconocido", datetime(2026, 8, 14), CLIENT_RUT, "C-502-2026",
             "dilatoria", TRIBUNAL, None, "SI"),          # error (abogado)
        ]
        data = _build_wb(rows)

        preview = _preview(client, data)
        assert preview.status_code == 200, preview.text
        imported = _import(client, data)
        assert imported.status_code == 200, imported.text

        preview_resumen = preview.json()["resumen"]
        import_resumen = imported.json()
        assert preview_resumen["nuevas"] == import_resumen["creadas"] == 1
        assert preview_resumen["duplicadas"] == import_resumen["omitidas_duplicadas"] == 1
        assert preview_resumen["errores"] == import_resumen["errores"] == 1
