"""Shared PDF text extraction helper.

Kept in its own tiny module (no DB / GCS / app imports) so it can be reused
from both the hito detector and the document download / full-text-search
pipeline without dragging in a heavy import chain. ``pypdf`` is imported
lazily inside the function so importing this module stays cheap.
"""
from __future__ import annotations

import io


def extraer_texto_pdf(data: bytes) -> str:
    """Extrae el texto de un PDF. Devuelve '' si es escaneado/ilegible. Nunca lanza."""
    if not data:
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""
