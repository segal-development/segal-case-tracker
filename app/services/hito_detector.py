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
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple


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
