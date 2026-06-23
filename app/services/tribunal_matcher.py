"""Tribunal name matching helpers for PJUD Consulta Unificada mapping.

Provides accent-insensitive, ordinal-symbol-aware normalization so that
Court.name values (as stored in the DB) can be matched against the tribunal
names returned by PJUD's cascading selects.

Design intent: EXACT normalized equality only. No fuzzy/substring matching
because a wrong tribunal code is worse than a None result.
"""

import re
import unicodedata


def normalize_court_name(name: str) -> str:
    """Normalize a court name for comparison.

    Steps (in order):
    1. Replace ordinal/degree symbols with the letter "o": "1°", "1º", "1 º" → "1o".
       The regex `\\s*[°º]` captures optional preceding whitespace so "1 º" → "1o".
    2. Strip diacritical marks via NFD decomposition + removal of Mn-category chars
       (combining marks): "á" → "a", "Copiapó" → "Copiapo", "ñ" → "n".
    3. Lowercase.
    4. Collapse any run of whitespace to a single space and strip leading/trailing.
    5. Strip trailing punctuation (.,;:).

    Returns "" for empty or whitespace-only input.
    """
    if not name or not name.strip():
        return ""

    # Step 1 — ordinal/degree symbols (must run before NFD; ° and º don't decompose)
    s = re.sub(r"\s*[°º]", "o", name)

    # Step 2 — strip accents
    nfkd = unicodedata.normalize("NFD", s)
    s = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")

    # Step 3 — lowercase
    s = s.lower()

    # Step 4 — collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Step 5 — trailing punctuation
    s = s.rstrip(".,;:")

    return s


def match_tribunal(court_name: str, directory: list[dict]) -> dict | None:
    """Return the directory entry whose normalized name equals the normalized court_name.

    Args:
        court_name: The Court.name value to look up.
        directory: List of dicts with keys ``{"corte": int, "tribunal": int, "name": str}``.

    Returns:
        The first matching entry dict, or ``None`` if no exact normalized match is found.
        Never performs fuzzy or substring matching — a wrong tribunal is worse than None.
    """
    if not court_name or not court_name.strip():
        return None
    if not directory:
        return None

    normalized_target = normalize_court_name(court_name)
    if not normalized_target:
        return None

    for entry in directory:
        if normalize_court_name(entry["name"]) == normalized_target:
            return entry

    return None
