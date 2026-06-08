"""Authentication schemas."""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Login request with PJUD credentials."""
    
    rut: str = Field(..., description="RUT without dots, with hyphen (e.g., 12345678-9)")
    password: str = Field(..., description="PJUD password")


class LoginResponse(BaseModel):
    """Successful login response."""
    
    access_token: str
    token_type: str = "bearer"
    lawyer_id: int
    rut: str
    name: str


class LogoutResponse(BaseModel):
    """Logout response."""
    
    message: str
