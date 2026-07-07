"""Supervisor credential-change alert — SMTP email notification.

When a lawyer's PJUD credential (Clave Única / segunda clave) stops working
because they changed their password, the background worker cannot recover
autonomously. This module sends a professional email to a configured
supervisor asking them to have the lawyer re-load the credential so sync
resumes.

Uses stdlib ``smtplib`` + ``ssl`` (no extra dependency). The actual network
call is synchronous, so ``send_supervisor_credential_alert`` runs it in a
thread executor via ``asyncio.get_running_loop().run_in_executor`` — safe to
call from the async worker without blocking the event loop.

Mirrors ``notification_service``'s graceful-skip pattern: if SMTP is not
configured, log a warning and no-op. Never raises — a notification failure
must never crash the sync worker.
"""

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

_CHILE_TZ = ZoneInfo("America/Santiago")

_AUTH_METHOD_LABELS = {
    "clave_unica": "Clave Única",
    "captcha": "Segunda Clave (RUT + Clave)",
}


def _auth_method_label(lawyer) -> str:
    method = getattr(lawyer, "preferred_auth_method", None)
    return _AUTH_METHOD_LABELS.get(method, method or "Desconocido")


def render_supervisor_credential_alert(lawyer, reason: str = "") -> tuple[str, str, str]:
    """Render (subject, html_body, text_body) for the supervisor alert email.

    Pure function — no I/O. Renders from the lawyer's data via an f-string
    template (no external template engine needed).
    """
    name = getattr(lawyer, "name", None) or "Abogado(a)"
    rut = getattr(lawyer, "rut", None) or "—"
    method_label = _auth_method_label(lawyer)
    now_chile = datetime.now(_CHILE_TZ)
    timestamp_str = now_chile.strftime("%d-%m-%Y %H:%M:%S (%Z)")

    subject = f"⚠️ Acción requerida: la Clave Única de {name} dejó de sincronizar"

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
                    SEGAL DEUDORES — DEFENSA LEGAL
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Title -->
          <tr>
            <td style="padding:32px 32px 8px 32px;">
              <p style="margin:0 0 4px 0; color:#b45309; font-size:13px; font-weight:bold; text-transform:uppercase; letter-spacing:0.5px;">Acción requerida</p>
              <h1 style="margin:0; color:#111827; font-size:20px; line-height:1.4;">La credencial de {name} dejó de sincronizar</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:8px 32px 0 32px; color:#374151; font-size:14px; line-height:1.6;">
              <p style="margin:0 0 16px 0;">
                El sistema intentó autenticar automáticamente a este abogado(a) en el sitio del Poder
                Judicial y la credencial fue rechazada. Lo más probable es que el abogado(a) haya
                cambiado su clave recientemente.
              </p>
              <p style="margin:0 0 16px 0;">
                Como consecuencia, <strong>las causas de este abogado(a) dejaron de sincronizarse</strong>
                hasta que la credencial sea actualizada en el sistema.
              </p>
            </td>
          </tr>

          <!-- Info block -->
          <tr>
            <td style="padding:8px 32px 0 32px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f9fafb; border:1px solid #e5e7eb; border-radius:6px;">
                <tr>
                  <td style="padding:16px 20px;">
                    <table role="presentation" width="100%" cellpadding="4" cellspacing="0" style="font-size:14px; color:#111827;">
                      <tr>
                        <td style="color:#6b7280; width:140px;">Abogado(a)</td>
                        <td style="font-weight:bold;">{name}</td>
                      </tr>
                      <tr>
                        <td style="color:#6b7280;">RUT</td>
                        <td>{rut}</td>
                      </tr>
                      <tr>
                        <td style="color:#6b7280;">Método de acceso</td>
                        <td>{method_label}</td>
                      </tr>
                      <tr>
                        <td style="color:#6b7280;">Fecha y hora</td>
                        <td>{timestamp_str}</td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- CTA -->
          <tr>
            <td style="padding:24px 32px 8px 32px; color:#374151; font-size:14px; line-height:1.6;">
              <p style="margin:0;">
                Por favor solicita al abogado(a) su clave actualizada y vuelve a cargarla en el sistema
                para que la sincronización de sus causas se reanude.
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:24px 32px 32px 32px;">
              <hr style="border:none; border-top:1px solid #e5e7eb; margin:0 0 16px 0;">
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

    text_body = (
        f"ACCIÓN REQUERIDA — Segal Deudores — Defensa Legal\n\n"
        f"La credencial de {name} dejó de sincronizar.\n\n"
        f"El sistema intentó autenticar automáticamente a este abogado(a) en el sitio del "
        f"Poder Judicial y la credencial fue rechazada. Lo más probable es que el abogado(a) "
        f"haya cambiado su clave recientemente.\n\n"
        f"Como consecuencia, las causas de este abogado(a) dejaron de sincronizarse hasta que "
        f"la credencial sea actualizada en el sistema.\n\n"
        f"Abogado(a): {name}\n"
        f"RUT: {rut}\n"
        f"Método de acceso: {method_label}\n"
        f"Fecha y hora: {timestamp_str}\n\n"
        f"Por favor solicita al abogado(a) su clave actualizada y vuelve a cargarla en el "
        f"sistema para que la sincronización de sus causas se reanude.\n"
    )

    return subject, html_body, text_body


def _send_smtp_sync(msg: EmailMessage) -> None:
    """Blocking SMTP send — must run off the event loop (thread executor)."""
    context = ssl.create_default_context()
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
        if settings.SMTP_USE_TLS:
            server.starttls(context=context)
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


async def send_supervisor_credential_alert(lawyer, reason: str = "") -> bool:
    """Send the supervisor credential-change alert email.

    Returns True on success, False otherwise. Never raises — a notification
    failure must never crash the sync worker (mirrors
    ``NotificationService.send_email_alert``'s graceful-skip pattern).

    Runs the blocking SMTP call in a thread executor so it never blocks the
    async worker's event loop.
    """
    if not settings.SMTP_HOST or not settings.SUPERVISOR_EMAIL:
        logger.warning(
            "SMTP_HOST/SUPERVISOR_EMAIL not configured; skipping supervisor "
            "credential alert for lawyer %s",
            getattr(lawyer, "id", None),
        )
        return False

    subject, html_body, text_body = render_supervisor_credential_alert(lawyer, reason)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM or settings.FROM_EMAIL
    msg["To"] = settings.SUPERVISOR_EMAIL
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send_smtp_sync, msg)
        logger.info(
            "Supervisor credential alert sent for lawyer %s",
            getattr(lawyer, "id", None),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to send supervisor credential alert for lawyer %s: %s",
            getattr(lawyer, "id", None),
            exc,
        )
        return False
