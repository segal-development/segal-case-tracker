"""Webhook schemas."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, HttpUrl


class WebhookCreate(BaseModel):
    """Create a webhook."""

    url: HttpUrl
    events: Optional[List[str]] = None  # defaults to ["movement.created"] in endpoint
    secret: Optional[str] = None  # auto-generated when omitted


class WebhookUpdate(BaseModel):
    """Update a webhook."""

    url: Optional[HttpUrl] = None
    events: Optional[List[str]] = None
    active: Optional[bool] = None
    secret: Optional[str] = None


class WebhookResponse(BaseModel):
    """Webhook response — includes secret so the client can configure HMAC verification."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    events: List[str]
    is_active: bool
    secret: Optional[str] = None
    lawyer_id: int
    created_at: datetime
    last_triggered_at: Optional[datetime] = None
    failure_count: int = 0
