"""Parser CONTRACT tests — pin the exact field-level output of the civil detail
parser against the real (synthetic) HTML fixtures.

Unlike test_case_detail_sync.py (which pins ENTITY COUNTS), these assertions pin
the actual VALUES the parser extracts. If PJUD drifts its DOM (renames a column,
reorders cells, changes a label), a count can stay stable while a field silently
goes null/wrong — these tests catch that. Fixtures are synthetic (CAJA FICTICIA,
C-9999-2099), so pinning values leaks no real data.
"""

from pathlib import Path

import pytest

from app.scrapper.pjud.civil import CivilScraper

_FX = Path(__file__).parent.parent / "fixtures/pjud"
_RICH = _FX / "detail_civil_rich.html"
_SYNTHETIC = _FX / "detail_civil_synthetic.html"


def _parse(path: Path):
    return CivilScraper()._parse_case_detail_html(path.read_text(encoding="utf-8"), "tok")


def _mv_text(m) -> str:
    return (getattr(m, "tipo_tramite", "") or "") + " " + (getattr(m, "descripcion", "") or "")


def _has_doc(m) -> bool:
    return bool(getattr(m, "doc_token", None) or getattr(m, "documento_token", None))


@pytest.mark.skipif(not _RICH.exists(), reason="rich fixture missing")
class TestRichFixtureContract:
    def test_case_meta(self):
        c = _parse(_RICH).case
        assert c.rol == "C-9999-2099"
        assert c.tribunal == "1º Juzgado de Letras Ficticio"
        assert c.fecha_ingreso == "18/12/2015"
        assert c.caratulado.startswith("CAJA FICTICIA DE PREVISION")

    def test_movements_fields(self):
        mvs = _parse(_RICH).movements
        assert len(mvs) == 3
        first = mvs[0]
        assert first.folio == "40"
        assert first.fecha == "04/05/2026"
        assert "Resolución" in _mv_text(first)
        assert _has_doc(first), "movement must expose a document token from the fixture"

    def test_litigantes_participantes_in_order(self):
        lits = _parse(_RICH).litigantes
        assert [l.participante for l in lits] == [
            "DTE.", "AB.DTE", "DDO.", "AB.DDO", "AB.DDO", "AB.DDO",
        ]

    def test_litigante_demandante_fields(self):
        dte = next(l for l in _parse(_RICH).litigantes if l.participante == "DTE.")
        assert dte.rut == "76543210-K"
        assert dte.nombre.startswith("CAJA FICTICIA DE PREVISION")

    def test_litigante_abogado_demandante_fields(self):
        ab = next(l for l in _parse(_RICH).litigantes if l.participante == "AB.DTE")
        assert ab.rut == "13456789-0"
        assert ab.nombre.startswith("PEDRO PABLO MIRANDA")

    def test_three_defending_lawyers(self):
        lits = _parse(_RICH).litigantes
        assert sum(1 for l in lits if l.participante == "AB.DDO") == 3

    def test_exhorto_fields(self):
        exhs = _parse(_RICH).exhortos
        assert len(exhs) == 1
        e = exhs[0]
        assert e.rol_origen == "C-9999-2099"
        assert e.rol_destino == "E-888-2099"
        assert e.tipo_exhorto == "Exhorto"
        assert e.estado == "Generado"

    def test_no_notificaciones_or_escritos(self):
        d = _parse(_RICH)
        assert d.notificaciones == []
        assert d.escritos == []


@pytest.mark.skipif(not _SYNTHETIC.exists(), reason="synthetic fixture missing")
class TestSyntheticFixtureContract:
    def test_notificaciones_fields(self):
        nots = _parse(_SYNTHETIC).notificaciones
        assert len(nots) == 2
        assert [n.tipo_participante for n in nots] == ["DTE.", "DDO."]
        assert all(n.estado_notif == "Notificada" for n in nots)
        assert [n.tipo_notif for n in nots] == ["Personal", "Cedula"]
        assert nots[0].nombre.startswith("CAJA FICTICIA DE PREVISION")

    def test_escritos_fields(self):
        escs = _parse(_SYNTHETIC).escritos
        assert len(escs) == 2
        assert [s.tipo_escrito for s in escs] == ["Demanda", "Contestacion"]
        assert escs[0].tiene_documento is True
        assert escs[1].tiene_documento is False
        assert escs[0].solicitante.startswith("CAJA FICTICIA DE PREVISION")
