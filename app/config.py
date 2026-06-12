import base64
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Known-insecure dev defaults — kept as field defaults so local dev works without .env.
# The model_validator below rejects them when ENVIRONMENT != "development".
_DEV_SECRET_KEY = "dev-secret-key-change-in-production"
_DEV_ENCRYPTION_KEY = "dev-32-byte-encryption-key-here!"


def _build_fernet(key: str) -> Fernet:
    """Build a Fernet instance from a key string, using the same derivation as security.py."""
    try:
        return Fernet(key.encode())
    except Exception:
        key_bytes = key.encode().ljust(32)[:32]
        key_b64 = base64.urlsafe_b64encode(key_bytes)
        return Fernet(key_b64)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/segal_case_tracker"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Security
    SECRET_KEY: str = _DEV_SECRET_KEY
    ENCRYPTION_KEY: str = _DEV_ENCRYPTION_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # 2Captcha
    CAPTCHA_API_KEY: str = ""

    # GCP
    GCP_PROJECT_ID: str = ""
    GCP_PUBSUB_TOPIC: str = "scrape-jobs"

    # Email
    SENDGRID_API_KEY: str = ""
    FROM_EMAIL: str = "notificaciones@segal.cl"

    # Firebase (push notifications)
    FIREBASE_CREDENTIALS_PATH: str = ""
    FIREBASE_CREDENTIALS_JSON: str = ""

    # App
    APP_URL: str = "http://localhost:3000"

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000"

    # PJUD Scraper
    PJUD_SELECTORS_PATH: str = ""  # Override default selectors path
    SCRAPER_FRESH_BROWSER: bool = True  # Use fresh browser per API request

    # PJUD Resilience
    PJUD_CB_FAILURE_THRESHOLD: int = 5      # Circuit breaker: failures before opening
    PJUD_CB_RECOVERY_TIMEOUT: int = 60      # Circuit breaker: seconds before half-open
    PJUD_RETRY_MAX_ATTEMPTS: int = 3        # Retry: max retry attempts
    PJUD_RETRY_BASE_DELAY: float = 1.0      # Retry: initial delay in seconds
    PJUD_RETRY_MAX_DELAY: float = 30.0      # Retry: max delay cap
    PJUD_RATE_LIMIT: float = 10.0           # Rate limiter: requests per second
    PJUD_RATE_BURST: int = 20               # Rate limiter: burst capacity
    PJUD_HEALTH_CHECK_INTERVAL: int = 300   # Health check: seconds between checks

    # PJUD Observability
    PJUD_ALERT_WEBHOOK_URL: str = ""        # Webhook URL for alerts (empty = disabled)
    PJUD_LOG_LEVEL: str = "INFO"            # Log level: DEBUG, INFO, WARNING, ERROR
    PJUD_METRICS_ENABLED: bool = True       # Enable/disable metrics collection
    PJUD_ALERTS_ENABLED: bool = True        # Enable/disable alert webhooks

    # Document storage (Slice 2)
    DOC_STORAGE_BACKEND: str = "local"          # "local" or "gcs"
    DOC_STORAGE_DIR: str = "./storage/documents"  # LocalStorageBackend base dir
    GCS_BUCKET: str = ""                        # Empty → LocalStorageBackend; set → GCSStorageBackend
    GCS_SIGNED_URL_TTL: int = 3600              # Signed URL TTL in seconds
    DOC_DOWNLOAD_ENABLED: bool = False          # Gate: download PDFs during sync

    # Notifications
    NOTIFY_MAX_PER_SYNC: int = 25           # Max notifications dispatched per sync_movements call

    # Environment
    ENVIRONMENT: str = "production"
    DEBUG: bool = True

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def cors_origins_list(self) -> list[str]:
        """Split CORS_ORIGINS on commas, strip whitespace, drop empties."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def effective_debug(self) -> bool:
        """True only when DEBUG is set AND we are in the development environment."""
        return self.DEBUG and self.ENVIRONMENT.lower() == "development"

    @property
    def has_2captcha(self) -> bool:
        """True when a 2Captcha API key is configured (single decision point, ADR-7).

        Controls the autonomous captcha re-auth path in the worker scheduler.
        When False, lawyers authenticated via captcha are skipped on session
        expiry (reason=``captcha_no_2captcha_key``) rather than crashing.
        """
        return bool(self.CAPTCHA_API_KEY)

    # ------------------------------------------------------------------
    # Fail-fast secrets validation
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        """Reject insecure dev defaults when running outside development."""
        if self.ENVIRONMENT.lower() == "development":
            return self

        if not self.SECRET_KEY or self.SECRET_KEY == _DEV_SECRET_KEY:
            raise ValueError(
                "SECRET_KEY must be set to a secure value in non-development environments. "
                "The current value is the insecure dev default."
            )

        if not self.ENCRYPTION_KEY or self.ENCRYPTION_KEY == _DEV_ENCRYPTION_KEY:
            raise ValueError(
                "ENCRYPTION_KEY must be set to a secure value in non-development environments. "
                "The current value is the insecure dev default."
            )

        # Verify the key can produce a working Fernet instance.
        try:
            _build_fernet(self.ENCRYPTION_KEY)
        except Exception as exc:
            raise ValueError(
                f"ENCRYPTION_KEY cannot be used to construct a valid Fernet key: {exc}"
            ) from exc

        # Reject empty CORS in non-development environments.
        if not self.cors_origins_list:
            raise ValueError(
                "CORS_ORIGINS must not be empty in non-development environments."
            )

        return self


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
