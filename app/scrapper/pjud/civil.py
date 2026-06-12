"""
PJUD Civil competency scraper.

Inherits from PJUDBaseScraper and implements civil-specific logic.
Uses SelectorRegistry for configurable, hot-reloadable selectors.
"""

import asyncio
import logging
import re
from typing import List, Optional

from playwright.async_api import Page

from app.scrapper.pjud.base import (
    PJUDBaseScraper,
    PJUDCase,
    PJUDDocument,
    PJUDMovement,
    PJUDCaseDetail,
    PJUDLitigante,
    PJUDNotificacion,
    PJUDEscrito,
    PJUDExhorto,
    CompetencyConfig,
    PJUD_BASE_URL,
)
from app.scrapper.pjud.exceptions import DocumentTokenExpiredError, ScrapingError
from app.scrapper.pjud.selectors import SelectorRegistry
from app.services.pjud_session import PJUDSession


logger = logging.getLogger(__name__)


# Civil-specific constants (kept for backward compatibility)
PJUD_SEARCH_CIVIL_URL = f"{PJUD_BASE_URL}/ADIR_871/civil/consultaRitCivil.php"
PJUD_MY_CASES_CIVIL_URL = f"{PJUD_BASE_URL}/misCausas/civil/consultaMisCausasCivil.php"
PJUD_MY_CASE_DETAIL_URL = f"{PJUD_BASE_URL}/misCausas/civil/modal/misCausasCivil.php"
PJUD_PUBLIC_CASE_DETAIL_URL = f"{PJUD_BASE_URL}/ADIR_871/civil/modal/causaCivil.php"

# Fixed tokens (discovered from PJUD JavaScript)
TOKEN_MY_CASES = "df32271e9cdca2704ff289941058a253"
TOKEN_PUBLIC_SEARCH = "917cfa057160fbb6de2eb86da2348e42"

COMPETENCIA_CIVIL = "3"

# Global selector registry instance (shared across all CivilScraper instances)
_selector_registry: Optional[SelectorRegistry] = None


def get_selector_registry() -> SelectorRegistry:
    """Get or create the global selector registry."""
    global _selector_registry
    if _selector_registry is None:
        _selector_registry = SelectorRegistry()
        # Pre-load civil selectors
        _selector_registry.load("civil")
    return _selector_registry


def reload_selectors(competencia: Optional[str] = None) -> dict:
    """
    Reload selectors from YAML files.
    
    Args:
        competencia: Specific competency to reload, or None for all.
    
    Returns:
        Dict mapping competencia -> success status.
    """
    registry = get_selector_registry()
    return registry.reload(competencia)


class CivilScraper(PJUDBaseScraper):
    """
    Scraper for PJUD Civil portal.
    
    Implements civil-specific parsing and selectors while inheriting
    shared login, session, and pagination logic from PJUDBaseScraper.
    
    Uses SelectorRegistry for configurable selectors that can be
    hot-reloaded without code changes when PJUD updates their HTML.
    """
    
    def __init__(self, *args, selector_registry: Optional[SelectorRegistry] = None, **kwargs):
        """
        Initialize CivilScraper.
        
        Args:
            selector_registry: Optional custom registry. Uses global if not provided.
            *args, **kwargs: Passed to PJUDBaseScraper.
        """
        super().__init__(*args, **kwargs)
        self._selector_registry = selector_registry or get_selector_registry()
    
    @property
    def selectors(self) -> SelectorRegistry:
        """Access the selector registry."""
        return self._selector_registry
    
    def get_selector(self, name: str) -> str:
        """Get a selector value from the registry."""
        return self._selector_registry.get("civil", name)
    
    def get_selector_with_fallbacks(self, name: str) -> List[str]:
        """Get a selector with all fallbacks."""
        return self._selector_registry.get_with_fallbacks("civil", name)
    
    def _get_competency_config(self) -> CompetencyConfig:
        """Return Civil competency configuration from registry."""
        registry = self._selector_registry
        
        return CompetencyConfig(
            name="civil",
            display_name="Civil",
            competencia_code=registry.get("civil", "competencia_code"),
            table_body_id=registry.get("civil", "table_body_id"),
            modal_id=registry.get("civil", "modal_id"),
            detail_function_name=registry.get("civil", "detail_function_name"),
            cases_endpoint=registry.get("civil", "cases_endpoint"),
            detail_endpoint=registry.get("civil", "detail_endpoint"),
            search_endpoint=registry.get("civil", "search_endpoint"),
        )
    
    async def _fetch_cases_page(
        self,
        page: Page,
        rut_num: str,
        dv: str,
        tipo_causa: str,
        year: str,
        fecha_desde: str,
        fecha_hasta: str,
        current_page: int,
    ) -> str:
        """Fetch a single page of civil cases."""
        result = await page.evaluate(f"""
            () => {{
                return new Promise((resolve) => {{
                    $.ajax({{
                        url: '{self.config.cases_endpoint}',
                        type: 'POST',
                        data: {{
                            rutMisCauCiv: '{rut_num}',
                            dvMisCauCiv: '{dv}',
                            tipoMisCauCiv: '0',
                            rolMisCauCiv: '',
                            anhoMisCauCiv: '{year}',
                            fecDesdeMisCauCiv: '{fecha_desde}',
                            fecHastaMisCauCiv: '{fecha_hasta}',
                            'tipCausaMisCauCiv[]': '{tipo_causa}',
                            nombreMisCauCiv: '',
                            apePatMisCauCiv: '',
                            apeMatMisCauCiv: '',
                            pagina: {current_page}
                        }},
                        success: function(data) {{ resolve(data); }},
                        error: function(xhr) {{ resolve('ERROR:' + xhr.status); }}
                    }});
                }});
            }}
        """)
        return result
    
    def _parse_cases_html(self, html: str) -> List[PJUDCase]:
        """Parse HTML response from civil cases endpoint."""
        cases = []
        
        # Pattern for case rows with token - from registry
        token_pattern = self.get_selector("case_detail_token_pattern")
        row_pattern = self.get_selector("case_row_pattern")
        
        rows = re.findall(row_pattern, html, re.DOTALL | re.IGNORECASE)
        
        for row in rows:
            # Extract token
            token_match = re.search(token_pattern, row)
            if not token_match:
                continue
            
            token = token_match.group(1)
            
            # Extract cell values
            # Columns: Actions, RIT, Tribunal, Caratulado, Fecha, Estado, Cuaderno, Institucion
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            if len(cells) < 5:
                continue
            
            def clean(s):
                return re.sub(r'<[^>]+>', '', s).strip()
            
            rol = clean(cells[1])
            
            # Skip cases without valid ROL (must match pattern like C-1234-2026)
            if not rol or not re.match(r'^[A-Z]+-\d+-\d{4}$', rol):
                continue
            
            cases.append(PJUDCase(
                rol=rol,
                tribunal=clean(cells[2]),
                caratulado=clean(cells[3]),
                fecha_ingreso=clean(cells[4]),
                estado_cuaderno=clean(cells[5]) if len(cells) > 5 else None,
                cuaderno=clean(cells[6]) if len(cells) > 6 else None,
                institucion=clean(cells[7]) if len(cells) > 7 else None,
                case_token=token,
            ))
        
        logger.info(f"Parsed {len(cases)} civil cases from HTML")
        return cases
    
    def _parse_case_detail_html(self, html: str, case_token: str) -> PJUDCaseDetail:
        """Parse civil case detail HTML including movements."""
        
        # Extract basic case info
        rol = ""
        tribunal = ""
        caratulado = ""
        fecha_ingreso = ""
        estado_adm = ""
        procedimiento = ""
        ubicacion = ""
        estado_proc = ""
        etapa = ""
        
        # ROL: <strong>ROL:</strong> C-7616-2026
        rol_match = re.search(r'<strong>ROL:</strong>\s*([^<]+)', html, re.IGNORECASE)
        if rol_match:
            rol = rol_match.group(1).strip()
        
        # Fecha Ingreso: <strong>F. Ing.:</strong> 03/06/2026
        fecha_match = re.search(r'<strong>F\.\s*Ing\.?:</strong>\s*(\d{2}/\d{2}/\d{4})', html, re.IGNORECASE)
        if fecha_match:
            fecha_ingreso = fecha_match.group(1)
        
        # Caratulado: after F. Ing. in the same row
        cara_match = re.search(r'<strong>F\.\s*Ing\.?:</strong>\s*\d{2}/\d{2}/\d{4}\s*</td>\s*<td>([^<]+)', html, re.IGNORECASE)
        if cara_match:
            caratulado = cara_match.group(1).strip()
        
        # Estado Administrativo
        est_adm_match = re.search(r'<strong>Est\.\s*Adm\.?:\s*</strong>\s*[^>]*>([^<]+)', html, re.IGNORECASE)
        if est_adm_match:
            estado_adm = est_adm_match.group(1).strip()
        
        # Procedimiento
        proc_match = re.search(r'<strong>Proc\.?:</strong>\s*([^<]+)', html, re.IGNORECASE)
        if proc_match:
            procedimiento = proc_match.group(1).strip()
        
        # Ubicacion
        ubic_match = re.search(r'<strong>Ubicacion:</strong>\s*([^<]+)', html, re.IGNORECASE)
        if ubic_match:
            ubicacion = ubic_match.group(1).strip()
        
        # Estado Procesal
        est_proc_match = re.search(r'<strong>Estado\s*Proc\.?:</strong>\s*([^<]+)', html, re.IGNORECASE)
        if est_proc_match:
            estado_proc = est_proc_match.group(1).strip()
        
        # Etapa
        etapa_match = re.search(r'<strong>Etapa:</strong>\s*([^<]+)', html, re.IGNORECASE)
        if etapa_match:
            etapa = etapa_match.group(1).strip()
        
        # Tribunal
        trib_match = re.search(r'<strong>Tribunal:</strong>\s*([^<]+)', html, re.IGNORECASE)
        if trib_match:
            tribunal = trib_match.group(1).strip()
        
        # Parse cuadernos from select
        cuadernos = []
        cuaderno_pattern = r'<option\s+value="([^"]+)"[^>]*>([^<]+)</option>'
        for match in re.finditer(cuaderno_pattern, html):
            cuadernos.append({
                'token': match.group(1),
                'name': match.group(2).strip()
            })
        
        # Parse movements from the history table
        movements = self._parse_movements_table(html)

        # Parse all four additional entity tabs from the same HTML response
        litigantes = self._parse_litigantes_table(html)
        notificaciones = self._parse_notificaciones_table(html)
        escritos = self._parse_escritos_table(html)
        exhortos = self._parse_exhortos_table(html)

        # Parse case-level static document tokens (texto_demanda, cert_envio, ebook)
        case_documents = self._parse_case_documents(html)

        # Validate ROL format
        if not rol or not re.match(r'^[A-Z]+-\d+-\d{4}$', rol):
            logger.warning(f"Invalid ROL format: '{rol}'")
            raise ValueError(f"Invalid ROL format: '{rol}'")

        logger.info(
            f"Parsed case {rol}: {len(movements)} movements, "
            f"{len(litigantes)} litigantes, {len(notificaciones)} notificaciones, "
            f"{len(escritos)} escritos, {len(exhortos)} exhortos, "
            f"{len(cuadernos)} cuadernos"
        )

        case = PJUDCase(
            rol=rol,
            tribunal=tribunal,
            caratulado=caratulado,
            fecha_ingreso=fecha_ingreso,
            case_token=case_token,
        )

        return PJUDCaseDetail(
            case=case,
            movements=movements,
            cuadernos=cuadernos,
            estado_administrativo=estado_adm,
            procedimiento=procedimiento,
            ubicacion=ubicacion,
            estado_procesal=estado_proc,
            etapa=etapa,
            raw_html=html,
            litigantes=litigantes,
            notificaciones=notificaciones,
            escritos=escritos,
            exhortos=exhortos,
            case_documents=case_documents,
        )
    
    # ========================================================================
    # STATIC DOCUMENT TOKEN HELPERS  (case-documents Slice 1)
    # ========================================================================

    @staticmethod
    def _extract_form_token(html: str, action_fragment: str, param_name: str) -> Optional[str]:
        """Extract a token from a form anchored on its action attribute.

        Anchoring on the form action disambiguates dtaCert:
        - docCertificadoEscrito.php → escrito_cert (movement-level)
        - docCertificadoDemanda.php → cert_envio   (case-level)

        Returns None when no matching form or param is found.
        """
        import re as _re
        pattern = (
            r'<form[^>]+action="[^"]*'
            + _re.escape(action_fragment)
            + r'[^"]*"[^>]*>(.*?)</form>'
        )
        form_match = _re.search(pattern, html, _re.DOTALL | _re.IGNORECASE)
        if not form_match:
            return None
        body = form_match.group(1)
        # Try name before value, then reversed attribute order
        for pat in (
            rf'name="{_re.escape(param_name)}"[^>]*value="([^"]+)"',
            rf'value="([^"]+)"[^>]*name="{_re.escape(param_name)}"',
        ):
            m = _re.search(pat, body, _re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    def _parse_cert_envio(self, html: str) -> Optional[PJUDDocument]:
        """Extract the cert_envio document from the case-level docs area.

        Uses docCertificadoDemanda.php form action for disambiguation from
        movement-level escrito_cert (docCertificadoEscrito.php).

        Returns:
        - PJUDDocument(available=True)  when form with token found
        - PJUDDocument(available=False) when fa-ban icon found in the cert-envio <td>
        - None when neither form nor fa-ban present (slot absent/empty)
        """
        token = self._extract_form_token(
            html,
            self.get_selector("cert_envio_form_action"),
            self.get_selector("cert_envio_param"),
        )
        if token is not None:
            return PJUDDocument(
                token=token,
                tipo="caso",
                url_type="cert_envio",
                doc_type="cert_envio",
                endpoint=f"documentos/{self.get_selector('cert_envio_form_action')}",
                param_name=self.get_selector("cert_envio_param"),
                available=True,
            )

        # No form found — check for fa-ban in the Certificado de Envío <td>
        cert_td_match = re.search(
            r'<td>\s*(?:<[^>]+>\s*)*Certificado de Env[íi]o:.*?</td>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if cert_td_match and re.search(
            self.get_selector("disabled_doc_indicator"),
            cert_td_match.group(0),
            re.IGNORECASE,
        ):
            return PJUDDocument(
                token=None,
                tipo="caso",
                url_type="cert_envio",
                doc_type="cert_envio",
                endpoint=f"documentos/{self.get_selector('cert_envio_form_action')}",
                param_name=self.get_selector("cert_envio_param"),
                available=False,
            )

        return None  # No form, no fa-ban → absent

    def _parse_fa_ban_slot(
        self,
        html: str,
        td_label_pattern: str,
        url_type: str,
        doc_type: str,
        form_action: str,
        param_name: str,
    ) -> Optional[PJUDDocument]:
        """Return PJUDDocument(available=False) when fa-ban is found in a labelled <td>.

        Scopes the fa-ban search to the <td> whose text content starts with the
        given label (e.g. "Texto Demanda:", "Ebook:").

        Returns None when no fa-ban is found in the matching <td> (slot absent/empty).
        """
        td_match = re.search(td_label_pattern, html, re.DOTALL | re.IGNORECASE)
        if td_match and re.search(
            self.get_selector("disabled_doc_indicator"),
            td_match.group(0),
            re.IGNORECASE,
        ):
            return PJUDDocument(
                token=None,
                tipo="caso",
                url_type=url_type,
                doc_type=doc_type,
                endpoint=f"documentos/{form_action}",
                param_name=param_name,
                available=False,
            )
        return None

    def _parse_case_documents(self, html: str) -> List[PJUDDocument]:
        """Extract the 3 case-level static document tokens.

        Tokens extracted (all case-scoped, scope_key=""):
        - texto_demanda: docu.php?valorEncTxtDmda
        - cert_envio:    docCertificadoDemanda.php?dtaCert  (disambiguated by action)
        - ebook:         newebookcivil.php?dtaEbook

        fa-ban → PJUDDocument(available=False) for all three slots (symmetric).
        Missing form AND no fa-ban → not included (slot absent).
        """
        docs: List[PJUDDocument] = []

        # texto_demanda
        td_action = self.get_selector("texto_demanda_form_action")
        td_param = self.get_selector("texto_demanda_param")
        td_token = self._extract_form_token(html, td_action, td_param)
        if td_token is not None:
            docs.append(PJUDDocument(
                token=td_token,
                tipo="caso",
                url_type="texto_demanda",
                doc_type="texto_demanda",
                endpoint=f"documentos/{td_action}",
                param_name=td_param,
                available=True,
            ))
        else:
            # fa-ban in the "Texto Demanda:" <td> → available=False row
            fa_doc = self._parse_fa_ban_slot(
                html,
                td_label_pattern=r'<td>\s*(?:<[^>]+>\s*)*Texto Demanda:.*?</td>',
                url_type="texto_demanda",
                doc_type="texto_demanda",
                form_action=td_action,
                param_name=td_param,
            )
            if fa_doc is not None:
                docs.append(fa_doc)

        # cert_envio (with fa-ban + missing-form handling)
        cert_envio_doc = self._parse_cert_envio(html)
        if cert_envio_doc is not None:
            docs.append(cert_envio_doc)

        # ebook
        eb_action = self.get_selector("ebook_form_action")
        eb_param = self.get_selector("ebook_param")
        eb_token = self._extract_form_token(html, eb_action, eb_param)
        if eb_token is not None:
            docs.append(PJUDDocument(
                token=eb_token,
                tipo="caso",
                url_type="ebook",
                doc_type="ebook",
                endpoint=f"documentos/{eb_action}",
                param_name=eb_param,
                available=True,
            ))
        else:
            # fa-ban in the "Ebook:" <td> → available=False row
            fa_doc = self._parse_fa_ban_slot(
                html,
                td_label_pattern=r'<td[^>]*>\s*(?:<[^>]+>\s*)*Ebook:.*?</td>',
                url_type="ebook",
                doc_type="ebook",
                form_action=eb_action,
                param_name=eb_param,
            )
            if fa_doc is not None:
                docs.append(fa_doc)

        return docs

    # ========================================================================
    # PRIVATE HELPERS
    # ========================================================================

    @staticmethod
    def _clean_cell(s: str) -> str:
        """Strip HTML tags and collapse internal whitespace."""
        s = re.sub(r'<[^>]+>', '', s)
        s = re.sub(r'\s+', ' ', s)
        return s.strip()

    @staticmethod
    def _extract_cell_content(raw: str) -> str:
        """Remove the leading <td...> opening tag from a split cell fragment."""
        return re.sub(r'^.*?<td[^>]*>', '', raw, flags=re.IGNORECASE | re.DOTALL)

    def _extract_pane_tbody(self, html: str, pane_id: str) -> Optional[str]:
        """Return inner HTML of the <tbody> inside the tab-pane with id=pane_id.

        Scopes the search to the pane div by slicing from the id= attribute
        position before matching the first table-bordered <tbody>.
        Returns None when the pane or table is absent (caller yields []).
        """
        pane_marker = re.search(
            r'id\s*=\s*["\']' + re.escape(pane_id) + r'["\']',
            html,
            re.IGNORECASE,
        )
        if not pane_marker:
            return None
        scoped_html = html[pane_marker.start():]
        tbody_match = re.search(
            r'<tbody>(.*?)</tbody>',
            scoped_html,
            re.DOTALL | re.IGNORECASE,
        )
        return tbody_match.group(1) if tbody_match else None

    # ========================================================================
    # ENTITY PARSERS
    # ========================================================================

    def _extract_doc_token(self, cell_html: str) -> Optional[str]:
        """Extract the first dtaDoc token from a cell, tolerant of attribute order.

        Tries the primary pattern (name before value) then each registered fallback
        (covers reversed attribute order). Logs a warning when name="dtaDoc" is
        present but no token can be extracted.
        """
        for pattern in self.get_selector_with_fallbacks("document_token_pattern"):
            m = re.search(pattern, cell_html, re.IGNORECASE)
            if m:
                return m.group(1)
        if re.search(r'name=["\']dtaDoc["\']', cell_html, re.IGNORECASE):
            logger.warning(
                "dtaDoc input detected in cell but document_token_pattern did not match; "
                "PJUD may have changed attribute order or HTML structure."
            )
        return None

    def _extract_all_doc_tokens(self, cell_html: str) -> List[str]:
        """Extract all dtaDoc tokens from a cell, tolerant of attribute order.

        Returns deduplicated tokens in order of first appearance.
        """
        tokens: List[str] = []
        seen: set = set()
        for pattern in self.get_selector_with_fallbacks("document_token_pattern"):
            for m in re.finditer(pattern, cell_html, re.IGNORECASE):
                tok = m.group(1)
                if tok not in seen:
                    seen.add(tok)
                    tokens.append(tok)
        return tokens

    def _parse_movements_table(self, html: str) -> List[PJUDMovement]:
        """Parse movements from the #historiaCiv history table."""
        movements = []

        # Scope to historiaCiv pane before matching table (scoping bug fix).
        # Pane id comes from the selector registry — no hardcoded DOM ids.
        # Fallback: if the pane id is absent, use the registry unscoped pattern
        # so behaviour degrades gracefully rather than silently returning [].
        tbody = self._extract_pane_tbody(html, self.get_selector("movements_pane_id"))
        if tbody is None:
            table_pattern = self.get_selector("movements_table")
            table_match = re.search(table_pattern, html, re.DOTALL | re.IGNORECASE)
            if not table_match:
                return movements
            tbody = table_match.group(1)

        row_pattern = self.get_selector("movement_row_pattern")
        
        for row_match in re.finditer(row_pattern, tbody, re.DOTALL | re.IGNORECASE):
            row = row_match.group(1)
            
            # Split by </td> to get cells
            # Columns: 0=Folio, 1=Doc, 2=Anexo, 3=Etapa, 4=Tramite, 5=Desc, 6=Fecha, 7=Foja, 8=Geo
            raw_cells = re.split(r'</td>', row, flags=re.IGNORECASE)
            if len(raw_cells) < 7:
                continue
            
            def clean(s):
                s = re.sub(r'<[^>]+>', '', s)
                s = re.sub(r'\s+', ' ', s)
                return s.strip()
            
            def extract_cell(raw):
                return re.sub(r'^.*?<td[^>]*>', '', raw, flags=re.IGNORECASE | re.DOTALL)
            
            cells = [extract_cell(c) for c in raw_cells]
            
            # Extract documents from cell 1 (Doc column)
            documentos = []
            doc_cell = cells[1] if len(cells) > 1 else ''

            # Use patterns from registry
            docuS_indicator = self.get_selector("document_type_docuS")
            docuN_indicator = self.get_selector("document_type_docuN")
            anexo_pattern = self.get_selector("anexo_indicator")

            doc_token = self._extract_doc_token(doc_cell)
            if doc_token is not None:
                if docuS_indicator in doc_cell:
                    url_type = 'docuS'
                    doc_type_val = 'resolution'
                elif docuN_indicator in doc_cell:
                    url_type = 'docuN'
                    doc_type_val = 'escrito_doc'
                else:
                    url_type = 'unknown'
                    doc_type_val = 'unknown'
                documentos.append(PJUDDocument(
                    token=doc_token,
                    tipo='principal',
                    url_type=url_type,
                    doc_type=doc_type_val,
                    endpoint=f"documentos/{url_type}.php",
                    param_name='dtaDoc',
                    available=True,
                ))

            # Extract escrito_cert (docCertificadoEscrito.php?dtaCert) from the same cell
            # Disambiguation: this uses docCertificadoEscrito.php action (not docCertificadoDemanda.php)
            escrito_cert_token = self._extract_form_token(
                doc_cell,
                self.get_selector("escrito_cert_form_action"),
                self.get_selector("escrito_cert_param"),
            )
            if escrito_cert_token is not None:
                documentos.append(PJUDDocument(
                    token=escrito_cert_token,
                    tipo='cert',
                    url_type='escrito_cert',
                    doc_type='escrito_cert',
                    endpoint=f"documentos/{self.get_selector('escrito_cert_form_action')}",
                    param_name=self.get_selector("escrito_cert_param"),
                    available=True,
                ))

            # Check for anexos in cell 2
            anexo_cell = cells[2] if len(cells) > 2 else ''
            tiene_anexos = bool(re.search(anexo_pattern, anexo_cell))

            for tok in self._extract_all_doc_tokens(anexo_cell):
                documentos.append(PJUDDocument(
                    token=tok,
                    tipo='anexo',
                    url_type='docuN',
                ))
            
            # Get fecha from cell 7
            fecha = ""
            fecha_match = re.search(r'(\d{2}/\d{2}/\d{4})', cells[7] if len(cells) > 7 else '')
            if fecha_match:
                fecha = fecha_match.group(1)
            
            movements.append(PJUDMovement(
                folio=clean(cells[0]),
                etapa=clean(cells[3]) if len(cells) > 3 else "",
                tipo_tramite=clean(cells[4]) if len(cells) > 4 else "",
                descripcion=clean(cells[6]) if len(cells) > 6 else "",
                fecha=fecha,
                foja=clean(cells[8]) if len(cells) > 8 else None,
                documento_token=doc_token,
                documentos=documentos,
                tiene_documento=len(documentos) > 0,
                tiene_anexos=tiene_anexos,
            ))
        
        return movements

    def _parse_litigantes_table(self, html: str) -> List[PJUDLitigante]:
        """Parse litigants from the litigantesCiv pane.

        Columns: participante(0) rut(1) persona_type(2) nombre(3).
        """
        tbody = self._extract_pane_tbody(html, self.get_selector("litigantes_pane_id"))
        if not tbody:
            return []

        result: List[PJUDLitigante] = []
        row_pattern = self.get_selector("movement_row_pattern")

        for row_match in re.finditer(row_pattern, tbody, re.DOTALL | re.IGNORECASE):
            raw_cells = re.split(r'</td>', row_match.group(1), flags=re.IGNORECASE)
            if len(raw_cells) < 4:
                continue
            cells = [
                self._clean_cell(self._extract_cell_content(c)) for c in raw_cells
            ]
            result.append(PJUDLitigante(
                participante=cells[0],
                rut=cells[1],
                persona_type=cells[2],
                nombre=cells[3],
            ))

        return result

    def _parse_notificaciones_table(self, html: str) -> List[PJUDNotificacion]:
        """Parse notifications from the notificacionesCiv pane.

        Columns: rol(0) estado_notif(1) tipo_notif(2) fecha_tramite(3)
                 tipo_participante(4) nombre(5) tramite(6) obs_fallida(7).

        # TODO(validate-real-rich-case): column mapping unconfirmed on real data.
        """
        tbody = self._extract_pane_tbody(html, self.get_selector("notificaciones_pane_id"))
        if not tbody:
            return []

        result: List[PJUDNotificacion] = []
        row_pattern = self.get_selector("movement_row_pattern")

        for row_match in re.finditer(row_pattern, tbody, re.DOTALL | re.IGNORECASE):
            raw_cells = re.split(r'</td>', row_match.group(1), flags=re.IGNORECASE)
            if len(raw_cells) < 8:
                continue
            cells = [
                self._clean_cell(self._extract_cell_content(c)) for c in raw_cells
            ]
            result.append(PJUDNotificacion(
                rol=cells[0],
                estado_notif=cells[1],
                tipo_notif=cells[2],
                fecha_tramite=cells[3],
                tipo_participante=cells[4],
                nombre=cells[5],
                tramite=cells[6],
                obs_fallida=cells[7] if len(cells) > 7 else "",
            ))

        return result

    def _parse_escritos_table(self, html: str) -> List[PJUDEscrito]:
        """Parse filings from the escritosCiv pane.

        Columns: doc(0) anexo(1) fecha_ingreso(2) tipo_escrito(3) solicitante(4).
        tiene_documento is True when cell 0 contains a dtaDoc hidden input.
        tiene_anexo is True when cell 1 contains an anexo indicator.

        # TODO(validate-real-rich-case): column mapping unconfirmed on real data.
        """
        tbody = self._extract_pane_tbody(html, self.get_selector("escritos_pane_id"))
        if not tbody:
            return []

        result: List[PJUDEscrito] = []
        row_pattern = self.get_selector("movement_row_pattern")
        anexo_pattern = self.get_selector("anexo_indicator")

        for row_match in re.finditer(row_pattern, tbody, re.DOTALL | re.IGNORECASE):
            raw_cells = re.split(r'</td>', row_match.group(1), flags=re.IGNORECASE)
            if len(raw_cells) < 5:
                continue
            cells_raw = [self._extract_cell_content(c) for c in raw_cells]

            doc_token: Optional[str] = self._extract_doc_token(cells_raw[0])
            tiene_documento: bool = doc_token is not None
            tiene_anexo: bool = bool(
                re.search(anexo_pattern, cells_raw[1], re.IGNORECASE)
            )

            result.append(PJUDEscrito(
                fecha_ingreso=self._clean_cell(cells_raw[2]),
                tipo_escrito=self._clean_cell(cells_raw[3]),
                solicitante=self._clean_cell(cells_raw[4]),
                tiene_documento=tiene_documento,
                tiene_anexo=tiene_anexo,
                doc_token=doc_token,
            ))

        return result

    def _parse_exhortos_table(self, html: str) -> List[PJUDExhorto]:
        """Parse rogatory letters from the exhortosCiv pane.

        Columns: rol_origen(0) tipo_exhorto(1) rol_destino(2)
                 fecha_ordena(3) fecha_ingreso(4) tribunal_destino(5) estado(6).
        rol_destino text is extracted by tag-stripping the <label onclick=...> element.
        detalle_token is captured via the exhorto_detail_token_pattern selector.
        """
        tbody = self._extract_pane_tbody(html, self.get_selector("exhortos_pane_id"))
        if not tbody:
            return []

        result: List[PJUDExhorto] = []
        row_pattern = self.get_selector("movement_row_pattern")

        for row_match in re.finditer(row_pattern, tbody, re.DOTALL | re.IGNORECASE):
            raw_cells = re.split(r'</td>', row_match.group(1), flags=re.IGNORECASE)
            if len(raw_cells) < 7:
                continue
            cells_raw = [self._extract_cell_content(c) for c in raw_cells]

            # rol_destino: plain text extracted via tag-stripping (handles <label> naturally)
            rol_destino = self._clean_cell(cells_raw[2])

            # detalle_token: JWT stored for future use, NOT fetched here.
            # Pattern comes from registry (exhorto_detail_token_pattern) — no hardcoded regex.
            exhorto_token_pat = self.get_selector("exhorto_detail_token_pattern")
            token_match = re.search(exhorto_token_pat, cells_raw[2], re.IGNORECASE)
            detalle_token: Optional[str] = token_match.group(1) if token_match else None

            result.append(PJUDExhorto(
                rol_origen=self._clean_cell(cells_raw[0]),
                tipo_exhorto=self._clean_cell(cells_raw[1]),
                rol_destino=rol_destino,
                fecha_ordena=self._clean_cell(cells_raw[3]),
                fecha_ingreso=self._clean_cell(cells_raw[4]),
                tribunal_destino=self._clean_cell(cells_raw[5]),
                estado=self._clean_cell(cells_raw[6]),
                detalle_token=detalle_token,
            ))

        return result

    # ========================================================================
    # DOCUMENT DOWNLOAD (Civil-specific endpoints)
    # ========================================================================
    
    async def download_document(
        self,
        session: PJUDSession,
        document: PJUDDocument,
        output_path: Optional[str] = None,
    ) -> bytes:
        """
        Download a document PDF from PJUD.
        
        Args:
            session: Active PJUD session
            document: PJUDDocument with token and url_type
            output_path: Optional path to save the file
        
        Returns:
            PDF content as bytes
        """
        page = await self._get_page(session)
        
        # Determine the correct endpoint - from registry
        if document.url_type == 'docuS':
            endpoint = self.get_selector("document_docuS_endpoint")
        elif document.url_type == 'docuN':
            endpoint = self.get_selector("document_docuN_endpoint")
        else:
            endpoint = self.get_selector("document_default_endpoint")
        url = f"{PJUD_BASE_URL}/{endpoint}"
        
        logger.info(f"Downloading document from {url}")
        
        result = await page.evaluate(f"""
            async () => {{
                const response = await fetch('{url}?dtaDoc={document.token}', {{
                    method: 'GET',
                    credentials: 'include',
                }});
                
                if (!response.ok) {{
                    return {{ error: response.status }};
                }}
                
                const blob = await response.blob();
                const arrayBuffer = await blob.arrayBuffer();
                const bytes = new Uint8Array(arrayBuffer);
                return {{ 
                    data: Array.from(bytes),
                    contentType: response.headers.get('content-type'),
                    size: bytes.length
                }};
            }}
        """)
        
        if 'error' in result:
            raise ScrapingError(f"Download failed with status {result['error']}")
        
        pdf_bytes = bytes(result['data'])
        logger.info(f"Downloaded {result['size']} bytes")
        
        if output_path:
            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)
            logger.info(f"Saved to {output_path}")
        
        return pdf_bytes
    
    # param name keyed by doc_type stored in Document.doc_type
    _DOC_TYPE_PARAM: dict = {
        "resolution":  "dtaDoc",
        "escrito_doc": "dtaDoc",
        "docuS":       "dtaDoc",
        "docuN":       "dtaDoc",
        "texto_demanda": "valorEncTxtDmda",
        "escrito_cert":  "dtaCert",
        "cert_envio":    "dtaCert",
        "ebook":         "dtaEbook",
    }

    async def download_document_generic(
        self,
        session: PJUDSession,
        endpoint: str,
        doc_type: str,
        token: str,
    ) -> bytes:
        """Download a document using the validated live-download mechanism.

        Builds the URL as:
            {PJUD_BASE_URL}/misCausas/civil/{endpoint}?{param}={token}

        The ``endpoint`` value is the raw ``Document.pjud_endpoint`` field
        (Slice-1 parser already stripped the ``misCausas/civil/`` prefix).

        Args:
            session:  Active PJUDSession (used to restore the browser page).
            endpoint: e.g. "documentos/docuS.php"
            doc_type: e.g. "resolution", "cert_envio" — drives the query-param name.
            token:    Live JWT stored in ``Document.pjud_token``.

        Returns:
            Raw PDF bytes.

        Raises:
            DocumentTokenExpiredError: Response was 404 or content-type is text/html
                                       (token expired / invalid).
            ScrapingError:             Any other download failure.
        """
        param = self._DOC_TYPE_PARAM.get(doc_type, "dtaDoc")
        url = f"{PJUD_BASE_URL}/misCausas/civil/{endpoint}?{param}={token}"

        logger.info("download_document_generic: fetching %s", url)

        page = await self._get_page(session)

        # The fetch below must run from a page that is ALREADY on the PJUD
        # origin — a freshly restored page sits at about:blank, so a credentialed
        # fetch to pjud.cl is cross-origin and fails with "Failed to fetch".
        # Navigate to indexN.php first so the fetch is same-origin (this mirrors
        # the live-validated path).
        try:
            await page.goto(
                f"{PJUD_BASE_URL}/indexN.php",
                wait_until="domcontentloaded",
                timeout=20000,
            )
        except Exception as exc:  # noqa: BLE001 — navigation hiccup must not block the fetch
            logger.warning("download_document_generic: pre-fetch navigation failed: %s", exc)

        # Use the browser's authenticated cookies via credentials:'include'.
        # Returns a plain object so we can detect expiry before reading bytes.
        js = r"""
async () => {
    const r = await fetch(""" + f'"{url}"' + r""", { credentials: 'include' });
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    if (r.status === 404 || ct.includes('text/html')) {
        return { expired: true, status: r.status, ct: ct };
    }
    if (!r.ok) {
        return { error: true, status: r.status };
    }
    const buf = await r.arrayBuffer();
    const arr = Array.from(new Uint8Array(buf));
    return { expired: false, error: false, data: arr, size: arr.length };
}
"""
        result = await page.evaluate(js)

        if result.get("expired"):
            raise DocumentTokenExpiredError(
                f"Document token expired or invalid "
                f"(status={result.get('status')}, ct={result.get('ct')})"
            )

        if result.get("error"):
            raise ScrapingError(
                f"download_document_generic failed with HTTP {result.get('status')}"
            )

        pdf_bytes = bytes(result["data"])
        logger.info("download_document_generic: downloaded %d bytes", result["size"])
        return pdf_bytes

    async def download_movement_documents(
        self,
        session: PJUDSession,
        movement: PJUDMovement,
        output_dir: str,
        rol: str,
    ) -> List[str]:
        """
        Download all documents from a movement.
        
        Args:
            session: Active PJUD session
            movement: Movement with documents
            output_dir: Directory to save files
            rol: Case ROL for filename
        
        Returns:
            List of downloaded file paths
        """
        import os
        os.makedirs(output_dir, exist_ok=True)
        
        downloaded = []
        
        for i, doc in enumerate(movement.documentos):
            safe_rol = rol.replace("/", "-")
            filename = f"{safe_rol}_folio{movement.folio}_{doc.tipo}"
            if i > 0 or doc.tipo == 'anexo':
                filename += f"_{i+1}"
            filename += ".pdf"
            
            filepath = os.path.join(output_dir, filename)
            
            try:
                await self.download_document(session, doc, filepath)
                downloaded.append(filepath)
                logger.info(f"Downloaded: {filename}")
            except Exception as e:
                logger.error(f"Failed to download {filename}: {e}")
        
        return downloaded
    
    # ========================================================================
    # CONSULTA UNIFICADA (Public search)
    # ========================================================================
    
    async def search_cases(
        self,
        session: PJUDSession,
        rol: str,
        tipo_causa: str = "C",
        corte: str = "0",
        tribunal: str = "0",
    ) -> List[PJUDCase]:
        """
        Search for civil cases by ROL in Consulta Unificada.
        
        Args:
            session: Active PJUD session
            rol: Case number only or full ROL
            tipo_causa: C, V, E, A, F, I
            corte: Court code or "0" for all
            tribunal: Tribunal code or "0" for all
        
        Returns:
            List of matching cases
        """
        from datetime import datetime
        
        # Extract ROL parts if full ROL provided
        if "-" in rol:
            parts = rol.split("-")
            tipo_causa = parts[0]
            numero = parts[1]
            anio = parts[2] if len(parts) > 2 else str(datetime.now().year)
        else:
            numero = rol
            anio = str(datetime.now().year)
        
        page = await self._get_page(session)
        
        try:
            from app.scrapper.pjud.base import PJUD_INDEX_URL
            await page.goto(PJUD_INDEX_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            result = await page.evaluate(f"""
                async () => {{
                    const formData = new URLSearchParams();
                    formData.append('competencia', '{COMPETENCIA_CIVIL}');
                    formData.append('conCorte', '{corte}');
                    formData.append('conTribunal', '{tribunal}');
                    formData.append('conTipoBus', '1');
                    formData.append('conTipoCausa', '{tipo_causa}');
                    formData.append('conRolCausa', '{numero}');
                    formData.append('conEraCausa', '{anio}');
                    formData.append('conCaratulado', '');
                    
                    const response = await fetch('{PJUD_SEARCH_CIVIL_URL}', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: formData.toString(),
                        credentials: 'include'
                    }});
                    
                    return await response.text();
                }}
            """)
            
            return self._parse_search_results_html(result)
            
        except Exception as e:
            logger.error(f"Search error: {e}")
            raise
    
    def _parse_search_results_html(self, html: str) -> List[PJUDCase]:
        """Parse search results from Consulta Unificada."""
        cases = []
        
        # Patterns from registry
        token_pattern = self.get_selector("search_detail_token_pattern")
        row_pattern = self.get_selector("case_row_pattern")
        
        rows = re.findall(row_pattern, html, re.DOTALL | re.IGNORECASE)
        
        for row in rows:
            token_match = re.search(token_pattern, row)
            if not token_match:
                continue
            
            token = token_match.group(1)
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            if len(cells) < 4:
                continue
            
            def clean(s):
                return re.sub(r'<[^>]+>', '', s).strip()
            
            cases.append(PJUDCase(
                rol=clean(cells[1]),
                fecha_ingreso=clean(cells[2]),
                caratulado=clean(cells[3]),
                tribunal=clean(cells[4]) if len(cells) > 4 else "",
                case_token=token,
            ))
        
        return cases
