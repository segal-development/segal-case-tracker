"""Webhooks CRUD endpoints."""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer
from app.schemas.webhook import WebhookCreate, WebhookResponse, WebhookUpdate

router = APIRouter()


@router.get("", response_model=List[WebhookResponse])
async def list_webhooks(
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """List all webhooks for the authenticated lawyer."""
    # TODO: Implement webhook listing
    
    return []


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    webhook_data: WebhookCreate,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """
    Create a new webhook.
    
    Webhooks are called when:
    - New movement detected
    - Case status changes
    - Document uploaded
    """
    # TODO: Implement webhook creation
    
    return WebhookResponse(
        id=1,
        url=str(webhook_data.url),
        events=webhook_data.events,
        active=True,
        created_at="2024-01-01T00:00:00Z",
    )


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get a specific webhook."""
    # TODO: Implement webhook retrieval
    
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Webhook not found",
    )


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: int,
    webhook_data: WebhookUpdate,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Update a webhook."""
    # TODO: Implement webhook update
    
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Webhook not found",
    )


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Delete a webhook."""
    # TODO: Implement webhook deletion
    pass


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Send a test event to a webhook."""
    # TODO: Implement webhook testing
    
    return {"message": "Test event sent"}
