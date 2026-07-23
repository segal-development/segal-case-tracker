"""Template-based .docx court filings (system requirement #3).

Unlike ``document_generation.build_escrito_oposicion`` — which hand-builds a
filing run-by-run in code — this renders the firm's OWN ``.docx`` models as
``docxtpl`` (Jinja2-in-Word) templates: the firm's exact formatting is
preserved and only the ``{{ variables }}`` are filled from case data. Facts the
system cannot derive (montos, fechas del título, profesión) are rendered as
visible «INDICAR …» prompts for the lawyer to complete in Word.

Each template maps to a defence recommendation the DecisionEngine already
fires (e.g. ``solicitar_abandono``), so the UI can offer "generar escrito"
straight from the recommendation. Nothing is persisted here — the caller streams
the bytes and records provenance.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from docxtpl import DocxTemplate

from app.services.document_generation import (
    _format_spanish_date,
    resolve_parties,
)
from app.utils.rut import format_rut

TEMPLATES_DIR = Path(__file__).parent / "document_templates"

# Firm constant — the procesal domicile / patrocinio address used across the
# firm's models. Kept here (not a per-case field) because it never varies.
DOMICILIO_ESTUDIO = "Santa Lucía N°232, oficina 41, Santiago"


def _prompt(label: str) -> str:
    """A visible, editable placeholder for a fact the system can't derive."""
    return f"«INDICAR {label}»"


def _derive_sjl(tribunal: Optional[str]) -> str:
    """Turn '15º Juzgado Civil de Santiago' → 'S.J.L. Civil de Santiago (15°)'.

    Falls back to a reasonable rendering when the court name doesn't match the
    expected shape, so the escrito always has SOMETHING sensible on that line.
    """
    if not tribunal:
        return _prompt("TRIBUNAL")
    m = re.match(r"\s*(\d+)\s*[º°]?\s*Juzgado\s+Civil\s+de\s+(.+)", tribunal, re.IGNORECASE)
    if m:
        return f"S.J.L. Civil de {m.group(2).strip()} ({m.group(1)}°)"
    return f"S.J.L. Civil ({tribunal})"


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------
def _common_context(
    case,
    litigantes,
    court_name: Optional[str],
    lawyer_name: Optional[str],
    lawyer_rut: Optional[str],
    lawyer_email: Optional[str],
) -> dict:
    """Variables shared by EVERY escrito: header, parties, patrocinio.

    Derivable values come from case data / the authenticated lawyer; anything
    absent becomes an «INDICAR …» prompt so the escrito is never silently blank.
    """
    # Parties: prefer the PJUD litigantes (cleaner names); fall back to the
    # parsed caratulado columns (plaintiff/defendant). Derive the carátula from
    # the SAME source so header and body stay coherent.
    ejecutante, ejecutado = resolve_parties(litigantes)
    ejecutante_nombre = (
        ejecutante.nombre if ejecutante and ejecutante.nombre
        else getattr(case, "plaintiff", None)
    )
    ejecutado_nombre = (
        ejecutado.nombre if ejecutado and ejecutado.nombre
        else getattr(case, "defendant", None)
    )
    caratulado = (
        f"{ejecutante_nombre}/{ejecutado_nombre}"
        if ejecutante_nombre and ejecutado_nombre
        else None
    )
    tribunal = court_name or _prompt("TRIBUNAL")
    return {
        "tribunal": tribunal,
        "sjl": _derive_sjl(court_name),
        "rol": getattr(case, "rol", None) or _prompt("ROL"),
        "caratulado": caratulado or _prompt("CARÁTULA DE LA CAUSA"),
        "ejecutante_nombre": ejecutante_nombre or _prompt("EJECUTANTE"),
        "ejecutado_nombre": ejecutado_nombre or _prompt("EJECUTADO"),
        "ejecutado_rut": (
            (format_rut(ejecutado.rut) or ejecutado.rut)
            if ejecutado and ejecutado.rut else _prompt("RUT DEL EJECUTADO")
        ),
        "abogado_nombre": lawyer_name or _prompt("ABOGADO PATROCINANTE"),
        "abogado_rut": (format_rut(lawyer_rut) or lawyer_rut) if lawyer_rut else _prompt("RUT DEL ABOGADO"),
        "abogado_email": lawyer_email or _prompt("EMAIL DEL ABOGADO"),
        "domicilio_estudio": DOMICILIO_ESTUDIO,
    }


def _abandono_context(
    case, litigantes, court_name, lawyer_name, lawyer_rut, lawyer_email
) -> dict:
    """Abandono del procedimiento (3 años, art. 153 inc. 2 CPC)."""
    ctx = _common_context(case, litigantes, court_name, lawyer_name, lawyer_rut, lawyer_email)
    # The 3-year clock runs from the last útil gestión in apremio — legally
    # load-bearing and NOT reliably the last movement, so we prompt it, seeding
    # the last-movement date as a hint the lawyer confirms/replaces.
    hint = _format_spanish_date(getattr(case, "last_movement_at", None))
    ctx["fecha_ultima_gestion"] = (
        f"«CONFIRMAR FECHA ÚLTIMA GESTIÓN ÚTIL (aprox. {hint})»"
        if hint
        else _prompt("FECHA ÚLTIMA GESTIÓN ÚTIL")
    )
    ctx["ejecutado_profesion"] = _prompt("PROFESIÓN DEL EJECUTADO")
    ctx["monto_demanda"] = _prompt("MONTO DEMANDADO")
    return ctx


def _objeta_remate_context(
    case, litigantes, court_name, lawyer_name, lawyer_rut, lawyer_email
) -> dict:
    """Objeta bases de remate (cuaderno de apremio). Header + abogado; el cuerpo
    (cláusulas objetadas) es texto fijo — nada más que derivar."""
    return _common_context(case, litigantes, court_name, lawyer_name, lawyer_rut, lawyer_email)


def _prescripcion_context(
    case, litigantes, court_name, lawyer_name, lawyer_rut, lawyer_email
) -> dict:
    """Excepción de prescripción de la acción ejecutiva (art. 464 N°17), pagaré
    en cuotas. Monto/profesión no son derivables → «INDICAR …»."""
    ctx = _common_context(case, litigantes, court_name, lawyer_name, lawyer_rut, lawyer_email)
    ejecutante, _ejecutado = resolve_parties(litigantes)
    ctx["ejecutante_rut"] = (
        (format_rut(ejecutante.rut) or ejecutante.rut)
        if ejecutante and ejecutante.rut else _prompt("RUT DEL EJECUTANTE")
    )
    ctx["ejecutado_profesion"] = _prompt("PROFESIÓN DEL EJECUTADO")
    ctx["monto_demanda"] = _prompt("MONTO DEMANDADO (CAPITAL)")
    # LOS HECHOS narrative: facts of the specific pagaré. None are derivable from
    # PJUD data — each is a visible prompt so no other debtor's figures leak in.
    ctx["ejecutante_representacion"] = _prompt("REPRESENTANTE Y DOMICILIO DEL EJECUTANTE")
    ctx["tasa_interes"] = _prompt("TASA DE INTERÉS")
    ctx["numero_cuotas"] = _prompt("N° DE CUOTAS")
    ctx["monto_cuota"] = _prompt("MONTO POR CUOTA")
    ctx["fecha_primera_cuota"] = _prompt("FECHA 1ª CUOTA")
    ctx["saldo_adeudado"] = _prompt("SALDO ADEUDADO")
    ctx["fecha_mora"] = _prompt("FECHA DE MORA")
    ctx["mes_demanda"] = _prompt("MES/AÑO DE LA DEMANDA")
    return ctx


# ---------------------------------------------------------------------------
# Registry — document_type → template + which recommendation offers it
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TemplateSpec:
    document_type: str
    template_filename: str
    # DecisionEngine recommendation code that offers this document (UI wiring).
    recommendation_code: str
    # Slug used to name the downloaded file.
    filename_prefix: str
    build_context: Callable[..., dict]


TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {
    "abandono_3anios": TemplateSpec(
        document_type="abandono_3anios",
        template_filename="abandono_3anios.docx",
        recommendation_code="solicitar_abandono",
        filename_prefix="abandono_procedimiento",
        build_context=_abandono_context,
    ),
    "prescripcion_cuotas": TemplateSpec(
        document_type="prescripcion_cuotas",
        template_filename="prescripcion_cuotas.docx",
        recommendation_code="oponer_excepciones",
        filename_prefix="excepcion_prescripcion",
        build_context=_prescripcion_context,
    ),
    "objeta_remate": TemplateSpec(
        document_type="objeta_remate",
        template_filename="objeta_remate.docx",
        recommendation_code="objetar_remate",
        filename_prefix="objeta_bases_remate",
        build_context=_objeta_remate_context,
    ),
}


def is_template_document(document_type: str) -> bool:
    return document_type in TEMPLATE_REGISTRY


def render_template_document(
    *,
    document_type: str,
    case,
    litigantes,
    court_name: Optional[str],
    acting_lawyer_name: Optional[str],
    acting_lawyer_rut: Optional[str],
    acting_lawyer_email: Optional[str],
) -> bytes:
    """Render a registered template to .docx bytes, filled from case data."""
    spec = TEMPLATE_REGISTRY[document_type]
    context = spec.build_context(
        case, litigantes, court_name, acting_lawyer_name, acting_lawyer_rut, acting_lawyer_email
    )
    template = DocxTemplate(str(TEMPLATES_DIR / spec.template_filename))
    template.render(context)
    buffer = io.BytesIO()
    template.save(buffer)
    return buffer.getvalue()
