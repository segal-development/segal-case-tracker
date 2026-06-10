"""
Clave Unica Authentication Module for PJUD.

Handles login via Clave Unica (Chilean digital identity) as an alternative
to the captcha-based login flow.

Flow:
1. Navigate to PJUD home page
2. Click "Clave Unica" login option
3. Redirected to Clave Unica portal
4. Fill RUT and password
5. Submit and handle redirect back to PJUD
6. Extract session cookies
"""

import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.scrapper.pjud.selectors.registry import SelectorRegistry
from app.services.pjud_session import PJUDSession


logger = logging.getLogger(__name__)


# PJUD URLs
PJUD_HOME_URL = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
PJUD_MY_CASES_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"


@dataclass
class ClaveUnicaCredentials:
    """
    Credentials for Clave Unica authentication.
    
    Attributes:
        rut: Chilean RUT (may differ from PJUD RUT)
        password: Clave Unica password (should be decrypted before use)
    """
    rut: str
    password: str
    
    def validate(self) -> bool:
        """Validate credentials are not empty."""
        return bool(self.rut and self.password)


class ClaveUnicaAuthError(Exception):
    """Raised when Clave Unica authentication fails."""
    pass


class ClaveUnicaAuth:
    """
    Handle Clave Unica authentication flow for PJUD.
    
    Usage:
        async with BrowserFactory() as factory:
            page = await factory.new_page()
            auth = ClaveUnicaAuth()
            session = await auth.login(page, credentials)
    """
    
    def __init__(self):
        """Initialize with selector registry."""
        self._registry = SelectorRegistry()
    
    async def login(
        self,
        page: Page,
        credentials: ClaveUnicaCredentials,
        lawyer_id: int,
    ) -> PJUDSession:
        """
        Login via Clave Unica portal.
        
        Args:
            page: Playwright page instance
            credentials: Clave Unica credentials
            lawyer_id: Database ID of the lawyer
            
        Returns:
            PJUDSession with cookies for session restoration
            
        Raises:
            ClaveUnicaAuthError: If login fails
        """
        if not credentials.validate():
            raise ClaveUnicaAuthError("Invalid credentials: RUT and password required")
        
        try:
            # Load selectors
            self._registry.load("clave_unica")
            
            # Step 1: Navigate to PJUD home
            logger.info("Navigating to PJUD home page")
            await page.goto(PJUD_HOME_URL, timeout=60000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            
            # Step 2: Close any welcome modal
            await self._close_modal_if_present(page)
            
            # Step 3: Click on services dropdown and Clave Unica option
            await self._click_clave_unica_option(page)
            
            # Step 4: Fill Clave Unica login form
            await self._fill_clave_unica_form(page, credentials)
            
            # Step 5: Submit and wait for redirect
            await self._submit_and_wait_redirect(page)
            
            # Step 6: Close welcome modal after login
            await self._close_welcome_modal(page)
            
            # Step 7: Verify we're logged in
            if not await self._verify_logged_in(page):
                raise ClaveUnicaAuthError("Login failed: Could not verify logged in state")
            
            # Step 8: Extract session cookies
            session = await self._extract_session(page, credentials.rut, lawyer_id)
            
            logger.info(f"Clave Unica login successful for lawyer {lawyer_id}")
            return session
            
        except PlaywrightTimeout as e:
            logger.error(f"Timeout during Clave Unica login: {e}")
            raise ClaveUnicaAuthError(f"Login timeout: {e}") from e
        except Exception as e:
            logger.error(f"Clave Unica login failed: {e}")
            raise ClaveUnicaAuthError(f"Login failed: {e}") from e
    
    async def _close_modal_if_present(self, page: Page) -> None:
        """Close any modal that appears on page load."""
        try:
            selector = self._registry.get("clave_unica", "modal_close_button")
            close_btn = page.locator(selector)
            if await close_btn.is_visible(timeout=3000):
                await close_btn.click()
                logger.debug("Closed initial modal")
        except Exception:
            # Modal might not be present, continue
            pass
    
    async def _click_clave_unica_option(self, page: Page) -> None:
        """Click the Clave Unica login option."""
        # First click on services dropdown
        services_btn_selector = self._registry.get("clave_unica", "services_button")
        services_btn = page.locator(services_btn_selector)
        await services_btn.wait_for(state="visible", timeout=10000)
        await services_btn.click()
        
        # Then click Clave Unica link
        clave_unica_link = self._registry.get("clave_unica", "clave_unica_link")
        link = page.locator(clave_unica_link)
        await link.wait_for(state="visible", timeout=5000)
        await link.click()
        
        # Wait for Clave Unica portal to load
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        logger.debug("Clicked Clave Unica option, waiting for portal")
    
    async def _fill_clave_unica_form(
        self,
        page: Page,
        credentials: ClaveUnicaCredentials,
    ) -> None:
        """Fill the Clave Unica login form."""
        # Wait for RUT input
        rut_input_selector = self._registry.get("clave_unica", "rut_input")
        rut_input = page.locator(rut_input_selector)
        await rut_input.wait_for(state="visible", timeout=15000)
        
        # Fill RUT
        await rut_input.click()
        await rut_input.fill(credentials.rut)
        
        # Fill password
        password_input_selector = self._registry.get("clave_unica", "password_input")
        password_input = page.locator(password_input_selector)
        await password_input.click()
        await password_input.fill(credentials.password)
        
        logger.debug("Filled Clave Unica login form")
    
    async def _submit_and_wait_redirect(self, page: Page) -> None:
        """Submit login form and wait for redirect to PJUD."""
        submit_button_selector = self._registry.get("clave_unica", "submit_button")
        submit_btn = page.locator(submit_button_selector)
        await submit_btn.click()
        
        # Wait for navigation back to PJUD
        # The redirect goes through Clave Unica -> PJUD
        try:
            await page.wait_for_url(
                lambda url: "oficinajudicialvirtual.pjud.cl" in url,
                timeout=30000
            )
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            logger.debug("Redirected back to PJUD after Clave Unica login")
        except PlaywrightTimeout:
            # Check if we got an error message on Clave Unica portal
            error_selector = self._registry.get("clave_unica", "error_message")
            try:
                error_el = page.locator(error_selector)
                if await error_el.is_visible(timeout=1000):
                    error_text = await error_el.text_content()
                    raise ClaveUnicaAuthError(f"Clave Unica error: {error_text}")
            except Exception:
                pass
            raise ClaveUnicaAuthError("Timeout waiting for redirect after login")
    
    async def _close_welcome_modal(self, page: Page) -> None:
        """Close the PJUD welcome modal after login."""
        try:
            welcome_btn_selector = self._registry.get("clave_unica", "welcome_modal_button")
            welcome_btn = page.locator(welcome_btn_selector)
            if await welcome_btn.is_visible(timeout=5000):
                await welcome_btn.click()
                logger.debug("Closed PJUD welcome modal")
        except Exception:
            # Modal might not appear, continue
            pass
    
    async def _verify_logged_in(self, page: Page) -> bool:
        """Verify that we're logged in to PJUD."""
        try:
            # Look for "Mis Causas" link which only appears when logged in
            mis_causas_selector = self._registry.get("clave_unica", "my_cases_link")
            mis_causas = page.locator(mis_causas_selector)
            return await mis_causas.is_visible(timeout=10000)
        except Exception:
            return False
    
    async def _extract_session(
        self,
        page: Page,
        rut: str,
        lawyer_id: int,
    ) -> PJUDSession:
        """Extract session cookies from the logged-in page."""
        # Get all cookies
        context = page.context
        cookies = await context.cookies()
        
        # Filter to PJUD domain cookies
        pjud_cookies = [
            c for c in cookies
            if "pjud.cl" in c.get("domain", "")
        ]
        
        # Get localStorage
        try:
            local_storage = await page.evaluate("JSON.stringify(localStorage)")
        except Exception:
            local_storage = "{}"
        
        # Playwright Cookie objects satisfy dict[str, Any] at runtime; cast
        # avoids mypy union-attr noise.
        cookie_dicts: List[Dict[str, Any]] = [dict(c) for c in pjud_cookies]

        # Use canonical factory — UTC-aware, 25-min expiry (ADR-4b), uuid4 id.
        session = PJUDSession.create(
            rut=rut,
            cookies=cookie_dicts,
            local_storage=local_storage,
            auth_method="clave_unica",
            lawyer_id=lawyer_id,
        )
        
        logger.info(
            f"Extracted session with {len(pjud_cookies)} cookies, "
            f"expires at {session.expires_at.isoformat()}"
        )
        
        return session
