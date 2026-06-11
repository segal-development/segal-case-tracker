"""
Parser unit tests for CivilScraper tab-scoped entity parsers.

Tests:
- Movements scoping regression (S1-T03)
- Litigante parsing from anonymized rich fixture (S1-T04)
- Exhorto parsing from anonymized rich fixture (S1-T05)
- Notificacion / Escrito parsing from synthetic fixture (S1-T06)
- _parse_case_detail_html populates all five entity lists (S1-T07)
"""

import pathlib
import pytest

from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.base import (
    PJUDLitigante,
    PJUDNotificacion,
    PJUDEscrito,
    PJUDExhorto,
)

FIXTURES_DIR = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "pjud"


def _load(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scraper() -> CivilScraper:
    return CivilScraper(headless=True)


@pytest.fixture(scope="module")
def rich_html() -> str:
    return _load("detail_civil_rich.html")


@pytest.fixture(scope="module")
def synthetic_html() -> str:
    return _load("detail_civil_synthetic.html")


# ---------------------------------------------------------------------------
# S1-T03 — Scoping regression: movements MUST NOT bleed from other panes
# ---------------------------------------------------------------------------

class TestMovementsScopingRegression:
    """Prove that _parse_movements_table is scoped to #historiaCiv."""

    def test_movements_scoped_when_litigantes_before_historia(self, scraper: CivilScraper):
        """
        Craft HTML where #litigantesCiv appears BEFORE #historiaCiv.
        Old unscoped selector would return litigante rows.
        New scoped selector MUST return only historia rows.
        """
        html = """
        <div id="litigantesCiv">
          <div class="panel panel-default">
            <div class="table-responsive">
              <table class="table table-bordered table-striped table-hover">
                <thead><tr><th>Participante</th></tr></thead>
                <tbody>
                  <tr><td>DTE.</td><td>76543210-K</td><td>JURIDICA</td><td>EMPRESA FAKE</td></tr>
                  <tr><td>DDO.</td><td>11234567-K</td><td>NATURAL</td><td>PEDRO FICTICIO</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div id="historiaCiv">
          <div class="panel panel-default">
            <div class="table-responsive">
              <table class="table table-bordered table-striped table-hover">
                <thead><tr><th>Folio</th><th>Doc.</th><th>Anexo</th><th>Etapa</th><th>Trámite</th><th>Desc.</th><th>Fecha</th><th>Foja</th><th>Geo</th></tr></thead>
                <tbody>
                  <tr>
                    <td>40</td>
                    <td align="left"></td>
                    <td align="center"></td>
                    <td>Contestación</td>
                    <td>Resolución</td>
                    <td>Exhórtese</td>
                    <td>04/05/2026</td>
                    <td>29</td>
                    <td></td>
                  </tr>
                  <tr>
                    <td>39</td>
                    <td align="left"></td>
                    <td align="center"></td>
                    <td>Terminada</td>
                    <td>Escrito</td>
                    <td>Solicita exhorto</td>
                    <td>27/04/2026</td>
                    <td>27</td>
                    <td></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        """
        movements = scraper._parse_movements_table(html)

        # Must return ONLY the 2 historia rows
        assert len(movements) == 2, (
            f"Expected 2 movements (from #historiaCiv only), got {len(movements)}. "
            "This may indicate the old unscoped selector is still in use."
        )
        # Verify the folio values come from historiaCiv
        folios = {m.folio for m in movements}
        assert "40" in folios
        assert "39" in folios

    def test_movements_rich_fixture_returns_3_rows(self, scraper: CivilScraper, rich_html: str):
        """Verify rich fixture still returns exactly 3 movement rows."""
        movements = scraper._parse_movements_table(rich_html)
        assert len(movements) == 3


# ---------------------------------------------------------------------------
# S1-T04 — Litigante parser
# ---------------------------------------------------------------------------

class TestLitiganteParser:
    """Tests for _parse_litigantes_table using the anonymized rich fixture."""

    def test_parse_litigantes_returns_6_rows(self, scraper: CivilScraper, rich_html: str):
        """Rich fixture has 6 litigante rows."""
        litigantes = scraper._parse_litigantes_table(rich_html)
        assert len(litigantes) == 6

    def test_parse_litigantes_first_row_fields(self, scraper: CivilScraper, rich_html: str):
        """First litigante: DTE. / 76543210-K / JURIDICA / CAJA FICTICIA..."""
        litigantes = scraper._parse_litigantes_table(rich_html)
        first = litigantes[0]
        assert first.participante == "DTE."
        assert first.rut == "76543210-K"
        assert first.persona_type == "JURIDICA"
        assert "CAJA FICTICIA" in first.nombre

    def test_parse_litigantes_whitespace_stripped(self, scraper: CivilScraper, rich_html: str):
        """Cell values must be stripped of leading/trailing whitespace."""
        litigantes = scraper._parse_litigantes_table(rich_html)
        for lit in litigantes:
            assert lit.participante == lit.participante.strip()
            assert lit.persona_type == lit.persona_type.strip()
            assert lit.nombre == lit.nombre.strip()

    def test_parse_litigantes_all_are_pjud_litigante_instances(
        self, scraper: CivilScraper, rich_html: str
    ):
        """All returned objects must be PJUDLitigante instances."""
        litigantes = scraper._parse_litigantes_table(rich_html)
        for lit in litigantes:
            assert isinstance(lit, PJUDLitigante)

    def test_parse_litigantes_empty_tbody_returns_empty_list(self, scraper: CivilScraper):
        """Empty litigantesCiv tbody must return [] without error."""
        html = """
        <div id="litigantesCiv">
          <table class="table table-bordered table-striped table-hover">
            <thead><tr><th>Participante</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        """
        result = scraper._parse_litigantes_table(html)
        assert result == []

    def test_parse_litigantes_missing_pane_returns_empty_list(self, scraper: CivilScraper):
        """HTML without litigantesCiv must return [] without error."""
        result = scraper._parse_litigantes_table("<html><body>nothing</body></html>")
        assert result == []


# ---------------------------------------------------------------------------
# S1-T05 — Exhorto parser
# ---------------------------------------------------------------------------

class TestExhortoParser:
    """Tests for _parse_exhortos_table using the anonymized rich fixture."""

    def test_parse_exhortos_returns_1_row(self, scraper: CivilScraper, rich_html: str):
        """Rich fixture has 1 exhorto row."""
        exhortos = scraper._parse_exhortos_table(rich_html)
        assert len(exhortos) == 1

    def test_parse_exhortos_rol_destino_label_stripped(
        self, scraper: CivilScraper, rich_html: str
    ):
        """rol_destino must be extracted as plain text from the <label> element."""
        exhortos = scraper._parse_exhortos_table(rich_html)
        assert exhortos[0].rol_destino == "E-355-2026"

    def test_parse_exhortos_detalle_token_captured(
        self, scraper: CivilScraper, rich_html: str
    ):
        """detalle_token must be captured from detalleExhortosCivil('...')."""
        exhortos = scraper._parse_exhortos_table(rich_html)
        assert exhortos[0].detalle_token is not None
        assert len(exhortos[0].detalle_token) > 10

    def test_parse_exhortos_tribunal_trimmed(self, scraper: CivilScraper, rich_html: str):
        """tribunal_destino must have trailing whitespace stripped."""
        exhortos = scraper._parse_exhortos_table(rich_html)
        tribunal = exhortos[0].tribunal_destino
        assert tribunal == tribunal.strip()
        assert "Juzgado Civil de Santiago" in tribunal

    def test_parse_exhortos_other_fields(self, scraper: CivilScraper, rich_html: str):
        """Verify remaining fields from the single exhorto row."""
        exhortos = scraper._parse_exhortos_table(rich_html)
        ex = exhortos[0]
        assert ex.rol_origen == "C-1253-2015"
        assert ex.tipo_exhorto == "Exhorto"
        assert ex.fecha_ordena == "05/05/2026"
        assert ex.estado == "Generado"

    def test_parse_exhortos_empty_tbody_returns_empty_list(self, scraper: CivilScraper):
        """Empty exhortosCiv tbody must return [] without error."""
        html = """
        <div id="exhortosCiv">
          <table class="table table-bordered table-striped table-hover">
            <thead><tr><th>Rol Origen</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        """
        result = scraper._parse_exhortos_table(html)
        assert result == []


# ---------------------------------------------------------------------------
# S1-T06 — Notificacion + Escrito parsers
# ---------------------------------------------------------------------------

class TestNotificacionParser:
    """Tests for _parse_notificaciones_table.

    # TODO(validate-real-rich-case): column mapping unconfirmed on real data.
    """

    def test_parse_notificaciones_rich_fixture_empty(
        self, scraper: CivilScraper, rich_html: str
    ):
        """Rich fixture has empty notificacionesCiv tbody → []."""
        result = scraper._parse_notificaciones_table(rich_html)
        assert result == []

    def test_parse_notificaciones_synthetic_fixture_2_rows(
        self, scraper: CivilScraper, synthetic_html: str
    ):
        """Synthetic fixture has 2 notificacion rows."""
        result = scraper._parse_notificaciones_table(synthetic_html)
        assert len(result) == 2

    def test_parse_notificaciones_all_pjud_notificacion_instances(
        self, scraper: CivilScraper, synthetic_html: str
    ):
        """All returned objects must be PJUDNotificacion instances."""
        result = scraper._parse_notificaciones_table(synthetic_html)
        for notif in result:
            assert isinstance(notif, PJUDNotificacion)

    def test_parse_notificaciones_empty_tbody_returns_empty_list(
        self, scraper: CivilScraper
    ):
        """Empty pane returns [] without error."""
        html = """
        <div id="notificacionesCiv">
          <table class="table table-bordered">
            <tbody></tbody>
          </table>
        </div>
        """
        assert scraper._parse_notificaciones_table(html) == []


class TestEscritoParser:
    """Tests for _parse_escritos_table.

    # TODO(validate-real-rich-case): column mapping unconfirmed on real data.
    """

    def test_parse_escritos_rich_fixture_empty(
        self, scraper: CivilScraper, rich_html: str
    ):
        """Rich fixture has empty escritosCiv tbody → []."""
        result = scraper._parse_escritos_table(rich_html)
        assert result == []

    def test_parse_escritos_synthetic_fixture_2_rows(
        self, scraper: CivilScraper, synthetic_html: str
    ):
        """Synthetic fixture has 2 escrito rows."""
        result = scraper._parse_escritos_table(synthetic_html)
        assert len(result) == 2

    def test_parse_escritos_tiene_documento_and_tiene_anexo_are_booleans(
        self, scraper: CivilScraper, synthetic_html: str
    ):
        """tiene_documento and tiene_anexo must be boolean values."""
        result = scraper._parse_escritos_table(synthetic_html)
        for escrito in result:
            assert isinstance(escrito.tiene_documento, bool)
            assert isinstance(escrito.tiene_anexo, bool)

    def test_parse_escritos_first_row_has_document(
        self, scraper: CivilScraper, synthetic_html: str
    ):
        """First synthetic escrito has a dtaDoc token → tiene_documento=True."""
        result = scraper._parse_escritos_table(synthetic_html)
        first = result[0]
        assert first.tiene_documento is True
        assert first.doc_token is not None

    def test_parse_escritos_second_row_no_document(
        self, scraper: CivilScraper, synthetic_html: str
    ):
        """Second synthetic escrito has no dtaDoc → tiene_documento=False."""
        result = scraper._parse_escritos_table(synthetic_html)
        second = result[1]
        assert second.tiene_documento is False

    def test_parse_escritos_all_pjud_escrito_instances(
        self, scraper: CivilScraper, synthetic_html: str
    ):
        """All returned objects must be PJUDEscrito instances."""
        result = scraper._parse_escritos_table(synthetic_html)
        for escrito in result:
            assert isinstance(escrito, PJUDEscrito)

    def test_parse_escritos_empty_tbody_returns_empty_list(self, scraper: CivilScraper):
        """Empty pane returns [] without error."""
        html = """
        <div id="escritosCiv">
          <table class="table table-bordered">
            <tbody></tbody>
          </table>
        </div>
        """
        assert scraper._parse_escritos_table(html) == []


# ---------------------------------------------------------------------------
# S1-T07 — _parse_case_detail_html populates all five entity lists
# ---------------------------------------------------------------------------

class TestParseDetailHtmlIntegration:
    """Integration test: _parse_case_detail_html must call all 5 parsers."""

    def test_parse_case_detail_html_populates_all_five_lists(
        self, scraper: CivilScraper, rich_html: str
    ):
        """
        Rich fixture: 3 movements, 6 litigantes, 0 notificaciones,
        0 escritos, 1 exhorto — all correctly populated on PJUDCaseDetail.
        """
        detail = scraper._parse_case_detail_html(rich_html, "fake-token")

        assert len(detail.movements) == 3
        assert len(detail.litigantes) == 6
        assert len(detail.notificaciones) == 0
        assert detail.notificaciones is not None  # not None — empty list
        assert len(detail.escritos) == 0
        assert detail.escritos is not None
        assert len(detail.exhortos) == 1
