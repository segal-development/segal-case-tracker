"""
PJUD Penal competency scraper.

Inherits from PJUDBaseScraper and implements penal-specific logic.
Uses SelectorRegistry for configurable, hot-reloadable selectors.
"""

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
    CompetencyConfig,
    PJUD_BASE_URL,
)
from app.scrapper.pjud.exceptions import ScrapingError
from app.scrapper.pjud.selectors import SelectorRegistry
from app.services.pjud_session import PJUDSession


logger = logging.getLogger(__name__)


# Penal-specific constants
PJUD_MY_CASES_PENAL_URL = f"{PJUD_BASE_URL}/misCausas/penal/consultaMisCausasPenal.php"
PJUD_MY_CASE_DETAIL_URL = f"{PJUD_BASE_URL}/misCausas/penal/modal/misCausasPenal.php"

COMPETENCIA_PENAL = "5"


# Global selector registry instance (shared across all PenalScraper instances)
_selector_registry: Optional[SelectorRegistry] = None


def get_selector_registry() -> SelectorRegistry:
    """Get or create the global selector registry."""
    global _selector_registry
    if _selector_registry is None:
        _selector_registry = SelectorRegistry()
        # Pre-load penal selectors
        _selector_registry.load("penal")
    return _selector_registry


class PenalScraper(PJUDBaseScraper):
    """
    Scraper for PJUD Penal portal.
    
    Implements penal-specific parsing and selectors while inheriting
    shared login, session, and pagination logic from PJUDBaseScraper.
    
    Uses SelectorRegistry for configurable selectors that can be
    hot-reloaded without code changes when PJUD updates their HTML.
    
    Note: Penal cases have 8 columns including Institucion.
    Penal uses RUC (Rol Unico de Causa) instead of ROL in some contexts.
    """
    
    def __init__(self, *args, selector_registry: Optional[SelectorRegistry] = None, **kwargs):
        """
        Initialize PenalScraper.
        
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
        return self._selector_registry.get("penal", name)
    
    def get_selector_with_fallbacks(self, name: str) -> List[str]:
        """Get a selector with all fallbacks."""
        return self._selector_registry.get_with_fallbacks("penal", name)
    
    def _get_competency_config(self) -> CompetencyConfig:
        """Return Penal competency configuration from registry."""
        registry = self._selector_registry
        
        return CompetencyConfig(
            name="penal",
            display_name="Penal",
            competencia_code=registry.get("penal", "competencia_code"),
            table_body_id=registry.get("penal", "table_body_id"),
            modal_id=registry.get("penal", "modal_id"),
            detail_function_name=registry.get("penal", "detail_function_name"),
            cases_endpoint=registry.get("penal", "cases_endpoint"),
            detail_endpoint=registry.get("penal", "detail_endpoint"),
            search_endpoint=registry.get("penal", "search_endpoint"),
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
        """Fetch a single page of penal cases."""
        result = await page.evaluate(f"""
            () => {{
                return new Promise((resolve) => {{
                    $.ajax({{
                        url: '{self.config.cases_endpoint}',
                        type: 'POST',
                        data: {{
                            rutMisCauPen: '{rut_num}',
                            dvMisCauPen: '{dv}',
                            tipoMisCauPen: '0',
                            rolMisCauPen: '',
                            anhoMisCauPen: '{year}',
                            fecDesdeMisCauPen: '{fecha_desde}',
                            fecHastaMisCauPen: '{fecha_hasta}',
                            'tipCausaMisCauPen[]': '{tipo_causa}',
                            nombreMisCauPen: '',
                            apePatMisCauPen: '',
                            apeMatMisCauPen: '',
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
        """
        Parse HTML response from penal cases endpoint.
        
        Penal has 8 columns:
        Actions, RUC/ROL, Tribunal, Caratulado, Fecha, Estado, Cuaderno, Institucion
        """
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
            # Penal columns: Actions, RUC/ROL, Tribunal, Caratulado, Fecha, Estado, Cuaderno, Institucion
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            if len(cells) < 5:
                continue
            
            def clean(s):
                return re.sub(r'<[^>]+>', '', s).strip()
            
            rol = clean(cells[1])
            
            # Skip cases without valid ROL/RUC
            # Penal can use various formats: RUC, RIT, or standard ROL
            if not rol:
                continue
            
            # Accept ROL patterns like: O-123-2026, RUC-1234567-8, RIT 123-2026
            if not re.match(r'^[A-Z]+-\d+-\d{4}$|^RUC-\d+-\d$|^RIT\s*\d+-\d{4}$', rol):
                # Try to clean RUC format
                if 'RUC' in rol.upper() or 'RIT' in rol.upper():
                    # Accept it as-is for penal
                    pass
                else:
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
        
        logger.info(f"Parsed {len(cases)} penal cases from HTML")
        return cases
    
    def _parse_case_detail_html(self, html: str, case_token: str) -> PJUDCaseDetail:
        """Parse penal case detail HTML including movements."""
        
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
        
        # ROL/RUC/RIT - Penal uses different identifiers
        rol_match = re.search(r'<strong>(?:ROL|RUC|RIT):</strong>\s*([^<]+)', html, re.IGNORECASE)
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
        
        # Validate ROL/RUC/RIT format (more lenient for penal)
        if not rol:
            logger.warning("Empty ROL/RUC/RIT in case detail")
            raise ValueError("Empty ROL/RUC/RIT in case detail")
        
        logger.info(f"Parsed penal case {rol}: {len(movements)} movements, {len(cuadernos)} cuadernos")
        
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
        )
    
    def _parse_movements_table(self, html: str) -> List[PJUDMovement]:
        """Parse movements from the history table."""
        movements = []
        
        # Find the history table - pattern from registry
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
            doc_token_pattern = self.get_selector("document_token_pattern")
            docuS_indicator = self.get_selector("document_type_docuS")
            docuN_indicator = self.get_selector("document_type_docuN")
            anexo_pattern = self.get_selector("anexo_indicator")
            
            doc_token = None
            doc_match = re.search(doc_token_pattern, doc_cell)
            if doc_match:
                doc_token = doc_match.group(1)
                if docuS_indicator in doc_cell:
                    url_type = 'docuS'
                elif docuN_indicator in doc_cell:
                    url_type = 'docuN'
                else:
                    url_type = 'unknown'
                documentos.append(PJUDDocument(
                    token=doc_token,
                    tipo='principal',
                    url_type=url_type,
                ))
            
            # Check for anexos in cell 2
            anexo_cell = cells[2] if len(cells) > 2 else ''
            tiene_anexos = bool(re.search(anexo_pattern, anexo_cell))
            
            for anexo_match in re.finditer(doc_token_pattern, anexo_cell):
                documentos.append(PJUDDocument(
                    token=anexo_match.group(1),
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
    
    # ========================================================================
    # DOCUMENT DOWNLOAD (Penal-specific endpoints)
    # ========================================================================
    
    async def download_document(
        self,
        session: PJUDSession,
        document: PJUDDocument,
        output_path: Optional[str] = None,
    ) -> bytes:
        """
        Download a document PDF from PJUD Penal.
        
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
