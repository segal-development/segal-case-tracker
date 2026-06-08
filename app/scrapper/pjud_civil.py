"""
PJUD Civil portal scraper.

DEPRECATED: This module is kept for backward compatibility.
Use `app.scrapper.pjud` package instead.

New imports:
    from app.scrapper.pjud import CivilScraper
    from app.scrapper.pjud import PJUDCase, PJUDDocument, PJUDMovement, PJUDCaseDetail
    from app.scrapper.pjud import LoginError
"""

import warnings

# Re-export everything from new location for backward compatibility
from app.scrapper.pjud import (
    CivilScraper,
    PJUDCase,
    PJUDDocument,
    PJUDMovement,
    PJUDCaseDetail,
    LoginError,
    PJUD_BASE_URL,
)

# Alias for backward compatibility
PJUDCivilScraper = CivilScraper


def __getattr__(name):
    """Emit deprecation warning on first access."""
    if name in ("PJUDCivilScraper", "CivilScraper"):
        warnings.warn(
            "Importing from app.scrapper.pjud_civil is deprecated. "
            "Use 'from app.scrapper.pjud import CivilScraper' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    return globals().get(name)


# Re-export constants for backward compatibility
PJUD_HOME_URL = f"{PJUD_BASE_URL}/home/index.php"
PJUD_SESSION_URL = f"{PJUD_BASE_URL}/sessionN.php"
PJUD_INDEX_URL = f"{PJUD_BASE_URL}/indexN.php"
PJUD_SEARCH_CIVIL_URL = f"{PJUD_BASE_URL}/ADIR_871/civil/consultaRitCivil.php"
PJUD_MY_CASES_CIVIL_URL = f"{PJUD_BASE_URL}/misCausas/civil/consultaMisCausasCivil.php"
PJUD_MY_CASE_DETAIL_URL = f"{PJUD_BASE_URL}/misCausas/civil/modal/misCausasCivil.php"
PJUD_PUBLIC_CASE_DETAIL_URL = f"{PJUD_BASE_URL}/ADIR_871/civil/modal/causaCivil.php"

TOKEN_MY_CASES = "df32271e9cdca2704ff289941058a253"
TOKEN_PUBLIC_SEARCH = "917cfa057160fbb6de2eb86da2348e42"
TOKEN_SII_BYPASS = "CONTENEDORSII"

RECAPTCHA_SITEKEY = "6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt"
RECAPTCHA_ACTION_LOGIN = "validate_captcha_seg_clave_hn"
RECAPTCHA_ACTION_DETAIL = "validate_captcha_detcau_civil"

COMPETENCIA_CIVIL = "3"


__all__ = [
    "PJUDCivilScraper",
    "CivilScraper",
    "PJUDCase",
    "PJUDDocument",
    "PJUDMovement",
    "PJUDCaseDetail",
    "LoginError",
    "PJUD_BASE_URL",
    "PJUD_HOME_URL",
    "PJUD_SESSION_URL",
    "PJUD_INDEX_URL",
    "PJUD_SEARCH_CIVIL_URL",
    "PJUD_MY_CASES_CIVIL_URL",
    "PJUD_MY_CASE_DETAIL_URL",
    "PJUD_PUBLIC_CASE_DETAIL_URL",
    "TOKEN_MY_CASES",
    "TOKEN_PUBLIC_SEARCH",
    "TOKEN_SII_BYPASS",
    "RECAPTCHA_SITEKEY",
    "RECAPTCHA_ACTION_LOGIN",
    "RECAPTCHA_ACTION_DETAIL",
    "COMPETENCIA_CIVIL",
]
