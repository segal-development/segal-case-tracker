"""
PJUD Base Scraper with Template Method pattern.

Provides shared login, session management, pagination, and browser lifecycle.
Competency-specific scrapers inherit and implement abstract methods.
"""

import asyncio
import json
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

# User-agent matching the real system Chrome installed on this Mac (v149).
# Generated from: /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --version
_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"  # match bundled Chromium: UA 149 mismatched the real browser and tripped Shape (regression 78246de); 120 is live-validated
)

from app.config import settings
from app.services.pjud_session import PJUDSession
from app.scrapper.pjud.exceptions import (
    InvalidCredentialsError,
    LoginError,
    ScrapingError,
    SessionExpiredError,
    SessionNotAuthenticatedError,
    ShapeChallengeError,
)


logger = logging.getLogger(__name__)


# ============================================================================
# PJUD CONSTANTS
# ============================================================================

PJUD_BASE_URL = "https://oficinajudicialvirtual.pjud.cl"
PJUD_HOME_URL = f"{PJUD_BASE_URL}/home/index.php"
PJUD_SESSION_URL = f"{PJUD_BASE_URL}/sessionN.php"
PJUD_INDEX_URL = f"{PJUD_BASE_URL}/indexN.php"


# ---------------------------------------------------------------------------
# Login POST builder — runs inside the login page.
#
# PJUD rotates the login POST field names DAILY (their field-rotation.php): the
# RUT, password and JWT are sent under obfuscated keys such as 'f_6b5373856e8c'
# that change every day, alongside a per-load constant field and an always-empty
# honeypot (sessionN.php cuts the session if a bot fills it). So instead of
# hardcoding 'rut'/'password', we PARSE the day's mapping straight from PJUD's own
# login handler — its `postData['<key>'] = <source>;` block — and reproduce it.
#
# It MUST be submitted as a full-page form NAVIGATION (never fetch/XHR): a real
# navigation lets F5 Shape/TSPD run its challenge transparently in the browser,
# exactly like a user's click. An XHR POST instead receives the raw TSPD challenge
# script as its response body and never establishes the session.
#
# Credentials/JWT/token are passed in as an argument object (never string-
# interpolated, so a quote/backslash in a password can't corrupt the JS). Every
# credential-critical field (rut, clave, JWT, recaptcha) MUST resolve to a
# non-empty value; otherwise we return {ok:false} and the caller raises a clear
# LoginError — never a silent empty-field POST, which PJUD would echo as a
# credential error and trip a false "clave inválida" supervisor alert. Returns
# roleMap (key → source category) so a future rotation drift is visible in logs
# without ever logging the values themselves.
# ---------------------------------------------------------------------------
_BUILD_LOGIN_POST_JS = r"""({ rut, clave, token, jwt }) => {
    const scripts = [...document.querySelectorAll('script:not([src])')]
        .map(s => s.textContent).join('\n');
    const i0 = scripts.indexOf('var postData');
    if (i0 === -1) return { ok: false, reason: 'postData block not found' };
    // Anchor the block end on the handler's own #formSSGGNN .load() call so an
    // unrelated .load( in another inline script can't truncate the mapping.
    const anchor = scripts.indexOf('formSSGGNN', i0);
    const i1 = scripts.indexOf('.load(', anchor === -1 ? i0 : anchor);
    if (i1 === -1) return { ok: false, reason: '.load() terminator not found' };
    const block = scripts.slice(i0, i1);
    const endpoint = (scripts.slice(i1).match(/\.load\(\s*["']([^"']+)["']/) || [])[1] || '../sessionN.php';
    const frm = document.forms['frm'];
    const re = /postData\[['"]([^'"]+)['"]\]\s*=\s*([^;]+);/g;
    const values = {}, roleMap = {}, roles = {}, unknown = [];
    let m;
    while ((m = re.exec(block)) !== null) {
        const key = m[1], src = m[2].trim();
        let val = '', cat;
        if (src === 'rut') { val = rut; cat = 'rut'; roles.rut = val; }
        else if (src === 'clave') { val = clave; cat = 'clave'; roles.clave = val; }
        else if (src === 'acceso_paso') { val = jwt; cat = 'jwt'; roles.jwt = val; }
        else if (src === 'g_recaptcha_clave_paso') { val = token; cat = 'recaptcha'; roles.token = val; }
        else if (src === 'action_paso') { val = frm && frm['action'] ? frm['action'].value : ''; cat = 'action'; }
        else if (/^['"]/.test(src)) { val = src.replace(/^['"]|['"]$/g, ''); cat = 'literal'; }
        else {
            const dm = src.match(/#([A-Za-z0-9_]+)/);
            if (dm) { const el = document.getElementById(dm[1]); val = el ? (el.value || '') : ''; cat = 'dom'; }
            else { unknown.push(src); cat = 'unknown'; }
        }
        values[key] = val; roleMap[key] = cat;
    }
    if (Object.keys(values).length === 0) return { ok: false, reason: 'no postData fields parsed' };
    // Fail loud if any credential-critical field didn't resolve to a real value.
    const missing = [];
    if (!roles.rut) missing.push('rut');
    if (!roles.clave) missing.push('clave');
    if (!roles.token) missing.push('recaptcha');
    if (!roles.jwt) missing.push('jwt');
    if (missing.length) return { ok: false,
        reason: 'unresolved required fields: ' + missing.join(',') +
                (unknown.length ? '; unknown sources: ' + unknown.join(',') : '') };
    const url = new URL(endpoint, location.href).href;
    const form = document.createElement('form');
    form.method = 'POST'; form.action = url;
    for (const [name, value] of Object.entries(values)) {
        const input = document.createElement('input');
        input.type = 'hidden'; input.name = name; input.value = value;
        form.appendChild(input);
    }
    document.body.appendChild(form);
    form.submit();
    return { ok: true, fields: Object.keys(values), endpoint: url, roleMap };
}"""


# Matches PJUD's "wrong password / wrong user" messages on the login page, so a
# stuck-on-login can be classified as bad credentials (the lawyer must update
# their clave) vs. a rejected captcha token or a transient error (retry-able).
# Pattern is broad on purpose; the captured snippet lets us refine on real cases.
_LOGIN_CRED_ERROR_RE = re.compile(
    r"(clave|contraseña|usuario|credencial(?:es)?|rut)"
    r"[^<>{}]{0,40}"
    r"(incorrect\w*|erróne\w*|inválid\w*|no\s+coincide\w*|no\s+válid\w*|bloquead\w*)",
    re.IGNORECASE,
)


def classify_login_failure(page_content: str) -> tuple:
    """Inspect a stuck-on-login page; return (is_invalid_credentials, short_msg).

    ``is_invalid_credentials`` is True when the page shows a wrong-password /
    wrong-user style message — meaning a retry is pointless and the lawyer must
    update their clave. ``short_msg`` is a cleaned snippet for logging/reporting.
    """
    if not isinstance(page_content, str) or not page_content:
        return False, ""
    text = re.sub(r"<[^>]+>", " ", page_content)
    text = re.sub(r"\s+", " ", text).strip()
    m = _LOGIN_CRED_ERROR_RE.search(text)
    if m:
        start = max(0, m.start() - 30)
        return True, text[start:m.end() + 50].strip()
    return False, ""


def detect_shape_challenge(page_content: str | None, url: str = "") -> tuple[bool, str]:
    """Detect PJUD Shape/TSPD challenge pages using conservative markers."""
    url_text = url or ""
    if re.search(r"(?:^|[/?&_=.-])TSPD(?:_101)?(?:$|[/?&_=.-])", url_text, re.IGNORECASE):
        return True, "TSPD URL"

    if not isinstance(page_content, str) or not page_content:
        return False, ""

    text = re.sub(r"<[^>]+>", " ", page_content)
    text = re.sub(r"\s+", " ", text).strip().lower()
    html = page_content.lower()

    if "tspd_101" in html and ("failureconfig" in html or "what code is in the image" in text or "support id" in text):
        return True, "TSPD_101 challenge"
    if "failureconfig" in html and ("support" in text or "tspd" in html):
        return True, "failureConfig/support"
    if "what code is in the image" in text and ("tspd" in html or "support id" in text):
        return True, "Shape image challenge"
    if "tspd" in html and ("support id" in text or "what code is in the image" in text):
        return True, "TSPD challenge content"

    return False, ""


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
        self._persistent_context = False
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
        """Start browser instance.

        Uses the system Chrome when ``PJUD_CHROME_PATH`` is set in env (more
        stealth — same Chrome humans use). Falls back to Playwright's bundled
        Chromium otherwise.
        """
        if self._browser or self._context:
            return
        
        chrome_path = settings.PJUD_CHROME_PATH or None
        user_data_dir = settings.PJUD_USER_DATA_DIR or None
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        if chrome_path:
            launch_args += [
                "--disable-sync",              # No Chrome sync noise
                "--no-first-run",              # Skip first-run dialogs
                "--hide-scrollbars",
                "--mute-audio",
            ]
            logger.info("Using system Chrome: %s", chrome_path)
        else:
            logger.info("Using Playwright bundled Chromium")

        self._playwright = await async_playwright().start()
        if user_data_dir:
            self._persistent_context = True
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=self.headless,
                executable_path=chrome_path,
                args=launch_args,
                viewport={
                    "width": settings.PJUD_VIEWPORT_WIDTH,
                    "height": settings.PJUD_VIEWPORT_HEIGHT,
                },
                user_agent=_CHROME_UA,
                locale="es-CL",
                timezone_id="America/Santiago",
            )
            await self._add_stealth_init_script(self._context)
            logger.info("Using persistent PJUD Chrome profile: %s", user_data_dir)
        else:
            self._persistent_context = False
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                executable_path=chrome_path,
                args=launch_args,
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
        if self._browser and not self._persistent_context:
            await self._browser.close()  # CDP connection: disconnects only
        if self._playwright:
            await self._playwright.stop()

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._persistent_context = False
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
        if not self._browser and not self._context:
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

        if self._persistent_context:
            if self._page and not self._page.is_closed():
                await self._page.close()
            self._page = None
        elif self._context:
            await self._context.close()
            self._panel_loaded = False
        
        # Build a Playwright storage_state so cookies AND localStorage are
        # present from the very FIRST page load. PJUD's JS checks localStorage
        # (the "logged-in" token) at load time; restoring localStorage AFTER
        # navigating (the old approach) lands on the login page and breaks the
        # session. storage_state injects both before any navigation.
        from app.config import settings
        from app.scrapper.pjud.browser import build_storage_state

        storage_state = build_storage_state(session) if session else None

        # Context options that make Playwright look more like a real human
        # browsing PJUD — anti-fingerprint / stealth:
        #   - Chrome 149 UA (same as the system Chrome on this Mac)
        #   - Full-HD viewport (configurable)
        #   - Chilean locale + timezone to match PJUD expectations
        #   - geolocation set to Santiago (Shape checks this)
        # Match the live-validated f708315 fingerprint: bundled Chromium + a
        # UA that matches it + storage_state, and NOTHING else. The locale /
        # timezone / viewport / webdriver overrides added in 78246de (meant as
        # "stealth") are exactly what Shape started blocking — the extra
        # injected/tampered signals made the browser MORE detectable, not less.
        context_options = {
            "user_agent": _CHROME_UA,
            "storage_state": storage_state,
        }

        if self._persistent_context:
            if self._context is None:
                raise RuntimeError("Persistent browser context not started")
            await self._restore_session_to_context(self._context, storage_state)
        else:
            if self._browser is None:
                raise RuntimeError("Browser not started")
            self._context = await self._browser.new_context(**context_options)

        self._page = await self._context.new_page()
        self._page_session_key = session_key
        return self._page

    async def _add_stealth_init_script(self, context: BrowserContext) -> None:
        # Patch navigator.webdriver before pages are created; Shape checks this signal.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

    async def _restore_session_to_context(
        self,
        context: BrowserContext,
        storage_state: Optional[Dict[str, Any]],
    ) -> None:
        if not storage_state:
            return

        cookies = storage_state.get("cookies") or []
        if cookies:
            await context.add_cookies(cookies)

        origins = storage_state.get("origins") or []
        local_storage = next(
            (
                origin.get("localStorage")
                for origin in origins
                if origin.get("origin") == PJUD_BASE_URL
            ),
            [],
        )
        if local_storage:
            await context.add_init_script(
                f"""
                (() => {{
                    const items = {json.dumps(local_storage)};
                    if (location.origin !== 'https://oficinajudicialvirtual.pjud.cl') return;
                    for (const item of items) localStorage.setItem(item.name, item.value);
                }})()
                """,
            )

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
    
    async def _generate_recaptcha_token(
        self,
        page: Page,
        sitekey: str,
        action: str,
    ) -> str:
        """Generate an organic reCAPTCHA v3 token in the given page.

        Runs entirely client-side, in the SAME real/headless browser that
        already passes F5 Shape — this is the legitimate path (same as a real
        user's browser), NOT a captcha farm: injects the reCAPTCHA script if
        it isn't already loaded, polls for ``grecaptcha.execute`` readiness
        (~10s), then calls ``grecaptcha.ready`` + ``grecaptcha.execute``.

        Args:
            page: Playwright page (already navigated to a page that hosts
                the reCAPTCHA widget, e.g. PJUD_HOME_URL).
            sitekey: reCAPTCHA v3 site key.
            action: reCAPTCHA v3 action name (server-side action binding).

        Returns:
            The organic reCAPTCHA v3 token string.

        Raises:
            LoginError: if grecaptcha never loads (``NO_GRECAPTCHA``) or
                ``execute()`` rejects (``ERR:<reason>``).
        """
        token = await page.evaluate(
            """async ({sitekey, action}) => {
                if (!(window.grecaptcha && window.grecaptcha.execute)) {
                    await new Promise((res) => {
                        const s = document.createElement('script');
                        s.src = 'https://www.google.com/recaptcha/api.js?render=' + sitekey;
                        s.onload = res; s.onerror = res;
                        document.head.appendChild(s);
                    });
                }
                for (let i=0; i<50 && !(window.grecaptcha && window.grecaptcha.execute); i++) {
                    await new Promise(r => setTimeout(r, 200));
                }
                if (!(window.grecaptcha && window.grecaptcha.execute)) return 'NO_GRECAPTCHA';
                return await new Promise((resolve) => {
                    grecaptcha.ready(() => {
                        grecaptcha.execute(sitekey, {action}).then(resolve).catch(e => resolve('ERR:'+e));
                    });
                });
            }""",
            {"sitekey": sitekey, "action": action},
        )

        if not isinstance(token, str) or not token or token.startswith(("NO_", "ERR:")):
            raise LoginError(f"Failed to generate organic reCAPTCHA token: {token!r}")

        return token

    async def login_with_segunda_clave(
        self,
        rut: str,
        password: str,
    ) -> PJUDSession:
        """Login via segunda clave using an ORGANICALLY-generated reCAPTCHA v3 token.

        Replaces the removed 2Captcha integration (policy: never 2Captcha).
        A live experiment confirmed that generating the token in the same
        real browser that already passes F5 Shape gives reCAPTCHA v3 a
        human-looking score, so ``login_with_token`` succeeds — this is the
        legitimate path (same as a real user's browser), not a captcha farm.

        Args:
            rut: RUT with verification digit (e.g., "19643548-4").
            password: PJUD segunda-clave password.

        Returns:
            PJUDSession with cookies for scraping.

        Raises:
            InvalidCredentialsError: PJUD rejected the RUT/password.
            ShapeChallengeError: PJUD served a Shape/TSPD challenge instead.
            LoginError: any other login failure (propagated as-is).
        """
        from app.scrapper.pjud_civil import RECAPTCHA_SITEKEY, RECAPTCHA_ACTION_LOGIN

        page = await self._get_page()
        await page.goto(PJUD_HOME_URL, wait_until="domcontentloaded")

        token = await self._generate_recaptcha_token(
            page, RECAPTCHA_SITEKEY, RECAPTCHA_ACTION_LOGIN
        )

        try:
            return await self.login_with_token(rut, password, token)
        except InvalidCredentialsError:
            raise
        except LoginError:
            # login_with_token already classifies wrong-credential pages as
            # InvalidCredentialsError; anything else that reaches here could
            # still be a Shape/TSPD block rather than a generic failure.
            try:
                content = await self._safe_page_content(page)
            except Exception:
                content = ""
            is_shape, marker = detect_shape_challenge(content, page.url)
            if is_shape:
                raise ShapeChallengeError(
                    url=page.url,
                    jquery_present=False,
                    looks_like_login=True,
                    marker=marker,
                ) from None
            raise

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
            
            # 2. Get the JWT token from the hidden field. Identify it by the token's
            # STRUCTURE (a JWT is 'eyJ...'.<payload>.<signature> — 3 dot-separated
            # parts), NOT by the field's name length. PJUD renamed this field (it was
            # a 40-char random name, now "ACCESO"); keying on name.length === 40 broke
            # every login. A structural check survives future field renames.
            jwt_data = await page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input[type="hidden"]');
                    for (const input of inputs) {
                        const v = input.value || "";
                        if (v.startsWith('eyJ') && v.includes('.')) {
                            return { name: input.name, value: v };
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
            
            # 3. Build and submit the login POST by parsing PJUD's own daily-rotating
            # field mapping (see _BUILD_LOGIN_POST_JS). The structurally-detected JWT
            # (jwt_token) is passed in rather than re-read by a hardcoded field name,
            # so a future rename of the "ACCESO" field fails with a clear reason
            # instead of an opaque page-context exception.
            posted = await page.evaluate(
                _BUILD_LOGIN_POST_JS,
                {
                    "rut": rut_clean,
                    "clave": password,
                    "token": captcha_token,
                    "jwt": jwt_token,
                },
            )

            if not posted or not posted.get("ok"):
                reason = (posted or {}).get("reason", "unknown")
                raise LoginError(f"Could not build PJUD login POST: {reason}")
            categories = ",".join(sorted(set((posted.get("roleMap") or {}).values())))
            logger.info(
                "Login POST → %s (%d fields; sources: %s)",
                posted["endpoint"],
                len(posted["fields"]),
                categories,
            )
            
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
                # Still on login → distinguish wrong credentials (the lawyer must
                # update their clave — retry is pointless) from a rejected captcha
                # token or a transient error (retry-able).
                stuck_content = await self._safe_page_content(page)
                invalid, snippet = classify_login_failure(stuck_content)
                if invalid:
                    raise InvalidCredentialsError(
                        f"PJUD rejected credentials for RUT {rut_clean}"
                        + (f": {snippet}" if snippet else "")
                    )
                raise LoginError(
                    "Session not established - still on login page"
                    + (f" ({snippet})" if snippet else "")
                )
            
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

        # Check if misCausas function exists. A late PJUD navigation can destroy
        # the execution context mid-evaluate; retry (bounded) so a second navigation
        # on the retry itself doesn't propagate out and abort the whole lawyer.
        has_fn = False
        for _attempt in range(3):
            try:
                has_fn = await page.evaluate("typeof misCausas === 'function'")
                break
            except Exception as e:
                if "Execution context was destroyed" not in str(e):
                    raise
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(1)
        
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
            try:
                page_content = await self._safe_page_content(page)
            except Exception:
                page_content = ""
            is_shape_challenge, shape_marker = detect_shape_challenge(page_content, current_url)

            logger.warning(
                f"misCausas not found — session restore failed. "
                f"url={current_url}, jquery={jquery_present}, login_page={looks_like_login}"
            )
            if is_shape_challenge:
                raise ShapeChallengeError(
                    url=current_url,
                    jquery_present=jquery_present,
                    looks_like_login=looks_like_login,
                    marker=shape_marker,
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

                html = await self._fetch_cases_page_resilient(
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

    async def _fetch_cases_page_resilient(
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
        """Fetch one list page, tolerating a mid-fetch navigation that destroys
        the JS execution context.

        On a very large caseload (e.g. ~2000 causas / 139 pages) the shared page
        can navigate while a list-page ``$.ajax`` is in flight, which makes the
        underlying ``page.evaluate`` reject with "Execution context was
        destroyed". Previously that propagated out of ``get_my_cases`` and
        aborted the ENTIRE lawyer's sync over a single transient page. Here we
        re-establish the panel (restoring jQuery + the authenticated context) and
        retry just that page, so one navigation no longer discards thousands of
        already-fetched cases. Only after the retries are exhausted do we give up.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                return await self._fetch_cases_page(
                    page, rut_num, dv, tipo_causa, year,
                    fecha_desde, fecha_hasta, current_page
                )
            except Exception as e:
                if "Execution context was destroyed" not in str(e):
                    raise
                last_exc = e
                logger.warning(
                    "list page %d: execution context destroyed by navigation "
                    "(attempt %d/3) — reloading panel and retrying",
                    current_page, attempt + 1,
                )
                self._panel_loaded = False
                try:
                    await self._ensure_panel_loaded(page)
                except Exception:
                    pass  # best-effort; the retried fetch surfaces a real failure
        raise ScrapingError(
            f"list page {current_page}: execution context destroyed after 3 retries"
        ) from last_exc

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
