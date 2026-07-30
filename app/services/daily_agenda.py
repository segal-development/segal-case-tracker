"""Daily calendar guide — per-lawyer next-day agenda email.

A daily job builds, for EACH active firm lawyer, their next-day agenda
(procedural DEADLINES and DecisionEngine REVIEW dates) and emails it to them
as a guide. The firm's admin ("Carla") is CC'd only when that lawyer's day
contains an URGENT (fatal) deadline.

This module performs NO new computation over cases — it is a read-only
fan-out over columns the engine already denormalizes on ``Case``
(``next_deadline_at``/``next_deadline_fatal``, ``next_review_at``,
``recommended_action_code``), reusing the same helpers the calendar endpoint
uses. SMTP transport mirrors ``supervisor_alert_service`` (stdlib smtplib +
ssl, graceful skip when unconfigured, never raises). This runs in a plain
sync script, so the blocking SMTP send is called directly — no event loop.

Email copy is neutral Spanish (tuteo). Code identifiers/comments are English.
"""

import html
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Optional

from app.config import settings
from app.api.v1.calendar import _caratulado, _resolve_deadline_label
from app.core.decision_rules import resolve_rule
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import case_ids_for_abogado
from app.utils.rut import format_rut

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agenda data model
# ---------------------------------------------------------------------------


@dataclass
class AgendaItem:
    """One agenda entry — either a procedural deadline or a review date."""

    rol: Optional[str]
    caratulado: str
    court_name: Optional[str] = None
    label: Optional[str] = None
    fatal: bool = False
    recommended_action: Optional[str] = None
    case_id: Optional[int] = None
    demandado_rut: Optional[str] = None


@dataclass
class DayAgenda:
    """A lawyer's deadlines and reviews for a single day."""

    deadlines: list = field(default_factory=list)
    reviews: list = field(default_factory=list)

    @property
    def has_urgent(self) -> bool:
        return any(d.fatal for d in self.deadlines)

    @property
    def is_empty(self) -> bool:
        return not self.deadlines and not self.reviews


# ---------------------------------------------------------------------------
# Agenda building
# ---------------------------------------------------------------------------


def _demandado_rut_by_case(db, case_ids) -> dict:
    """Map ``case_id -> formatted RUT of the demandado (ejecutado)``.

    The demandado is the litigante coded ``DDO.`` (first one wins if several
    co-demandados). One batched query for all agenda cases — no N+1.
    """
    if not case_ids:
        return {}
    rows = (
        db.query(CaseLitigante)
        .filter(
            CaseLitigante.case_id.in_(list(case_ids)),
            CaseLitigante.participante == "DDO.",
        )
        .order_by(CaseLitigante.id.asc())
        .all()
    )
    out: dict = {}
    for r in rows:
        if r.case_id in out:
            continue  # keep the first DDO. per case
        rut = (r.rut or "").strip()
        if rut:
            out[r.case_id] = format_rut(rut) or rut
    return out


def lawyer_day_agenda(db, lawyer: Lawyer, day: date) -> DayAgenda:
    """Build ``lawyer``'s agenda (deadlines + reviews) for ``day``.

    Selects the lawyer's abogado-of-record cases (litigante-derived, via
    ``case_ids_for_abogado``), then reads the denormalized deadline/review
    columns already written by the engine. Archived cases are excluded. Each
    item carries the demandado's RUT (batched, no N+1).
    """
    ids = case_ids_for_abogado(db, lawyer.rut, lawyer.rut)
    if not ids:
        return DayAgenda()

    cases = (
        db.query(Case)
        .filter(Case.id.in_(list(ids)), Case.status != "archived")
        .all()
    )

    deadlines: list[AgendaItem] = []
    reviews: list[AgendaItem] = []
    for case in cases:
        court_name = case.court.name if case.court else None
        caratulado = _caratulado(case)

        if case.next_deadline_at == day:
            deadlines.append(
                AgendaItem(
                    rol=case.rol,
                    caratulado=caratulado,
                    court_name=court_name,
                    label=_resolve_deadline_label(db, case),
                    fatal=bool(case.next_deadline_fatal),
                    case_id=case.id,
                )
            )

        if case.next_review_at == day:
            rule = resolve_rule(case.recommended_action_code)
            reviews.append(
                AgendaItem(
                    rol=case.rol,
                    caratulado=caratulado,
                    court_name=court_name,
                    recommended_action=rule.action_text if rule else None,
                    case_id=case.id,
                )
            )

    # Demandado RUT per case — one batched query over just the agenda cases.
    rut_by_case = _demandado_rut_by_case(db, {i.case_id for i in deadlines + reviews})
    for item in deadlines + reviews:
        item.demandado_rut = rut_by_case.get(item.case_id)

    # Deadlines: fatal first, then by rol. Reviews: by rol.
    deadlines.sort(key=lambda i: (not i.fatal, i.rol or ""))
    reviews.sort(key=lambda i: (i.rol or ""))

    return DayAgenda(deadlines=deadlines, reviews=reviews)


# ---------------------------------------------------------------------------
# Novedades ("Requiere atención") — actionable alerts from the last N days
# ---------------------------------------------------------------------------

# Days of alert history to include in the digest ("últimos 5 días para atrás").
NOVEDADES_DIAS = 5


@dataclass
class Novedad:
    """One actionable alert to surface in the daily digest."""

    title: str
    message: Optional[str]
    rol: Optional[str]
    caratulado: str
    fatal: bool = False
    court_name: Optional[str] = None
    created_at: Optional[datetime] = None


def lawyer_novedades(db, lawyer: Lawyer, since: datetime) -> list:
    """Actionable alerts for ``lawyer`` created since ``since`` (most recent first).

    Mirrors the "Requiere atención" feed of the Novedades screen: alerts whose
    ``type`` is in ``ACTIONABLE_ALERT_TYPES`` (causa a ROJO, plazo fatal, plazo
    agregado, cambio de estado…), scoped per-recipient (``Alert.lawyer_id``),
    joined to the case for rol/caratulado/tribunal. Archived cases excluded.
    """
    # Local import: keeps the single source of truth in the alerts API without
    # a module-level coupling from this service.
    from app.api.v1.alerts import ACTIONABLE_ALERT_TYPES

    rows = (
        db.query(Alert, Case)
        .join(Case, Alert.case_id == Case.id)
        .filter(
            Alert.lawyer_id == lawyer.id,
            Alert.type.in_(ACTIONABLE_ALERT_TYPES),
            Alert.created_at >= since,
            Case.status != "archived",
        )
        .order_by(Alert.created_at.desc())
        .all()
    )
    out: list[Novedad] = []
    for alert, case in rows:
        out.append(
            Novedad(
                title=alert.title,
                message=alert.message,
                rol=case.rol,
                caratulado=_caratulado(case),
                fatal=(alert.type == "deadline_fatal"),
                court_name=case.court.name if case.court else None,
                created_at=alert.created_at,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

_BRAND = "SEGAL DEUDORES — DEFENSA LEGAL"


def _deadline_html(item: AgendaItem) -> str:
    badge = (
        '<span style="display:inline-block; background-color:#dc2626; color:#ffffff; '
        'font-size:11px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px; '
        'padding:2px 8px; border-radius:4px; margin-left:8px;">URGENTE</span>'
        if item.fatal
        else ""
    )
    label = html.escape(item.label or "Plazo")
    rol = html.escape(item.rol or "—")
    caratulado = html.escape(item.caratulado)
    court = (
        f'<div style="color:#6b7280; font-size:13px; margin-top:2px;">{html.escape(item.court_name)}</div>'
        if item.court_name else ""
    )
    rut = (
        f'<div style="color:#6b7280; font-size:13px; margin-top:2px;">RUT demandado: {html.escape(item.demandado_rut)}</div>'
        if item.demandado_rut else ""
    )
    return f"""\
              <tr>
                <td style="padding:12px 16px; border-bottom:1px solid #f0f1f3;">
                  <div style="color:#111827; font-size:14px; font-weight:bold;">{label}{badge}</div>
                  <div style="color:#374151; font-size:13px; margin-top:2px;">{rol} · {caratulado}</div>
                  {rut}
                  {court}
                </td>
              </tr>"""


def _review_html(item: AgendaItem) -> str:
    rol = html.escape(item.rol or "—")
    caratulado = html.escape(item.caratulado)
    rut = (
        f'<div style="color:#6b7280; font-size:13px; margin-top:2px;">RUT demandado: {html.escape(item.demandado_rut)}</div>'
        if item.demandado_rut else ""
    )
    action = (
        f'<div style="color:#374151; font-size:13px; margin-top:2px;">{html.escape(item.recommended_action)}</div>'
        if item.recommended_action
        else ""
    )
    return f"""\
              <tr>
                <td style="padding:12px 16px; border-bottom:1px solid #f0f1f3;">
                  <div style="color:#111827; font-size:14px; font-weight:bold;">{rol} · {caratulado}</div>
                  {rut}
                  {action}
                </td>
              </tr>"""


def _novedad_html(n: "Novedad") -> str:
    badge = (
        '<span style="display:inline-block; background-color:#dc2626; color:#ffffff; '
        'font-size:11px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px; '
        'padding:2px 8px; border-radius:4px; margin-left:8px;">URGENTE</span>'
        if n.fatal
        else ""
    )
    title = html.escape(n.title or "Novedad")
    rol = html.escape(n.rol or "—")
    caratulado = html.escape(n.caratulado)
    message = (
        f'<div style="color:#374151; font-size:13px; margin-top:2px;">{html.escape(n.message)}</div>'
        if n.message else ""
    )
    court = (
        f'<div style="color:#6b7280; font-size:13px; margin-top:2px;">{html.escape(n.court_name)}</div>'
        if n.court_name else ""
    )
    return f"""\
              <tr>
                <td style="padding:12px 16px; border-bottom:1px solid #f0f1f3;">
                  <div style="color:#111827; font-size:14px; font-weight:bold;">{title}{badge}</div>
                  <div style="color:#374151; font-size:13px; margin-top:2px;">{rol} · {caratulado}</div>
                  {message}
                  {court}
                </td>
              </tr>"""


def _section_html(title: str, rows: str) -> str:
    return f"""\
          <tr>
            <td style="padding:8px 32px 0 32px;">
              <p style="margin:0 0 8px 0; color:#0b2e4f; font-size:14px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px;">{title}</p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border:1px solid #e5e7eb; border-radius:6px;">
{rows}
              </table>
            </td>
          </tr>"""


def render_daily_agenda_email(
    lawyer: Lawyer, day: date, agenda: DayAgenda, novedades: Optional[list] = None
) -> tuple[str, str, str]:
    """Render ``(subject, html_body, text_body)`` for a lawyer's day agenda.

    Pure function — no I/O. Neutral Spanish, tuteo (tú/tienes). Fatal
    deadlines carry a red "URGENTE" badge; the footer notes that fatal
    deadlines are also copied to the coordinación. ``novedades`` (actionable
    alerts from the last few days) render as a leading "Requiere atención"
    section above the agenda.
    """
    raw_name = getattr(lawyer, "name", None) or "Abogado(a)"
    name = html.escape(raw_name)
    day_str = f"{day:%d-%m-%Y}"
    subject = f"Tu agenda para el {day_str}"
    novedades = novedades or []

    # --- HTML sections: novedades first (they need attention), then agenda ---
    sections_html = ""
    if novedades:
        rows = "\n".join(_novedad_html(n) for n in novedades)
        sections_html += _section_html(
            f"Requiere atención (últimos {NOVEDADES_DIAS} días)", rows
        )
    if agenda.deadlines:
        rows = "\n".join(_deadline_html(i) for i in agenda.deadlines)
        sections_html += _section_html("Plazos", rows)
    if agenda.reviews:
        rows = "\n".join(_review_html(i) for i in agenda.reviews)
        sections_html += _section_html("Revisiones", rows)
    if agenda.is_empty and not novedades:
        sections_html = """\
          <tr>
            <td style="padding:8px 32px 0 32px; color:#374151; font-size:14px; line-height:1.6;">
              <p style="margin:0;">No tienes plazos ni revisiones para hoy. ¡Aprovecha para ponerte al día!</p>
            </td>
          </tr>"""

    html_body = f"""\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject}</title>
</head>
<body style="margin:0; padding:0; background-color:#f4f5f7; font-family:Arial, Helvetica, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
          <!-- Header band -->
          <tr>
            <td style="background-color:#0b2e4f; padding:20px 32px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td width="48" style="vertical-align:middle;">
                    <table role="presentation" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:6px; width:40px; height:40px;">
                      <tr>
                        <td align="center" valign="middle" style="width:40px; height:40px; color:#0b2e4f; font-size:16px; font-weight:bold; font-family:Arial, Helvetica, sans-serif;">SD</td>
                      </tr>
                    </table>
                  </td>
                  <td style="vertical-align:middle; padding-left:12px; color:#ffffff; font-size:15px; font-weight:bold; letter-spacing:0.3px;">
                    {_BRAND}
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Title -->
          <tr>
            <td style="padding:32px 32px 8px 32px;">
              <p style="margin:0 0 4px 0; color:#0b2e4f; font-size:13px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px;">Tu agenda del día</p>
              <h1 style="margin:0; color:#111827; font-size:20px; line-height:1.4;">Hola {name}, esto tienes para el {day_str}</h1>
            </td>
          </tr>

{sections_html}

          <!-- Footer -->
          <tr>
            <td style="padding:24px 32px 32px 32px;">
              <hr style="border:none; border-top:1px solid #e5e7eb; margin:16px 0 16px 0;">
              <p style="margin:0 0 8px 0; color:#6b7280; font-size:12px; line-height:1.5;">
                Los plazos marcados como <strong style="color:#dc2626;">URGENTE</strong> (fatales) se copian
                también a la coordinación para su seguimiento.
              </p>
              <p style="margin:0; color:#9ca3af; font-size:12px; line-height:1.5;">
                Este es un correo automático de Segal Deudores — Defensa Legal. No responder
                directamente a este mensaje.
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

    # --- Plain-text body ---
    lines = [
        f"TU AGENDA — {_BRAND}",
        "",
        f"Hola {raw_name}, esto tienes para el {day_str}:",
        "",
    ]
    if novedades:
        lines.append(f"REQUIERE ATENCIÓN (últimos {NOVEDADES_DIAS} días)")
        for n in novedades:
            tag = " [URGENTE]" if n.fatal else ""
            rol = n.rol or "—"
            msg = f" — {n.message}" if n.message else ""
            lines.append(f"  - {n.title}{tag}: {rol} · {n.caratulado}{msg}")
        lines.append("")
    if agenda.is_empty and not novedades:
        lines.append("No tienes plazos ni revisiones para hoy.")
    else:
        if agenda.deadlines:
            lines.append("PLAZOS")
            for i in agenda.deadlines:
                tag = " [URGENTE]" if i.fatal else ""
                label = i.label or "Plazo"
                rol = i.rol or "—"
                court = f" ({i.court_name})" if i.court_name else ""
                rut = f" · RUT demandado: {i.demandado_rut}" if i.demandado_rut else ""
                lines.append(f"  - {label}{tag}: {rol} · {i.caratulado}{rut}{court}")
            lines.append("")
        if agenda.reviews:
            lines.append("REVISIONES")
            for i in agenda.reviews:
                rol = i.rol or "—"
                action = f" — {i.recommended_action}" if i.recommended_action else ""
                rut = f" · RUT demandado: {i.demandado_rut}" if i.demandado_rut else ""
                lines.append(f"  - {rol} · {i.caratulado}{rut}{action}")
            lines.append("")
    lines.append(
        "Los plazos marcados como URGENTE (fatales) se copian también a la "
        "coordinación para su seguimiento."
    )
    text_body = "\n".join(lines) + "\n"

    return subject, html_body, text_body


def render_novedades_digest(day: date, grouped: list) -> tuple[str, str, str]:
    """Render Carla's consolidated firm-wide novedades digest.

    ``grouped`` is a list of ``(lawyer_name, [Novedad, ...])`` — one section per
    lawyer that has novedades. Pure function; neutral Spanish.
    """
    day_str = f"{day:%d-%m-%Y}"
    total = sum(len(novs) for _, novs in grouped)
    subject = f"Novedades del estudio — {day_str} ({total})"

    sections_html = ""
    for lawyer_name, novs in grouped:
        rows = "\n".join(_novedad_html(n) for n in novs)
        sections_html += _section_html(html.escape(lawyer_name), rows)

    html_body = f"""\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject}</title>
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
          <tr>
            <td style="padding:32px 32px 8px 32px;">
              <p style="margin:0 0 4px 0; color:#0b2e4f; font-size:13px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px;">Resumen del estudio</p>
              <h1 style="margin:0; color:#111827; font-size:20px; line-height:1.4;">Novedades del {day_str} · {total} en total</h1>
            </td>
          </tr>

{sections_html}

          <tr>
            <td style="padding:24px 32px 32px 32px;">
              <hr style="border:none; border-top:1px solid #e5e7eb; margin:16px 0 16px 0;">
              <p style="margin:0; color:#9ca3af; font-size:12px; line-height:1.5;">
                Resumen consolidado de las novedades ("Requiere atención") de los últimos {NOVEDADES_DIAS} días,
                agrupadas por abogado. Correo automático de {_BRAND}.
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

    lines = [f"NOVEDADES DEL ESTUDIO — {day_str} ({total})", ""]
    for lawyer_name, novs in grouped:
        lines.append(lawyer_name.upper())
        for n in novs:
            tag = " [URGENTE]" if n.fatal else ""
            rol = n.rol or "—"
            msg = f" — {n.message}" if n.message else ""
            lines.append(f"  - {n.title}{tag}: {rol} · {n.caratulado}{msg}")
        lines.append("")
    text_body = "\n".join(lines) + "\n"

    return subject, html_body, text_body


# ---------------------------------------------------------------------------
# SMTP transport
# ---------------------------------------------------------------------------


def _send_email(
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    cc: Optional[list] = None,
) -> bool:
    """Send one email via blocking SMTP. Returns True on success, else False.

    Mirrors ``supervisor_alert_service._send_smtp_sync`` config (SMTP_HOST/
    PORT/USE_TLS/USER/PASSWORD, From = SMTP_FROM or FROM_EMAIL) but is generic
    and synchronous — this runs in a sync script, so no event loop / executor.
    Graceful skip when SMTP is unconfigured; never raises.
    """
    if not settings.SMTP_HOST:
        logger.warning(
            "SMTP_HOST not configured; skipping daily agenda email to %s", to
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM or settings.FROM_EMAIL
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls(context=context)
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Daily agenda email sent to %s (cc=%s)", to, cc or [])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send daily agenda email to %s: %s", to, exc)
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def send_daily_calendar_emails(db, target_day: date) -> dict:
    """Send each active firm lawyer their ``target_day`` agenda.

    Every active firm lawyer with a non-null email gets an email (even when
    their agenda is empty — it is a guide). Carla (the admin) is CC'd only
    when the lawyer's day has an urgent (fatal) deadline. Each lawyer is
    isolated in its own try/except so one failure never stops the rest.
    """
    carla = (
        db.query(Lawyer)
        .filter(Lawyer.role == "admin", Lawyer.email.isnot(None))
        .first()
    )
    carla_email = carla.email if carla else None

    lawyers = (
        db.query(Lawyer)
        .filter(
            Lawyer.is_firm_lawyer.is_(True),
            Lawyer.is_active.is_(True),
        )
        .all()
    )

    sent = 0
    cc_count = 0
    skipped_no_email = 0
    errors = 0

    # Novedades window: actionable alerts from the last NOVEDADES_DIAS days. Built
    # per lawyer for their own email AND collected here for Carla's consolidated
    # digest (grouped by lawyer), sent once after the loop.
    since = datetime.utcnow() - timedelta(days=NOVEDADES_DIAS)
    digest_groups: list = []

    for lawyer in lawyers:
        try:
            # Build novedades BEFORE the no-email skip so a lawyer without an
            # inbox still shows up in Carla's firm-wide digest.
            novedades = lawyer_novedades(db, lawyer, since)
            if novedades:
                digest_groups.append((lawyer.name or lawyer.rut or "Abogado(a)", novedades))

            if not lawyer.email:
                skipped_no_email += 1
                logger.info(
                    "Skipping daily agenda for lawyer %s (%s): no email",
                    getattr(lawyer, "id", None),
                    getattr(lawyer, "rut", None),
                )
                continue

            agenda = lawyer_day_agenda(db, lawyer, target_day)
            subject, html_body, text_body = render_daily_agenda_email(
                lawyer, target_day, agenda, novedades
            )

            cc = (
                [carla_email]
                if (agenda.has_urgent and carla_email and carla_email != lawyer.email)
                else []
            )

            ok = _send_email(lawyer.email, subject, html_body, text_body, cc=cc)
            if ok:
                sent += 1
                if cc:
                    cc_count += 1
            else:
                errors += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.error(
                "Error building/sending daily agenda for lawyer %s: %s",
                getattr(lawyer, "id", None),
                exc,
            )

    # Carla's consolidated firm-wide novedades digest (one email, grouped by
    # lawyer). Sent only when there is at least one novedad across the firm.
    carla_digest_sent = False
    if carla_email and digest_groups:
        try:
            d_subject, d_html, d_text = render_novedades_digest(target_day, digest_groups)
            if _send_email(carla_email, d_subject, d_html, d_text):
                carla_digest_sent = True
            else:
                errors += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.error("Error building/sending Carla's novedades digest: %s", exc)

    return {
        "sent": sent,
        "cc_count": cc_count,
        "skipped_no_email": skipped_no_email,
        "errors": errors,
        "carla_digest_sent": carla_digest_sent,
        "novedades_lawyers": len(digest_groups),
        "target_day": str(target_day),
    }
