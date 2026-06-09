"""
PJUD Base Scraper with Template Method pattern.

Provides shared login, session management, pagination, and browser lifecycle.
Competency-specific scrapers inherit and implement abstract methods.
"""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from app.scrapper.session_manager import SessionManager, PJUDSession
from app.scrapper.pjud.exceptions import LoginError, SessionExpiredError, ScrapingError


logger = logging.getLogger(__name__)


# ============================================================================
# PJUD CONSTANTS
# ============================================================================

PJUD_BASE_URL = "https://oficinajudicialvirtual.pjud.cl"
PJUD_HOME_URL = f"{PJUD_BASE_URL}/home/index.php"
PJUD_SESSION_URL = f"{PJUD_BASE_URL}/sessionN.php"
PJUD_INDEX_URL = f"{PJUD_BASE_URL}/indexN.php"


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class PJUDCase:
    """Case data extracted from PJUD search results."""
    rol: str                          # e.g., "C-1234-2024"
    tribunal: str                     # e.g., "1er Juzgado Civil de Santiago"
    caratulado: str                   # e.g., "BANCO/DEMANDADO"
    fecha_ingreso: str                # e.g., "31/05/2024"
    case_token: Optional[str] = None  # JWT token for detail request
    estado_cuaderno: Optional[str] = None
    cuaderno: Optional[str] = None
    institucion: Optional[str] = None
    
    @property
    def tipo(self) -> str:
        """Extract case type from ROL (e.g., 'C' from 'C-1234-2024')."""
        return self.rol.split("-")[0] if "-" in self.rol else ""
    
    @property 
    def numero(self) -> str:
        """Extract case number from ROL."""
        parts = self.rol.split("-")
        return parts[1] if len(parts) > 1 else ""
    
    @property
    def anio(self) -> str:
        """Extract year from ROL."""
        parts = self.rol.split("-")
        return parts[2] if len(parts) > 2 else ""


@dataclass
class PJUDDocument:
    """Document attached to a movement."""
    token: str                        # JWT token for download
    tipo: str                         # "principal", "anexo"
    url_type: str                     # "docuS" (resolucion), "docuN" (escrito)
    descripcion: Optional[str] = None


@dataclass
class PJUDMovement:
    """Movement/actuacion data extracted from PJUD case detail."""
    folio: str
    fecha: str
    tipo_tramite: str
    descripcion: str
    cuaderno: Optional[str] = None
    documento_url: Optional[str] = None
    documento_token: Optional[str] = None  # Primary document token (legacy)
    etapa: Optional[str] = None
    foja: Optional[str] = None
    documentos: List[PJUDDocument] = field(default_factory=list)
    tiene_documento: bool = False
    tiene_anexos: bool = False


@dataclass
class PJUDCaseDetail:
    """Full case detail including movements."""
    case: PJUDCase
    movements: List[PJUDMovement] = field(default_factory=list)
    cuadernos: List[Dict[str, str]] = field(default_factory=list)  # [{name, token}]
    partes: Dict[str, str] = field(default_factory=dict)
    estado_administrativo: Optional[str] = None
    procedimiento: Optional[str] = None
    ubicacion: Optional[str] = None
    estado_procesal: Optional[str] = None
    etapa: Optional[str] = None
    raw_html: Optional[str] = None


@dataclass
class CompetencyConfig:
    """Configuration for a specific competency (Civil, Laboral, Penal)."""
    name: str                    # "civil", "laboral", "penal"
    display_name: str            # "Civil", "Laboral", "Penal"
    competencia_code: str        # "3" for Civil
    table_body_id: str           # DOM ID for case list table
    modal_id: str                # DOM ID for detail modal
    detail_function_name: str    # JS function to call for detail
    cases_endpoint: str          # PHP endpoint for cases list
    detail_endpoint: str         # PHP endpoint for case detail
    search_endpoint: str         # PHP endpoint for public search


# ============================================================================
# ABSTRACT BASE SCRAPER
# ============================================================================

class PJUDBaseScraper(ABC):
    """
    Abstract base class for PJUD competency scrapers.
    
    Uses Template Method pattern:
    - Concrete methods handle shared logic (login, browser, pagination)
    - Abstract methods handle competency-specific logic (selectors, parsing)
    
    Subclasses must implement:
    - _get_competency_config() -> CompetencyConfig
    - _parse_cases_html(html: str) -> List[PJUDCase]
    - _parse_case_detail_html(html: str, case_token: str) -> PJUDCaseDetail
    """
    
    def __init__(
        self,
        session_manager: Optional[SessionManager] = None,
        headless: bool = True,
    ):
        self.session_manager = session_manager or SessionManager()
        self.headless = headless
        
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._panel_loaded: bool = False
        
        # Cache competency config
        self._config: Optional[CompetencyConfig] = None
    
    @property
    def config(self) -> CompetencyConfig:
        """Get competency configuration (cached)."""
        if self._config is None:
            self._config = self._get_competency_config()
        return self._config
    
    # ========================================================================
    # ABSTRACT METHODS - Subclasses must implement
    # ========================================================================
    
    @abstractmethod
    def _get_competency_config(self) -> CompetencyConfig:
        """Return configuration for this competency."""
        ...
    
    @abstractmethod
    def _parse_cases_html(self, html: str) -> List[PJUDCase]:
        """Parse HTML response from cases endpoint into case list."""
        ...
    
    @abstractmethod
    def _parse_case_detail_html(self, html: str, case_token: str) -> PJUDCaseDetail:
        """Parse case detail HTML including movements."""
        ...
    
    # ========================================================================
    # BROWSER LIFECYCLE (Concrete)
    # ========================================================================
    
    async def start(self) -> None:
        """Start browser instance."""
        if self._browser:
            return
        
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        logger.info(f"Browser started for {self.config.display_name} scraper")
    
    async def stop(self) -> None:
        """Stop browser and cleanup."""
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._panel_loaded = False
        logger.info(f"Browser stopped for {self.config.display_name} scraper")
    
    async def close(self) -> None:
        """Alias for stop() for backward compatibility."""
        await self.stop()
    
    async def _get_page(self, session: Optional[PJUDSession] = None) -> Page:
        """Get a page with optional session restoration."""
        if not self._browser:
            await self.start()
        
        # Close existing context if any
        if self._context:
            await self._context.close()
            self._panel_loaded = False
        
        # Create new context
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Restore cookies if session provided
        if session and session.cookies:
            await self._context.add_cookies(session.cookies)
        
        self._page = await self._context.new_page()
        
        # Restore localStorage if session provided
        if session and session.local_storage:
            await self._page.goto(PJUD_HOME_URL)
            await self._page.evaluate(
                f"Object.assign(localStorage, {session.local_storage})"
            )
        
        return self._page
    
    # ========================================================================
    # AUTHENTICATION (Concrete)
    # ========================================================================
    
    async def login_with_token(
        self,
        rut: str,
        password: str,
        captcha_token: str,
    ) -> PJUDSession:
        """
        Login to PJUD using a captcha token provided by the frontend.
        
        IMPORTANT: Uses form submit (not fetch) to properly establish session.
        
        Args:
            rut: RUT with verification digit (e.g., "16021492-9")
            password: PJUD password
            captcha_token: reCAPTCHA token from frontend
        
        Returns:
            PJUDSession with cookies for scraping
        """
        # Clean RUT - remove dots, hyphens, and verification digit
        rut_clean = rut.replace("-", "").replace(".", "")
        if len(rut_clean) > 8:
            rut_clean = rut_clean[:-1]  # Remove verification digit
        
        page = await self._get_page()
        
        try:
            # 1. Navigate to home to get JWT token
            logger.info(f"Logging in RUT {rut_clean}...")
            await page.goto(PJUD_HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(1)
            
            # 2. Get the JWT token from the hidden field (dynamically find it by value pattern)
            jwt_data = await page.evaluate("""
                () => {
                    // Find hidden input whose value starts with 'eyJ' (JWT header)
                    const inputs = document.querySelectorAll('input[type="hidden"]');
                    for (const input of inputs) {
                        if (input.value && input.value.startsWith('eyJ') && input.name.length === 40) {
                            return { name: input.name, value: input.value };
                        }
                    }
                    return null;
                }
            """)
            
            if not jwt_data:
                raise LoginError("No JWT token found on login page")
            
            jwt_field_name = jwt_data['name']
            jwt_token = jwt_data['value']
            logger.info(f"JWT token found in field '{jwt_field_name}': {bool(jwt_token)}")
            
            # 3. Login via FORM SUBMIT (not fetch!) - this is critical
            await page.evaluate(f"""
                () => {{
                    const form = document.createElement('form');
                    form.method = 'POST';
                    form.action = '{PJUD_SESSION_URL}';
                    
                    const fields = {{
                        '{jwt_field_name}': '{jwt_token}',
                        'g-recaptcha-response-seg-clave_hn': '{captcha_token}',
                        'rut': '{rut_clean}',
                        'password': '{password}'
                    }};
                    
                    for (const [name, value] of Object.entries(fields)) {{
                        const input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = name;
                        input.value = value;
                        form.appendChild(input);
                    }}
                    
                    document.body.appendChild(form);
                    form.submit();
                }}
            """)
            
            # 4. Wait for navigation to complete
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
            
            current_url = page.url
            logger.info(f"Post-login URL: {current_url}")
            
            # 5. Check we're logged in (should be at indexN.php)
            if "home/index.php" in current_url:
                raise LoginError("Session not established - still on login page")
            
            # 6. Verify session by checking page content
            page_content = await page.content()
            has_user = (
                rut_clean in page_content or 
                'misCausas' in page_content.lower() or
                'indexN.php' in current_url or
                'Mis Causas' in page_content or
                'cerrar sesion' in page_content.lower() or
                'salir' in page_content.lower()
            )
            
            if not has_user and 'home/index.php' in current_url:
                raise LoginError("Login succeeded but session not active")
            
            # 7. Extract session data
            cookies = await self._context.cookies()
            local_storage = await page.evaluate("JSON.stringify(localStorage)")
            
            session = PJUDSession(
                rut=rut,
                cookies=cookies,
                local_storage=local_storage,
                created_at=datetime.now(),
            )
            
            # 8. Cache session
            await self.session_manager.save_session(session)
            
            logger.info(f"Login successful for RUT {rut_clean}")
            return session
            
        except LoginError:
            raise
        except Exception as e:
            logger.error(f"Login error: {e}")
            raise LoginError(f"Error during login: {str(e)}", cause=e)
    
    async def get_session(self, rut: str) -> Optional[PJUDSession]:
        """Get cached session if valid."""
        return await self.session_manager.get_session(rut)
    
    # ========================================================================
    # PANEL LOADING (Template Method - uses abstract config)
    # ========================================================================
    
    async def _ensure_panel_loaded(self, page: Page) -> None:
        """
        Ensure the competency panel is loaded in the page.
        
        The PJUD site uses a SPA-like pattern where content loads
        into #contMain via AJAX. The native JS functions for
        detail retrieval only work when this content is loaded.
        """
        if self._panel_loaded:
            return
        
        # Check if we're on indexN.php
        if 'indexN.php' not in page.url:
            await page.goto(PJUD_INDEX_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        
        # Check if misCausas function exists
        has_fn = await page.evaluate("typeof misCausas === 'function'")
        
        if has_fn:
            # Call misCausas() to load the panel
            await page.evaluate("misCausas()")
            await asyncio.sleep(5)
            
            # Verify content loaded
            content_loaded = await page.evaluate("""
                () => {
                    const cont = document.querySelector('#contMain');
                    return cont && cont.innerHTML.length > 1000;
                }
            """)
            
            if content_loaded:
                self._panel_loaded = True
                logger.info(f"{self.config.display_name} panel loaded")
            else:
                logger.warning("misCausas() called but content not loaded")
        else:
            logger.warning("misCausas function not found - may not be logged in")
    
    # ========================================================================
    # PAGINATION HELPERS (Concrete)
    # ========================================================================
    
    def _parse_pagination_info(self, html: str) -> tuple[int, int]:
        """
        Extract total count and total pages from HTML response.
        
        Returns:
            (total_count, total_pages)
        """
        # Pattern: Total de registros: <b>2855</b>
        total_match = re.search(r'Total de registros:\s*<b>(\d+)</b>', html, re.IGNORECASE)
        total_count = int(total_match.group(1)) if total_match else 0
        
        # Pattern: onclick="pagina(191,3);">Fin
        fin_match = re.search(r'onclick="pagina\((\d+),\d+\);">Fin', html, re.IGNORECASE)
        total_pages = int(fin_match.group(1)) if fin_match else 1
        
        # Fallback: calculate from total_count (15 per page)
        if total_pages == 1 and total_count > 15:
            total_pages = (total_count + 14) // 15
        
        return total_count, total_pages
    
    # ========================================================================
    # PUBLIC API (Template Methods)
    # ========================================================================
    
    async def get_my_cases(
        self,
        session: PJUDSession,
        tipo_causa: str = "M",
        estado: str = "",
        max_pages: int = 0,
        year: str = "",
        fecha_desde: str = "",
        fecha_hasta: str = "",
    ) -> List[PJUDCase]:
        """
        Get all cases where the logged-in lawyer is a party.
        Fetches ALL pages automatically.
        
        This is a template method - uses abstract _parse_cases_html().
        
        Args:
            session: Active PJUD session
            tipo_causa: "M" for own cases (Causas Propias)
            estado: Filter by estado or "" for all
            max_pages: Maximum pages to fetch (0 = all)
            year: Filter by specific year
            fecha_desde: Filter from date (dd/mm/yyyy)
            fecha_hasta: Filter to date (dd/mm/yyyy)
        
        Returns:
            List of cases
        """
        # Extract RUT parts
        rut = session.rut.replace("-", "").replace(".", "")
        rut_num = rut[:-1] if len(rut) > 8 else rut
        dv = rut[-1] if len(rut) > 8 else session.rut.split("-")[-1]
        
        page = await self._get_page(session)
        
        try:
            await self._ensure_panel_loaded(page)
            
            all_cases = []
            current_page = 1
            total_pages = None
            
            while True:
                logger.info(f"Fetching {self.config.display_name} cases page {current_page}...")
                
                html = await self._fetch_cases_page(
                    page, rut_num, dv, tipo_causa, year, 
                    fecha_desde, fecha_hasta, current_page
                )
                
                if isinstance(html, str) and html.startswith('ERROR:'):
                    raise ScrapingError(f"AJAX error: {html}")
                
                # Parse total on first page
                if total_pages is None:
                    total_count, total_pages = self._parse_pagination_info(html)
                    logger.info(f"Total cases: {total_count}, Total pages: {total_pages}")
                
                # Parse cases (delegated to subclass)
                page_cases = self._parse_cases_html(html)
                all_cases.extend(page_cases)
                
                logger.info(f"Page {current_page}/{total_pages}: {len(page_cases)} cases")
                
                if current_page >= total_pages:
                    break
                if max_pages > 0 and current_page >= max_pages:
                    logger.info(f"Reached max_pages limit ({max_pages})")
                    break
                
                current_page += 1
            
            return all_cases
            
        except Exception as e:
            logger.error(f"Error getting cases: {e}")
            raise
    
    @abstractmethod
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
        """
        Fetch a single page of cases from the endpoint.
        
        Returns HTML response string.
        """
        ...
    
    async def get_case_detail(
        self,
        session: PJUDSession,
        case_token: str,
    ) -> PJUDCaseDetail:
        """
        Get full case detail including movements.
        
        This is a template method - uses abstract _parse_case_detail_html().
        
        Args:
            session: Active PJUD session
            case_token: JWT token from search results
        
        Returns:
            Full case detail with movements
        """
        page = await self._get_page(session)
        
        try:
            await self._ensure_panel_loaded(page)
            
            # Check if the detail function exists
            fn_name = self.config.detail_function_name
            fn_exists = await page.evaluate(f"typeof {fn_name} === 'function'")
            
            if not fn_exists:
                raise ScrapingError(
                    f"{fn_name} function not found - panel not loaded"
                )
            
            # Call the native function
            await page.evaluate(f"{fn_name}('{case_token}')")
            await asyncio.sleep(3)
            
            # Capture modal content
            modal_id = self.config.modal_id
            detail_html = await page.evaluate(f"""
                () => {{
                    const modal = document.querySelector('#{modal_id}');
                    return modal ? modal.innerHTML : '';
                }}
            """)
            
            if not detail_html or len(detail_html) < 100:
                raise ScrapingError("Modal content empty - detail failed to load")
            
            logger.info(f"Captured detail modal: {len(detail_html)} chars")
            return self._parse_case_detail_html(detail_html, case_token)
            
        except Exception as e:
            logger.error(f"Error getting case detail: {e}")
            raise
    
    async def get_cases_count(
        self,
        session: PJUDSession,
        year: str = "",
        tipo_causa: str = "M",
    ) -> tuple[int, int]:
        """
        Get total count and pages WITHOUT fetching all data.
        
        Args:
            session: Active PJUD session
            year: Optional year filter
            tipo_causa: "M" for own cases
        
        Returns:
            (total_count, total_pages)
        """
        rut = session.rut.replace("-", "").replace(".", "")
        rut_num = rut[:-1] if len(rut) > 8 else rut
        dv = rut[-1] if len(rut) > 8 else session.rut.split("-")[-1]
        
        page = await self._get_page(session)
        await self._ensure_panel_loaded(page)
        
        html = await self._fetch_cases_page(
            page, rut_num, dv, tipo_causa, year, "", "", 1
        )
        
        if isinstance(html, str) and html.startswith('ERROR:'):
            raise ScrapingError(f"AJAX error: {html}")
        
        return self._parse_pagination_info(html)
    
    async def get_my_cases_recent(
        self,
        session: PJUDSession,
        years: int = 2,
        tipo_causa: str = "M",
    ) -> List[PJUDCase]:
        """
        Get cases from the last N years.
        
        Args:
            session: Active PJUD session
            years: Number of years to fetch (default: 2)
            tipo_causa: "M" for own cases
        
        Returns:
            List of cases from the specified period
        """
        current_year = datetime.now().year
        all_cases = []
        
        for i in range(years):
            year = str(current_year - i)
            logger.info(f"Fetching cases for year {year}...")
            
            year_cases = await self.get_my_cases(
                session=session,
                tipo_causa=tipo_causa,
                year=year,
            )
            
            logger.info(f"Year {year}: {len(year_cases)} cases")
            all_cases.extend(year_cases)
        
        logger.info(f"Total cases from last {years} year(s): {len(all_cases)}")
        return all_cases
