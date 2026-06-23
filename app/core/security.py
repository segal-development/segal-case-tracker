"""Security utilities - JWT, encryption, password hashing."""

import base64
from datetime import datetime, timedelta
from typing import Optional, cast

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from cryptography.fernet import Fernet, MultiFernet

from app.config import settings


# Password hashing (app-level, bcrypt-incompatible with passlib on bcrypt>=4; use pbkdf2_sha256)
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# JWT Bearer scheme
security = HTTPBearer()


# =============================================================================
# JWT TOKENS
# =============================================================================

def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    return str(encoded_jwt)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
        )
        return cast(dict, payload)
    except JWTError:
        return None


async def get_current_lawyer(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """
    Dependency to get current lawyer RUT from JWT token.
    
    Usage:
        @router.get("/my-cases")
        async def get_my_cases(rut: str = Depends(get_current_lawyer)):
            ...
    """
    token = credentials.credentials
    payload = decode_access_token(token)
    
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    rut = payload.get("sub")
    if not rut:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token no contiene RUT",
        )

    return str(rut)


# =============================================================================
# PASSWORD HASHING (for internal passwords, not PJUD)
# =============================================================================

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return bool(pwd_context.verify(plain_password, hashed_password))


def hash_password(password: str) -> str:
    """Hash a password."""
    return str(pwd_context.hash(password))


# =============================================================================
# PASSWORD ENCRYPTION (for PJUD passwords - reversible)
# =============================================================================

def _get_fernet() -> MultiFernet:
    """Get a MultiFernet: encrypts with the primary ENCRYPTION_KEY, decrypts with
    the primary OR any ENCRYPTION_KEY_FALLBACKS entry (zero-downtime rotation).

    Strict (real Fernet key required) outside development; lenient padding is
    allowed only in dev/test so local work doesn't need a generated key.
    """
    from app.config import _build_multifernet

    strict = settings.ENVIRONMENT.lower() != "development"
    fallbacks = [
        k.strip() for k in (settings.ENCRYPTION_KEY_FALLBACKS or "").split(",") if k.strip()
    ]
    return _build_multifernet(settings.ENCRYPTION_KEY, fallbacks, strict=strict)


def encrypt_pjud_password(password: str) -> str:
    """
    Encrypt PJUD password for storage.
    
    This is REVERSIBLE encryption because we need to send
    the password to PJUD for login.
    """
    f = _get_fernet()
    return f.encrypt(password.encode()).decode()


def decrypt_pjud_password(encrypted: str) -> str:
    """Decrypt stored PJUD password."""
    f = _get_fernet()
    return f.decrypt(encrypted.encode()).decode()
