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

from playwright.async_api import (
    async_playwright,
    Browser,
    Page,
    BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
)

from app.services.pjud_session import PJUDSession
from app.scrapper.pjud.exceptions import (
    LoginError,
    ScrapingError,
    SessionExpiredError,
    SessionNotAuthenticatedError,
)


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
    """Document attached to a movement or case-level slot."""
    token: Optional[str]              # JWT token for download; None when unavailable (fa-ban)
    tipo: str                         # "principal", "anexo", "caso", "cert"
    url_type: str                     # "docuS", "docuN", "texto_demanda", "cert_envio", etc.
    descripcion: Optional[str] = None
    # Fields added for case-documents Slice 1 (all backward-compatible defaults)
    doc_type: Optional[str] = None    # Normalized type: "resolution", "escrito_doc", "escrito_cert",
                                      # "texto_demanda", "cert_envio", "ebook"
    endpoint: Optional[str] = None   # PHP endpoint, e.g. "documentos/docuS.php"
    param_name: Optional[str] = None # Query param, e.g. "dtaDoc", "dtaCert", "dtaEbook"
    available: bool = True           # False when fa-ban icon present (doc not available from PJUD)


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
class PJUDLitigante:
    """Litigant/party extracted from the Litigantes tab of a civil case detail."""
    participante: str    # Role code: "DTE.", "DDO.", "AB.DTE", "AB.DDO", …
    rut: str             # RUT with verification digit (may be "" when absent)
    persona_type: str    # "JURIDICA" or "NATURAL"
    nombre: str          # Full name or company name


@dataclass
class PJUDNotificacion:
    """Notification record extracted from the Notificaciones tab."""
    rol: str
    estado_notif: str
    tipo_notif: str
    fecha_tramite: str
    tipo_participante: str
    nombre: str
    tramite: str
    obs_fallida: str     # Observation for failed notification; may be ""


@dataclass
class PJUDEscrito:
    """Filing/escrito extracted from the Escritos por Resolver tab."""
    fecha_ingreso: str
    tipo_escrito: str
    solicitante: str
    tiene_documento: bool
    tiene_anexo: bool
    doc_token: Optional[str] = None   # JWT for document download (stored, not fetched here)


@dataclass
class PJUDExhorto:
    """Exhorto/rogatory letter extracted from the Exhortos tab."""
    rol_origen: str
    tipo_exhorto: str
    rol_destino: str           # Text inside the <label onclick=detalleExhortosCivil(...)>
    fecha_ordena: str
    fecha_ingreso: str
    tribunal_destino: str
    estado: str
    detalle_token: Optional[str] = None   # JWT from detalleExhortosCivil (stored, not fetched)


@dataclass
class PJUDCaseDetail:
    """Full case detail including movements and all tab entities."""
    case: PJUDCase
    movements: List[PJUDMovement] = field(default_factory=list)
    cuadernos: List[Dict[str, str]] = field(default_factory=list)  # [{name, token}]
    partes: Dict[str, str] = field(default_factory=dict)           # superseded by litigantes
    estado_administrativo: Optional[str] = None
    procedimiento: Optional[str] = None
    ubicacion: Optional[str] = None
    estado_procesal: Optional[str] = None
    etapa: Optional[str] = None
    raw_html: Optional[str] = None
    # New entity lists populated from the four non-Historia tabs
    litigantes: List[PJUDLitigante] = field(default_factory=list)
    notificaciones: List[PJUDNotificacion] = field(default_factory=list)
    escritos: List[PJUDEscrito] = field(default_factory=list)
    exhortos: List[PJUDExhorto] = field(default_factory=list)
    # case-documents Slice 1: case-level static doc tokens (texto_demanda, cert_envio, ebook)
    case_documents: List[PJUDDocument] = field(default_factory=list)


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
        headless: bool = True,
    ):
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
        """Stop browser and cleanup.

        When CDP-attached (per-abogado), only DISCONNECT — do NOT close the real
        Chrome's page/context, which belong to the external, human-driven browser.
        """
        cdp = getattr(self, "_cdp_attached", False)
        if self._page and not cdp:
            await self._page.close()
        if self._context and not cdp:
            await self._context.close()
        if self._browser:
            await self._browser.close()  # CDP connection: disconnects only
        if self._playwright:
            await self._playwright.stop()

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._panel_loaded = False
        self._cdp_attached = False
        logger.info(f"Browser stopped for {self.config.display_name} scraper")

    async def attach_cdp(self, cdp_url: str, session: "PJUDSession") -> Page:
        """Attach to a running, already-logged-in real Chrome over CDP and wire its
        authenticated page as this scraper's page.

        For per-abogado scraping: the PJUD session is F5-Shape-bound to the real
        browser and cannot be moved to a Playwright-launched one, so we drive the
        real Chrome directly. All scraper methods (get_my_cases, get_case_detail,
        download_document_generic) then work over CDP unchanged. Call stop() when
        done — it disconnects WITHOUT closing the real Chrome's tab.
        """
        if self._browser or self._playwright:
            await self.stop()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)

        page = None
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                if "pjud.cl" in p.url:
                    page = p
                    if "indexN.php" in p.url:
                        break
            if page is not None and "indexN.php" in page.url:
                break
        if page is None:
            await self.stop()
            raise ScrapingError("attach_cdp: no authenticated PJUD tab on the CDP target.")

        self._context = page.context
        self._page = page
        self.reuse_context = True
        self._cdp_attached = True
        self._page_session_key = id(session)
        logger.info("attach_cdp: wired real-Chrome page %s", page.url)
        return page

    async def close(self) -> None:
        """Alias for stop() for backward compatibility."""
        await self.stop()
    
    async def _get_page(self, session: Optional[PJUDSession] = None) -> Page:
        """Get a page with optional session restoration."""
        if not self._browser:
            await self.start()
        
        # Opt-in context reuse: in HEADFUL mode every new_context() opens a new
        # OS window, so for the same session reuse the existing page instead of
        # recreating the context on every call (which pops a window per case).
        # Off by default — production/headless behavior is unchanged.
        session_key = id(session) if session is not None else None
        if (
            getattr(self, "reuse_context", False)
            and self._context is not None
            and self._page is not None
            and not self._page.is_closed()
            and getattr(self, "_page_session_key", None) == session_key
        ):
            return self._page

        # Close existing context if any
        if self._context:
            await self._context.close()
            self._panel_loaded = False
        
        # Build a Playwright storage_state so cookies AND localStorage are
        # present from the very FIRST page load. PJUD's JS checks localStorage
        # (the "logged-in" token) at load time; restoring localStorage AFTER
        # navigating (the old approach) lands on the login page and breaks the
        # session. storage_state injects both before any navigation.
        from app.scrapper.pjud.browser import build_storage_state

        storage_state = build_storage_state(session) if session else None
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            storage_state=storage_state,
        )

        self._page = await self._context.new_page()
        self._page_session_key = session_key
        return self._page

    async def _safe_page_content(self, page: Page, attempts: int = 3) -> str:
        """Return page.content(), retrying if the page is mid-navigation.

        Playwright raises when page.content() is called while the page is
        still navigating (e.g. mid-redirect chain).  This helper retries up to
        *attempts* times, waiting for "domcontentloaded" between each try.

        Only navigation-related errors trigger a retry; non-transient errors
        (e.g. closed context) propagate immediately.  Recovery steps on each
        iteration are wrapped so their failures never mask the original error.
        The recovery wait is skipped on the final iteration to avoid a useless
        delay before re-raising.
        """
        if attempts < 1:
            raise ValueError("attempts must be >= 1")

        last_exc: Optional[Exception] = None
        for i in range(attempts):
            try:
                return await page.content()
            except Exception as exc:
                if "navigat" not in str(exc).lower():
                    raise
                last_exc = exc
                if i < attempts - 1:
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=5000
                        )
                    except Exception:
                        pass  # Recovery failure must not mask the original error.
                    await asyncio.sleep(1)
        raise last_exc  # type: ignore[misc]

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
        from app.scrapper.pjud.resilience.rate_limiter import pjud_action_limiter
        await pjud_action_limiter("login").acquire()

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
            
            # 4. Wait for the redirect chain to settle at the authenticated destination.
            # PJUD does: sessionN.php → indexN.php (sometimes an extra hop).
            # wait_for_url pins us to the concrete final URL instead of guessing
            # with a fixed sleep.  On timeout we fall through; the logged-in
            # checks below will raise LoginError if auth truly failed.
            # NOTE: networkidle was intentionally removed (caused timeouts) —
            # do NOT reintroduce it.
            try:
                await page.wait_for_url("**/indexN.php**", timeout=15000)
            except PlaywrightTimeoutError:
                pass  # Let the logged-in checks decide
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)
            
            current_url = page.url
            logger.info(f"Post-login URL: {current_url}")
            
            # 5. Check we're logged in (should be at indexN.php)
            if "home/index.php" in current_url:
                raise LoginError("Session not established - still on login page")
            
            # 6. Verify session by checking page content (retry-safe against mid-navigation reads)
            page_content = await self._safe_page_content(page)
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

            # Playwright Cookie objects satisfy the dict[str, Any] contract at
            # runtime; cast avoids mypy union-attr noise.
            cookie_dicts: List[Dict[str, Any]] = [dict(c) for c in cookies]

            # Use canonical factory — UTC-aware timestamps, uuid4 session_id.
            # lawyer_id is resolved by the endpoint (Slice 2); default 0 here.
            session = PJUDSession.create(
                rut=rut,
                cookies=cookie_dicts,
                local_storage=local_storage,
                auth_method="captcha",
            )

            logger.info(f"Login successful for RUT {rut_clean}")
            return session
            
        except LoginError:
            raise
        except Exception as e:
            logger.error(f"Login error: {e}")
            raise LoginError(f"Error during login: {str(e)}", cause=e)
    
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
            # A prior detail call can navigate away and break the panel, leaving
            # the cache stale. Verify the detail function is ACTUALLY present
            # before trusting the cache; if it's gone, fall through and reload.
            try:
                still_ok = await page.evaluate(
                    f"typeof {self.config.detail_function_name} === 'function'"
                )
            except Exception:
                still_ok = False
            if still_ok:
                return
            self._panel_loaded = False

        # Check if we're on indexN.php
        if 'indexN.php' not in page.url:
            await page.goto(PJUD_INDEX_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        
        # Let any client-side redirect after the initial load settle before we
        # evaluate — PJUD can navigate post-domcontentloaded, which would destroy
        # the execution context mid-evaluate.
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # Check if misCausas function exists. Retry once if a late navigation
        # destroys the execution context mid-evaluate.
        try:
            has_fn = await page.evaluate("typeof misCausas === 'function'")
        except Exception as e:
            if "Execution context was destroyed" not in str(e):
                raise
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            await asyncio.sleep(1)
            has_fn = await page.evaluate("typeof misCausas === 'function'")
        
        if has_fn:
            # Call misCausas() to load the panel. It can trigger a navigation
            # that destroys the JS execution context mid-call — that specific
            # error is EXPECTED here, so swallow it and wait for the panel below.
            try:
                await page.evaluate("misCausas()")
            except Exception as e:
                if "Execution context was destroyed" not in str(e):
                    raise
                logger.debug("misCausas() triggered navigation (context destroyed) — expected")

            # Wait for the panel content to load (AJAX completes)
            try:
                await page.wait_for_selector("#contMain", state="attached", timeout=10000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await asyncio.sleep(2)  # Extra buffer for AJAX content
                
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
            except Exception as e:
                logger.warning(f"Error waiting for panel: {e}")
        else:
            # Gather diagnostics before raising so callers can act on them.
            current_url = page.url
            try:
                jquery_present: bool = await page.evaluate(
                    "typeof window.jQuery !== 'undefined' || typeof window.$ !== 'undefined'"
                )
            except Exception:
                jquery_present = False
            looks_like_login = "home/index.php" in current_url

            logger.warning(
                f"misCausas not found — session restore failed. "
                f"url={current_url}, jquery={jquery_present}, login_page={looks_like_login}"
            )
            raise SessionNotAuthenticatedError(
                url=current_url,
                jquery_present=jquery_present,
                looks_like_login=looks_like_login,
            )
    
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
        from app.scrapper.pjud.resilience.rate_limiter import pjud_action_limiter
        await pjud_action_limiter("list").acquire()

        # Extract RUT parts
        rut = session.rut.replace("-", "").replace(".", "")
        rut_num = rut[:-1] if len(rut) > 8 else rut
        dv = rut[-1] if len(rut) > 8 else session.rut.split("-")[-1]
        
        # Use injected page if valid, otherwise create new one
        if self._page is not None and not self._page.is_closed():
            page = self._page
        else:
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
        from app.scrapper.pjud.resilience.rate_limiter import pjud_action_limiter
        await pjud_action_limiter("detail").acquire()

        # Use injected page if valid, otherwise create new one
        if self._page is not None and not self._page.is_closed():
            page = self._page
        else:
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
            
            modal_id = self.config.modal_id
            detail_html = ""

            # In a rapid backfill loop the modal is frequently still empty after
            # one open (headless timing + the detail call's own navigation). Retry
            # the whole open+poll up to 3 times, re-establishing the panel between
            # attempts, before giving up. Most cases succeed on the first attempt.
            for attempt in range(3):
                # Clear stale modal content so we detect THIS case's fresh load,
                # not the previous one (the page/modal element is reused).
                try:
                    await page.evaluate(
                        f"() => {{ const m = document.querySelector('#{modal_id}'); if (m) m.innerHTML = ''; }}"
                    )
                except Exception:
                    pass

                # Open + AJAX-load the detail modal. The call can trigger a
                # navigation that destroys the JS context — the poll tolerates that.
                try:
                    await page.evaluate(f"{fn_name}('{case_token}')")
                except Exception:
                    pass

                detail_html = ""
                for _ in range(30):  # ~15s per attempt
                    await asyncio.sleep(0.5)
                    try:
                        detail_html = await page.evaluate(f"""
                            () => {{
                                const modal = document.querySelector('#{modal_id}');
                                return modal ? modal.innerHTML : '';
                            }}
                        """)
                    except Exception:
                        # Execution context destroyed mid-navigation — wait + retry.
                        continue
                    if detail_html and len(detail_html) >= 100:
                        break

                if detail_html and len(detail_html) >= 100:
                    break  # got the detail — done

                # Empty modal: re-establish the panel and retry the open.
                logger.warning(
                    "detail modal empty (attempt %d/3) — reloading panel and retrying",
                    attempt + 1,
                )
                self._panel_loaded = False
                try:
                    await self._ensure_panel_loaded(page)
                except Exception:
                    pass

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
        
        # Use injected page if valid, otherwise create new one
        if self._page is not None and not self._page.is_closed():
            page = self._page
        else:
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
