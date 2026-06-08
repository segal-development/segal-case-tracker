from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/segal_case_tracker"
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # Security
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    ENCRYPTION_KEY: str = "dev-32-byte-encryption-key-here!"
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
    
    # Environment
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    
    class Config:
        env_file = ".env"
        extra = "ignore"  # Ignore extra fields in .env


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
