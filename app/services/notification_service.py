"""Notification service - Email (SMTP) and HMAC-signed webhook notifications."""

import hashlib
import hmac
import json
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import List

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_notificacion import CaseNotificacion
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.webhook import Webhook

logger = logging.getLogger(__name__)


class NotificationService:
    """Handle email and webhook notifications."""

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_email_alert(self, alert: Alert, lawyer: Lawyer) -> bool:
        """
        Send an email alert via SMTP — the firm's own provider credentials, the
        same transport as the daily agenda / supervisor alerts (mirrors
        ``supervisor_alert_service._send_smtp_sync``). No third-party email API.

        Returns True on success, False otherwise.
        Never raises — failures are logged and suppressed so callers (e.g. the
        sync pipeline) are never blocked by notification errors.
        """
        if not settings.SMTP_HOST:
            logger.warning(
                "SMTP_HOST is not configured; skipping email for alert %s",
                alert.id,
            )
            return False
        if not lawyer.email:
            logger.warning(
                "Lawyer %s has no email; skipping email for alert %s",
                getattr(lawyer, "id", None),
                alert.id,
            )
            return False

        try:
            msg = EmailMessage()
            msg["Subject"] = alert.title
            msg["From"] = settings.SMTP_FROM or settings.FROM_EMAIL
            msg["To"] = lawyer.email
            msg.set_content(alert.message)

            context = ssl.create_default_context()
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
                if settings.SMTP_USE_TLS:
                    server.starttls(context=context)
                if settings.SMTP_USER:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.send_message(msg)

            alert.email_sent = True
            alert.email_sent_at = datetime.utcnow()
            self.db.flush()
            return True

        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send email for alert %s: %s", alert.id, exc)
            return False

    def send_webhook(self, webhook: Webhook, payload: dict) -> bool:
        """
        POST a JSON payload to a webhook URL with HMAC-SHA256 signature.

        The body is the canonical JSON of the payload (sorted keys, no spaces).
        The signature covers exactly that byte sequence.

        Returns True on 2xx, False otherwise.
        Never raises — failures increment webhook.failure_count and are logged.
        """
        # Canonical JSON — deterministic key order, no extra whitespace
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        headers: dict = {"Content-Type": "application/json"}

        if webhook.secret:
            sig = hmac.new(
                webhook.secret.encode(),
                body.encode(),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={sig}"
        else:
            logger.warning(
                "Webhook %s has no secret configured; sending unsigned request",
                webhook.id,
            )

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(webhook.url, content=body, headers=headers)

            if 200 <= response.status_code < 300:
                return True

            logger.error(
                "Webhook %s returned non-2xx status %s",
                webhook.id,
                response.status_code,
            )
            webhook.failure_count = (webhook.failure_count or 0) + 1
            self.db.flush()
            return False

        except Exception as exc:  # noqa: BLE001
            logger.error("Webhook %s POST failed: %s", webhook.id, exc)
            webhook.failure_count = (webhook.failure_count or 0) + 1
            self.db.flush()
            return False

    def notify_new_movement(
        self,
        alert: Alert,
        case: Case,
        movement: Movement,
        lawyer: Lawyer,
        webhooks: List[Webhook],
    ) -> None:
        """
        Dispatch email + webhook notifications for a new movement.

        Builds the v1 event payload, sends email via SendGrid, then fans out
        to all active webhooks.  One channel failing never blocks the other.
        Never raises.

        Field-name notes vs. the raw PJUD concepts:
        - case.tribunal  → case.court.name  (Court relationship)
        - case.caratulado → composed from case.plaintiff / case.defendant
        - movement.fecha  → movement.movement_date  (DateTime field)
        - movement.descripcion → movement.description
        - movement.etapa → movement.stage
        """
        payload = {
            "event": "movement.created",
            "version": "1",
            "data": {
                "lawyer_id": lawyer.id,
                "case": {
                    "rol": case.rol or "",
                    # FIX 5: guard court relationship — court may be None.
                    "tribunal": case.court.name if case.court else "",
                    # FIX 5: guard None plaintiff/defendant so the literal
                    # string "None" never appears in the serialized payload.
                    "caratulado": (
                        f"{case.plaintiff or ''}/{case.defendant or ''}"
                    ),
                },
                "movement": {
                    "folio": movement.folio or "",
                    # FIX 5: guard movement_date — None would produce "None".
                    "fecha": str(movement.movement_date) if movement.movement_date else "",
                    "descripcion": movement.description or "",
                    "etapa": movement.stage or "",
                },
            },
        }

        self._dispatch(alert, lawyer, webhooks, payload)

    def notify_new_notificacion(
        self,
        case: Case,
        notificacion_row: CaseNotificacion,
        lawyer: Lawyer,
        webhooks: List[Webhook],
        alert: Alert,
    ) -> None:
        """Dispatch email + webhook notifications for a new notificacion.

        Builds a v1 event payload with event='notificacion.created' and fans out
        via _dispatch (mirrors notify_new_movement structure).  Never raises.
        """
        payload = {
            "event": "notificacion.created",
            "version": "1",
            "data": {
                "lawyer_id": lawyer.id,
                "case": {
                    "rol": case.rol or "",
                    "tribunal": case.court.name if case.court else "",
                    "caratulado": f"{case.plaintiff or ''}/{case.defendant or ''}",
                },
                "notificacion": {
                    "rol": notificacion_row.rol or "",
                    "tipo_notif": notificacion_row.tipo_notif or "",
                    "fecha_tramite": (
                        str(notificacion_row.fecha_tramite)
                        if notificacion_row.fecha_tramite
                        else ""
                    ),
                    "nombre": notificacion_row.nombre or "",
                    "tramite": notificacion_row.tramite or "",
                },
            },
        }
        self._dispatch(alert, lawyer, webhooks, payload)

    def notify_new_escrito(
        self,
        case: Case,
        escrito_row: CaseEscrito,
        lawyer: Lawyer,
        webhooks: List[Webhook],
        alert: Alert,
    ) -> None:
        """Dispatch email + webhook notifications for a new escrito.

        Builds a v1 event payload with event='escrito.created'.  Never raises.
        """
        payload = {
            "event": "escrito.created",
            "version": "1",
            "data": {
                "lawyer_id": lawyer.id,
                "case": {
                    "rol": case.rol or "",
                    "tribunal": case.court.name if case.court else "",
                    "caratulado": f"{case.plaintiff or ''}/{case.defendant or ''}",
                },
                "escrito": {
                    "tipo_escrito": escrito_row.tipo_escrito or "",
                    "solicitante": escrito_row.solicitante or "",
                    "fecha_ingreso": (
                        str(escrito_row.fecha_ingreso)
                        if escrito_row.fecha_ingreso
                        else ""
                    ),
                },
            },
        }
        self._dispatch(alert, lawyer, webhooks, payload)

    def notify_new_exhorto(
        self,
        case: Case,
        exhorto_row: CaseExhorto,
        lawyer: Lawyer,
        webhooks: List[Webhook],
        alert: Alert,
    ) -> None:
        """Dispatch email + webhook notifications for a new exhorto.

        Builds a v1 event payload with event='exhorto.created'.  Never raises.
        """
        payload = {
            "event": "exhorto.created",
            "version": "1",
            "data": {
                "lawyer_id": lawyer.id,
                "case": {
                    "rol": case.rol or "",
                    "tribunal": case.court.name if case.court else "",
                    "caratulado": f"{case.plaintiff or ''}/{case.defendant or ''}",
                },
                "exhorto": {
                    "tipo_exhorto": exhorto_row.tipo_exhorto or "",
                    "rol_destino": exhorto_row.rol_destino or "",
                    "tribunal_destino": exhorto_row.tribunal_destino or "",
                },
            },
        }
        self._dispatch(alert, lawyer, webhooks, payload)

    def notify_deadline_alert(
        self,
        alert: Alert,
        case: Case,
        lawyer: Lawyer,
        webhooks: List[Webhook],
    ) -> None:
        """Dispatch email + webhook notifications for a ROJO-entry or
        fatal-deadline alert (``alert.type`` is ``"semaforo_rojo"`` or
        ``"deadline_fatal"``).

        Builds a v1 event payload named ``deadline.<alert.type>`` and fans
        out via ``_dispatch`` (mirrors notify_new_movement structure).
        Never raises.
        """
        payload = {
            "event": f"deadline.{alert.type}",
            "version": "1",
            "data": {
                "lawyer_id": lawyer.id,
                "case": {
                    "rol": case.rol or "",
                    "tribunal": case.court.name if case.court else "",
                    "caratulado": f"{case.plaintiff or ''}/{case.defendant or ''}",
                },
                "deadline": {
                    "semaforo": case.semaforo or "",
                    "next_deadline_at": (
                        str(case.next_deadline_at) if case.next_deadline_at else ""
                    ),
                    "next_deadline_fatal": bool(case.next_deadline_fatal),
                },
            },
        }
        self._dispatch(alert, lawyer, webhooks, payload)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        alert: Alert,
        lawyer: Lawyer,
        webhooks: List[Webhook],
        payload: dict,
    ) -> None:
        """Send email + fan out to all active webhooks.

        Shared implementation for all notify_new_* methods — absorbs the
        send_email_alert + webhook fan-out loop that would otherwise be
        copy-pasted across every entity type.  One channel failing never
        blocks the other.  Never raises.
        """
        # Email — failure must not block webhooks
        try:
            self.send_email_alert(alert, lawyer)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Unexpected error sending email for alert %s: %s", alert.id, exc
            )

        # Webhooks — each failure is isolated
        webhook_success = False
        for webhook in webhooks:
            if not webhook.is_active:
                continue
            try:
                if self.send_webhook(webhook, payload):
                    webhook_success = True
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Unexpected error delivering webhook %s: %s", webhook.id, exc
                )

        if webhook_success:
            alert.webhook_sent = True
            alert.webhook_sent_at = datetime.utcnow()
            self.db.flush()

    def get_lawyer_webhooks(
        self,
        lawyer_id: int,
        event_type: str,
    ) -> List[Webhook]:
        """Get active webhooks for a lawyer that handle a specific event."""
        return (
            self.db.query(Webhook)
            .filter(
                Webhook.lawyer_id == lawyer_id,
                Webhook.is_active == True,  # noqa: E712
            )
            .all()
        )
