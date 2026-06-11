"""
Parser unit tests for CivilScraper tab-scoped entity parsers.

Tests:
- Movements scoping regression (S1-T03)
- Litigante parsing from anonymized rich fixture (S1-T04)
- Exhorto parsing from anonymized rich fixture (S1-T05)
- Notificacion / Escrito parsing from synthetic fixture (S1-T06)
- _parse_case_detail_html populates all five entity lists (S1-T07)
- Static document token extraction — 5 types + dtaCert disambiguation + fa-ban (case-documents Slice 1)
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
        """Verify rich fixture still returns exactly 3 movement rows with correct folios."""
        movements = scraper._parse_movements_table(rich_html)
        assert len(movements) == 3
        folios = {m.folio for m in movements}
        assert "40" in folios, f"Expected folio 40 in historiaCiv rows, got {folios}"
        assert "39" in folios, f"Expected folio 39 in historiaCiv rows, got {folios}"
        assert "38" in folios, f"Expected folio 38 in historiaCiv rows, got {folios}"


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
        assert exhortos[0].rol_destino == "E-888-2099"

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
        assert "Juzgado Civil Ficticio" in tribunal

    def test_parse_exhortos_other_fields(self, scraper: CivilScraper, rich_html: str):
        """Verify remaining fields from the single exhorto row."""
        exhortos = scraper._parse_exhortos_table(rich_html)
        ex = exhortos[0]
        assert ex.rol_origen == "C-9999-2099"
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
        # Field spot-checks to catch scoping bugs returning wrong rows
        assert detail.litigantes[0].participante == "DTE."
        assert detail.exhortos[0].rol_destino == "E-888-2099"


# ---------------------------------------------------------------------------
# FIX 3 — document_token_pattern attribute-order tolerance
# ---------------------------------------------------------------------------

class TestDocTokenAttributeOrderTolerance:
    """document_token_pattern extraction must be tolerant of attribute order."""

    def test_doc_token_value_before_name_in_escrito(self, scraper: CivilScraper):
        """Extract token when value= appears BEFORE name= (reversed attribute order)."""
        html = """
        <div id="escritosCiv">
          <table class="table table-bordered">
            <tbody>
              <tr>
                <td align="left">
                  <form>
                    <input type="hidden" value="TOKEN-REVERSED-XYZ" name="dtaDoc">
                  </form>
                </td>
                <td align="center"></td>
                <td>01/01/2099</td>
                <td>Demanda</td>
                <td>PARTE FICTICIA</td>
              </tr>
            </tbody>
          </table>
        </div>
        """
        result = scraper._parse_escritos_table(html)
        assert len(result) == 1, "Expected 1 escrito row"
        assert result[0].tiene_documento is True, (
            "tiene_documento must be True when dtaDoc token is present (value before name)"
        )
        assert result[0].doc_token == "TOKEN-REVERSED-XYZ", (
            f"doc_token extraction failed for reversed attribute order; got {result[0].doc_token!r}"
        )


# ---------------------------------------------------------------------------
# case-documents Slice 1 — S1-T2: Static Document Token Extraction
# ---------------------------------------------------------------------------

# Fake JWT used for the cert_envio disambiguation slot in the rich fixture.
_FAKE_CERT_ENVIO_TOKEN = "eyJhbGciOiJub25lIn0.e30.FAKE_CERT_ENVIO"


class TestStaticDocumentTokenExtraction:
    """Tests for the 5 static document token types and disambiguation logic.

    Depends on:
    - detail_civil_rich.html: all 5 token types present (cert_envio has the fake JWT)
    - detail_civil_synthetic.html: cert_envio slot has fa-ban, no form
    """

    def test_all_five_static_token_types_parsed(
        self, scraper: CivilScraper, rich_html: str
    ) -> None:
        """Rich fixture: texto_demanda, cert_envio, ebook and a docuS movement token all non-null."""
        detail = scraper._parse_case_detail_html(rich_html, "fake-token")

        texto_demanda_doc = next(
            (d for d in detail.case_documents if d.doc_type == "texto_demanda"), None
        )
        cert_envio_doc = next(
            (d for d in detail.case_documents if d.doc_type == "cert_envio"), None
        )
        ebook_doc = next(
            (d for d in detail.case_documents if d.doc_type == "ebook"), None
        )

        assert texto_demanda_doc is not None, "texto_demanda doc must be parsed"
        assert texto_demanda_doc.token is not None
        assert cert_envio_doc is not None, "cert_envio doc must be parsed"
        assert cert_envio_doc.token is not None
        assert ebook_doc is not None, "ebook doc must be parsed"
        assert ebook_doc.token is not None

        # At least one movement must have a document token
        assert any(m.documento_token is not None for m in detail.movements), (
            "At least one movement must have a document token"
        )

    def test_dtaCert_disambiguation_by_endpoint(
        self, scraper: CivilScraper, rich_html: str
    ) -> None:
        """escrito_cert (docCertificadoEscrito.php) and cert_envio (docCertificadoDemanda.php)
        must not be confused: same param name dtaCert, different form action."""
        detail = scraper._parse_case_detail_html(rich_html, "fake-token")

        # cert_envio comes from case-level docCertificadoDemanda.php form
        cert_envio_doc = next(
            (d for d in detail.case_documents if d.doc_type == "cert_envio"), None
        )
        assert cert_envio_doc is not None
        assert cert_envio_doc.token == _FAKE_CERT_ENVIO_TOKEN

        # escrito_cert comes from movement-level docCertificadoEscrito.php form
        # At least one movement should have an escrito_cert document
        escrito_cert_docs = [
            doc
            for m in detail.movements
            for doc in m.documentos
            if doc.doc_type == "escrito_cert"
        ]
        assert len(escrito_cert_docs) > 0, (
            "Rich fixture must have at least one escrito_cert in movements"
        )
        # Tokens must differ — different form actions produce different tokens
        assert escrito_cert_docs[0].token != cert_envio_doc.token

    def test_fa_ban_yields_none_without_exception(
        self, scraper: CivilScraper, synthetic_html: str
    ) -> None:
        """Synthetic fixture: cert_envio has fa-ban → available=False, token=None, no exception."""
        detail = scraper._parse_case_detail_html(synthetic_html, "fake-token")

        cert_envio_doc = next(
            (d for d in detail.case_documents if d.doc_type == "cert_envio"), None
        )
        assert cert_envio_doc is not None, (
            "cert_envio must be present even when fa-ban is shown (unavailable doc)"
        )
        assert cert_envio_doc.token is None
        assert cert_envio_doc.available is False

    def test_missing_form_yields_none_without_exception(
        self, scraper: CivilScraper
    ) -> None:
        """HTML with neither form nor fa-ban for cert_envio → no cert_envio doc, no exception."""
        # Minimal valid HTML with correct ROL but no cert_envio form or icon
        html = """
        <div>
          <strong>ROL:</strong> C-0001-2099
          <strong>F. Ing.:</strong> 01/01/2026
          <table class="table table-responsive table-titulos">
            <tbody>
              <tr>
                <td><strong>Texto Demanda:</strong></td>
                <td><strong>Anexos de la causa:</strong></td>
                <td><strong>Certificado de Envío:</strong><br><!-- empty slot --></td>
                <td id="boton"><strong>Ebook:</strong></td>
              </tr>
            </tbody>
          </table>
          <div id="historiaCiv">
            <table class="table table-bordered table-striped table-hover">
              <thead><tr><th>Folio</th><th>Doc.</th><th>Anexo</th><th>Etapa</th><th>Trámite</th>
              <th>Desc.</th><th>Fecha</th><th>Foja</th><th>Geo</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        """
        detail = scraper._parse_case_detail_html(html, "fake-token")

        cert_envio_doc = next(
            (d for d in detail.case_documents if d.doc_type == "cert_envio"), None
        )
        assert cert_envio_doc is None, (
            "No cert_envio doc expected when neither form nor fa-ban is present"
        )

    def test_texto_demanda_fa_ban_yields_unavailable(
        self, scraper: CivilScraper
    ) -> None:
        """FIX 6: texto_demanda with fa-ban → PJUDDocument(available=False, token=None).

        Previously texto_demanda with fa-ban yielded None (slot silently absent),
        making it impossible to distinguish 'never seen' from 'seen-unavailable'.
        After the fix, both texto_demanda and ebook behave consistently with cert_envio.
        """
        html = """
        <div>
          <strong>ROL:</strong> C-0001-2099
          <strong>F. Ing.:</strong> 01/01/2026
          <table class="table table-responsive table-titulos">
            <tbody>
              <tr>
                <td>
                  <strong>Texto Demanda:</strong>
                  <br>
                  <i class="fas fa-ban fa-lg" aria-hidden="true" style="color:#660000;"></i>
                </td>
                <td><strong>Certificado de Envío:</strong></td>
                <td id="boton"><strong>Ebook:</strong></td>
              </tr>
            </tbody>
          </table>
          <div id="historiaCiv">
            <table class="table table-bordered table-striped table-hover">
              <thead><tr><th>Folio</th><th>Doc.</th><th>Anexo</th><th>Etapa</th><th>Trámite</th>
              <th>Desc.</th><th>Fecha</th><th>Foja</th><th>Geo</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        """
        detail = scraper._parse_case_detail_html(html, "fake-token")

        texto_demanda_doc = next(
            (d for d in detail.case_documents if d.doc_type == "texto_demanda"), None
        )
        assert texto_demanda_doc is not None, (
            "texto_demanda must be present as unavailable when fa-ban is shown"
        )
        assert texto_demanda_doc.token is None
        assert texto_demanda_doc.available is False

    def test_ebook_fa_ban_yields_unavailable(
        self, scraper: CivilScraper
    ) -> None:
        """FIX 6: ebook with fa-ban → PJUDDocument(available=False, token=None).

        Symmetric with cert_envio and texto_demanda fa-ban handling.
        """
        html = """
        <div>
          <strong>ROL:</strong> C-0001-2099
          <strong>F. Ing.:</strong> 01/01/2026
          <table class="table table-responsive table-titulos">
            <tbody>
              <tr>
                <td><strong>Texto Demanda:</strong></td>
                <td><strong>Certificado de Envío:</strong></td>
                <td id="boton">
                  <strong>Ebook:</strong>
                  <br>
                  <i class="fas fa-ban fa-lg" aria-hidden="true" style="color:#660000;"></i>
                </td>
              </tr>
            </tbody>
          </table>
          <div id="historiaCiv">
            <table class="table table-bordered table-striped table-hover">
              <thead><tr><th>Folio</th><th>Doc.</th><th>Anexo</th><th>Etapa</th><th>Trámite</th>
              <th>Desc.</th><th>Fecha</th><th>Foja</th><th>Geo</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        """
        detail = scraper._parse_case_detail_html(html, "fake-token")

        ebook_doc = next(
            (d for d in detail.case_documents if d.doc_type == "ebook"), None
        )
        assert ebook_doc is not None, (
            "ebook must be present as unavailable when fa-ban is shown"
        )
        assert ebook_doc.token is None
        assert ebook_doc.available is False
