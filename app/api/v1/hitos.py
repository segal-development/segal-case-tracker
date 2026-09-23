"""Hitos (milestone → bonus) endpoints — Slice 1.

Replaces the manual "SISTEMA DE HITOS" sheet: a pre-loaded hito-type catalog, a
guided entry with a PJUD evidence capture, admin approval, and a per-lawyer
monthly total. Firm rule enforced here: a hito submitted by a lawyer through the
public form (origen=formulario) can never be approved without evidence ("sin
evidencia no se paga"); hitos loaded by the admin, Excel or the detector are exempt.
"""
import hashlib
import io
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_lawyer, get_db, require_admin
from app.models.hito import (
    Hito, HitoTipo, HITO_APROBADO, HITO_PENDIENTE, HITO_RECHAZADO, HITO_SUGERIDO,
    ORIGEN_FORMULARIO, ORIGEN_MANUAL,
)
from app.models.hito_form_link import FORM_LINK_KIND_PROCURADORES, HitoFormLink
from app.models.lawyer import Lawyer
from app.services import bono_cierre_service as cierre_svc

# A ROL like "C-9960-2026" embedded anywhere in the free-text descripcion.
_ROL_RE = re.compile(r"[A-Za-z]+-\d+-\d{4}")
# A bare ROL typed without its letter prefix ("6147-2026"). This system only
# tracks civil causas, so the missing prefix is always "C-".
_BARE_ROL_RE = re.compile(r"^\d+-\d{4}$")


def _normalize_rol_text(descripcion: Optional[str]) -> Optional[str]:
    """Normalize the causa text stored in ``descripcion`` at every write site.

    Trims whitespace; a bare ROL (``6147-2026``) becomes ``C-6147-2026`` and a
    ROL with a letter prefix is upper-cased (``c-6147-2026`` → ``C-6147-2026``).
    Free text is left untouched. ``None`` for empty input.

    Why: in the August 2026 audit hito 457 had been stored as ``6147-2026`` and did
    NOT dedup against ``C-6147-2026`` — the same causa could have been paid twice.
    """
    if not descripcion:
        return None
    text = str(descripcion).strip()
    if not text:
        return None
    if _BARE_ROL_RE.match(text):
        return f"C-{text}"
    if _ROL_RE.fullmatch(text):
        return text.upper()
    return text


def _duplicado_detail(row) -> str:
    """Spanish 409 message naming the hito that already covers this causa."""
    _, _, fecha_hito, estado, created_by_name = row
    partes = ["Ya existe un hito de este abogado para esa causa"]
    if fecha_hito is not None:
        partes.append(f"con fecha {fecha_hito.strftime('%d-%m-%Y')}")
    if estado:
        partes.append(f"y está {estado}")
    detalle = " ".join(partes)
    if created_by_name:
        detalle = f"{detalle}. Lo registró {created_by_name}"
    return f"{detalle}."


def _causa_key(descripcion: Optional[str]) -> Optional[str]:
    """Causa identifier used for hito dedup.

    ``rol_causa`` stores the CLIENT RUT, so a lawyer could legitimately earn hitos
    for the same client across DIFFERENT causas. The distinguishing causa lives in
    ``descripcion`` (a ROL like ``C-9960-2026``), so dedup keys on
    ``(lawyer, rol_causa, _causa_key(descripcion))``: the ROL when present, else the
    normalized free text. ``None`` for empty descripcion.

    A bare ROL (``6147-2026``) keys as ``C-6147-2026`` so legacy rows written
    before ``_normalize_rol_text`` existed still collide with the prefixed form.
    """
    if not descripcion:
        return None
    text = descripcion.strip()
    if _BARE_ROL_RE.match(text):
        return f"C-{text}"
    m = _ROL_RE.search(text)
    return (m.group(0).upper() if m else text.upper()[:120]) or None


def _tribunal_key(tribunal: Optional[str]) -> Optional[str]:
    """Normalized tribunal token used for hito dedup.

    The same ROL in DIFFERENT tribunals is a DIFFERENT causa, so the tribunal is
    part of the dedup identity. Compared case-insensitively on trimmed text; two
    hitos with no tribunal (both ``None``) still collide, preserving the old
    (RUT, ROL) behaviour when the field is left blank.
    """
    if not tribunal:
        return None
    return tribunal.strip().upper()[:255] or None


def _tribunal_collides(a: Optional[str], b: Optional[str]) -> bool:
    """Tolerant tribunal match for dedup (a, b are already ``_tribunal_key`` tokens).

    Two hitos on the same (abogado, RUT, causa) are the SAME causa when their
    tribunales are equal OR either one is blank. A legacy hito stored without a
    tribunal (created before migration 048) must still block a re-entry that now
    carries one, and vice versa — otherwise the same causa is paid twice. This is
    exactly how Silvia's August 2026 duplicates slipped in: old hitos had no
    tribunal, so re-adding them WITH a tribunal did not collide under a strict
    equality check. The bulk importer already uses this rule; create/edit now match.
    """
    return a == b or a is None or b is None


logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_EVIDENCE_BYTES = 15 * 1024 * 1024  # 15 MB
_ALLOWED_EVIDENCE = {"image/png", "image/jpeg", "image/webp", "application/pdf"}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class HitoTipoResponse(BaseModel):
    id: int
    code: str
    label: str
    nivel: str
    valor_bruto: int
    etapa_tramite: Optional[str] = None
    verificacion: Optional[str] = None

    class Config:
        from_attributes = True


class HitoResponse(BaseModel):
    id: int
    lawyer_id: int
    lawyer_nombre: Optional[str] = None
    hito_tipo_id: int
    tipo_label: str
    nivel: str
    valor_bruto: int
    fecha_hito: date
    rol_causa: Optional[str] = None
    procedimiento: Optional[str] = None
    descripcion: Optional[str] = None
    tribunal: Optional[str] = None
    etapa_sysgal: Optional[str] = None
    tramite_sysgal: Optional[str] = None
    tiene_evidencia: bool
    estado: str
    origen: str = "manual"          # manual | detector
    confianza: Optional[str] = None  # alta | media | baja (solo detector)
    created_by_name: Optional[str] = None
    aprobado_by_name: Optional[str] = None
    aprobado_at: Optional[datetime] = None
    rechazo_motivo: Optional[str] = None


class HitoListResponse(BaseModel):
    """Paginated hito list (bounded server-side pagination)."""
    items: List[HitoResponse]
    total: int
    page: int
    per_page: int
    pages: int


class HitoResumenRow(BaseModel):
    lawyer_id: int
    lawyer_nombre: str
    aprobados: int
    total_bruto: int
    pendientes: int


class RechazoBody(BaseModel):
    motivo: Optional[str] = None


class HitoBulkIds(BaseModel):
    ids: list[int]


class HitoBulkResult(BaseModel):
    procesados: int
    ids: list[int]  # the ids actually acted on
    # aprobar-lote only: public-form hitos (origen=formulario) skipped because they
    # have no evidence ("sin evidencia no se paga"). Always 0 / [] for the other bulk actions.
    sin_evidencia: int = 0
    omitidos_ids: list[int] = []


class FormLinkRow(BaseModel):
    """One active firm lawyer and the state of their public hito-form link."""
    lawyer_id: int
    nombre: str
    rut: str
    nivel: Optional[str] = None
    tiene_link: bool
    token: Optional[str] = None


class FormLinkCreated(BaseModel):
    lawyer_id: int
    nombre: str
    token: str


class PublicAbogado(BaseModel):
    id: int
    nombre: str
    rut: str
    nivel: Optional[str] = None


class PublicFormResponse(BaseModel):
    """Everything the public form needs: who the link belongs to + the tipo catalog."""
    abogado: PublicAbogado
    tipos: List[HitoTipoResponse]


class ProcuradoresFormLink(BaseModel):
    """State of the single shared link the procuradores use."""
    kind: str = FORM_LINK_KIND_PROCURADORES
    tiene_link: bool
    token: Optional[str] = None


class ProcuradoresFormLinkCreated(BaseModel):
    kind: str = FORM_LINK_KIND_PROCURADORES
    token: str


class PublicProcuradoresFormResponse(BaseModel):
    """What the shared procuradores form needs: the tipo catalog + the lawyers to pick from."""
    tipos: List[HitoTipoResponse]
    abogados: List[PublicAbogado]


_SIN_EVIDENCIA_DETAIL = "El hito no tiene evidencia adjunta; no se puede aprobar sin evidencia."
_EVIDENCIA_ESTADO_DETAIL = "Solo se puede adjuntar evidencia a un hito pendiente, sugerido o rechazado."
_EVIDENCIA_OBLIGATORIA_DETAIL = "La evidencia es obligatoria"
_LINK_INVALIDO_DETAIL = "Link inválido o vencido"
_ABOGADO_INVALIDO_DETAIL = "Selecciona un abogado válido"
# created_by stamp for hitos submitted through the shared procuradores link, so
# the admin can tell them apart from the lawyer's own submissions.
_PROCURADOR_CREATED_BY = ("procurador", "Procurador (link genérico)")
# States in which evidence may still be attached/replaced. An approved hito was
# paid on the evidence it had, so it is frozen.
_EVIDENCIA_ESTADOS = (HITO_PENDIENTE, HITO_SUGERIDO, HITO_RECHAZADO)
_EVIDENCE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}
_PUBLIC_LIST_LIMIT = 100
_PERIODO_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")  # strict YYYY-MM for the public list
_PERIODO_INVALIDO_DETAIL = "Período inválido, usa YYYY-MM"
_CHILE_TZ = ZoneInfo("America/Santiago")


def _today_santiago() -> date:
    """Today's date in America/Santiago (the firm's calendar), not the server's UTC
    date, which is a day ahead during the Chilean evening. Module-level so tests
    can freeze it."""
    return datetime.now(_CHILE_TZ).date()


def _month_bounds(y: int, m: int) -> tuple[date, date]:
    """``[first day of month, first day of next month)`` — the same window the
    authenticated list and the resumen use for a ``periodo`` filter."""
    return date(y, m, 1), date(y + (m == 12), (m % 12) + 1, 1)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve_lawyer(db: Session, current_lawyer: dict) -> Optional[Lawyer]:
    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if sub is None:
        return None
    if isinstance(sub, int) or (isinstance(sub, str) and str(sub).isdigit()):
        return db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    return db.query(Lawyer).filter(Lawyer.rut == str(sub)).first()


def _is_admin(lawyer: Optional[Lawyer]) -> bool:
    return bool(lawyer and lawyer.role == "admin")


def _store_evidencia(lawyer_id: int, data: bytes, upload: UploadFile) -> tuple[str, Optional[str], str]:
    """Validate an evidence upload and store it; return ``(storage_key, filename, content_type)``.

    Single place for the evidence rules shared by create and ``PUT /{id}/evidencia``:
    max 15 MB, PNG/JPG/WEBP/PDF only, content-addressed key under the owning
    lawyer (``hitos/evidencia/{lawyer_id}/{sha256[:16]}.{ext}``). ``data`` is the
    already-read body so the caller decides what an empty upload means.
    """
    if len(data) > _MAX_EVIDENCE_BYTES:
        raise HTTPException(status_code=413, detail="La evidencia supera el tamaño máximo (15 MB)")
    content_type = upload.content_type or "application/octet-stream"
    if content_type not in _ALLOWED_EVIDENCE:
        raise HTTPException(
            status_code=415,
            detail="Formato de evidencia no permitido (usa PNG, JPG, WEBP o PDF)",
        )
    from app.config import settings
    from app.services.storage_service import get_storage_backend

    digest = hashlib.sha256(data).hexdigest()[:16]
    ext = _EVIDENCE_EXT.get(content_type, "bin")
    key = f"hitos/evidencia/{lawyer_id}/{digest}.{ext}"
    storage_uri = get_storage_backend(settings).upload(data, key, content_type=content_type)
    return storage_uri, upload.filename, content_type


async def _attach_evidencia(db: Session, hito: Hito, evidencia: UploadFile) -> Hito:
    """Attach or replace ``hito``'s evidence (shared by the authenticated and public PUT).

    Only while the hito is in one of ``_EVIDENCIA_ESTADOS``; an empty upload is a 422.
    """
    if hito.estado not in _EVIDENCIA_ESTADOS:
        raise HTTPException(status_code=409, detail=_EVIDENCIA_ESTADO_DETAIL)
    data = await evidencia.read()
    if not data:
        raise HTTPException(status_code=422, detail="La evidencia está vacía")
    storage_uri, ev_filename, ev_content_type = _store_evidencia(hito.lawyer_id, data, evidencia)
    hito.evidencia_storage_key = storage_uri
    hito.evidencia_filename = ev_filename
    hito.evidencia_content_type = ev_content_type
    db.commit()
    db.refresh(hito)
    return hito


def _stream_evidencia(hito: Hito) -> StreamingResponse:
    """Stream ``hito``'s stored evidence (shared by the authenticated and public GET)."""
    if not hito.evidencia_storage_key:
        raise HTTPException(status_code=404, detail="Sin evidencia")
    from app.config import settings
    from app.services.storage_service import get_storage_backend

    data = get_storage_backend(settings).retrieve(hito.evidencia_storage_key)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=hito.evidencia_content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{hito.evidencia_filename or "evidencia"}"'},
    )


async def _create_hito(
    db: Session,
    actor: Lawyer,
    target_lawyer_id: int,
    *,
    hito_tipo_id: int,
    fecha_hito: date,
    rol_causa: Optional[str],
    procedimiento: Optional[str],
    descripcion: Optional[str],
    tribunal: Optional[str],
    etapa_sysgal: Optional[str],
    tramite_sysgal: Optional[str],
    evidencia: Optional[UploadFile],
    origen: str = ORIGEN_MANUAL,
    created_by: Optional[tuple[str, str]] = None,
) -> Hito:
    """Create a hito for ``target_lawyer_id`` on behalf of ``actor`` (who is recorded
    as ``created_by``). Shared by the authenticated create and the public form so
    the period lock, tipo lookup, dedup and normalization rules live in one place.
    Authorization (may ``actor`` write for ``target_lawyer_id``?) is the caller's job.
    """
    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")

    tipo = db.query(HitoTipo).filter(HitoTipo.id == hito_tipo_id, HitoTipo.activo.is_(True)).first()
    if tipo is None:
        raise HTTPException(status_code=404, detail="Tipo de hito no encontrado")

    # No duplicate on the SAME causa: a lawyer may repeat the same client
    # (rol_causa = RUT) across DIFFERENT causas, so the block is keyed on
    # (abogado, RUT, ROL, tribunal) — the ROL is derived from descripcion (see
    # _causa_key) and the tribunal disambiguates same-ROL causas in different
    # courts, tolerating a blank tribunal on either side (see _tribunal_collides).
    # Only enforced when a rol_causa is given.
    # Checked before storing evidence so a rejected duplicate never uploads a file.
    rol_norm = (rol_causa or "").strip() or None
    tribunal_norm = (tribunal or "").strip() or None
    if rol_norm is not None:
        causa = _causa_key(descripcion)
        trib = _tribunal_key(tribunal_norm)
        prior = (
            db.query(Hito.descripcion, Hito.tribunal, Hito.fecha_hito, Hito.estado, Hito.created_by_name)
            .filter(Hito.lawyer_id == target_lawyer_id, Hito.rol_causa == rol_norm)
            .all()
        )
        choque = next(
            (
                row
                for row in prior
                if _causa_key(row[0]) == causa and _tribunal_collides(_tribunal_key(row[1]), trib)
            ),
            None,
        )
        if choque is not None:
            # Name the existing hito instead of just refusing. A procurador filing
            # through the shared link cannot see what is already loaded, so a bare
            # "ya existe" reads like a bug and gets escalated; the date, the estado
            # and who filed it let them close the question themselves.
            raise HTTPException(status_code=409, detail=_duplicado_detail(choque))

    # Evidence is optional at creation (it can be attached later via
    # PUT /{id}/evidencia) but mandatory to approve. If provided, validate + store it.
    storage_uri = ev_filename = ev_content_type = None
    data = await evidencia.read() if evidencia is not None else b""
    if data:
        storage_uri, ev_filename, ev_content_type = _store_evidencia(target_lawyer_id, data, evidencia)

    hito = Hito(
        lawyer_id=target_lawyer_id,
        hito_tipo_id=tipo.id,
        valor_bruto=tipo.valor_bruto,  # snapshot
        fecha_hito=fecha_hito,
        rol_causa=rol_norm,
        procedimiento=procedimiento,
        descripcion=_normalize_rol_text(descripcion),
        tribunal=tribunal_norm,
        etapa_sysgal=etapa_sysgal or tipo.etapa_tramite,
        tramite_sysgal=tramite_sysgal,
        evidencia_storage_key=storage_uri,
        evidencia_filename=ev_filename,
        evidencia_content_type=ev_content_type,
        estado=HITO_PENDIENTE,
        origen=origen,
        created_by_rut=created_by[0] if created_by else actor.rut,
        created_by_name=created_by[1] if created_by else actor.name,
    )
    db.add(hito)
    db.commit()
    db.refresh(hito)
    return hito


def _falta_evidencia_para_aprobar(hito: Hito) -> bool:
    """The "sin evidencia no se paga" gate, scoped by the client's decision: it
    applies ONLY to hitos submitted through the public form (``origen ==
    formulario``, which always carry evidence — this is a defensive check).
    Hitos loaded by the admin, by Excel import or by the detector may be
    approved without evidence.
    """
    return hito.origen == ORIGEN_FORMULARIO and not hito.tiene_evidencia


def _required_form_text(value: Optional[str], field: str) -> str:
    """Public-form fields are mandatory server-side (the form cannot be bypassed
    by posting directly): blank after strip is a 422 naming the field."""
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail=f"El campo {field} es obligatorio")
    return text


async def _validate_public_form_fields(
    hito_tipo_id: Optional[int],
    fecha_hito: Optional[date],
    rol_causa: Optional[str],
    descripcion: Optional[str],
    tribunal: Optional[str],
    procedimiento: Optional[str],
    evidencia: Optional[UploadFile],
) -> tuple[int, date, str, str, str, str, UploadFile]:
    """The mandatory-field contract shared by every public hito form (per-lawyer
    link and shared procuradores link): each business field must be present and
    non-blank, evidence present and non-empty; otherwise 422 naming the field.
    Returns the cleaned values; the upload is rewound for ``_create_hito``."""
    if hito_tipo_id is None:
        raise HTTPException(status_code=422, detail="El campo hito_tipo_id es obligatorio")
    if fecha_hito is None:
        raise HTTPException(status_code=422, detail="El campo fecha_hito es obligatorio")
    rol_causa = _required_form_text(rol_causa, "rol_causa")
    descripcion = _required_form_text(descripcion, "descripcion")
    tribunal = _required_form_text(tribunal, "tribunal")
    procedimiento = _required_form_text(procedimiento, "procedimiento")
    if evidencia is None:
        raise HTTPException(status_code=422, detail=_EVIDENCIA_OBLIGATORIA_DETAIL)
    data = await evidencia.read()
    if not data:
        raise HTTPException(status_code=422, detail=_EVIDENCIA_OBLIGATORIA_DETAIL)
    await evidencia.seek(0)  # _create_hito reads it again
    return hito_tipo_id, fecha_hito, rol_causa, descripcion, tribunal, procedimiento, evidencia


def _procuradores_link(db: Session) -> Optional[HitoFormLink]:
    return db.query(HitoFormLink).filter(HitoFormLink.kind == FORM_LINK_KIND_PROCURADORES).first()


def _procuradores_link_or_404(db: Session, token: str) -> HitoFormLink:
    """Resolve the shared procuradores token, else 404. A revoked (NULL) token is
    indistinguishable from an unknown one. Never logged."""
    link = None
    if token:
        link = (
            db.query(HitoFormLink)
            .filter(HitoFormLink.kind == FORM_LINK_KIND_PROCURADORES, HitoFormLink.token == token)
            .first()
        )
    if link is None:
        raise HTTPException(status_code=404, detail=_LINK_INVALIDO_DETAIL)
    return link


def _active_firm_lawyers(db: Session) -> list[Lawyer]:
    return (
        db.query(Lawyer)
        .filter(Lawyer.is_active.is_(True), Lawyer.is_firm_lawyer.is_(True))
        .order_by(Lawyer.name)
        .all()
    )


def _lawyer_by_token(db: Session, token: str) -> Lawyer:
    """Resolve a public form-link token to its ACTIVE firm lawyer, else 404.

    Exact match only; an inactive/non-firm lawyer or a revoked (NULL) token is
    indistinguishable from an unknown one. The token is never logged.
    """
    lawyer = None
    if token:
        lawyer = (
            db.query(Lawyer)
            .filter(
                Lawyer.hito_form_token == token,
                Lawyer.is_active.is_(True),
                Lawyer.is_firm_lawyer.is_(True),
            )
            .first()
        )
    if lawyer is None:
        raise HTTPException(status_code=404, detail=_LINK_INVALIDO_DETAIL)
    return lawyer


def _own_hito_or_404(db: Session, lawyer: Lawyer, hito_id: int) -> Hito:
    """A hito reachable from the public form: it must belong to the token's lawyer."""
    hito = db.query(Hito).filter(Hito.id == hito_id, Hito.lawyer_id == lawyer.id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    return hito


def _active_firm_lawyer_or_404(db: Session, lawyer_id: int) -> Lawyer:
    lawyer = (
        db.query(Lawyer)
        .filter(Lawyer.id == lawyer_id, Lawyer.is_active.is_(True), Lawyer.is_firm_lawyer.is_(True))
        .first()
    )
    if lawyer is None:
        raise HTTPException(status_code=404, detail="Abogado no encontrado")
    return lawyer


def _to_response(h: Hito) -> HitoResponse:
    return HitoResponse(
        id=h.id,
        lawyer_id=h.lawyer_id,
        lawyer_nombre=h.lawyer.name if h.lawyer else None,
        hito_tipo_id=h.hito_tipo_id,
        tipo_label=h.tipo.label if h.tipo else "",
        nivel=h.tipo.nivel if h.tipo else "",
        valor_bruto=h.valor_bruto,
        fecha_hito=h.fecha_hito,
        rol_causa=h.rol_causa,
        procedimiento=h.procedimiento,
        descripcion=h.descripcion,
        tribunal=h.tribunal,
        etapa_sysgal=h.etapa_sysgal,
        tramite_sysgal=h.tramite_sysgal,
        tiene_evidencia=h.tiene_evidencia,
        estado=h.estado,
        origen=h.origen,
        confianza=h.confianza,
        created_by_name=h.created_by_name,
        aprobado_by_name=h.aprobado_by_name,
        aprobado_at=h.aprobado_at,
        rechazo_motivo=h.rechazo_motivo,
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/tipos", response_model=List[HitoTipoResponse])
async def list_hito_tipos(
    db: Session = Depends(get_db),
    _lawyer: dict = Depends(get_current_lawyer),
):
    """The hito-type catalog (for the entry form: label → value auto-fills)."""
    return (
        db.query(HitoTipo)
        .filter(HitoTipo.activo.is_(True))
        .order_by(HitoTipo.orden)
        .all()
    )


@router.post("", response_model=HitoResponse, status_code=status.HTTP_201_CREATED)
async def create_hito(
    hito_tipo_id: int = Form(...),
    fecha_hito: date = Form(...),
    rol_causa: Optional[str] = Form(None),
    procedimiento: Optional[str] = Form(None),
    descripcion: Optional[str] = Form(None),
    tribunal: Optional[str] = Form(None),
    etapa_sysgal: Optional[str] = Form(None),
    tramite_sysgal: Optional[str] = Form(None),
    lawyer_id: Optional[int] = Form(None),  # admins may register for another lawyer
    evidencia: Optional[UploadFile] = File(None),  # PJUD capture — optional
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Register a hito. PJUD evidence is optional."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None:
        raise HTTPException(status_code=401, detail="No se pudo resolver el abogado")

    # A lawyer registers hitos for themselves; only an admin may set another lawyer.
    target_lawyer_id = actor.id
    if lawyer_id is not None and lawyer_id != actor.id:
        if not _is_admin(actor):
            raise HTTPException(status_code=403, detail="Solo un admin puede registrar hitos de otro abogado")
        if db.query(Lawyer).filter(Lawyer.id == lawyer_id).first() is None:
            raise HTTPException(status_code=404, detail="Abogado no encontrado")
        target_lawyer_id = lawyer_id

    hito = await _create_hito(
        db, actor, target_lawyer_id,
        hito_tipo_id=hito_tipo_id, fecha_hito=fecha_hito, rol_causa=rol_causa,
        procedimiento=procedimiento, descripcion=descripcion, tribunal=tribunal,
        etapa_sysgal=etapa_sysgal, tramite_sysgal=tramite_sysgal, evidencia=evidencia,
    )
    return _to_response(hito)


@router.get("", response_model=HitoListResponse)
async def list_hitos(
    periodo: Optional[str] = Query(None, description="Filtrar por mes YYYY-MM"),
    lawyer_id: Optional[int] = Query(None),
    estado: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """List hitos. Admins see all; a lawyer sees only their own.

    Bounded server-side pagination: the response is a ``{items, total, page,
    per_page, pages}`` envelope. The screen still consumes the full list — the
    frontend hook loops every page and flattens client-side.
    """
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None:
        raise HTTPException(status_code=401, detail="No se pudo resolver el abogado")

    q = db.query(Hito).options(
        selectinload(Hito.lawyer), selectinload(Hito.tipo)
    )
    if not _is_admin(actor):
        q = q.filter(Hito.lawyer_id == actor.id)  # non-admins: own hitos only
    elif lawyer_id is not None:
        q = q.filter(Hito.lawyer_id == lawyer_id)
    if estado:
        q = q.filter(Hito.estado == estado)
    if periodo:
        try:
            y, m = (int(x) for x in periodo.split("-"))
            start = date(y, m, 1)
            end = date(y + (m == 12), (m % 12) + 1, 1)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="periodo inválido (usa YYYY-MM)")
        q = q.filter(Hito.fecha_hito >= start, Hito.fecha_hito < end)

    # Count the filtered set BEFORE offset/limit, then page over a DETERMINISTIC
    # order so slices are stable across requests.
    total = q.count()
    pages = (total + per_page - 1) // per_page if total else 0
    rows = (
        q.order_by(Hito.fecha_hito.desc(), Hito.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return HitoListResponse(
        items=[_to_response(h) for h in rows],
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


# --------------------------------------------------------------------------- #
# Public form links (admin) — lawyers have NO app login, so each one gets a
# secret per-lawyer link to a public hito form. Declared BEFORE every
# ``/{hito_id}`` route so ``/form-links`` and ``/public/...`` never fall into them.
# --------------------------------------------------------------------------- #
@router.get("/form-links", response_model=List[FormLinkRow])
async def list_form_links(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Every active firm lawyer with the state of their public form link (admin only)."""
    lawyers = (
        db.query(Lawyer)
        .filter(Lawyer.is_active.is_(True), Lawyer.is_firm_lawyer.is_(True))
        .order_by(Lawyer.name)
        .all()
    )
    return [
        FormLinkRow(
            lawyer_id=lw.id, nombre=lw.name, rut=lw.rut, nivel=lw.nivel,
            tiene_link=bool(lw.hito_form_token), token=lw.hito_form_token,
        )
        for lw in lawyers
    ]


# Shared procuradores link. LITERAL paths declared BEFORE ``/form-links/{lawyer_id}``
# so "procuradores" is never parsed as a lawyer id.
@router.get("/form-links/procuradores", response_model=ProcuradoresFormLink)
async def get_procuradores_form_link(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """State of the single shared link the procuradores use (admin only)."""
    link = _procuradores_link(db)
    token = link.token if link else None
    return ProcuradoresFormLink(tiene_link=bool(token), token=token)


@router.post("/form-links/procuradores", response_model=ProcuradoresFormLinkCreated)
async def create_procuradores_form_link(
    regenerar: bool = Query(False, description="Reemplazar el token existente (invalida el link anterior)"),
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Issue the shared procuradores token (admin only). Idempotent unless
    ``regenerar=true``, which replaces it and invalidates the previous link."""
    link = _procuradores_link(db)
    if link is None:
        link = HitoFormLink(kind=FORM_LINK_KIND_PROCURADORES)
        db.add(link)
    if not link.token or regenerar:
        link.token = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(link)
    return ProcuradoresFormLinkCreated(token=link.token)


@router.delete("/form-links/procuradores", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_procuradores_form_link(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Revoke the shared procuradores link (admin only). Idempotent."""
    link = _procuradores_link(db)
    if link is not None:
        link.token = None
        db.commit()
    return None


@router.post("/form-links/{lawyer_id}", response_model=FormLinkCreated)
async def create_form_link(
    lawyer_id: int,
    regenerar: bool = Query(False, description="Reemplazar el token existente (invalida el link anterior)"),
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Issue the lawyer's public form token (admin only). Idempotent unless
    ``regenerar=true``, which replaces it and invalidates the previous link."""
    lawyer = _active_firm_lawyer_or_404(db, lawyer_id)
    if not lawyer.hito_form_token or regenerar:
        lawyer.hito_form_token = secrets.token_urlsafe(32)
        db.commit()
        db.refresh(lawyer)
    return FormLinkCreated(lawyer_id=lawyer.id, nombre=lawyer.name, token=lawyer.hito_form_token)


@router.delete("/form-links/{lawyer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_form_link(
    lawyer_id: int,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Revoke the lawyer's public form link (admin only). Idempotent."""
    lawyer = _active_firm_lawyer_or_404(db, lawyer_id)
    lawyer.hito_form_token = None
    db.commit()
    return None


# --------------------------------------------------------------------------- #
# Public hito form — NO auth dependency. The secret token in the path IS the
# identity: every route resolves it through ``_lawyer_by_token`` and is scoped
# to that lawyer's own hitos.
# --------------------------------------------------------------------------- #
@router.get("/public/{token}", response_model=PublicFormResponse)
async def public_form(
    token: str,
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). Who the link belongs to + the hito-type catalog."""
    lawyer = _lawyer_by_token(db, token)
    tipos = (
        db.query(HitoTipo)
        .filter(HitoTipo.activo.is_(True))
        .order_by(HitoTipo.orden)
        .all()
    )
    return PublicFormResponse(
        abogado=PublicAbogado(id=lawyer.id, nombre=lawyer.name, rut=lawyer.rut, nivel=lawyer.nivel),
        tipos=[HitoTipoResponse.model_validate(t) for t in tipos],
    )


@router.post("/public/{token}", response_model=HitoResponse, status_code=status.HTTP_201_CREATED)
async def public_create_hito(
    token: str,
    # Every field is declared optional so a missing/blank one yields OUR 422
    # ("El campo X es obligatorio") instead of FastAPI's generic body error.
    hito_tipo_id: Optional[int] = Form(None),
    fecha_hito: Optional[date] = Form(None),
    rol_causa: Optional[str] = Form(None),
    procedimiento: Optional[str] = Form(None),
    descripcion: Optional[str] = Form(None),
    tribunal: Optional[str] = Form(None),
    etapa_sysgal: Optional[str] = Form(None),
    tramite_sysgal: Optional[str] = Form(None),
    evidencia: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). Register a hito for the token's lawyer.

    ALL business fields are mandatory server-side (tipo, fecha, RUT cliente,
    ROL/causa, tribunal, procedimiento, evidencia) — the public form cannot be
    bypassed by posting directly; only ``etapa_sysgal``/``tramite_sysgal`` stay
    optional. The lawyer is both owner and ``created_by``; there is no
    ``lawyer_id`` field, so the link can never create for someone else. The hito
    is stamped ``origen=formulario``.
    """
    lawyer = _lawyer_by_token(db, token)
    hito_tipo_id, fecha_hito, rol_causa, descripcion, tribunal, procedimiento, evidencia = (
        await _validate_public_form_fields(
            hito_tipo_id, fecha_hito, rol_causa, descripcion, tribunal, procedimiento, evidencia,
        )
    )
    hito = await _create_hito(
        db, lawyer, lawyer.id,
        hito_tipo_id=hito_tipo_id, fecha_hito=fecha_hito, rol_causa=rol_causa,
        procedimiento=procedimiento, descripcion=descripcion, tribunal=tribunal,
        etapa_sysgal=etapa_sysgal, tramite_sysgal=tramite_sysgal, evidencia=evidencia,
        origen=ORIGEN_FORMULARIO,
    )
    return _to_response(hito)


# --------------------------------------------------------------------------- #
# Shared procuradores form — NO auth dependency. One token for every procurador;
# they pick the lawyer they work for. Deliberately NO list/evidence endpoints:
# the link is shared, so it must not expose anyone's hitos.
# --------------------------------------------------------------------------- #
@router.get("/public-procuradores/{token}", response_model=PublicProcuradoresFormResponse)
async def public_procuradores_form(
    token: str,
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). The hito-type catalog + the active firm lawyers to pick from."""
    _procuradores_link_or_404(db, token)
    tipos = (
        db.query(HitoTipo)
        .filter(HitoTipo.activo.is_(True))
        .order_by(HitoTipo.orden)
        .all()
    )
    return PublicProcuradoresFormResponse(
        tipos=[HitoTipoResponse.model_validate(t) for t in tipos],
        abogados=[
            PublicAbogado(id=lw.id, nombre=lw.name, rut=lw.rut, nivel=lw.nivel)
            for lw in _active_firm_lawyers(db)
        ],
    )


@router.post("/public-procuradores/{token}", response_model=HitoResponse, status_code=status.HTTP_201_CREATED)
async def public_procuradores_create_hito(
    token: str,
    lawyer_id: Optional[int] = Form(None),
    hito_tipo_id: Optional[int] = Form(None),
    fecha_hito: Optional[date] = Form(None),
    rol_causa: Optional[str] = Form(None),
    procedimiento: Optional[str] = Form(None),
    descripcion: Optional[str] = Form(None),
    tribunal: Optional[str] = Form(None),
    etapa_sysgal: Optional[str] = Form(None),
    tramite_sysgal: Optional[str] = Form(None),
    evidencia: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). A procurador registers a hito for the lawyer they pick.

    ``lawyer_id`` must be an active firm lawyer (422 otherwise); the rest follows
    the same mandatory-field contract as the per-lawyer public form. The hito is
    owned by the selected lawyer, stamped ``origen=formulario`` and
    ``created_by = Procurador (link genérico)``.
    """
    _procuradores_link_or_404(db, token)
    lawyer = None
    if lawyer_id is not None:
        lawyer = (
            db.query(Lawyer)
            .filter(Lawyer.id == lawyer_id, Lawyer.is_active.is_(True), Lawyer.is_firm_lawyer.is_(True))
            .first()
        )
    if lawyer is None:
        raise HTTPException(status_code=422, detail=_ABOGADO_INVALIDO_DETAIL)
    hito_tipo_id, fecha_hito, rol_causa, descripcion, tribunal, procedimiento, evidencia = (
        await _validate_public_form_fields(
            hito_tipo_id, fecha_hito, rol_causa, descripcion, tribunal, procedimiento, evidencia,
        )
    )
    hito = await _create_hito(
        db, lawyer, lawyer.id,
        hito_tipo_id=hito_tipo_id, fecha_hito=fecha_hito, rol_causa=rol_causa,
        procedimiento=procedimiento, descripcion=descripcion, tribunal=tribunal,
        etapa_sysgal=etapa_sysgal, tramite_sysgal=tramite_sysgal, evidencia=evidencia,
        origen=ORIGEN_FORMULARIO, created_by=_PROCURADOR_CREATED_BY,
    )
    return _to_response(hito)


@router.get("/public/{token}/hitos", response_model=List[HitoResponse])
async def public_list_hitos(
    token: str,
    periodo: Optional[str] = Query(None, description="Mes YYYY-MM (default: mes en curso en America/Santiago)"),
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). The token's lawyer's own hitos for ONE month (by
    ``fecha_hito``), newest first (max 100). ``periodo`` is a strict ``YYYY-MM``;
    omitted = the current month in America/Santiago."""
    lawyer = _lawyer_by_token(db, token)
    if periodo is None:
        today = _today_santiago()
        y, m = today.year, today.month
    else:
        if not _PERIODO_RE.match(periodo):
            raise HTTPException(status_code=422, detail=_PERIODO_INVALIDO_DETAIL)
        y, m = (int(x) for x in periodo.split("-"))
    start, end = _month_bounds(y, m)
    rows = (
        db.query(Hito)
        .options(selectinload(Hito.lawyer), selectinload(Hito.tipo))
        .filter(Hito.lawyer_id == lawyer.id, Hito.fecha_hito >= start, Hito.fecha_hito < end)
        .order_by(Hito.fecha_hito.desc(), Hito.id.desc())
        .limit(_PUBLIC_LIST_LIMIT)
        .all()
    )
    return [_to_response(h) for h in rows]


@router.put("/public/{token}/hitos/{hito_id}/evidencia", response_model=HitoResponse)
async def public_put_evidencia(
    token: str,
    hito_id: int,
    evidencia: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). Attach/replace evidence on one of the lawyer's OWN hitos
    (anyone else's is a 404). Same state rule and storage as the authenticated PUT."""
    lawyer = _lawyer_by_token(db, token)
    hito = _own_hito_or_404(db, lawyer, hito_id)
    return _to_response(await _attach_evidencia(db, hito, evidencia))


@router.get("/public/{token}/hitos/{hito_id}/evidencia")
async def public_get_evidencia(
    token: str,
    hito_id: int,
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). Stream the evidence of one of the lawyer's OWN hitos."""
    lawyer = _lawyer_by_token(db, token)
    hito = _own_hito_or_404(db, lawyer, hito_id)
    return _stream_evidencia(hito)


@router.post("/{hito_id}/aprobar", response_model=HitoResponse)
async def aprobar_hito(
    hito_id: int,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Approve a hito (admin only). A public-form hito ("formulario") cannot be
    approved without evidence; see ``_falta_evidencia_para_aprobar``."""
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(hito.fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")
    if _falta_evidencia_para_aprobar(hito):
        raise HTTPException(status_code=409, detail=_SIN_EVIDENCIA_DETAIL)

    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    hito.estado = HITO_APROBADO
    hito.aprobado_by_rut = admin_rut
    hito.aprobado_by_name = admin.name if admin else None
    hito.aprobado_at = datetime.utcnow()
    hito.rechazo_motivo = None
    db.commit()
    db.refresh(hito)
    return _to_response(hito)


@router.post("/{hito_id}/rechazar", response_model=HitoResponse)
async def rechazar_hito(
    hito_id: int,
    body: RechazoBody,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Reject a hito (admin only), with an optional reason."""
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    hito.estado = HITO_RECHAZADO
    hito.aprobado_by_rut = admin_rut
    hito.aprobado_by_name = admin.name if admin else None
    hito.aprobado_at = datetime.utcnow()
    hito.rechazo_motivo = body.motivo
    db.commit()
    db.refresh(hito)
    return _to_response(hito)


# --------------------------------------------------------------------------- #
# Bulk actions (mirror the single-item aprobar / rechazar / delete)
# --------------------------------------------------------------------------- #
@router.post("/aprobar-lote", response_model=HitoBulkResult)
async def aprobar_hitos_lote(
    body: HitoBulkIds,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Approve every existing hito in ``ids`` (admin only). Skips ids that don't
    exist; a closed period blocks the whole batch with the same 409 as the
    single endpoint. Public-form hitos ("formulario") without evidence are NOT
    approved: they are left untouched and reported in ``sin_evidencia`` /
    ``omitidos_ids`` (see ``_falta_evidencia_para_aprobar``)."""
    hitos = db.query(Hito).filter(Hito.id.in_(body.ids)).all()
    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    now = datetime.utcnow()
    acted: list[int] = []
    omitidos: list[int] = []
    for hito in hitos:
        if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(hito.fecha_hito)):
            raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")
        if _falta_evidencia_para_aprobar(hito):
            omitidos.append(hito.id)
            continue
        hito.estado = HITO_APROBADO
        hito.aprobado_by_rut = admin_rut
        hito.aprobado_by_name = admin.name if admin else None
        hito.aprobado_at = now
        hito.rechazo_motivo = None
        acted.append(hito.id)
    db.commit()
    return HitoBulkResult(
        procesados=len(acted), ids=acted,
        sin_evidencia=len(omitidos), omitidos_ids=omitidos,
    )


@router.post("/rechazar-lote", response_model=HitoBulkResult)
async def rechazar_hitos_lote(
    body: HitoBulkIds,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Reject every existing hito in ``ids`` (admin only). Skips ids that don't exist."""
    hitos = db.query(Hito).filter(Hito.id.in_(body.ids)).all()
    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    now = datetime.utcnow()
    acted: list[int] = []
    for hito in hitos:
        hito.estado = HITO_RECHAZADO
        hito.aprobado_by_rut = admin_rut
        hito.aprobado_by_name = admin.name if admin else None
        hito.aprobado_at = now
        hito.rechazo_motivo = None
        acted.append(hito.id)
    db.commit()
    return HitoBulkResult(procesados=len(acted), ids=acted)


@router.post("/eliminar-lote", response_model=HitoBulkResult)
async def eliminar_hitos_lote(
    body: HitoBulkIds,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Delete every existing hito in ``ids`` (admin only). POST so the id list
    travels in the body. Skips ids that don't exist; a closed period blocks the
    whole batch with the same 409 as the single endpoint."""
    hitos = db.query(Hito).filter(Hito.id.in_(body.ids)).all()
    acted: list[int] = []
    for hito in hitos:
        if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(hito.fecha_hito)):
            raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")
        acted.append(hito.id)
    for hito in hitos:
        db.delete(hito)
    db.commit()
    return HitoBulkResult(procesados=len(acted), ids=acted)


@router.get("/resumen", response_model=List[HitoResumenRow])
async def resumen_hitos(
    periodo: Optional[str] = Query(None, description="Mes YYYY-MM (default: mes actual)"),
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Per-lawyer totals for a period: approved count + gross sum + pending count."""
    if periodo:
        try:
            y, m = (int(x) for x in periodo.split("-"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="periodo inválido (usa YYYY-MM)")
    else:
        now = datetime.utcnow()
        y, m = now.year, now.month
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)

    rows = (
        db.query(Hito)
        .options(selectinload(Hito.lawyer))
        .filter(Hito.fecha_hito >= start, Hito.fecha_hito < end)
        .all()
    )
    by_lawyer: dict[int, dict] = {}
    for h in rows:
        r = by_lawyer.setdefault(
            h.lawyer_id,
            {"lawyer_id": h.lawyer_id, "lawyer_nombre": h.lawyer.name if h.lawyer else "",
             "aprobados": 0, "total_bruto": 0, "pendientes": 0},
        )
        if h.estado == HITO_APROBADO:
            r["aprobados"] += 1
            r["total_bruto"] += h.valor_bruto
        elif h.estado == HITO_PENDIENTE:
            r["pendientes"] += 1
    result = sorted(by_lawyer.values(), key=lambda x: x["total_bruto"], reverse=True)
    return [HitoResumenRow(**r) for r in result]


class HitoMensualAbogado(BaseModel):
    lawyer_id: int
    lawyer_nombre: str
    por_mes: dict[str, int]  # "YYYY-MM" -> cantidad ingresada ese mes
    total: int


class HitoStatsMensual(BaseModel):
    meses: List[str]                 # columnas del rango, ordenadas
    abogados: List[HitoMensualAbogado]
    totales_por_mes: dict[str, int]  # todos los abogados por mes (la "proyección")
    total_general: int


def _parse_month(value: str, field: str) -> tuple[int, int]:
    try:
        y, m = (int(x) for x in value.split("-"))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail=f"{field} inválido (usa YYYY-MM)")
    if not (1 <= m <= 12):
        raise HTTPException(status_code=400, detail=f"{field} inválido (mes 1–12)")
    return y, m


@router.get("/stats/mensual", response_model=HitoStatsMensual)
async def stats_mensual_hitos(
    desde: Optional[str] = Query(None, description="Mes inicial YYYY-MM (default: 11 meses atrás)"),
    hasta: Optional[str] = Query(None, description="Mes final YYYY-MM (default: mes actual)"),
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Hitos INGRESADOS por mes, por abogado + total general (proyección).

    Cuenta TODOS los hitos ingresados (cualquier estado) agrupados por el mes de
    ``fecha_hito`` — la misma base que usa el bono. Refleja la actividad/movimientos
    de cada abogado. Rango por defecto: los últimos 12 meses (incluye meses en 0
    para que las columnas sean estables). Ordenado por total de hitos desc."""
    now = datetime.utcnow()
    hy, hm = _parse_month(hasta, "hasta") if hasta else (now.year, now.month)
    if desde:
        dy, dm = _parse_month(desde, "desde")
    else:
        # ventana de 12 meses inclusive → 11 meses hacia atrás desde 'hasta'
        idx = hy * 12 + (hm - 1) - 11
        dy, dm = idx // 12, idx % 12 + 1
    if (dy, dm) > (hy, hm):
        raise HTTPException(status_code=400, detail="'desde' no puede ser posterior a 'hasta'")

    start = date(dy, dm, 1)
    end = date(hy + (hm == 12), (hm % 12) + 1, 1)

    # Meses del rango (columnas estables, incluso los que no tienen hitos).
    meses: List[str] = []
    yy, mm = dy, dm
    while (yy, mm) <= (hy, hm):
        meses.append(f"{yy:04d}-{mm:02d}")
        yy, mm = (yy + (mm == 12), (mm % 12) + 1)

    rows = (
        db.query(Hito)
        .options(selectinload(Hito.lawyer))
        .filter(Hito.fecha_hito >= start, Hito.fecha_hito < end)
        .all()
    )
    by_lawyer: dict[int, dict] = {}
    totales_por_mes: dict[str, int] = {m: 0 for m in meses}
    for h in rows:
        mk = f"{h.fecha_hito.year:04d}-{h.fecha_hito.month:02d}"
        if mk not in totales_por_mes:
            continue  # defensivo; el filtro ya acota al rango
        r = by_lawyer.setdefault(
            h.lawyer_id,
            {"lawyer_id": h.lawyer_id,
             "lawyer_nombre": h.lawyer.name if h.lawyer else "",
             "por_mes": {m: 0 for m in meses}, "total": 0},
        )
        r["por_mes"][mk] += 1
        r["total"] += 1
        totales_por_mes[mk] += 1

    abogados = sorted(by_lawyer.values(), key=lambda x: x["total"], reverse=True)
    return HitoStatsMensual(
        meses=meses,
        abogados=[HitoMensualAbogado(**a) for a in abogados],
        totales_por_mes=totales_por_mes,
        total_general=sum(totales_por_mes.values()),
    )


# --------------------------------------------------------------------------- #
# Excel import (SISTEMA DE HITOS.xlsx — hojas HITOS JUNIOR / HITOS PLENO)
# --------------------------------------------------------------------------- #
class HitoHojaInfo(BaseModel):
    nombre: str
    filas: int


class HitoImportResult(BaseModel):
    total_leidas: int
    creadas: int
    aprobados: int
    pendientes: int
    omitidas_duplicadas: int
    errores: int  # rows skipped (no lawyer match, bad date, unknown pleno tipo)


class HitoPreviewHoja(BaseModel):
    nombre: str
    reconocida: bool  # False for non-hito sheets (PARÁMETROS, VARIABLES BONO, ...)
    filas: int


class HitoPreviewFila(BaseModel):
    fila: int  # 1-based row number in the sheet
    hoja: str
    resultado: str  # "nueva" | "duplicada" | "error"
    abogado: Optional[str] = None
    tipo: Optional[str] = None
    fecha: Optional[date] = None
    rut_cliente: Optional[str] = None  # what would go to rol_causa
    causa: Optional[str] = None  # what would go to descripcion (the ROL)
    tribunal: Optional[str] = None
    estado: str  # "aprobado" | "pendiente"
    valor: int
    motivo: Optional[str] = None  # why it is duplicada/error
    advertencias: List[str] = []


class HitoPreviewResumen(BaseModel):
    total_leidas: int
    nuevas: int
    duplicadas: int
    errores: int
    aprobados: int
    pendientes: int
    advertencias: int  # total count of per-row warnings
    advertencias_generales: List[str] = []
    sospechoso: bool = False
    motivo_sospecha: Optional[str] = None


class HitoPreviewPorAbogado(BaseModel):
    abogado: str
    nuevas: int
    duplicadas: int
    ya_tiene_en_el_mes: int


class HitoImportPreview(BaseModel):
    hojas: List[HitoPreviewHoja]
    columnas_detectadas: dict  # field -> header text found (e.g. "rol" -> "ROL")
    resumen: HitoPreviewResumen
    filas: List[HitoPreviewFila]
    filas_truncadas: bool
    por_abogado: List[HitoPreviewPorAbogado]


def _norm(s) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper().strip()


def _cell(row, i):
    return row[i] if row is not None and i < len(row) else None


def _parse_fecha(value):
    """Return a ``date`` from an Excel cell that may be a real datetime OR text.

    RRHH files often have the fecha column formatted as TEXT ("7/14/2026",
    "14/07/2026", "2026-07-14"), which the import used to reject as an error —
    silently dropping those rows. Tries the file's apparent M/D/Y first, then
    D/M/Y and ISO. Returns ``None`` if unparseable.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _is_hito_row(row) -> bool:
    ab = _cell(row, 1)
    return bool(ab and str(ab).strip() and _norm(ab) not in ("ABOGADO AT", "ABOGADO"))


def _open_hito_wb(data: bytes):
    if not data:
        raise HTTPException(status_code=400, detail="El archivo está vacío")
    try:
        import openpyxl
        return openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(status_code=415, detail="No se pudo leer el archivo (usa un .xlsx)")


# Column headers → field, matched by WHOLE WORD (so "RUT" doesn't hit "bRUTo").
_COL_KEYS = {
    "abogado": ["ABOGADO"], "fecha": ["FECHA"], "rut": ["RUT"], "rol": ["ROL"],
    "tipo": ["TIPO DE HITO", "TIPO"], "proc": ["PROCEDIMIENTO"], "desc": ["DESCRIPCION"],
    "etapa": ["ETAPA"], "tramite": ["TRAMITE"], "aprobado": ["APROBADO"], "valor": ["VALOR"],
    "tribunal": ["TRIBUNAL"],
}


def _resolve_hito_columns(ws):
    """Find the header row of a hito sheet and map field → column index by header
    NAME (robust to layout differences: with/without RUT, any sheet name). Returns
    ``(header_row_index, cols)`` or ``(None, None)`` if no hito header is found."""
    for ri, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True)):
        headers = [_norm(c) for c in row]
        cols: dict = {}
        for field, keys in _COL_KEYS.items():
            for i, h in enumerate(headers):
                if h and i not in cols.values() and any(
                    re.search(r"\b" + re.escape(k) + r"\b", h) for k in keys
                ):
                    cols[field] = i
                    break
        # A real header row has 'abogado' + several other fields in distinct columns.
        if "abogado" in cols and len(cols) >= 4:
            return ri, cols
    return None, None


def _sheet_is_hito(ws) -> bool:
    """True for a sheet the importer can read: NEW format (resolvable header with
    tipo+aprobado, any sheet name) or OLD format (name contains 'HITO')."""
    if "HITO" in _norm(ws.title):
        return True
    _ri, cols = _resolve_hito_columns(ws)
    return bool(cols and "tipo" in cols and "aprobado" in cols)


def _hito_sheets(wb, hoja: Optional[str]):
    """Importable sheets — by header/structure, NOT by name (skips PARÁMETROS,
    VARIABLES BONO, LIQUIDACIÓN, HISTORIAL, which have no hito header)."""
    sheets = [ws for ws in wb.worksheets if _sheet_is_hito(ws)]
    if hoja:
        sheets = [ws for ws in sheets if _norm(ws.title) == _norm(hoja)]
    return sheets


def _count_hito_rows(ws) -> int:
    """Data-row count for a hito sheet, for both formats (new header-based / old)."""
    header_row, cols = _resolve_hito_columns(ws)
    if cols and "tipo" in cols and "aprobado" in cols:
        ab_i = cols["abogado"]
        return sum(
            1
            for ri, row in enumerate(ws.iter_rows(values_only=True))
            if ri > header_row
            and ab_i < len(row)
            and row[ab_i]
            and _norm(row[ab_i]) not in ("ABOGADO AT", "ABOGADO")
        )
    return sum(1 for r in ws.iter_rows(values_only=True) if _is_hito_row(r))


def _match_tipo_any(text, tipos) -> Optional[HitoTipo]:
    """Match a 'Tipo de hito' cell against ALL hito tipos by label (nivel-agnostic)."""
    n = _norm(text)
    if not n:
        return None
    for t in tipos:
        lt = _norm(t.label)
        if n == lt or n in lt or lt in n:
            return t
    ntok = set(n.split())
    best, best_k = None, 0
    for t in tipos:
        k = len(ntok & set(_norm(t.label).split()))
        if k > best_k:
            best, best_k = t, k
    return best if best_k >= 2 else None


def _match_lawyer_by_name(name: str, lawyers) -> Optional[Lawyer]:
    """Match an Excel abogado name to a firm lawyer: all Excel name tokens must
    appear in the lawyer's full name (e.g. 'Gonzalo Calderón' → 'GONZALO ... CALDERÓN ...')."""
    toks = set(t for t in _norm(name).split() if len(t) > 1)
    if not toks:
        return None
    for l in lawyers:
        if toks <= set(_norm(l.name).split()):
            return l
    return None


# --------------------------------------------------------------------------- #
# Shared import engine — the ONE place that parses, matches, normalizes and
# dedups a hitos workbook. ``POST /importar`` and ``POST /importar/preview``
# both build on ``_run_hito_import``: the preview simply never persists the
# ``Hito`` objects it builds. This is what the August 2026 incident needed —
# a preview built from separate logic could silently drift from the real
# import and stop meaning anything.
# --------------------------------------------------------------------------- #
def _looks_like_rol(text: Optional[str]) -> bool:
    """True when ``text`` has ROL/RIT shape (LETTER(S)-DIGITS-YEAR, e.g.
    "C-8818-2026") or is blank. Reuses ``_ROL_RE`` — the same shape the dedup
    key already treats as a real ROL — so this warning and dedup never disagree.
    Blank causa is legitimate (e.g. a recurso de protección with no ROL yet) and
    never warns."""
    if not text:
        return True
    return bool(_ROL_RE.fullmatch(text.strip()))


def _str_or_none(v) -> Optional[str]:
    s = str(v).strip() if v is not None else ""
    return s or None


def _period_bounds(periodo: str):
    """``date`` range ``[start, end)`` covered by a 'YYYY-MM' period string."""
    year, month = (int(x) for x in periodo.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _load_hito_matching_context(db: Session):
    """Firm lawyers + hito-type catalog, split the way the importer needs them."""
    lawyers = db.query(Lawyer).filter(Lawyer.is_firm_lawyer.is_(True)).all()
    tipos = db.query(HitoTipo).all()
    junior_tipo = next((t for t in tipos if t.nivel == "junior"), None)
    pleno_tipos = [t for t in tipos if t.nivel == "pleno"]
    return lawyers, tipos, junior_tipo, pleno_tipos


def _hito_header_texts(ws, header_row: Optional[int], cols: Optional[dict]) -> dict:
    """Original (non-normalized) header text found for each resolved field, e.g.
    ``{"rol": "ROL causa", "desc": "Descripción"}``. Used for ``columnas_detectadas``
    and to name the two headers in the ROL/DESCRIPCION shape-mismatch warning."""
    if header_row is None or not cols:
        return {}
    row = next(ws.iter_rows(min_row=header_row + 1, max_row=header_row + 1, values_only=True), None)
    if row is None:
        return {}
    out: dict = {}
    for field_name, idx in cols.items():
        if idx < len(row) and row[idx] is not None:
            out[field_name] = str(row[idx]).strip()
    return out


@dataclass
class _RawHitoRow:
    """One data row of a hito sheet, parsed into raw field values only — no
    lawyer/tipo matching, no dedup. The single source both the real import and
    the preview read a row from, so they can never disagree on what a cell means."""
    hoja: str
    fila: int  # 1-based row number, as it appears in the sheet
    ab_raw: object
    fecha_raw: object
    rut_raw: object
    rol_raw: object
    desc_raw: object
    procedimiento_raw: object
    tribunal_raw: object
    etapa_raw: object
    tramite_raw: object
    aprobado_raw: object
    valor_raw: object
    tipo_text: Optional[str]  # cell text to match against tipo_candidates
    tipo_candidates: list
    tipo_fixed: Optional[HitoTipo] = None  # pre-assigned tipo (old-format JUNIOR sheets)


def _iterate_hito_rows(sheets, tipos, junior_tipo, pleno_tipos) -> List[_RawHitoRow]:
    """Parse every hito data row of every sheet, NEW format (header-based, any
    layout/sheet name) or OLD format (fixed position by nivel) — mirrors exactly
    what ``importar_hitos`` used to do inline, now the only place that does it."""
    rows: List[_RawHitoRow] = []
    for ws in sheets:
        header_row, cols = _resolve_hito_columns(ws)
        new_format = bool(cols and "tipo" in cols and "aprobado" in cols)

        if new_format:
            def g(row, field_name, _cols=cols):
                i = _cols.get(field_name)
                return row[i] if i is not None and i < len(row) else None

            for ri, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if ri <= header_row + 1:
                    continue
                ab = g(row, "abogado")
                if not ab or _norm(ab) in ("ABOGADO AT", "ABOGADO"):
                    continue
                rows.append(_RawHitoRow(
                    hoja=ws.title, fila=ri, ab_raw=ab, fecha_raw=g(row, "fecha"),
                    rut_raw=g(row, "rut"), rol_raw=g(row, "rol"), desc_raw=g(row, "desc"),
                    procedimiento_raw=g(row, "proc"), tribunal_raw=g(row, "tribunal"),
                    etapa_raw=g(row, "etapa"), tramite_raw=g(row, "tramite"),
                    aprobado_raw=g(row, "aprobado"), valor_raw=g(row, "valor"),
                    tipo_text=g(row, "tipo"), tipo_candidates=tipos, tipo_fixed=None,
                ))
            continue

        # OLD format (hojas 'HITOS JUNIOR/PLENO'): posición fija por nivel.
        is_junior = "JUNIOR" in _norm(ws.title)
        for ri, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if not _is_hito_row(row):
                continue
            if is_junior:
                procedimiento, rut, descripcion = _cell(row, 3), _cell(row, 4), _cell(row, 5)
                tipo_text, tipo_candidates, tipo_fixed = None, [], junior_tipo
            else:
                procedimiento, rut, descripcion = None, _cell(row, 3), None
                tipo_text, tipo_candidates, tipo_fixed = _cell(row, 5), pleno_tipos, None
            rows.append(_RawHitoRow(
                hoja=ws.title, fila=ri, ab_raw=_cell(row, 1), fecha_raw=_cell(row, 2),
                rut_raw=rut, rol_raw=None, desc_raw=descripcion, procedimiento_raw=procedimiento,
                tribunal_raw=None, etapa_raw=_cell(row, 6), tramite_raw=_cell(row, 7),
                aprobado_raw=_cell(row, 8), valor_raw=_cell(row, 9),
                tipo_text=tipo_text, tipo_candidates=tipo_candidates, tipo_fixed=tipo_fixed,
            ))
    return rows


@dataclass
class _HitoRowResult:
    """The interpretation of ONE row: what the real import would do with it."""
    hoja: str
    fila: int
    resultado: str  # "nueva" | "duplicada" | "error"
    estado: str  # "aprobado" | "pendiente"
    valor: int
    abogado: Optional[str] = None
    lawyer_id: Optional[int] = None
    tipo: Optional[str] = None
    fecha: Optional[date] = None
    rut_cliente: Optional[str] = None
    causa: Optional[str] = None
    tribunal: Optional[str] = None
    motivo: Optional[str] = None
    advertencias: List[str] = field(default_factory=list)
    hito: Optional[Hito] = None  # populated only when resultado == "nueva"


class _HitoDedupTracker:
    """(abogado, RUT, causa, tribunal) dedup state shared by preview and import.

    Loads the existing DB rows once, then also tracks rows seen so far THIS run
    so within-file duplicates are caught the same way for both. Mirrors the old
    inline ``existing3``/``existing4``/``seen3``/``seen4`` sets — the tolerant
    rule where a blank tribunal on either side still collides (``_tribunal_key``).
    """

    def __init__(self, db: Session):
        self.existing3: set = set()
        self.existing4: set = set()
        self.seen3: set = set()
        self.seen4: set = set()
        for h in (
            db.query(Hito.lawyer_id, Hito.rol_causa, Hito.descripcion, Hito.tribunal)
            .filter((Hito.rol_causa.isnot(None)) | (Hito.descripcion.isnot(None)))
            .all()
        ):
            causa = _causa_key(h.descripcion)
            if h.rol_causa is None and causa is None:
                continue
            self.existing3.add((h.lawyer_id, h.rol_causa, causa))
            self.existing4.add((h.lawyer_id, h.rol_causa, causa, _tribunal_key(h.tribunal)))

    def check(self, lawyer_id: int, rol_causa: Optional[str], causa: Optional[str],
              tribunal_norm: Optional[str]):
        """Returns ``(is_dup, motivo)``; records the row as seen when not a dup."""
        if rol_causa is None and causa is None:
            return False, None
        trib = _tribunal_key(tribunal_norm)
        k3 = (lawyer_id, rol_causa, causa)
        k4 = k3 + (trib,)
        if trib is None:
            is_dup = k3 in self.existing3 or k3 in self.seen3
        else:
            k4_none = k3 + (None,)
            is_dup = (
                k4 in self.existing4 or k4 in self.seen4
                or k4_none in self.existing4 or k4_none in self.seen4
            )
        if is_dup:
            return True, "Ya existe un hito para este abogado en esta causa (mismo cliente, ROL y tribunal)."
        self.seen3.add(k3)
        self.seen4.add(k4)
        return False, None


def _resolve_and_evaluate_row(
    raw: _RawHitoRow, *, lawyers, dedup: _HitoDedupTracker, admin, admin_rut: str,
    sheet_has_tribunal: bool,
) -> _HitoRowResult:
    """Interpret ONE raw row exactly like the real import would: match lawyer and
    tipo, parse the date, normalize the causa (ROL manda sobre DESCRIPCION), check
    dedup, and build the ``Hito`` that would be created. Never writes to the
    database — the caller (import or preview) decides whether to persist it."""
    aprobado_flag = _norm(raw.aprobado_raw) in ("SI", "SÍ", "S")
    estado_txt = "aprobado" if aprobado_flag else "pendiente"

    fecha = _parse_fecha(raw.fecha_raw)
    if fecha is None:
        return _HitoRowResult(
            hoja=raw.hoja, fila=raw.fila, resultado="error", estado=estado_txt, valor=0,
            rut_cliente=_str_or_none(raw.rut_raw), motivo="No se pudo interpretar la fecha.",
        )

    lawyer = _match_lawyer_by_name(raw.ab_raw, lawyers) if raw.ab_raw else None
    tipo = raw.tipo_fixed if raw.tipo_fixed is not None else _match_tipo_any(raw.tipo_text, raw.tipo_candidates)

    if lawyer is None or tipo is None:
        motivos = []
        if lawyer is None:
            motivos.append(f'No se encontró un abogado que coincida con "{raw.ab_raw}".')
        if tipo is None:
            motivos.append("No se pudo determinar el tipo de hito.")
        return _HitoRowResult(
            hoja=raw.hoja, fila=raw.fila, resultado="error", estado=estado_txt, valor=0,
            abogado=(lawyer.name if lawyer else None), tipo=(tipo.label if tipo else None),
            fecha=fecha, rut_cliente=_str_or_none(raw.rut_raw), motivo=" ".join(motivos),
        )

    try:
        valor = int(raw.valor_raw) if raw.valor_raw else tipo.valor_bruto
    except (ValueError, TypeError):
        valor = tipo.valor_bruto
    if valor <= 0:
        valor = tipo.valor_bruto

    rol_causa = (str(raw.rut_raw).strip()[:50] or None) if raw.rut_raw is not None else None
    # La columna ROL manda sobre DESCRIPCION (ver fix(hitos) #273): una planilla
    # que trae ambas nunca debe usar el texto libre de DESCRIPCION como causa.
    desc_src = raw.rol_raw if raw.rol_raw else raw.desc_raw
    desc_norm = _normalize_rol_text(str(desc_src)[:500]) if desc_src else None
    tribunal_norm = (str(raw.tribunal_raw).strip()[:255] or None) if raw.tribunal_raw else None

    advertencias: List[str] = []
    if desc_norm and not _looks_like_rol(desc_norm):
        advertencias.append(
            f'La causa "{desc_norm}" no tiene forma de ROL. Revisa que la columna ROL esté bien.'
        )
    if tribunal_norm is None and sheet_has_tribunal:
        advertencias.append(
            "Sin tribunal: se va a considerar la misma causa que cualquier hito de este cliente en este ROL."
        )

    is_dup, motivo_dup = dedup.check(lawyer.id, rol_causa, desc_norm, tribunal_norm)
    if is_dup:
        return _HitoRowResult(
            hoja=raw.hoja, fila=raw.fila, resultado="duplicada", estado=estado_txt, valor=valor,
            abogado=lawyer.name, lawyer_id=lawyer.id, tipo=tipo.label, fecha=fecha,
            rut_cliente=rol_causa, causa=desc_norm, tribunal=tribunal_norm,
            motivo=motivo_dup, advertencias=advertencias,
        )

    hito = Hito(
        lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=valor,
        fecha_hito=fecha, rol_causa=rol_causa,
        procedimiento=(str(raw.procedimiento_raw).strip()[:100] if raw.procedimiento_raw else None),
        descripcion=desc_norm, tribunal=tribunal_norm,
        etapa_sysgal=(str(raw.etapa_raw).strip()[:100] if raw.etapa_raw else None),
        tramite_sysgal=(str(raw.tramite_raw).strip()[:255] if raw.tramite_raw else None),
        estado=(HITO_APROBADO if aprobado_flag else HITO_PENDIENTE), created_by_rut=admin_rut,
        created_by_name=admin.name if admin else None,
        aprobado_by_rut=admin_rut if aprobado_flag else None,
        aprobado_by_name=(admin.name if admin else None) if aprobado_flag else None,
        aprobado_at=datetime.utcnow() if aprobado_flag else None,
    )
    return _HitoRowResult(
        hoja=raw.hoja, fila=raw.fila, resultado="nueva", estado=estado_txt, valor=valor,
        abogado=lawyer.name, lawyer_id=lawyer.id, tipo=tipo.label, fecha=fecha,
        rut_cliente=rol_causa, causa=desc_norm, tribunal=tribunal_norm,
        advertencias=advertencias, hito=hito,
    )


def _compute_por_abogado_and_sospecha(outcomes: List[_HitoRowResult], db: Session):
    """Per-lawyer nuevas/duplicadas/ya_tiene_en_el_mes + the batch-level suspicious
    guard: MORE THAN 10 new hitos with ZERO duplicates for a lawyer who ALREADY has
    hitos in that same month — exactly the shape of the August 2026 incident (40
    nuevas, 0 duplicadas, 35 ya cargados en el mes)."""
    by_lawyer: dict = {}
    for o in outcomes:
        if o.lawyer_id is None:
            continue
        entry = by_lawyer.setdefault(o.lawyer_id, {
            "abogado": o.abogado, "nuevas": 0, "duplicadas": 0, "periodos": {},
        })
        if o.resultado == "nueva":
            entry["nuevas"] += 1
            if o.fecha:
                periodo = cierre_svc.periodo_de_fecha(o.fecha)
                entry["periodos"][periodo] = entry["periodos"].get(periodo, 0) + 1
        elif o.resultado == "duplicada":
            entry["duplicadas"] += 1

    por_abogado = []
    sospechoso = False
    motivo_sospecha = None
    for lawyer_id, entry in by_lawyer.items():
        periodo = max(entry["periodos"], key=entry["periodos"].get) if entry["periodos"] else None
        ya_tiene = 0
        if periodo:
            start, end = _period_bounds(periodo)
            ya_tiene = (
                db.query(Hito)
                .filter(Hito.lawyer_id == lawyer_id, Hito.fecha_hito >= start, Hito.fecha_hito < end)
                .count()
            )
        por_abogado.append({
            "abogado": entry["abogado"], "nuevas": entry["nuevas"],
            "duplicadas": entry["duplicadas"], "ya_tiene_en_el_mes": ya_tiene,
        })
        if not sospechoso and entry["nuevas"] > 10 and entry["duplicadas"] == 0 and ya_tiene > 0:
            sospechoso = True
            motivo_sospecha = (
                f'Esta carga crearía {entry["nuevas"]} hitos nuevos para {entry["abogado"]} y '
                f'ninguno sale duplicado, pero ya tiene {ya_tiene} hitos cargados en {periodo}. '
                f'Revisa la columna ROL antes de confirmar.'
            )
    return por_abogado, sospechoso, motivo_sospecha


@dataclass
class _HitoImportRun:
    hojas: list
    columnas_detectadas: dict
    rows: List[_HitoRowResult]  # capped for the preview response
    rows_truncated: bool
    total: int
    creadas: int
    aprobados: int
    pendientes: int
    dup: int
    err: int
    advertencias_generales: List[str]
    advertencias_count: int
    por_abogado: list
    sospechoso: bool
    motivo_sospecha: Optional[str]
    all_outcomes: List[_HitoRowResult]  # UNcapped — what importar_hitos persists


def _run_hito_import(
    db: Session, wb, sheets: list, admin, admin_rut: str, *, max_rows: int = 500,
) -> _HitoImportRun:
    """Parse + interpret + dedup an uploaded hitos workbook. Pure computation: it
    never calls ``db.add``/``db.commit`` — the caller decides whether to persist
    the ``Hito`` objects it built. ``importar_hitos`` persists them;
    ``importar_hitos_preview`` never does."""
    lawyers, tipos, junior_tipo, pleno_tipos = _load_hito_matching_context(db)

    hojas_info = [
        {"nombre": ws.title, "reconocida": _sheet_is_hito(ws),
         "filas": _count_hito_rows(ws) if _sheet_is_hito(ws) else 0}
        for ws in wb.worksheets
    ]

    columnas_detectadas: dict = {}
    for ws in sheets:
        header_row, cols = _resolve_hito_columns(ws)
        if cols and "tipo" in cols and "aprobado" in cols:
            columnas_detectadas = _hito_header_texts(ws, header_row, cols)
            break

    raw_rows = _iterate_hito_rows(sheets, tipos, junior_tipo, pleno_tipos)

    sheet_has_tribunal: dict = {}
    for raw in raw_rows:
        has_it = bool(str(raw.tribunal_raw or "").strip())
        sheet_has_tribunal[raw.hoja] = sheet_has_tribunal.get(raw.hoja, False) or has_it

    advertencias_generales: List[str] = []
    for ws in sheets:
        header_row, cols = _resolve_hito_columns(ws)
        if not cols or "rol" not in cols or "desc" not in cols:
            continue
        headers = _hito_header_texts(ws, header_row, cols)
        for raw in raw_rows:
            if raw.hoja != ws.title:
                continue
            rol_txt = _normalize_rol_text(str(raw.rol_raw)[:500]) if raw.rol_raw else None
            desc_txt = _normalize_rol_text(str(raw.desc_raw)[:500]) if raw.desc_raw else None
            if rol_txt and desc_txt and _looks_like_rol(rol_txt) != _looks_like_rol(desc_txt):
                advertencias_generales.append(
                    f'En la hoja "{ws.title}" las columnas "{headers.get("rol", "ROL")}" y '
                    f'"{headers.get("desc", "DESCRIPCION")}" no concuerdan en formato: revisa '
                    f'cuál de las dos trae realmente el ROL de la causa.'
                )
                break

    dedup = _HitoDedupTracker(db)
    total = creadas = aprobados = pendientes = dup_count = err = 0
    all_outcomes: List[_HitoRowResult] = []
    for raw in raw_rows:
        total += 1
        outcome = _resolve_and_evaluate_row(
            raw, lawyers=lawyers, dedup=dedup, admin=admin, admin_rut=admin_rut,
            sheet_has_tribunal=sheet_has_tribunal.get(raw.hoja, False),
        )
        if outcome.resultado == "error":
            err += 1
        elif outcome.resultado == "duplicada":
            dup_count += 1
        else:
            creadas += 1
            aprobados += 1 if outcome.estado == "aprobado" else 0
            pendientes += 0 if outcome.estado == "aprobado" else 1
        all_outcomes.append(outcome)

    por_abogado, sospechoso, motivo_sospecha = _compute_por_abogado_and_sospecha(all_outcomes, db)
    advertencias_count = sum(len(o.advertencias) for o in all_outcomes)

    return _HitoImportRun(
        hojas=hojas_info, columnas_detectadas=columnas_detectadas,
        rows=all_outcomes[:max_rows], rows_truncated=len(all_outcomes) > max_rows,
        total=total, creadas=creadas, aprobados=aprobados, pendientes=pendientes,
        dup=dup_count, err=err, advertencias_generales=advertencias_generales,
        advertencias_count=advertencias_count, por_abogado=por_abogado,
        sospechoso=sospechoso, motivo_sospecha=motivo_sospecha, all_outcomes=all_outcomes,
    )


class HitoAbogado(BaseModel):
    lawyer_id: int
    nombre: str


@router.get("/abogados", response_model=List[HitoAbogado])
async def list_hito_abogados(
    db: Session = Depends(get_db),
    _lawyer: dict = Depends(get_current_lawyer),
):
    """Firm lawyers for the hito 'abogado' selector (admins register for another)."""
    lawyers = (
        db.query(Lawyer)
        .filter(Lawyer.is_firm_lawyer.is_(True))
        .order_by(Lawyer.name)
        .all()
    )
    return [HitoAbogado(lawyer_id=l.id, nombre=l.name) for l in lawyers]


@router.delete("/{hito_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hito(
    hito_id: int,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Delete a hito (admin only) — e.g. one registered for the wrong lawyer."""
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(hito.fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")
    db.delete(hito)
    db.commit()


class HitoUpdate(BaseModel):
    hito_tipo_id: int
    lawyer_id: Optional[int] = None
    fecha_hito: date
    rol_causa: Optional[str] = None
    procedimiento: Optional[str] = None
    descripcion: Optional[str] = None
    tribunal: Optional[str] = None


@router.put("/{hito_id}", response_model=HitoResponse)
async def update_hito(
    hito_id: int,
    body: HitoUpdate,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Edit a hito (admin only). Re-snapshots the value if the tipo changes.
    Preserves ETAPA/TRAMITE and evidence; period-close blocks edits both ways."""
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(hito.fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")
    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(body.fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de destino está cerrado")

    tipo = db.query(HitoTipo).filter(HitoTipo.id == body.hito_tipo_id, HitoTipo.activo.is_(True)).first()
    if tipo is None:
        raise HTTPException(status_code=404, detail="Tipo de hito no encontrado")

    if body.lawyer_id is not None:
        abogado = db.query(Lawyer).filter(Lawyer.id == body.lawyer_id).first()
        if abogado is None or not abogado.is_firm_lawyer:
            raise HTTPException(status_code=404, detail="Abogado no encontrado en el estudio")
        hito.lawyer_id = abogado.id

    # No duplicate: the resulting (abogado, RUT, ROL, tribunal) must not collide
    # with ANOTHER hito. Same client on a DIFFERENT causa — different ROL, or same
    # ROL in a different (non-blank) tribunal — is allowed; a blank tribunal on
    # either side still collides (see _causa_key / _tribunal_collides).
    rol_norm = (body.rol_causa or "").strip() or None
    tribunal_norm = (body.tribunal or "").strip() or None
    if rol_norm is not None:
        causa = _causa_key(body.descripcion)
        trib = _tribunal_key(tribunal_norm)
        prior = (
            db.query(Hito.descripcion, Hito.tribunal)
            .filter(
                Hito.id != hito.id,
                Hito.lawyer_id == hito.lawyer_id,
                Hito.rol_causa == rol_norm,
            )
            .all()
        )
        if any(_causa_key(d) == causa and _tribunal_collides(_tribunal_key(t), trib) for (d, t) in prior):
            raise HTTPException(
                status_code=409,
                detail="Ya existe un hito de este abogado para esa causa",
            )

    hito.hito_tipo_id = tipo.id
    hito.valor_bruto = tipo.valor_bruto  # re-snapshot
    hito.fecha_hito = body.fecha_hito
    hito.rol_causa = rol_norm
    hito.procedimiento = body.procedimiento
    hito.descripcion = _normalize_rol_text(body.descripcion)
    hito.tribunal = tribunal_norm
    db.commit()
    db.refresh(hito)
    return _to_response(hito)


@router.post("/importar/hojas", response_model=List[HitoHojaInfo])
async def listar_hojas_hitos(
    archivo: UploadFile = File(...),
    _admin_rut: str = Depends(require_admin),
):
    """List the HITOS sheets of an uploaded SISTEMA DE HITOS.xlsx (name + row count)."""
    wb = _open_hito_wb(await archivo.read())
    out = [HitoHojaInfo(nombre=ws.title, filas=_count_hito_rows(ws)) for ws in _hito_sheets(wb, None)]
    wb.close()
    return out


@router.post("/importar/preview", response_model=HitoImportPreview)
async def importar_hitos_preview(
    archivo: UploadFile = File(...),
    hoja: Optional[str] = Query(None, description="Hoja a previsualizar (default: todas las HITOS)"),
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Preview a hitos Excel import WITHOUT writing anything to the database.

    Runs the exact same column resolution, lawyer/tipo matching, date parsing,
    normalization and dedup logic as ``POST /importar`` (both call
    ``_run_hito_import``), so what the admin previews and what actually gets
    written can never drift. Surfaces the warnings that a healthy-looking import
    report can otherwise hide: a real incident (2026-09) had a free-text column
    stored as the causa ROL, silently creating 64 duplicate hitos while the
    report read "40 creadas, 0 duplicadas". The frontend calls this first, then
    ``POST /importar`` to actually commit.
    """
    wb = _open_hito_wb(await archivo.read())
    sheets = _hito_sheets(wb, hoja)
    if hoja and not sheets:
        wb.close()
        raise HTTPException(status_code=404, detail=f"La hoja '{hoja}' no existe en el archivo")

    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    run = _run_hito_import(db, wb, sheets, admin, admin_rut)
    wb.close()

    return HitoImportPreview(
        hojas=[HitoPreviewHoja(**h) for h in run.hojas],
        columnas_detectadas=run.columnas_detectadas,
        resumen=HitoPreviewResumen(
            total_leidas=run.total, nuevas=run.creadas, duplicadas=run.dup, errores=run.err,
            aprobados=run.aprobados, pendientes=run.pendientes, advertencias=run.advertencias_count,
            advertencias_generales=run.advertencias_generales, sospechoso=run.sospechoso,
            motivo_sospecha=run.motivo_sospecha,
        ),
        filas=[
            HitoPreviewFila(
                fila=o.fila, hoja=o.hoja, resultado=o.resultado, abogado=o.abogado, tipo=o.tipo,
                fecha=o.fecha, rut_cliente=o.rut_cliente, causa=o.causa, tribunal=o.tribunal,
                estado=o.estado, valor=o.valor, motivo=o.motivo, advertencias=o.advertencias,
            )
            for o in run.rows
        ],
        filas_truncadas=run.rows_truncated,
        por_abogado=[HitoPreviewPorAbogado(**p) for p in run.por_abogado],
    )


@router.post("/importar", response_model=HitoImportResult)
async def importar_hitos(
    archivo: UploadFile = File(...),
    hoja: Optional[str] = Query(None, description="Hoja a importar (default: todas las HITOS)"),
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Bulk-import hitos from SISTEMA DE HITOS.xlsx. JUNIOR and PLENO sheets have
    different columns; nivel comes from the sheet name. Maps abogado by name and
    (pleno) tipo by its label; skips duplicates and rows it can't resolve. Row
    parsing/matching/dedup all come from ``_run_hito_import`` — the same helper
    ``POST /importar/preview`` uses — so this endpoint's contract/behavior is
    unchanged; only its internals were extracted to share with the preview."""
    wb = _open_hito_wb(await archivo.read())
    sheets = _hito_sheets(wb, hoja)
    if hoja and not sheets:
        wb.close()
        raise HTTPException(status_code=404, detail=f"La hoja '{hoja}' no existe en el archivo")

    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    run = _run_hito_import(db, wb, sheets, admin, admin_rut)
    wb.close()

    nuevos = [o.hito for o in run.all_outcomes if o.resultado == "nueva" and o.hito is not None]
    if nuevos:
        db.bulk_save_objects(nuevos)
        db.commit()

    return HitoImportResult(
        total_leidas=run.total, creadas=run.creadas, aprobados=run.aprobados,
        pendientes=run.pendientes, omitidas_duplicadas=run.dup, errores=run.err,
    )


@router.get("/{hito_id}/evidencia")
async def get_evidencia(
    hito_id: int,
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Stream a hito's PJUD evidence. Admins, or the owning lawyer, only."""
    actor = _resolve_lawyer(db, current_lawyer)
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if not _is_admin(actor) and (actor is None or actor.id != hito.lawyer_id):
        raise HTTPException(status_code=403, detail="Sin acceso a esta evidencia")
    return _stream_evidencia(hito)


@router.put("/{hito_id}/evidencia", response_model=HitoResponse)
async def put_evidencia(
    hito_id: int,
    evidencia: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Attach or replace a hito's PJUD evidence after creation.

    Admins, or the owning lawyer, only — same boundary as ``GET /{id}/evidencia``.
    Allowed only while the hito is pendiente, sugerido or rechazado (see
    ``_attach_evidencia``): an approved hito was paid on the evidence it had, so
    it is frozen. Same validation and storage rules as create (``_store_evidencia``).
    """
    actor = _resolve_lawyer(db, current_lawyer)
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if not _is_admin(actor) and (actor is None or actor.id != hito.lawyer_id):
        raise HTTPException(status_code=403, detail="Sin acceso a esta evidencia")
    return _to_response(await _attach_evidencia(db, hito, evidencia))
