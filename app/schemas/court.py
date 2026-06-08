"""Court schemas."""

from pydantic import BaseModel


class CourtResponse(BaseModel):
    """Court response."""
    
    id: int
    code: str
    name: str
    region: str
    type: str
