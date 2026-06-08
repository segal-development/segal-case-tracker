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
