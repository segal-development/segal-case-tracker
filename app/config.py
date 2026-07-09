import base64
from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Known-insecure dev defaults — kept as field defaults so local dev works without .env.
# The model_validator below rejects them when ENVIRONMENT != "development".
_DEV_SECRET_KEY = "dev-secret-key-change-in-production"
_DEV_ENCRYPTION_KEY = "dev-32-byte-encryption-key-here!"


def _build_fernet(key: str, *, strict: bool = False) -> Fernet:
    """Build a Fernet instance from a key string.

    strict=True (production): require a real url-safe-base64 32-byte Fernet key;
    a malformed/weak key raises instead of being silently padded. strict=False
    (dev/test only): pad an arbitrary string to 32 bytes so local development
    works without generating a key. NEVER use strict=False in production — it
    accepts low-entropy junk like "miclave123".
    """
    try:
        return Fernet(key.encode())
    except Exception:
        if strict:
            raise
        key_bytes = key.encode().ljust(32)[:32]
        key_b64 = base64.urlsafe_b64encode(key_bytes)
        return Fernet(key_b64)


def _build_multifernet(primary: str, fallbacks=(), *, strict: bool = False) -> MultiFernet:
    """Build a MultiFernet that ENCRYPTS/rotates with ``primary`` and DECRYPTS with
    the primary OR any fallback key.

    During a key rotation, set ENCRYPTION_KEY_FALLBACKS to the OLD key(s) so existing
    ciphertext keeps decrypting with zero downtime while new writes use the new
    primary; once everything is re-encrypted to the primary, drop the fallbacks.
    """
    keys = [_build_fernet(primary, strict=strict)]
    keys += [_build_fernet(k, strict=strict) for k in fallbacks if k]
    return MultiFernet(keys)


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
    # Comma-separated OLD keys, decrypt-only, set during a key rotation window.
    ENCRYPTION_KEY_FALLBACKS: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # GCP
    GCP_PROJECT_ID: str = ""
    GCP_PUBSUB_TOPIC: str = "scrape-jobs"

    # Email
    SENDGRID_API_KEY: str = ""
    FROM_EMAIL: str = "notificaciones@segal.cl"

    # SMTP (supervisor credential-change alerts — firm-provided SMTP creds,
    # not the SendGrid API)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_FROM: str = ""
    SUPERVISOR_EMAIL: str = ""

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

    # PJUD Per-action rate limits
    PJUD_RL_LOGIN_RATE: float = 0.033     # ~1 login / 30s
    PJUD_RL_LOGIN_BURST: int = 3
    PJUD_RL_LIST_RATE: float = 0.1        # ~1 list / 10s
    PJUD_RL_LIST_BURST: int = 3
    PJUD_RL_DETAIL_RATE: float = 0.5      # ~1 detail / 2s
    PJUD_RL_DETAIL_BURST: int = 2         # anti-Shape: no instant multi-request burst
    PJUD_RL_DOCUMENT_RATE: float = 0.15   # ~1 document / 6-7s (anti-Shape pacing)
    PJUD_RL_DOCUMENT_BURST: int = 1       # anti-Shape: zero-spaced document bursts trip Shape
    PJUD_RL_WAIT_TIMEOUT: float = 120.0   # pace up to 2 min before giving up

    # Anti-Shape: minimum spacing between consecutive document downloads within
    # the same case, so a batch of documents isn't fetched back-to-back on the
    # same session immediately after the case detail (see AsyncSleepLimiter
    # usage in sync_service.detect_and_sync_movements).
    DOCUMENT_INTER_DELAY: float = 4.0

    # Shape/TSPD cooldown (station-wide, see app.services.shape_cooldown)
    SHAPE_COOLDOWN_BASE_SECONDS: int = 300   # 5 min initial cooldown after a Shape hit
    SHAPE_COOLDOWN_MAX_SECONDS: int = 900    # 15 min cap after repeated consecutive hits

    # PJUD Observability
    PJUD_ALERT_WEBHOOK_URL: str = ""        # Webhook URL for alerts (empty = disabled)
    PJUD_LOG_LEVEL: str = "INFO"            # Log level: DEBUG, INFO, WARNING, ERROR
    PJUD_METRICS_ENABLED: bool = True       # Enable/disable metrics collection
    PJUD_CHROME_PATH: str = ""              # Path to system Chrome (empty = Playwright bundled Chromium)
    PJUD_USER_DATA_DIR: str = ""            # Persistent user data dir for PJUD Chrome, empty means ephemeral Playwright context
    PJUD_VIEWPORT_WIDTH: int = 1920         # Browser viewport width (default full-HD)
    PJUD_VIEWPORT_HEIGHT: int = 1080        # Browser viewport height
    PJUD_ALERTS_ENABLED: bool = True        # Enable/disable alert webhooks

    # Document storage (Slice 2)
    DOC_STORAGE_BACKEND: str = "local"          # "local" or "gcs"
    DOC_STORAGE_DIR: str = "./storage/documents"  # LocalStorageBackend base dir
    GCS_BUCKET: str = ""                        # Empty → LocalStorageBackend; set → GCSStorageBackend
    GCS_SIGNED_URL_TTL: int = 3600              # Signed URL TTL in seconds
    DOC_DOWNLOAD_ENABLED: bool = False          # Gate: download PDFs during sync

    # PJUD session lifetime
    PJUD_SESSION_EXPIRY_MINUTES: int = 90  # Measured session lasts ≥2h; 90 min is conservative

    # Detail rotation (sync-rotacion Slice 1)
    DETAIL_BATCH_SIZE: int = 30    # Cases per scheduled rotation batch
    DETAIL_FETCH_DELAY: float = 2.0  # Seconds between consecutive detail fetches
    DETAIL_MIN_YEAR: int = 2021    # Only detail-scrape cases with ROL year >= this (0 = all)

    # Notifications
    NOTIFY_MAX_PER_SYNC: int = 25           # Max notifications dispatched per sync_movements call

    # Worker sync
    SYNC_INTERVAL_HOURS: int = 4            # Hours between scheduled sync runs
    MAX_DATA_AGE_HOURS: int = 4             # Hours after which cached data is considered stale

    # Sync commit resilience (Mac -> Cloud SQL Auth Proxy long-write drops).
    # SyncService.sync_cases commits progress every SYNC_COMMIT_CHUNK_SIZE
    # upserted cases instead of one commit for the whole batch, so a dropped
    # connection mid-run only loses the current chunk, not the whole caseload.
    SYNC_COMMIT_CHUNK_SIZE: int = 200       # Cases processed between commits
    SYNC_COMMIT_MAX_RETRIES: int = 2        # Max commit attempts on OperationalError (incl. the first)

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

        # Outside development, require a REAL Fernet key — no lenient padding.
        # Padding silently accepts weak keys (e.g. "miclave123"), which is exactly
        # what we refuse in production. Generate one with:
        #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        try:
            _build_fernet(self.ENCRYPTION_KEY, strict=True)
        except Exception as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a real Fernet key (44-char url-safe base64, "
                "32 bytes) in non-development environments. The current value is not a "
                f"valid Fernet key: {exc}"
            ) from exc

        # Rotation fallbacks (if any) must also be real Fernet keys.
        for fb in (k.strip() for k in (self.ENCRYPTION_KEY_FALLBACKS or "").split(",") if k.strip()):
            try:
                _build_fernet(fb, strict=True)
            except Exception as exc:
                raise ValueError(
                    "ENCRYPTION_KEY_FALLBACKS must contain only real Fernet keys "
                    f"(comma-separated). Invalid entry: {exc}"
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
