"""Emails of the plazo semaforo: one immediate email per amarillo/rojo alert and
one grouped email per lawyer for the day's new verde plazos.

Same visual language as ``movement_digest`` / ``daily_agenda`` (it imports their
brand and section card instead of copying them). Neutral Spanish, tuteo (tu).
No I/O: sending and the ``Alert.email_sent`` bookkeeping live in
``NotificationService``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, List, Set, Tuple

from app.core.semaforo_config import LEVEL_BY_ALERT_TYPE, Level
from app.services.daily_agenda import _BRAND, _section_html

_LEVEL_COLOR = {
    Level.VERDE: "#15803d",
    Level.AMARILLO: "#b45309",
    Level.ROJO: "#dc2626",
}


@dataclass
class PlazoEntry:
    """Snapshot of one new verde plazo, taken when it was queued.

    Plain values (not ORM objects) so rendering never lazy-loads. ``alert`` is
    kept so the flush can mark ``email_sent`` on exactly what a sent email
    covered.
    """

    alert: Any
    case_id: Any
    rol: str
    caratulado: str
    court_name: str
    title: str
    message: str


class PlazoBucket:
    """All the new verde plazos of ONE lawyer waiting for the grouped email.

    Unlike the movement digest there is no cap: a verde plazo that is not in
    the email would stay ``email_sent=false`` meaning "not sent yet", and a
    cap would turn that into "deliberately not sent". ~11 per lawyer per day
    is not an email nobody reads.
    """

    def __init__(self) -> None:
        self.entries: List[PlazoEntry] = []
        self.case_ids: Set[Any] = set()

    def add(self, entry: PlazoEntry) -> None:
        self.entries.append(entry)
        self.case_ids.add(entry.case_id)

    @property
    def total(self) -> int:
        return len(self.entries)


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _paragraphs_html(message: str) -> str:
    out = []
    for line in (message or "").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(
            f'<div style="color:#374151; font-size:13px; margin-top:4px;">{html.escape(line)}</div>'
        )
    return "\n".join(out)


def _shell(title: str, body_rows: str, footer: str) -> str:
    return f"""\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
</head>
<body style="margin:0; padding:0; background-color:#f4f5f7; font-family:Arial, Helvetica, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
          <tr>
            <td style="background-color:#0b2e4f; padding:20px 32px; color:#ffffff; font-size:15px; font-weight:bold; letter-spacing:0.3px;">
              {_BRAND}
            </td>
          </tr>
{body_rows}
          <tr>
            <td style="padding:24px 32px 32px 32px;">
              <hr style="border:none; border-top:1px solid #e5e7eb; margin:16px 0 16px 0;">
              <p style="margin:0; color:#9ca3af; font-size:12px; line-height:1.5;">
                {html.escape(footer)} Correo automático de {_BRAND}. No responder directamente a este mensaje.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def _case_line(rol: str, caratulado: str, court_name: str) -> str:
    caratulado_part = f" · {caratulado}" if caratulado.strip("/ ") else ""
    court = f" ({court_name})" if court_name else ""
    return f"{rol or '—'}{caratulado_part}{court}"


# ---------------------------------------------------------------------------
# Immediate email: one amarillo / rojo alert
# ---------------------------------------------------------------------------


def render_plazo_alert(lawyer: Any, alert: Any, case: Any) -> Tuple[str, str, str]:
    """``(subject, html_body, text_body)`` for one amarillo/rojo alert."""
    level = LEVEL_BY_ALERT_TYPE[alert.type]
    color = _LEVEL_COLOR[level]
    raw_name = getattr(lawyer, "name", None) or "Abogado(a)"
    rol = (case.rol if case is not None else "") or ""
    caratulado = (
        f"{case.plaintiff or ''}/{case.defendant or ''}" if case is not None else ""
    )
    court = case.court.name if case is not None and case.court else ""
    subject = alert.title

    body_rows = f"""\
          <tr>
            <td style="padding:32px 32px 8px 32px;">
              <p style="margin:0 0 4px 0; font-size:13px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px;">
                <span style="display:inline-block; background-color:{color}; color:#ffffff; padding:2px 8px; border-radius:4px;">Plazo en {level.value}</span>
              </p>
              <h1 style="margin:8px 0 0 0; color:#111827; font-size:20px; line-height:1.4;">Hola {html.escape(raw_name)}, {html.escape(alert.title)}</h1>
            </td>
          </tr>
{_section_html(html.escape(_case_line(rol, caratulado, court)), '''              <tr>
                <td style="padding:12px 16px;">
''' + _paragraphs_html(alert.message) + '''
                </td>
              </tr>''')}"""
    html_body = _shell(
        subject,
        body_rows,
        "Los plazos amarillos y rojos te llegan en el momento, uno por uno; "
        "los nuevos en verde, agrupados una vez al día.",
    )
    text_lines = [
        f"PLAZO EN {level.value.upper()} — {_BRAND}",
        "",
        f"Hola {raw_name},",
        "",
        _case_line(rol, caratulado, court),
        "",
        alert.title,
        "",
        alert.message or "",
        "",
    ]
    return subject, html_body, "\n".join(text_lines) + "\n"


# ---------------------------------------------------------------------------
# Grouped email: the day's new verde plazos of one lawyer
# ---------------------------------------------------------------------------


def verde_subject(plazos: int, causas: int) -> str:
    return (
        f"{plazos} {_plural(plazos, 'plazo nuevo', 'plazos nuevos')} "
        f"en {causas} {_plural(causas, 'causa', 'causas')}"
    )


def _group_by_case(entries: List[PlazoEntry]) -> List[Tuple[PlazoEntry, List[PlazoEntry]]]:
    groups: dict = {}
    for e in entries:
        groups.setdefault(e.case_id, []).append(e)
    return [(items[0], items) for items in groups.values()]


def _plazo_row_html(e: PlazoEntry) -> str:
    return f"""\
              <tr>
                <td style="padding:12px 16px; border-bottom:1px solid #f0f1f3;">
                  <div style="color:#111827; font-size:14px; font-weight:bold;">{html.escape(e.title)}</div>
{_paragraphs_html(e.message)}
                </td>
              </tr>"""


def render_plazo_verde_digest(lawyer: Any, bucket: PlazoBucket) -> Tuple[str, str, str]:
    """``(subject, html_body, text_body)`` for one lawyer's new verde plazos.

    Pure. The caller guarantees the bucket is non-empty.
    """
    groups = _group_by_case(bucket.entries)
    plazos, causas = bucket.total, len(bucket.case_ids)
    subject = "VERDE · " + verde_subject(plazos, causas)
    raw_name = getattr(lawyer, "name", None) or "Abogado(a)"
    summary = verde_subject(plazos, causas)

    sections = ""
    for first, items in groups:
        rows = "\n".join(_plazo_row_html(e) for e in items)
        sections += _section_html(
            html.escape(_case_line(first.rol, first.caratulado, first.court_name)), rows
        )

    body_rows = f"""\
          <tr>
            <td style="padding:32px 32px 8px 32px;">
              <p style="margin:0 0 4px 0; font-size:13px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px;">
                <span style="display:inline-block; background-color:{_LEVEL_COLOR[Level.VERDE]}; color:#ffffff; padding:2px 8px; border-radius:4px;">Plazos nuevos en verde</span>
              </p>
              <h1 style="margin:8px 0 0 0; color:#111827; font-size:20px; line-height:1.4;">Hola {html.escape(raw_name)}, tienes {summary}</h1>
            </td>
          </tr>
{sections}"""
    html_body = _shell(
        subject,
        body_rows,
        "Resumen de los plazos nuevos detectados, agrupados por causa. Cuando un "
        "plazo pase a amarillo o rojo te llegará por separado, en el momento.",
    )

    lines = [f"PLAZOS NUEVOS EN VERDE — {_BRAND}", "", f"Hola {raw_name}, tienes {summary}:", ""]
    for first, items in groups:
        lines.append(_case_line(first.rol, first.caratulado, first.court_name).upper())
        for e in items:
            lines.append(f"  {e.title}")
            lines.extend(f"    {m}" for m in e.message.splitlines() if m.strip())
        lines.append("")
    lines.append("Cuando un plazo pase a amarillo o rojo te llegará por separado, en el momento.")
    return subject, html_body, "\n".join(lines) + "\n"
