"""Notification service - Email (SMTP) and HMAC-signed webhook notifications."""

import hashlib
import hmac
import json
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

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
from app.services.cycle_timing import timed

logger = logging.getLogger(__name__)


class NotificationService:
    """Handle email and webhook notifications."""

    def __init__(self, db: Session):
        self.db = db
        # One SMTP connection reused across the batch. Measured 2026-10-12: a
        # fresh connection + TLS handshake + login per email cost ~3.4s, so 407
        # emails in a sync took 23 minutes and made `email` 51-55% of the whole
        # scraping cycle -- more than the movements it announces.
        self._smtp: Optional[smtplib.SMTP] = None
        # New-movement emails are not sent one by one: they accumulate here,
        # keyed by lawyer id, and go out as ONE digest per lawyer (see
        # ``flush_digests``). Value: (lawyer, DigestBucket).
        self._digests: Dict[Any, Tuple[Lawyer, list]] = {}
        # Same shape for the semaforo's new VERDE plazos: one grouped email per
        # lawyer (``PlazoBucket``), sent by the same ``flush_digests``.
        self._plazo_digests: Dict[Any, Tuple[Lawyer, Any]] = {}

    # ------------------------------------------------------------------
    # SMTP connection lifecycle
    # ------------------------------------------------------------------

    def _connect_smtp(self) -> smtplib.SMTP:
        """Open and authenticate one SMTP connection."""
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10)
        if settings.SMTP_USE_TLS:
            server.starttls(context=ssl.create_default_context())
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        return server

    def _smtp_connection(self) -> smtplib.SMTP:
        if self._smtp is None:
            self._smtp = self._connect_smtp()
        return self._smtp

    def _drop_smtp(self) -> None:
        """Forget the current connection; the next send opens a fresh one."""
        server, self._smtp = self._smtp, None
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 — already broken, nothing to save
                pass

    def close(self) -> None:
        """Flush any pending digests, then release the shared connection.

        Flushing lives here (not only in the explicit ``flush_digests``) so the
        safe thing is the default: a caller that forgets ``flush_digests`` and
        just closes -- or leaves a ``with`` block -- still delivers every
        accumulated email. Safe to call more than once; never raises.
        """
        self.flush_digests()
        self._drop_smtp()

    def __enter__(self) -> "NotificationService":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _deliver(self, msg: EmailMessage) -> None:
        """Send one message on the shared connection, reconnecting once if the
        provider dropped it. Raises on failure; callers own the error contract."""
        # Charged to the cycle's "email" bucket (no-op outside a worker cycle).
        with timed("email"):
            try:
                self._smtp_connection().send_message(msg)
            except (
                smtplib.SMTPServerDisconnected,
                smtplib.SMTPConnectError,
                ConnectionError,
                TimeoutError,
            ):
                # The provider closes idle connections. That must never cost
                # an email: drop the dead one, reconnect and send once more.
                #
                # NOT `OSError`: smtplib.SMTPException inherits from it, so
                # catching OSError would also catch a refused recipient --
                # a protocol answer, not a broken pipe -- and tear down the
                # connection the rest of the batch is still using. A test
                # covers exactly that.
                self._drop_smtp()
                self._smtp_connection().send_message(msg)
            except smtplib.SMTPResponseException as exc:
                # 421 is the same event as above, only announced instead of
                # silent: RFC 5321 defines it as "service not available,
                # closing the transmission channel". Seen in production as
                # (421, b'4.4.2 ... Error: timeout exceeded'), which cost a
                # whole lawyer's digest -- and a digest is never resent, so
                # that lawyer simply never heard about those movements.
                #
                # Every OTHER code is a protocol answer about THIS message (a
                # refused sender or recipient). Reconnecting on those would pay
                # a handshake to be refused again and tear down the connection
                # the rest of the batch is still using. Hence the exact code,
                # never the whole exception class.
                if exc.smtp_code != 421:
                    raise
                self._drop_smtp()
                self._smtp_connection().send_message(msg)

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

            self._deliver(msg)

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

        Builds the v1 event payload and fans out to all active webhooks
        IMMEDIATELY (they are machine consumers, one event per movement).
        The EMAIL half is deferred: the movement is accumulated and goes out in
        one digest per lawyer on ``flush_digests`` / ``close``. Only
        ``new_movement`` is deferred -- actionable alerts (deadlines, semaforo,
        status changes) keep using ``_dispatch`` and stay immediate.
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

        self._dispatch(alert, lawyer, webhooks, payload, send_email=False)
        self.accumulate_movement(alert, case, movement, lawyer)

    def accumulate_movement(
        self, alert: Alert, case: Case, movement: Movement, lawyer: Lawyer
    ) -> None:
        """Queue one movement for the lawyer's digest. Never raises.

        Public so the sync can feed the digest for movements whose webhook
        dispatch the per-case ``NotifyBudget`` skipped: the budget caps
        webhooks, it must not decide what the email counts.
        """
        try:
            from app.services.movement_digest import DigestBucket, DigestEntry

            entry = DigestEntry(
                alert=alert,
                case_id=case.id,
                rol=case.rol or "",
                caratulado=f"{case.plaintiff or ''}/{case.defendant or ''}",
                court_name=case.court.name if case.court else "",
                date_str=(
                    movement.movement_date.strftime("%d-%m-%Y")
                    if movement.movement_date
                    else ""
                ),
                procedure=movement.procedure or "",
                description=movement.description or "",
            )
            self._digests.setdefault(lawyer.id, (lawyer, DigestBucket()))[1].add(entry)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Could not queue movement for digest (alert %s): %s",
                getattr(alert, "id", None),
                exc,
            )

    def flush_digests(self) -> None:
        """Send ONE digest email per lawyer for the movements accumulated so
        far, then forget them.

        Nothing accumulated -> no email. A lawyer with no email address, an
        unconfigured SMTP or a failed send leaves ``alert.email_sent`` untouched
        (the alerts stay persisted either way). Never raises: a digest failure
        must not break the sync cycle.
        """
        pending, self._digests = self._digests, {}
        for lawyer, bucket in pending.values():
            try:
                self._send_digest(lawyer, bucket)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to send movement digest to lawyer %s: %s",
                    getattr(lawyer, "id", None),
                    exc,
                )
        # The two digests are independent: one failing never costs the other.
        pending_plazos, self._plazo_digests = self._plazo_digests, {}
        for lawyer, bucket in pending_plazos.values():
            try:
                self._send_plazo_verde_digest(lawyer, bucket)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to send verde plazo digest to lawyer %s: %s",
                    getattr(lawyer, "id", None),
                    exc,
                )

    def _send_digest(self, lawyer: Lawyer, bucket: Any) -> bool:
        from app.services.movement_digest import render_movement_digest

        if not bucket.total_movements:
            return False  # never send "0 movimientos nuevos"
        if not settings.SMTP_HOST:
            logger.warning(
                "SMTP_HOST is not configured; skipping movement digest for lawyer %s",
                getattr(lawyer, "id", None),
            )
            return False
        if not lawyer.email:
            logger.warning(
                "Lawyer %s has no email; skipping movement digest",
                getattr(lawyer, "id", None),
            )
            return False

        subject, html_body, text_body = render_movement_digest(lawyer, bucket)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM or settings.FROM_EMAIL
        msg["To"] = lawyer.email
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")

        self._deliver(msg)

        # Only now -- the digest really left -- mark the alerts it covered.
        # Only the alerts actually rendered: the ones that merely fed the "y N
        # más" counter stay false ("deliberately not sent", never a lie).
        self._mark_sent_and_commit([entry.alert for entry in bucket.entries])
        logger.info(
            "Movement digest sent to lawyer %s: %d movements (%d rendered)",
            getattr(lawyer, "id", None),
            bucket.total_movements,
            len(bucket.entries),
        )
        return True

    def _mark_sent_and_commit(self, alerts: list) -> None:
        """Mark ``alerts`` as emailed -- they really left -- and COMMIT.

        COMMIT, not flush. A digest is flushed at the END of the batch, past the
        last commit of the sync path (``sync_via_consulta`` has none after it at
        all), so a mark that is only flushed dies with the session: the email
        would have left while the database still said it never did. And
        ``email_sent=false`` does not mean "pending" for a movement digest, it
        means "deliberately not sent" -- a lie in that column is read as a
        decision.
        """
        sent_at = datetime.utcnow()
        for alert in alerts:
            alert.email_sent = True
            alert.email_sent_at = sent_at
        try:
            self.db.commit()
        except Exception:
            # Keep the session usable for the next lawyer's digest; the caller
            # logs and moves on, leaving these alerts honestly unmarked.
            self.db.rollback()
            raise

    # ------------------------------------------------------------------
    # Plazo semaforo: amarillo/rojo immediate, verde grouped
    # ------------------------------------------------------------------

    def notify_plazo_alert(
        self, alert: Alert, lawyer: Lawyer, webhooks: List[Webhook]
    ) -> None:
        """Dispatch ONE amarillo/rojo alert right now: its own email (never part
        of a digest) and the lawyer's webhooks. Never raises."""
        try:
            self._send_plazo_email(alert, lawyer)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to email plazo alert %s: %s", alert.id, exc)
        case = alert.case
        payload = {
            "event": f"deadline.{alert.type}",
            "version": "1",
            "data": {
                "lawyer_id": lawyer.id,
                "case": {
                    "rol": case.rol or "" if case else "",
                    "tribunal": case.court.name if case and case.court else "",
                    "caratulado": (
                        f"{case.plaintiff or ''}/{case.defendant or ''}" if case else ""
                    ),
                },
                "deadline": {"title": alert.title, "message": alert.message or ""},
            },
        }
        self._dispatch(alert, lawyer, webhooks, payload, send_email=False)

    def _send_plazo_email(self, alert: Alert, lawyer: Lawyer) -> bool:
        from app.services.plazo_email import render_plazo_alert

        if not settings.SMTP_HOST:
            logger.warning(
                "SMTP_HOST is not configured; skipping plazo alert %s", alert.id
            )
            return False
        if not lawyer.email:
            logger.warning(
                "Lawyer %s has no email; skipping plazo alert %s",
                getattr(lawyer, "id", None),
                alert.id,
            )
            return False
        subject, html_body, text_body = render_plazo_alert(lawyer, alert, alert.case)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM or settings.FROM_EMAIL
        msg["To"] = lawyer.email
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        self._deliver(msg)
        self._mark_sent_and_commit([alert])
        return True

    def accumulate_plazo_verde(self, alert: Alert, lawyer: Lawyer) -> None:
        """Queue one new VERDE plazo for the lawyer's grouped email. Never raises."""
        try:
            from app.services.plazo_email import PlazoBucket, PlazoEntry

            case = alert.case
            entry = PlazoEntry(
                alert=alert,
                case_id=alert.case_id,
                rol=(case.rol if case else "") or "",
                caratulado=(
                    f"{case.plaintiff or ''}/{case.defendant or ''}" if case else ""
                ),
                court_name=case.court.name if case and case.court else "",
                title=alert.title,
                message=alert.message or "",
            )
            self._plazo_digests.setdefault(lawyer.id, (lawyer, PlazoBucket()))[1].add(entry)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Could not queue verde plazo (alert %s): %s",
                getattr(alert, "id", None),
                exc,
            )

    def _send_plazo_verde_digest(self, lawyer: Lawyer, bucket: Any) -> bool:
        from app.services.plazo_email import render_plazo_verde_digest

        if not bucket.total:
            return False
        if not settings.SMTP_HOST:
            logger.warning(
                "SMTP_HOST is not configured; skipping verde plazo digest for lawyer %s",
                getattr(lawyer, "id", None),
            )
            return False
        if not lawyer.email:
            logger.warning(
                "Lawyer %s has no email; skipping verde plazo digest",
                getattr(lawyer, "id", None),
            )
            return False
        subject, html_body, text_body = render_plazo_verde_digest(lawyer, bucket)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM or settings.FROM_EMAIL
        msg["To"] = lawyer.email
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        self._deliver(msg)
        # Every queued entry is rendered (no cap), so every one is marked.
        self._mark_sent_and_commit([entry.alert for entry in bucket.entries])
        logger.info(
            "Verde plazo digest sent to lawyer %s: %d plazos",
            getattr(lawyer, "id", None),
            bucket.total,
        )
        return True

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
        *,
        send_email: bool = True,
    ) -> None:
        """Send email + fan out to all active webhooks.

        Shared implementation for all notify_new_* methods — absorbs the
        send_email_alert + webhook fan-out loop that would otherwise be
        copy-pasted across every entity type.  One channel failing never
        blocks the other.  Never raises.
        """
        # Email — failure must not block webhooks. ``send_email=False`` is used
        # by the digest path, which sends its own email later.
        if send_email:
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
