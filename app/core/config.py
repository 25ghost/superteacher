"""Application configuration.

Values are read from environment variables and a local `.env` file (see
`backend/.env.example`). No credentials are hard-coded in source code.

The database connection is configured with discrete variables
(DB_NAME, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD). An explicit DATABASE_URL
may override them entirely (e.g. a managed-provider URL).
"""
import re
from functools import lru_cache
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# Matches the localhost/127.0.0.1 development origins that must never leak
# into a production CORS configuration (Phase 5F hardening).
_LOCAL_HOST_PATTERN = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.IGNORECASE
)

# Environments where development-stage conveniences remain allowed.
_DEVELOPMENT_STAGE_ENVIRONMENTS = frozenset({"development", "testing"})

# Secret values that must never sign production tokens (Phase 5G Step 8).
_UNSAFE_SECRETS = frozenset({"change-me", "secret", "changeme", "placeholder", "test"})


class Settings(BaseSettings):
    """Environment-driven application settings for the SuperTeacher backend."""

    # Application — this backend serves the whole SuperTeacher project; the
    # Student Registration Portal is its first module.
    APP_NAME: str = "superteacher-api"
    ENVIRONMENT: str = "development"

    # Logging (M9): level applied by ``app.core.logging.configure_logging()``
    # exactly once at startup. One of DEBUG, INFO, WARNING, ERROR, CRITICAL.
    LOG_LEVEL: str = "INFO"

    # Database (PostgreSQL) — combined into a SQLAlchemy URL; never hard-coded.
    DB_NAME: str = "super_teacher_db"
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""

    # Optional explicit override (takes precedence over the DB_* variables).
    DATABASE_URL: str = ""

    # Security — JWT / authentication (Phase 5G).
    # SECRET_KEY signs access tokens (HS256). The insecure placeholder default
    # below is refused outside development/testing (see jwt_secret property):
    # production must set a real secret via the environment. Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    SECRET_KEY: str = "change-me"

    # Access-token lifetime in minutes (short-lived identity proof). The
    # refresh session (auth_sessions) has its own, much longer lifetime.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Refresh-session lifetime in days (revocable server-side).
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # Password policy: minimum characters (Phase 5G Step 3 — deliberately
    # modest; length is the only hard rule so it never harms usability).
    PASSWORD_MIN_LENGTH: int = 8

    # Student profile DOB sanity bounds (profile hardening): a date of birth
    # must describe an age between these values (inclusive). Enforced by the
    # service layer on every profile-creation path; the database additionally
    # carries a static 1900 floor as a final backstop.
    STUDENT_MIN_AGE_YEARS: int = 2
    STUDENT_MAX_AGE_YEARS: int = 120

    # CORS (Phase 5E): browser clients (e.g. the Student Portal dev server)
    # call this API from a different local origin. Only explicitly listed
    # origins are allowed — never a wildcard. Extend via env if a new dev
    # origin is added.
    CORS_ORIGINS: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174"
    )

    # Rate limiting (slowapi): per-endpoint request throttling.
    # Format: "<max_requests>/<period>" (e.g. "10/minute").
    RATE_LIMIT_LOGIN: str = "10/minute"
    RATE_LIMIT_REGISTER: str = "5/minute"
    RATE_LIMIT_REFRESH: str = "30/minute"
    RATE_LIMIT_ME_READ: str = "60/minute"
    RATE_LIMIT_STUDENT_READ: str = "30/minute"
    RATE_LIMIT_STUDENT_WRITE: str = "10/minute"
    RATE_LIMIT_STUDENT_HISTORY: str = "30/minute"
    #: Liveness probe throttle — generous for real probes, hostile to storms.
    RATE_LIMIT_HEALTH: str = "120/minute"
    #: Password self-service and account-lifecycle writes.
    RATE_LIMIT_CHANGE_PASSWORD: str = "10/minute"
    RATE_LIMIT_FORGOT_PASSWORD: str = "5/minute"
    RATE_LIMIT_RESET_PASSWORD: str = "5/minute"
    RATE_LIMIT_DEACTIVATE_ACCOUNT: str = "5/minute"
    #: Registration portal: readiness/catalog reads vs. enrollment writes.
    RATE_LIMIT_REGISTRATION_READ: str = "30/minute"
    RATE_LIMIT_REGISTRATION_WRITE: str = "10/minute"

    # slowapi storage backend. "memory://" (default) is per-process: limits
    # are not shared between workers, so each worker allows its own budget.
    # Multi-worker deployments must point this at a shared store (e.g. a
    # Redis URL). No Redis client is bundled — supplying the URI is enough
    # for slowapi/limits to load the matching backend.
    RATE_LIMIT_STORAGE_URI: str = "memory://"

    # Login lockout (Phase B, slice 7): after this many refused login
    # attempts the account is locked for LOGIN_LOCKOUT_MINUTES. The lock
    # answer is the same generic 401 as a wrong password — the response
    # never discloses it. A successful login or an administrator
    # (POST /admin/users/{id}/unlock) clears it early.
    LOGIN_LOCKOUT_THRESHOLD: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    # Maximum active refresh sessions per user. When exceeded, the oldest
    # session is revoked before creating a new one.
    MAX_SESSIONS_PER_USER: int = 10

    # Email (Resend API) — used for password reset and account notifications.
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "noreply@example.com"

    # Frontend URL — base URL for password reset links.
    FRONTEND_URL: str = "http://localhost:5173"

    # Teaching-material uploads (Phase 2, slice 2B).
    # Hard ceiling on one uploaded file (bytes). Content-type allowlist and
    # magic-byte checks live in app.services.file_validation.
    MATERIAL_MAX_FILE_BYTES: int = 20 * 1024 * 1024  # 20 MiB
    # Root directory for the local StorageBackend. Relative paths resolve
    # against the process working directory (backend/ in development).
    STORAGE_LOCAL_ROOT: str = "var/storage"

    # Connection pool tuning (SQLAlchemy engine).
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 1800  # seconds (30 minutes)

    # WebSocket classroom (Phase 3, slice 3C).
    # Lifetime of one freshly minted single-use class ticket (§37): long
    # enough to survive a slow handshake, short enough that a leaked
    # ticket is worthless almost immediately.
    WS_TICKET_TTL_SECONDS: int = 120
    # Client heartbeat cadence (§32) and how long a silent socket may
    # stay registered before the server terminates it (§33). Overridable
    # in tests (staleness is asserted with a sub-second timeout).
    WS_HEARTBEAT_INTERVAL_SECONDS: int = 20
    WS_STALE_TIMEOUT_SECONDS: int = 60
    # Message quota of the live classroom (Phase 3, slice 3D, §41):
    # at most WS_MESSAGE_RATE_LIMIT new messages from ONE participant in
    # ONE class per sliding WS_MESSAGE_RATE_WINDOW_SECONDS. Enforced by a
    # process-local limiter (see app.services.message_rate_limiter for its
    # honest scope); an idempotent retry never consumes quota.
    WS_MESSAGE_RATE_LIMIT: int = 10
    WS_MESSAGE_RATE_WINDOW_SECONDS: int = 10

    @property
    def jwt_secret(self) -> str:
        """The JWT signing secret, validated against the environment.

        The ``change-me`` placeholder is acceptable ONLY in the development
        and testing stages. In production an unsafe placeholder (or an
        absent/empty value) is a hard configuration error — authentication
        refuses to run rather than signing tokens with a known secret
        (Phase 5G Step 8).

        In production the secret must also be at least 32 characters to
        prevent brute-force attacks on weak keys.
        """
        secret = self.SECRET_KEY.strip()
        if not secret or secret.lower() in _UNSAFE_SECRETS:
            if self.ENVIRONMENT.lower() not in _DEVELOPMENT_STAGE_ENVIRONMENTS:
                raise RuntimeError(
                    "REFUSING authentication: SECRET_KEY is an unsafe placeholder "
                    "(or empty) while ENVIRONMENT is not a development stage. "
                    'Set a real secret, e.g. python -c "import secrets; '
                    'print(secrets.token_urlsafe(48))".'
                )
        if self.ENVIRONMENT.lower() not in _DEVELOPMENT_STAGE_ENVIRONMENTS:
            if len(secret) < 32:
                raise RuntimeError(
                    "REFUSING authentication: SECRET_KEY is too short "
                    f"({len(secret)} chars, minimum 32) while ENVIRONMENT "
                    "is not a development stage."
                )
        return secret

    def validate_startup(self) -> None:
        """Fail fast on configuration that must never serve traffic (Phase B2).

        Two silent defaults could otherwise boot an insecure server:

        - ``ENVIRONMENT`` falls back to ``"development"``, so a deployment
          that forgets to set it would silently accept the placeholder
          ``SECRET_KEY`` and development CORS/seed guards;
        - ``SECRET_KEY`` validation lives in the ``jwt_secret`` property,
          which only fires when a token is first signed — after the server
          is already up.

        Called at import time by ``app.main``, so ``uvicorn app.main:app``
        refuses to boot instead of starting insecurely. Every check here
        reuses the property logic above (single source of truth), and
        nothing in this method may log a secret value.
        """
        if "ENVIRONMENT" not in self.model_fields_set:
            raise RuntimeError(
                "REFUSING to start: ENVIRONMENT is not set. It must be set "
                "explicitly (development | testing | production) — the "
                "development default is never applied implicitly at startup."
            )
        if not self.ENVIRONMENT.strip():
            raise RuntimeError(
                "REFUSING to start: ENVIRONMENT is blank. "
                "Use development, testing or production."
            )
        # Raises for a placeholder/empty/too-short secret outside the
        # development stages — one source of truth for the rules.
        _ = self.jwt_secret
        # Forces the production CORS rules (no wildcard, no inherited
        # localhost origins) to run before the first request.
        _ = self.cors_origins
        return None

    @property
    def cors_origins(self) -> list[str]:
        """Parse the comma-separated CORS_ORIGINS env value into a list.

        Production hardening (Phase 5F): the localhost development defaults
        are never silently inherited by a production deployment — there the
        origins MUST be set explicitly via CORS_ORIGINS to non-localhost
        values, or CORS stays closed rather than trusting dev machine origins.
        """
        origins = [
            origin.strip()
            for origin in self.CORS_ORIGINS.split(",")
            if origin.strip()
        ]
        if self.ENVIRONMENT.lower() == "production":
            if "*" in origins:
                raise RuntimeError(
                    "CORS_ORIGINS must not contain a wildcard '*' in production. "
                    "Set explicit allowed origins."
                )
            return [origin for origin in origins if not _LOCAL_HOST_PATTERN.match(origin)]
        return origins

    @property
    def database_url(self) -> str:
        """Return the effective SQLAlchemy database URL.

        Builds ``postgresql+psycopg://user:password@host:port/dbname`` from
        the DB_* variables; a non-empty DATABASE_URL overrides them. The
        password is URL-encoded so special characters are safe.
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL
        credentials = self.DB_USER
        if self.DB_PASSWORD:
            credentials += f":{quote(self.DB_PASSWORD, safe='')}"
        return (
            f"postgresql+psycopg://{credentials}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
