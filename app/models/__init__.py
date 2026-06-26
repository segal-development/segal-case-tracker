"""SQLAlchemy models."""

from app.models.lawyer import Lawyer
from app.models.court import Court
from app.models.client import Client
from app.models.case import Case
from app.models.movement import Movement
from app.models.document import Document
from app.models.alert import Alert
from app.models.webhook import Webhook
from app.models.audit_log import AuditLog
from app.models.sync_history import SyncHistory
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_deadline import CaseDeadline
from app.models.goal import Goal

__all__ = [
    "Lawyer",
    "Court",
    "Client",
    "Case",
    "Movement",
    "Document",
    "Alert",
    "Webhook",
    "AuditLog",
    "SyncHistory",
    "CaseLitigante",
    "CaseNotificacion",
    "CaseEscrito",
    "CaseExhorto",
    "CaseDeadline",
    "Goal",
]
