"""Motor de detección de hitos desde movimientos PJUD (Fase 1).

Lógica PURA y testeable (sin DB ni GCS): dado el ETAPA/TRÁMITE de un movimiento
y el TEXTO del PDF de su resolución, decide si corresponde un hito candidato y
con qué confianza. La orquestación (consultar movimientos, bajar el PDF de GCS,
atribución, ventana temporal, persistir) vive en el job del Slice 3.

Diseño de dos etapas (ver docs/guia-metricas-hitos.md):
  A. `regla_para_movimiento` — filtro barato por metadata (ETAPA/TRÁMITE) → regla.
  B. `clasificar_favorable`  — sobre el texto del PDF: ¿la resolución FAVORECIÓ?
     (positivos, veto negativo, y firmeza cuando el hito lo exige).

MVP: 3 hitos limpios (prescripción, exhibición, abandono 3 años). Las reglas son
declarativas y se amplían sin tocar la lógica.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _norm(t: Optional[str]) -> str:
    """Minúsculas + sin tildes + espacios colapsados, para matching robusto."""
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).lower().strip()


@dataclass(frozen=True)
class ReglaHito:
    """Mapea una señal PJUD (ETAPA/TRÁMITE) a un tipo de hito + su condición de
    resultado favorable. Todos los patrones/keywords en forma NORMALIZADA."""

    code: str                    # id de la regla (regla_code del hito)
    hito_tipo_code: str          # HitoTipo.code destino
    stage_patterns: Tuple[str, ...]      # substrings que califican la ETAPA
    procedure_patterns: Tuple[str, ...]  # substrings que califican el TRÁMITE
    kw_favorable: Tuple[str, ...]        # frases positivas en el texto del PDF
    kw_veto: Tuple[str, ...]             # frases de rechazo → NO es hito
    requiere_firmeza: bool               # exige resolución firme/ejecutoriada


# Marcadores de firmeza (normalizados). Requeridos por los hitos que pagan solo
# sobre resolución firme (M1 Alta y cierres).
FIRMEZA_KW: Tuple[str, ...] = ("firme", "ejecutoriad", "certifiquese ejecutoria")

# Catálogo de reglas MVP (Fase 1). Códigos de hito verificados contra hito_tipos.
REGLAS: Tuple[ReglaHito, ...] = (
    ReglaHito(
        code="prescripcion",
        hito_tipo_code="pleno_prescripcion",
        stage_patterns=("excepci",),
        procedure_patterns=("resoluci",),
        kw_favorable=(
            "acoge la excepcion de prescripcion",
            "acoge la prescripcion",
            "prescripcion extintiva",
            "ha lugar",
            "se acoge",
        ),
        kw_veto=("no ha lugar", "se rechaza", "rechazase", "deniega", "desestima"),
        requiere_firmeza=True,
    ),
    ReglaHito(
        code="exhibicion",
        hito_tipo_code="pleno_exhibicion",
        stage_patterns=("prejudic", "exhibici", "medida prejud"),
        procedure_patterns=("resoluci",),
        kw_favorable=("tengase por exhibido", "ha lugar a la exhibicion", "se acoge", "ha lugar"),
        kw_veto=("no ha lugar", "se rechaza", "deniega"),
        requiere_firmeza=False,
    ),
    ReglaHito(
        code="abandono_3a",
        hito_tipo_code="pleno_abandono_3a",
        stage_patterns=("tramitaci", "abandono"),
        procedure_patterns=("resoluci",),
        kw_favorable=(
            "declara abandonado",
            "se declara el abandono",
            "abandono del procedimiento",
            "ha lugar al abandono",
        ),
        kw_veto=("no ha lugar", "se rechaza", "deniega"),
        requiere_firmeza=True,
    ),
)


def regla_para_movimiento(stage: Optional[str], procedure: Optional[str]) -> Optional[ReglaHito]:
    """Etapa A — ¿este movimiento (por ETAPA/TRÁMITE) califica para alguna regla?"""
    s = _norm(stage)
    p = _norm(procedure)
    if not s or not p:
        return None
    for r in REGLAS:
        if any(pp in p for pp in r.procedure_patterns) and any(sp in s for sp in r.stage_patterns):
            return r
    return None


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


@dataclass
class Clasificacion:
    es_candidato: bool          # ¿crear el hito candidato?
    confianza: str              # "alta" | "media" | "baja"
    frase: Optional[str]        # frase que gatilló (evidencia para Carla)


def clasificar_favorable(regla: ReglaHito, texto: str) -> Clasificacion:
    """Etapa B — sobre el texto del PDF: ¿la resolución fue favorable al deudor?

    - Veto primero: cualquier rechazo ("no ha lugar", "se rechaza") descarta.
    - Positivo + (firmeza si la regla la exige) → confianza alta; sin firmeza → media.
    - Sin texto (PDF escaneado/ilegible) → candidato de BAJA confianza para que
      Carla lo lea a mano (no lo descartamos por no poder leerlo).
    """
    t = _norm(texto)
    if not t:
        return Clasificacion(True, "baja", "PDF no legible — revisar manualmente")
    for kw in regla.kw_veto:
        if kw in t:  # veto ya viene normalizado
            return Clasificacion(False, "alta", kw)
    frase = next((kw for kw in regla.kw_favorable if kw in t), None)
    if frase is None:
        return Clasificacion(False, "baja", None)
    if regla.requiere_firmeza and not any(f in t for f in FIRMEZA_KW):
        return Clasificacion(True, "media", frase)
    return Clasificacion(True, "alta", frase)


@dataclass
class DeteccionResultado:
    hito_tipo_code: str
    regla_code: str
    confianza: str
    frase: Optional[str]


def evaluar_movimiento(
    stage: Optional[str], procedure: Optional[str], pdf_texto: str
) -> Optional[DeteccionResultado]:
    """Pipeline completo sobre UN movimiento (con el texto del PDF ya extraído).

    Devuelve el hito candidato o ``None`` si no califica / la resolución no fue
    favorable. La atribución, ventana temporal y persistencia las hace el job.
    """
    regla = regla_para_movimiento(stage, procedure)
    if regla is None:
        return None
    cl = clasificar_favorable(regla, pdf_texto)
    if not cl.es_candidato:
        return None
    return DeteccionResultado(regla.hito_tipo_code, regla.code, cl.confianza, cl.frase)


# --------------------------------------------------------------------------- #
# Orquestación (DB + GCS) — Slice 3
# --------------------------------------------------------------------------- #
from app.models.case import Case
from app.models.document import Document
from app.models.hito import HITO_SUGERIDO, ORIGEN_DETECTOR, Hito, HitoTipo
from app.models.lawyer import Lawyer
from app.models.movement import Movement


@dataclass
class DeteccionResumen:
    periodo: str
    cerrado: bool
    creados: int          # hitos sugeridos creados
    ya_existe: int        # movimientos que ya tenían un hito (idempotencia)
    rechazados: int       # regla matcheó pero la resolución no fue favorable
    sin_pdf: int          # candidato sin PDF stored → sin evidencia, se salta
    sin_atribucion: int   # causa no atribuible a un abogado del estudio


def _period_bounds(periodo: str) -> Tuple[date, date]:
    """('YYYY-MM') → (primer día, primer día del mes siguiente)."""
    y, m = int(periodo[:4]), int(periodo[5:7])
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start, end


class HitoDetectorService:
    """Detecta hitos candidatos sobre los movimientos de un período abierto.

    Recorre las RESOLUCIONES con ``movement_date`` en el mes (nunca por
    ``created_at`` — ver ventana temporal en docs/guia-metricas-hitos.md), matchea
    la regla, baja el PDF de GCS, clasifica el resultado y crea el hito en estado
    ``sugerido`` con la evidencia PJUD adjunta, atribuido al abogado de récord.
    """

    def __init__(self, db, storage=None):
        self.db = db
        self._storage = storage

    def _storage_backend(self):
        if self._storage is None:
            from app.config import settings
            from app.services.storage_service import get_storage_backend

            self._storage = get_storage_backend(settings)
        return self._storage

    def _mapa_case_lawyer(self) -> dict:
        """case_id → lawyer_id del abogado de récord (litigante), primer abogado gana."""
        from app.services.lawyer_roster import case_ids_for_abogado

        mapa: dict = {}
        lawyers = (
            self.db.query(Lawyer)
            .filter(Lawyer.is_firm_lawyer.is_(True), Lawyer.is_active.is_(True))
            .all()
        )
        for lw in lawyers:
            for cid in case_ids_for_abogado(self.db, lw.rut, lw.rut):
                mapa.setdefault(cid, lw.id)
        return mapa

    def _doc_stored(self, movement_id: int):
        return (
            self.db.query(Document)
            .filter(
                Document.movement_id == movement_id,
                Document.status == "stored",
                Document.gcs_path.isnot(None),
            )
            .first()
        )

    def _texto(self, doc) -> str:
        try:
            return extraer_texto_pdf(self._storage_backend().retrieve(doc.gcs_path))
        except Exception:  # noqa: BLE001 — un PDF ilegible degrada, no rompe
            return ""

    def detectar(self, periodo: str) -> DeteccionResumen:
        from app.services import bono_cierre_service as cierre_svc

        start, end = _period_bounds(periodo)
        if cierre_svc.is_cerrado(self.db, periodo):
            return DeteccionResumen(periodo, True, 0, 0, 0, 0, 0)

        case_lawyer = self._mapa_case_lawyer()
        tipos = {
            t.code: t
            for t in self.db.query(HitoTipo)
            .filter(HitoTipo.code.in_(tuple({r.hito_tipo_code for r in REGLAS})))
            .all()
        }
        ya = {
            mid
            for (mid,) in self.db.query(Hito.movement_id).filter(Hito.movement_id.isnot(None)).all()
        }

        # Solo RESOLUCIONES del período (por movement_date), en causas no archivadas.
        movs = (
            self.db.query(Movement)
            .join(Case, Movement.case_id == Case.id)
            .filter(
                Movement.movement_date >= start,
                Movement.movement_date < end,
                Movement.procedure.ilike("%resoluci%"),
                Case.status != "archived",
            )
            .all()
        )

        creados = ya_existe = rechazados = sin_pdf = sin_atrib = 0
        for mv in movs:
            regla = regla_para_movimiento(mv.stage, mv.procedure)
            if regla is None:
                continue
            if mv.id in ya:
                ya_existe += 1
                continue
            lawyer_id = case_lawyer.get(mv.case_id)
            if lawyer_id is None:
                sin_atrib += 1
                continue
            doc = self._doc_stored(mv.id)
            if doc is None:
                sin_pdf += 1  # sin captura PJud → sin hito (regla del estudio)
                continue
            det = evaluar_movimiento(mv.stage, mv.procedure, self._texto(doc))
            if det is None:
                rechazados += 1
                continue
            tipo = tipos.get(det.hito_tipo_code)
            if tipo is None:
                continue
            rol = mv.case.rol if mv.case else None
            desc = " · ".join(x for x in (rol, det.frase) if x) or None
            self.db.add(
                Hito(
                    lawyer_id=lawyer_id,
                    hito_tipo_id=tipo.id,
                    valor_bruto=tipo.valor_bruto,
                    fecha_hito=mv.movement_date.date(),
                    case_id=mv.case_id,
                    movement_id=mv.id,
                    estado=HITO_SUGERIDO,
                    origen=ORIGEN_DETECTOR,
                    regla_code=det.regla_code,
                    confianza=det.confianza,
                    descripcion=(desc[:500] if desc else None),
                    evidencia_storage_key=doc.gcs_path,
                    evidencia_filename=doc.filename,
                    evidencia_content_type=doc.content_type,
                    created_by_name="Detector PJUD",
                )
            )
            creados += 1

        self.db.commit()
        return DeteccionResumen(periodo, False, creados, ya_existe, rechazados, sin_pdf, sin_atrib)
