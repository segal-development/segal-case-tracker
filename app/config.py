import base64
from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from typing import Literal

from pydantic import Field, field_validator, model_validator
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

    # Excepciones verdict: calendar days after due_date still treated as "can't
    # tell if late" (PJUD publishes escritos ~4 days late and they carry no
    # Diligencia date). 5 covers 92% of the measured lateness. 0 disables it.
    # Rationale in app/services/deadline_verdict.py.
    DEADLINE_PUBLICATION_MARGIN_DAYS: int = 5

    # Extra calendar days added to the publication margin ONLY for the
    # acreditar_poder_3d verdict. Our due_date counts from the resolution's own
    # date; the firm's spec runs the 3 days from its NOTIFICATION, which is later.
    # NOT MEASURED (the publication margin was measured on a different deadline):
    # a judgment, deliberately on the safe side, since a wider margin can only turn
    # a "fuera_de_plazo" into the weaker "registro_tardio", never into "cumplido".
    # Calibrate with the Diligencia date of the notification of the resolution vs
    # its movement_date. 0 disables it. Rationale in app/services/deadline_verdict.py.
    DEADLINE_PODER_NOTIFICATION_ALLOWANCE_DAYS: int = 2

    # Semaforo de plazos: who is told in ADDITION to the case's lawyer(s).
    # Comma-separated lawyer ids (lawyers.id). Amarillo adds the first list;
    # ROJO sends to the second, which is the full escalation (it repeats the
    # amarillo people on purpose: each list reads as "who gets this level").
    # Defaults, verified 2026-10-14: 15 = Melissa Acevedo, 1 = Carla Lavin.
    # To change a person: edit these in the worker's .env and restart it.
    SEMAFORO_AMARILLO_EXTRA_LAWYER_IDS: str = "15"
    SEMAFORO_ROJO_EXTRA_LAWYER_IDS: str = "15,1"
    # Semaforo cutoff: a plazo whose ``triggered_at`` is more than this many
    # calendar days before today raises NO alert (no email, no webhook); it stays
    # visible on the Plazos screen. Exists because the semaforo was switched on
    # over a base with years of never-closed plazos. 0 or negative = no cutoff
    # (alert on everything). It must stay above the longest live term: an
    # 8-business-day plazo reaches ROJO ~9-12 calendar days after it triggers,
    # so a value below that silences the very escalation the semaforo exists for.
    SEMAFORO_MAX_TRIGGER_AGE_DAYS: int = 14
    # Expired-and-unfiled notice: a FATAL plazo whose ``due_date`` passed with
    # the firm NOT having acted (verdict no_cumplido / fuera_de_plazo) is
    # announced once, but only while it expired within this many calendar days.
    # Older ones are history, not something a lawyer can still react to.
    # 0 or negative = no window (announce every such plazo, however old).
    SEMAFORO_EXPIRED_WINDOW_DAYS: int = 30
    # Excepciones term (business days): legally 4 or 8 depending on where the
    # requerimiento was served, and no source we have says where. 4 or 8 pins
    # that single term for every excepciones plazo; 0 = unknown, keep BOTH
    # readings. Anything else is rejected at load. Reversible by env, no deploy.
    #
    # Default 8, by the firm's own instruction: drop the 4-day reading and keep
    # only the 8-day one. It is a DEFAULT and not an env var on purpose -- this
    # value has to hold wherever the code runs, and setting it only on the
    # scraping station (its LaunchAgent plist) left the API still on 0, so the
    # panel kept printing "Plazo no confirmado: 4 días / 8 días" to the lawyers.
    SEMAFORO_EXCEPCIONES_TERM_DAYS: int = 8

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

    # Alerta operativa de scraping. Casilla propia y NO SUPERVISOR_EMAIL: el
    # aviso de credencial vencida le sirve a quien coordina abogados, mientras
    # que "el scraping lleva horas sin completar" le sirve a quien opera el
    # sistema. Mezclarlos entrena a ignorar los dos.
    # Commit desplegado, inyectado por el deploy. Permite verificar QUE quedo
    # corriendo en vez de solo que la app responde: un deploy que sube el
    # commit equivocado contesta /readyz igual de bien que el correcto.
    APP_COMMIT: str = ""

    SCRAPING_ALERT_EMAIL: str = ""
    #: Horas sin una corrida exitosa para considerar el scraping caído.
    SCRAPING_STALE_HOURS: int = 6
    #: Corridas fallidas seguidas para considerarlo en falla. El incidente del
    #: 26-09-2026 acumuló 29 antes de que alguien lo viera.
    SCRAPING_MAX_FALLAS: int = 5
    #: Espera mínima entre dos avisos del mismo tipo.
    SCRAPING_ALERT_COOLDOWN_HOURS: int = 6

    # Sysgal CRM (read-only client-coverage lookup by party RUT).
    # Both empty = integration disabled (sync is a no-op).
    #
    # ATENCION — SYSGAL NO TIENE AMBIENTE DE PRUEBAS.
    # Sysgal Defensoria corre en una sola instalacion: la URL que usamos es
    # PRODUCCION y la credencial abre la base real del estudio. Confirmado por
    # su equipo el 28-09-2026. Importa decirlo aca porque estas variables viven
    # en un archivo llamado `.env.qa`, y sin esta advertencia cualquiera asume
    # que apuntan a un sandbox donde se puede experimentar.
    #
    # Lo que consultamos es de solo lectura, asi que no podemos modificar nada
    # de ellos — pero son datos de clientes reales y cada llamada queda en sus
    # registros de produccion. Para probar conectividad esta
    # GET /api_sync/health, que no pide autenticacion ni toca la base.
    SYSGAL_BASE_URL: str = ""
    SYSGAL_API_KEY: str = ""
    # How long a cached per-RUT answer stays usable. The sync covers every
    # demandado in the portfolio, so without this every cycle would re-ask
    # Sysgal for thousands of RUTs that did not change; with it, the first run
    # fetches the backlog and later runs refresh only what went stale.
    SYSGAL_CACHE_TTL_DAYS: int = 7

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
    PJUD_SCRAPING_ENABLED: bool = True      # False where the IP can't authenticate against PJUD (cloud VM)

    # Document storage (Slice 2)
    DOC_STORAGE_BACKEND: str = "local"          # "local" or "gcs"
    DOC_STORAGE_DIR: str = "./storage/documents"  # LocalStorageBackend base dir
    GCS_BUCKET: str = ""                        # Empty → LocalStorageBackend; set → GCSStorageBackend
    GCS_SIGNED_URL_TTL: int = 3600              # Signed URL TTL in seconds
    DOC_DOWNLOAD_ENABLED: bool = False          # Gate: download PDFs during sync
    # Year floor for PDF DOWNLOAD only (independent of DETAIL_MIN_YEAR): only
    # download documents of cases whose ROL year >= this. Detail-scraping still
    # covers all DETAIL_MIN_YEAR+ cases; older docs stay in PJUD (we keep pjud_url)
    # and can be fetched on demand. 0 = download for all detailed cases.
    DOC_DOWNLOAD_MIN_YEAR: int = 2024
    # Max HISTORICAL documents downloaded per causa visit (0 = no cap). Documents of recent
    # movements (see DOC_RECENT_DAYS) are never capped. Deferred ones stay pending.
    DOC_MAX_PER_CASE: int = 0
    # A document is never capped when its movement is newer than this many days.
    DOC_RECENT_DAYS: int = 30
    # False = fetch ONLY recent documents and skip the historical ones entirely.
    # Measured 2026-10-07: documents take 45-52% of the cycle while PJUD caps
    # detail at ~60 min per account, so every minute on a PDF is a minute not
    # spent on movements. The pending queue is 83% older than six months while
    # only THIRTEEN documents are newer than a week -- on the daily station that
    # history is pure cost. It belongs to the dedicated station.
    # A separate flag because DOC_MAX_PER_CASE <= 0 already means "no cap" (and
    # 0 is its default), so it cannot also express "none".
    DOC_HISTORICAL_ENABLED: bool = True

    # PJUD session lifetime
    PJUD_SESSION_EXPIRY_MINUTES: int = 90  # Measured session lasts ≥2h; 90 min is conservative

    # Detail rotation (sync-rotacion Slice 1)
    DETAIL_BATCH_SIZE: int = 30    # Cases per scheduled rotation batch
    DETAIL_FETCH_DELAY: float = 2.0  # Seconds between consecutive detail fetches
    # Observe-only probe of the OJV "estado diario" feed (see
    # app/services/estado_diario_probe.py). One in-page POST per lawyer per
    # cycle, one log line, no effect on what the sync scrapes or alerts.
    # OFF by default: flip it in the environment to kill it without a deploy.
    ESTADO_DIARIO_PROBE_ENABLED: bool = False
    # Max business days of the estado-diario feed one probe may request per
    # lawyer per cycle. The probe asks for every business day since the last
    # successful read (so a cycle skipped by the freshness guard leaves no hole),
    # oldest first; each day is a PJUD request, so a lawyer who was away for
    # weeks catches up over several cycles instead of in one burst. Env var,
    # tunable without a deploy.
    ESTADO_DIARIO_MAX_DAYS_PER_CYCLE: int = Field(default=3, ge=1)
    # Days since the last detail check after which the on-screen case diagnostic
    # (GET /cases/diagnostico) reports "latencia" instead of "al_dia".
    DIAGNOSTICO_LATENCIA_DIAS: int = Field(default=7, ge=1)
    #: Dias de antelacion con que una causa con plazo FATAL por vencer salta
    #: al frente de la rotacion de detalle. 0 = sin prioridad.
    #: Incidente 2026-10-20: se alerto "vence hoy" sobre un poder ya acreditado
    #: el dia anterior, porque la causa no se releia hace 18 horas. Ese dia
    #: habia 14 plazos fatales venciendo en <=1 dia sin relectura.
    DETAIL_FATAL_PLAZO_DAYS: int = 2
    DETAIL_MIN_YEAR: int = 2021    # Only detail-scrape cases with ROL year >= this (0 = all)
    DETAIL_PENDING_DOCS_FIRST: bool = False  # when true, detail rotation prioritizes cases with the most pending documents (PDF backfill)
    # Which side of the freshness/backlog cut this station works on. "all" = no cut
    # (default, legacy behavior); "fresh" = only causas already detailed
    # (last_detail_checked_at NOT NULL); "backlog" = only never-detailed ones.
    # An unknown value FAILS AT STARTUP: silently degrading to "all" would make a
    # freshness station sweep the whole backlog.
    DETAIL_ROTATION_SCOPE: Literal["all", "fresh", "backlog"] = "all"
    # Delta-sync: give the causas of the OJV estado-diario feed priority in the
    # detail rotation, with a CAP (feed slots per batch) and a derived FLOOR
    # (batch size - cap: slots the age rotation always keeps). OFF by default;
    # with it off the selection is the one that existed before. It needs
    # ESTADO_DIARIO_PROBE_ENABLED on, because the probe is what fetches the feed.
    # The cap is a number to tune from real data, so it is an env var, not a constant.
    DETAIL_FEED_PRIORITY_ENABLED: bool = False
    DETAIL_FEED_MAX_PER_BATCH: int = Field(default=20, ge=0)

    # Notifications
    NOTIFY_MAX_PER_SYNC: int = 25           # Max notifications dispatched per sync_movements call

    # Worker sync
    SYNC_INTERVAL_HOURS: int = 4            # Hours between scheduled sync runs
    MAX_DATA_AGE_HOURS: int = 4             # Hours after which cached data is considered stale

    # Competencias the scheduled worker syncs (CSV). The firm only handles
    # juicio-ejecutivo DEFENSE (civil), so laboral/penal are wasted PJUD
    # requests and extra Shape exposure. Default civil-only; override via env
    # (e.g. "civil,laboral") without a code change.
    SYNC_COMPETENCIAS: str = "civil"

    # Sync commit resilience (Mac -> Cloud SQL Auth Proxy long-write drops).
    # SyncService.sync_cases commits progress every SYNC_COMMIT_CHUNK_SIZE
    # upserted cases instead of one commit for the whole batch, so a dropped
    # connection mid-run only loses the current chunk, not the whole caseload.
    SYNC_COMMIT_CHUNK_SIZE: int = 200       # Cases processed between commits
    SYNC_COMMIT_MAX_RETRIES: int = 2        # Max commit attempts on OperationalError (incl. the first)

    # Presentación worker (Slice 2a — scaffolding). Master gate defaults OFF:
    # while False the station-side worker only logs queue depth and claims/
    # processes NOTHING (the OJV/PJUD automation lands in Slice 2b).
    PRESENTACION_WORKER_ENABLED: bool = False   # Gate: drain the presentaciones queue
    PRESENTACION_POLL_INTERVAL_SECONDS: int = 120  # Seconds between queue polls
    PRESENTACION_MAX_INTENTOS: int = 3          # Max attempts before a filing goes to error

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
    def sync_competencias_list(self) -> list[str]:
        """Split SYNC_COMPETENCIAS on commas, strip whitespace, drop empties."""
        return [c.strip() for c in self.SYNC_COMPETENCIAS.split(",") if c.strip()]

    @property
    def effective_debug(self) -> bool:
        """True only when DEBUG is set AND we are in the development environment."""
        return self.DEBUG and self.ENVIRONMENT.lower() == "development"

    # ------------------------------------------------------------------
    # Fail-fast secrets validation
    # ------------------------------------------------------------------

    @field_validator("DETAIL_ROTATION_SCOPE", mode="before")
    @classmethod
    def _normalize_rotation_scope(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("SEMAFORO_EXCEPCIONES_TERM_DAYS")
    @classmethod
    def _excepciones_term_is_0_4_or_8(cls, v: int) -> int:
        if v not in (0, 4, 8):
            raise ValueError(
                "SEMAFORO_EXCEPCIONES_TERM_DAYS must be 0 (unknown, keep both "
                f"readings), 4 or 8; got {v}"
            )
        return v

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
