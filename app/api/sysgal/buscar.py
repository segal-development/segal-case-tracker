"""External read-only ``GET /buscar`` endpoint for the Sysgal CRM.

Full-text search over the extracted text of the DOCUMENTS belonging to the
client's ACTIVE causas. Mirrors the internal ``/api/v1/documents/search``
dialect-aware logic (PostgreSQL ``to_tsvector``/``ts_rank``/``ts_headline``;
SQLite ``LIKE`` + a Python snippet) but restricts the search to the client's
active-case documents and exposes NO download URL or internal ids.

Bounded field set — no document ids, no download links.
"""

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import DateTime, func, text as sql_text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.sysgal._scope import active_case_ids_for_cliente
from app.api.sysgal.deps import require_sysgal_key
from app.models.case import Case
from app.models.court import Court
from app.models.document import Document

router = APIRouter()


class SysgalBuscarItem(BaseModel):
    """Bounded, read-only document search hit for the Sysgal CRM."""

    rol: str
    doc_type: Optional[str] = None
    fecha: Optional[date] = None
    snippet: str
    tribunal: Optional[str] = None


def _sqlite_snippet(texto: str, q: str, width: int = 200) -> str:
    """~``width``-char window around the first case-insensitive match.

    Used on SQLite (tests), where there is no ``ts_headline``. On PostgreSQL
    the snippet comes from ``ts_headline`` instead.
    """
    if not texto:
        return ""
    low = texto.lower()
    idx = low.find(q.lower())
    if idx == -1:
        return texto[:width].strip()
    half = max(0, (width - len(q)) // 2)
    start = max(0, idx - half)
    end = min(len(texto), idx + len(q) + half)
    snip = texto[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(texto) else ""
    return f"{prefix}{snip}{suffix}"


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


@router.get("/buscar", response_model=List[SysgalBuscarItem])
def buscar_documentos(
    cliente_rut: str = Query(..., description="RUT del cliente (litigante) a consultar"),
    q: str = Query(..., min_length=2, description="Texto a buscar (mínimo 2 caracteres)"),
    limit: int = Query(30, ge=1, le=100, description="Máximo de resultados (tope 100)"),
    db: Session = Depends(get_db),
    _key=Depends(require_sysgal_key),
) -> List[SysgalBuscarItem]:
    """Full-text search over the documents of the client's ACTIVE causas.

    Only documents whose ``texto`` has been extracted (``texto IS NOT NULL``)
    are searchable, and only within the client's active cases.

    Dialect-aware:
      - PostgreSQL: Spanish ``to_tsvector @@ websearch_to_tsquery`` match,
        ordered by ``ts_rank``, snippet from ``ts_headline``.
      - SQLite (tests): case-insensitive ``LIKE`` scan, ordered by id, snippet
        built in Python.
    """
    case_ids = active_case_ids_for_cliente(db, cliente_rut)
    if not case_ids:
        return []

    dialect = db.get_bind().dialect.name

    court_ids_cache: dict = {}

    def _court_name(db: Session, court_id) -> Optional[str]:
        if court_id is None:
            return None
        if court_id not in court_ids_cache:
            row = db.query(Court.name).filter(Court.id == court_id).first()
            court_ids_cache[court_id] = row[0] if row else None
        return court_ids_cache[court_id]

    if dialect == "postgresql":
        cfg = sql_text("'spanish'")  # inline regconfig literal (NOT interpolated q)
        tsvector = func.to_tsvector(cfg, func.coalesce(Document.texto, ""))
        tsquery = func.websearch_to_tsquery(cfg, q)  # q -> bound param
        headline = func.ts_headline(
            cfg,
            Document.texto,
            tsquery,
            "MaxWords=30, MinWords=12, ShortWord=2",
        )
        rows = (
            db.query(
                Case.rol,
                Case.court_id,
                Document.doc_type,
                Document.document_date,
                headline.label("snippet"),
            )
            .join(Case, Case.id == Document.case_id)
            .filter(Document.case_id.in_(case_ids))
            .filter(Document.texto.isnot(None))
            .filter(tsvector.op("@@")(tsquery))
            .order_by(func.ts_rank(tsvector, tsquery).desc())
            .limit(limit)
            .all()
        )
        return [
            SysgalBuscarItem(
                rol=rol,
                doc_type=doc_type,
                fecha=_as_date(doc_date),
                snippet=snippet or "",
                tribunal=_court_name(db, court_id),
            )
            for rol, court_id, doc_type, doc_date, snippet in rows
        ]

    # SQLite / fallback: case-insensitive LIKE (value is bound, not interpolated).
    rows = (
        db.query(
            Case.rol,
            Case.court_id,
            Document.doc_type,
            Document.document_date,
            Document.texto,
        )
        .join(Case, Case.id == Document.case_id)
        .filter(Document.case_id.in_(case_ids))
        .filter(Document.texto.isnot(None))
        .filter(func.lower(Document.texto).like(f"%{q.lower()}%"))
        .order_by(Document.id)
        .limit(limit)
        .all()
    )
    return [
        SysgalBuscarItem(
            rol=rol,
            doc_type=doc_type,
            fecha=_as_date(doc_date),
            snippet=_sqlite_snippet(texto or "", q),
            tribunal=_court_name(db, court_id),
        )
        for rol, court_id, doc_type, doc_date, texto in rows
    ]
