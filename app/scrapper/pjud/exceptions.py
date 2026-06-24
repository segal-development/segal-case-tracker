"""
PJUD Scraper custom exceptions.

Provides granular exception handling for different failure modes.
"""


class PJUDError(Exception):
    """Base exception for all PJUD scraper errors."""
    
    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause


class LoginError(PJUDError):
    """Raised when PJUD login fails."""
    pass


class SessionExpiredError(PJUDError):
    """Raised when PJUD session has expired."""
    pass


class ScrapingError(PJUDError):
    """Raised when scraping operation fails."""
    pass


class SelectorNotFoundError(PJUDError):
    """Raised when a required selector is not found in the page."""
    
    def __init__(self, selector: str, context: str = ""):
        message = f"Selector not found: {selector}"
        if context:
            message += f" (context: {context})"
        super().__init__(message)
        self.selector = selector
        self.context = context


class StructureChangeError(PJUDError):
    """Raised when PJUD HTML structure has changed unexpectedly."""
    
    def __init__(self, expected: str, actual: str, element: str = ""):
        message = f"PJUD structure changed"
        if element:
            message += f" for {element}"
        message += f": expected '{expected}', got '{actual}'"
        super().__init__(message)
        self.expected = expected
        self.actual = actual
        self.element = element


class CircuitOpenError(PJUDError):
    """Raised when circuit breaker is open and requests are being rejected."""
    
    def __init__(self, competencia: str, recovery_time: int):
        message = f"Circuit breaker open for {competencia}. Recovery in {recovery_time}s."
        super().__init__(message)
        self.competencia = competencia
        self.recovery_time = recovery_time


class RateLimitError(PJUDError):
    """Raised when rate limit is exceeded."""

    def __init__(self, wait_time: float):
        message = f"Rate limit exceeded. Wait {wait_time:.1f}s before retrying."
        super().__init__(message)
        self.wait_time = wait_time


class DocumentTokenExpiredError(PJUDError):
    """Raised when a document JWT token has expired or is no longer valid.

    The API layer maps this to HTTP 410 Gone — caller should re-sync the case
    to obtain a fresh token.
    """
    pass


class ConsultaSessionExpired(PJUDError):
    """Raised when consulta_by_rol detects a dead session redirect instead of search results."""


class SessionNotAuthenticatedError(PJUDError):
    """Raised when session restoration did not produce an authenticated PJUD page.

    This happens when cookies and/or localStorage were restored but the page
    still renders as the login screen (misCausas() is absent), indicating the
    server did not recognise the session.
    """

    def __init__(
        self,
        url: str,
        jquery_present: bool,
        looks_like_login: bool,
    ):
        hint = (
            "The restored session did not yield an authenticated PJUD page "
            "(misCausas() absent). Re-login or verify cookie/localStorage restoration."
        )
        message = (
            f"Not authenticated after session restore. "
            f"url={url}, jquery_present={jquery_present}, "
            f"looks_like_login={looks_like_login}. {hint}"
        )
        super().__init__(message)
        self.url = url
        self.jquery_present = jquery_present
        self.looks_like_login = looks_like_login
