"""Webhooks CRUD endpoints — scoped to the authenticated lawyer."""

import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer
from app.models.webhook import Webhook
from app.schemas.webhook import WebhookCreate, WebhookResponse, WebhookUpdate

router = APIRouter()

_DEFAULT_EVENTS = ["movement.created"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_lawyer_id(current_lawyer: dict) -> int:
    """Extract the integer lawyer id from the JWT payload.

    Mirrors the pattern in sync.py: ``current_lawyer.get("sub") or
    current_lawyer.get("lawyer_id")``.  ``sub`` carries the lawyer primary key
    as a string (set at token-creation time).
    """
    raw = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing lawyer identity",
        )
    return int(raw)


def _get_webhook_or_404(db: Session, webhook_id: int, lawyer_id: int) -> Webhook:
    """Return the webhook if it exists and belongs to *lawyer_id*, else 404."""
    webhook = (
        db.query(Webhook)
        .filter(Webhook.id == webhook_id, Webhook.lawyer_id == lawyer_id)
        .first()
    )
    if not webhook:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )
    return webhook


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    webhook_data: WebhookCreate,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Create a new webhook for the authenticated lawyer.

    If *secret* is omitted a cryptographically-strong secret is generated
    automatically (``secrets.token_urlsafe(32)``).  The secret is returned in
    the response so the client can configure HMAC signature verification.
    """
    lawyer_id = _resolve_lawyer_id(current_lawyer)

    webhook = Webhook(
        lawyer_id=lawyer_id,
        url=str(webhook_data.url),
        events=webhook_data.events if webhook_data.events is not None else _DEFAULT_EVENTS,
        secret=webhook_data.secret or secrets.token_urlsafe(32),
        is_active=True,
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)

    return WebhookResponse.model_validate(webhook)


@router.get("", response_model=List[WebhookResponse])
async def list_webhooks(
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """List all webhooks belonging to the authenticated lawyer."""
    lawyer_id = _resolve_lawyer_id(current_lawyer)
    webhooks = db.query(Webhook).filter(Webhook.lawyer_id == lawyer_id).all()
    return [WebhookResponse.model_validate(w) for w in webhooks]


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get a single webhook by id (404 if not found or not owned by caller)."""
    lawyer_id = _resolve_lawyer_id(current_lawyer)
    webhook = _get_webhook_or_404(db, webhook_id, lawyer_id)
    return WebhookResponse.model_validate(webhook)


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: int,
    webhook_data: WebhookUpdate,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Partially update a webhook (url, events, active, secret)."""
    lawyer_id = _resolve_lawyer_id(current_lawyer)
    webhook = _get_webhook_or_404(db, webhook_id, lawyer_id)

    if webhook_data.url is not None:
        webhook.url = str(webhook_data.url)
    if webhook_data.events is not None:
        webhook.events = webhook_data.events
    if webhook_data.active is not None:
        webhook.is_active = webhook_data.active
    if webhook_data.secret is not None:
        webhook.secret = webhook_data.secret

    db.commit()
    db.refresh(webhook)
    return WebhookResponse.model_validate(webhook)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Hard-delete a webhook (404 if not found or not owned by caller)."""
    lawyer_id = _resolve_lawyer_id(current_lawyer)
    webhook = _get_webhook_or_404(db, webhook_id, lawyer_id)
    db.delete(webhook)
    db.commit()


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Send a test event to a webhook."""
    # TODO: Implement webhook testing
    return {"message": "Test event sent"}
