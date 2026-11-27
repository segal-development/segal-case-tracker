"""Pure parsers for PJUD's public "Búsqueda por Fecha" (synthetic fixtures)."""

from datetime import date
from pathlib import Path

from app.scrapper.pjud import publico_fecha as pf

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "pjud_publico"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_resultados_returns_every_row_with_its_fields():
    rows = pf.parse_resultados(_read("resultados_fecha.html"))
    assert len(rows) == 6
    first = rows[0]
    assert first.rol == "C-1001-2026"
    assert first.fecha == date(2026, 11, 26)
    assert first.caratulado == "/FICTICIO UNO"
    assert first.tribunal == "1º Juzgado Civil de Santiago"
    assert first.detalle == "FAKE.JWT.TOKEN-1"


def test_parse_resultados_empty_response():
    assert pf.parse_resultados(_read("resultados_vacio.html")) == []
    assert pf.parse_resultados("") == []


def test_solo_civiles_keeps_only_c_roles():
    rows = pf.parse_resultados(_read("resultados_fecha.html"))
    assert [r.rol for r in pf.solo_civiles(rows)] == [
        "C-1001-2026", "C-1002-2026", "C-1003-2026", "C-1004-2026",
    ]


def test_parse_litigantes_reads_codes_and_names():
    lit = pf.parse_litigantes(_read("modal_sin_abogado.html"))
    assert [x.participante for x in lit] == ["DTE.", "AB.DTE", "AP.DTE", "DDO."]
    assert lit[3].persona == "NATURAL"
    assert lit[3].nombre == "DEMANDADA FICTICIA TRES"
    assert lit[3].rut == "66666666-6"


def test_sin_abogado_demandado_when_ddo_has_no_lawyer():
    assert pf.sin_abogado_demandado(pf.parse_litigantes(_read("modal_sin_abogado.html"))) is True
    assert pf.sin_abogado_demandado(pf.parse_litigantes(_read("modal_dos_demandados_sin_abogado.html"))) is True


def test_a_plaintiff_lawyer_does_not_count_as_a_defendant_lawyer():
    lit = pf.parse_litigantes(_read("modal_sin_abogado.html"))
    assert any(x.participante == "AB.DTE" for x in lit)
    assert pf.sin_abogado_demandado(lit) is True


def test_ab_ddo_means_has_lawyer():
    assert pf.sin_abogado_demandado(pf.parse_litigantes(_read("modal_con_abogado.html"))) is False


def test_ap_ddo_means_has_lawyer():
    assert pf.sin_abogado_demandado(pf.parse_litigantes(_read("modal_con_apoderado_ddo.html"))) is False


def test_ddor_only_is_not_kept():
    assert pf.sin_abogado_demandado(pf.parse_litigantes(_read("modal_solo_ddor.html"))) is False


def test_sin_abogado_codes_compared_after_strip_and_upper():
    L = pf.Litigante
    assert pf.sin_abogado_demandado([L(" ddo. ", "", "", "x")]) is True
    assert pf.sin_abogado_demandado([L("DDO.", "", "", "x"), L(" ab.ddo ", "", "", "y")]) is False
    assert pf.sin_abogado_demandado([]) is False


def test_parse_modal_extracts_rol_caratulado_tribunal_fecha():
    m = pf.parse_modal(_read("modal_sin_abogado.html"))
    assert m.rol == "C-1002-2026"
    assert m.caratulado == "BANCO EJEMPLO S.A./FICTICIA TRES"
    assert m.tribunal == "1º Juzgado Civil de Santiago"
    assert m.fecha_ingreso == date(2026, 11, 25)


def test_extraer_valor_demanda():
    assert pf.extraer_valor_demanda(_read("modal_sin_abogado.html")) == "FAKE.JWT.DEMANDA-2"
    assert pf.extraer_valor_demanda("<div>sin demanda</div>") is None
