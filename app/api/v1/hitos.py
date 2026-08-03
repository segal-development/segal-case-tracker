"""Hitos (milestone → bonus) endpoints — Slice 1.

Replaces the manual "SISTEMA DE HITOS" sheet: a pre-loaded hito-type catalog, a
guided entry with a MANDATORY PJUD evidence capture, admin approval, and a
per-lawyer monthly total. Firm rule enforced here: a hito can never be approved
without evidence ("sin evidencia no se paga").
"""
import hashlib
import io
import logging
import re
import unicodedata
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db, require_admin
from app.models.hito import Hito, HitoTipo, HITO_APROBADO, HITO_PENDIENTE, HITO_RECHAZADO
from app.models.lawyer import Lawyer
from app.services import bono_cierre_service as cierre_svc

# A ROL like "C-9960-2026" embedded anywhere in the free-text descripcion.
_ROL_RE = re.compile(r"[A-Za-z]+-\d+-\d{4}")


def _causa_key(descripcion: Optional[str]) -> Optional[str]:
    """Causa identifier used for hito dedup.

    ``rol_causa`` stores the CLIENT RUT, so a lawyer could legitimately earn hitos
    for the same client across DIFFERENT causas. The distinguishing causa lives in
    ``descripcion`` (a ROL like ``C-9960-2026``), so dedup keys on
    ``(lawyer, rol_causa, _causa_key(descripcion))``: the ROL when present, else the
    normalized free text. ``None`` for empty descripcion.
    """
    if not descripcion:
        return None
    m = _ROL_RE.search(descripcion)
    return (m.group(0).upper() if m else descripcion.strip().upper()[:120]) or None

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


class HitoResumenRow(BaseModel):
    lawyer_id: int
    lawyer_nombre: str
    aprobados: int
    total_bruto: int
    pendientes: int


class RechazoBody(BaseModel):
    motivo: Optional[str] = None


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

    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")

    tipo = db.query(HitoTipo).filter(HitoTipo.id == hito_tipo_id, HitoTipo.activo.is_(True)).first()
    if tipo is None:
        raise HTTPException(status_code=404, detail="Tipo de hito no encontrado")

    # A lawyer registers hitos for themselves; only an admin may set another lawyer.
    target_lawyer_id = actor.id
    if lawyer_id is not None and lawyer_id != actor.id:
        if not _is_admin(actor):
            raise HTTPException(status_code=403, detail="Solo un admin puede registrar hitos de otro abogado")
        if db.query(Lawyer).filter(Lawyer.id == lawyer_id).first() is None:
            raise HTTPException(status_code=404, detail="Abogado no encontrado")
        target_lawyer_id = lawyer_id

    # No duplicate on the SAME causa: a lawyer may repeat the same client
    # (rol_causa = RUT) across DIFFERENT causas, so the block is keyed on
    # (abogado, RUT, causa) — the causa is derived from descripcion (see
    # _causa_key). Only enforced when a rol_causa is given. Checked before storing
    # evidence so a rejected duplicate never uploads a file.
    rol_norm = (rol_causa or "").strip() or None
    if rol_norm is not None:
        causa = _causa_key(descripcion)
        prior = (
            db.query(Hito.descripcion)
            .filter(Hito.lawyer_id == target_lawyer_id, Hito.rol_causa == rol_norm)
            .all()
        )
        if any(_causa_key(d) == causa for (d,) in prior):
            raise HTTPException(
                status_code=409,
                detail="Ya existe un hito de este abogado para esa causa",
            )

    # Evidence is optional. If provided, validate + store it.
    storage_uri = ev_filename = ev_content_type = None
    data = await evidencia.read() if evidencia is not None else b""
    if data:
        if len(data) > _MAX_EVIDENCE_BYTES:
            raise HTTPException(status_code=413, detail="La evidencia supera el tamaño máximo (15 MB)")
        content_type = evidencia.content_type or "application/octet-stream"
        if content_type not in _ALLOWED_EVIDENCE:
            raise HTTPException(
                status_code=415,
                detail="Formato de evidencia no permitido (usa PNG, JPG, WEBP o PDF)",
            )
        from app.config import settings
        from app.services.storage_service import get_storage_backend

        digest = hashlib.sha256(data).hexdigest()[:16]
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}.get(content_type, "bin")
        key = f"hitos/evidencia/{target_lawyer_id}/{digest}.{ext}"
        storage_uri = get_storage_backend(settings).upload(data, key, content_type=content_type)
        ev_filename = evidencia.filename
        ev_content_type = content_type

    hito = Hito(
        lawyer_id=target_lawyer_id,
        hito_tipo_id=tipo.id,
        valor_bruto=tipo.valor_bruto,  # snapshot
        fecha_hito=fecha_hito,
        rol_causa=rol_norm,
        procedimiento=procedimiento,
        descripcion=descripcion,
        etapa_sysgal=etapa_sysgal or tipo.etapa_tramite,
        tramite_sysgal=tramite_sysgal,
        evidencia_storage_key=storage_uri,
        evidencia_filename=ev_filename,
        evidencia_content_type=ev_content_type,
        estado=HITO_PENDIENTE,
        created_by_rut=actor.rut,
        created_by_name=actor.name,
    )
    db.add(hito)
    db.commit()
    db.refresh(hito)
    return _to_response(hito)


@router.get("", response_model=List[HitoResponse])
async def list_hitos(
    periodo: Optional[str] = Query(None, description="Filtrar por mes YYYY-MM"),
    lawyer_id: Optional[int] = Query(None),
    estado: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """List hitos. Admins see all; a lawyer sees only their own."""
    actor = _resolve_lawyer(db, current_lawyer)
    if actor is None:
        raise HTTPException(status_code=401, detail="No se pudo resolver el abogado")

    q = db.query(Hito)
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

    return [_to_response(h) for h in q.order_by(Hito.fecha_hito.desc(), Hito.id.desc()).all()]


@router.post("/{hito_id}/aprobar", response_model=HitoResponse)
async def aprobar_hito(
    hito_id: int,
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Approve a hito (admin only)."""
    hito = db.query(Hito).filter(Hito.id == hito_id).first()
    if hito is None:
        raise HTTPException(status_code=404, detail="Hito no encontrado")
    if cierre_svc.is_cerrado(db, cierre_svc.periodo_de_fecha(hito.fecha_hito)):
        raise HTTPException(status_code=409, detail="El período de ese hito está cerrado")

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


def _norm(s) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper().strip()


def _cell(row, i):
    return row[i] if row is not None and i < len(row) else None


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


def _hito_sheets(wb, hoja: Optional[str]):
    """Only the HITOS sheets (skip PARÁMETROS, VARIABLES BONO, etc.)."""
    sheets = [ws for ws in wb.worksheets if "HITO" in _norm(ws.title)]
    if hoja:
        sheets = [ws for ws in sheets if _norm(ws.title) == _norm(hoja)]
    return sheets


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


def _match_pleno_tipo(text: str, pleno_tipos) -> Optional[HitoTipo]:
    n = _norm(text)
    if not n:
        return None
    for t in pleno_tipos:
        lt = _norm(t.label)
        if n == lt or n in lt or lt in n:
            return t
    ntok = set(n.split())
    best, best_k = None, 0
    for t in pleno_tipos:
        k = len(ntok & set(_norm(t.label).split()))
        if k > best_k:
            best, best_k = t, k
    return best if best_k >= 2 else None


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

    # No duplicate: the resulting (abogado, RUT, causa) must not collide with
    # ANOTHER hito. Same client on a DIFFERENT causa is allowed (see _causa_key).
    rol_norm = (body.rol_causa or "").strip() or None
    if rol_norm is not None:
        causa = _causa_key(body.descripcion)
        prior = (
            db.query(Hito.descripcion)
            .filter(
                Hito.id != hito.id,
                Hito.lawyer_id == hito.lawyer_id,
                Hito.rol_causa == rol_norm,
            )
            .all()
        )
        if any(_causa_key(d) == causa for (d,) in prior):
            raise HTTPException(
                status_code=409,
                detail="Ya existe un hito de este abogado para esa causa",
            )

    hito.hito_tipo_id = tipo.id
    hito.valor_bruto = tipo.valor_bruto  # re-snapshot
    hito.fecha_hito = body.fecha_hito
    hito.rol_causa = rol_norm
    hito.procedimiento = body.procedimiento
    hito.descripcion = body.descripcion
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
    out = [
        HitoHojaInfo(nombre=ws.title, filas=sum(1 for r in ws.iter_rows(values_only=True) if _is_hito_row(r)))
        for ws in _hito_sheets(wb, None)
    ]
    wb.close()
    return out


@router.post("/importar", response_model=HitoImportResult)
async def importar_hitos(
    archivo: UploadFile = File(...),
    hoja: Optional[str] = Query(None, description="Hoja a importar (default: todas las HITOS)"),
    db: Session = Depends(get_db),
    admin_rut: str = Depends(require_admin),
):
    """Bulk-import hitos from SISTEMA DE HITOS.xlsx. JUNIOR and PLENO sheets have
    different columns; nivel comes from the sheet name. Maps abogado by name and
    (pleno) tipo by its label; skips duplicates and rows it can't resolve."""
    wb = _open_hito_wb(await archivo.read())
    sheets = _hito_sheets(wb, hoja)
    if hoja and not sheets:
        wb.close()
        raise HTTPException(status_code=404, detail=f"La hoja '{hoja}' no existe en el archivo")

    admin = db.query(Lawyer).filter(Lawyer.rut == admin_rut).first()
    lawyers = db.query(Lawyer).filter(Lawyer.is_firm_lawyer.is_(True)).all()
    tipos = db.query(HitoTipo).all()
    junior_tipo = next((t for t in tipos if t.nivel == "junior"), None)
    pleno_tipos = [t for t in tipos if t.nivel == "pleno"]
    # Dedup key: (abogado, RUT, causa) — matching the create/edit rule. Same client
    # (rol_causa = RUT) on a DIFFERENT causa (see _causa_key) is NOT a duplicate. A
    # hito without a rol_causa can't collide, so those are excluded here.
    existing = {
        (h.lawyer_id, h.rol_causa, _causa_key(h.descripcion))
        for h in db.query(Hito.lawyer_id, Hito.rol_causa, Hito.descripcion)
        .filter(Hito.rol_causa.isnot(None))
        .all()
    }

    total = creadas = aprobados = pendientes = dup = err = 0
    seen: set = set()
    nuevos: list[Hito] = []

    for ws in sheets:
        is_junior = "JUNIOR" in _norm(ws.title)
        for row in ws.iter_rows(values_only=True):
            if not _is_hito_row(row):
                continue
            total += 1
            fecha = _cell(row, 2)
            if not isinstance(fecha, datetime):
                err += 1
                continue
            lawyer = _match_lawyer_by_name(_cell(row, 1), lawyers)
            if lawyer is None:
                err += 1
                continue

            if is_junior:
                procedimiento = _cell(row, 3)
                rut = _cell(row, 4)
                descripcion = _cell(row, 5)
                tipo = junior_tipo
            else:  # pleno
                procedimiento = None
                rut = _cell(row, 3)
                descripcion = None
                tipo = _match_pleno_tipo(_cell(row, 5), pleno_tipos)
            etapa = _cell(row, 6)
            tramite = _cell(row, 7)
            aprobado = _norm(_cell(row, 8)) in ("SI", "SÍ", "S")
            if tipo is None:
                err += 1
                continue

            try:
                valor = int(_cell(row, 9)) if _cell(row, 9) else tipo.valor_bruto
            except (ValueError, TypeError):
                valor = tipo.valor_bruto
            if valor <= 0:
                valor = tipo.valor_bruto

            desc_norm = (str(descripcion).strip()[:500] if descripcion else None)
            rol_causa = (str(rut).strip()[:50] or None) if rut is not None else None
            # (abogado, RUT, causa) dedup — same client on a different causa is NOT a
            # duplicate. Rol-less rows can't collide → always insert.
            if rol_causa is not None:
                key = (lawyer.id, rol_causa, _causa_key(desc_norm))
                if key in existing or key in seen:
                    dup += 1
                    continue
                seen.add(key)
            estado = HITO_APROBADO if aprobado else HITO_PENDIENTE
            nuevos.append(Hito(
                lawyer_id=lawyer.id,
                hito_tipo_id=tipo.id,
                valor_bruto=valor,
                fecha_hito=fecha.date(),
                rol_causa=rol_causa,
                procedimiento=(str(procedimiento).strip()[:100] if procedimiento else None),
                descripcion=desc_norm,
                etapa_sysgal=(str(etapa).strip()[:100] if etapa else None),
                tramite_sysgal=(str(tramite).strip()[:255] if tramite else None),
                estado=estado,
                created_by_rut=admin_rut,
                created_by_name=admin.name if admin else None,
                aprobado_by_rut=admin_rut if aprobado else None,
                aprobado_by_name=(admin.name if admin else None) if aprobado else None,
                aprobado_at=datetime.utcnow() if aprobado else None,
            ))
            creadas += 1
            if aprobado:
                aprobados += 1
            else:
                pendientes += 1

    wb.close()
    if nuevos:
        db.bulk_save_objects(nuevos)
        db.commit()
    return HitoImportResult(
        total_leidas=total, creadas=creadas, aprobados=aprobados, pendientes=pendientes,
        omitidas_duplicadas=dup, errores=err,
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
