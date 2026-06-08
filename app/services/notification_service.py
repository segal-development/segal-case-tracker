"""Notification service - Email and webhook notifications."""

from typing import List, Optional
from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.webhook import Webhook


class NotificationService:
    """Handle email and webhook notifications."""
    
    def __init__(self, db: Session):
        self.db = db
    
    async def send_email_alert(
        self,
        to_email: str,
        subject: str,
        body: str,
    ) -> bool:
        """
        Send an email alert via SendGrid.
        
        Returns:
            True if sent successfully
        """
        # TODO: Implement SendGrid email sending
        raise NotImplementedError("Email sending not implemented")
    
    async def send_webhook(
        self,
        webhook: Webhook,
        event_type: str,
        payload: dict,
    ) -> bool:
        """
        Send a webhook notification.
        
        Returns:
            True if sent successfully
        """
        # TODO: Implement webhook sending with HMAC signing
        raise NotImplementedError("Webhook sending not implemented")
    
    async def notify_new_movement(
        self,
        lawyer_id: int,
        case_id: int,
        movement_id: int,
    ) -> None:
        """
        Send notifications for a new movement.
        
        1. Create alert record
        2. Send email if configured
        3. Send webhooks if configured
        """
        # TODO: Implement notification logic
        pass
    
    def get_lawyer_webhooks(
        self,
        lawyer_id: int,
        event_type: str,
    ) -> List[Webhook]:
        """Get active webhooks for a lawyer that handle a specific event."""
        return self.db.query(Webhook).filter(
            Webhook.lawyer_id == lawyer_id,
            Webhook.is_active == True,
        ).all()  # TODO: Filter by event type in JSON
