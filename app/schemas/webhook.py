"""Webhook schemas."""

from typing import List, Optional
from pydantic import BaseModel, HttpUrl


class WebhookCreate(BaseModel):
    """Create a webhook."""
    
    url: HttpUrl
    events: List[str]  # e.g., ["movement.new", "case.status_change"]
    secret: str | None = None  # Optional signing secret


class WebhookUpdate(BaseModel):
    """Update a webhook."""
    
    url: HttpUrl | None = None
    events: List[str] | None = None
    active: bool | None = None
    secret: str | None = None


class WebhookResponse(BaseModel):
    """Webhook response."""
    
    id: int
    url: str
    events: List[str]
    active: bool
    created_at: str
