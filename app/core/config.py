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

    # Maximum active refresh sessions per user. When exceeded, the oldest
    # session is revoked before creating a new one.
    MAX_SESSIONS_PER_USER: int = 10

    # Email (Resend API) — used for password reset and account notifications.
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "noreply@example.com"

    # Frontend URL — base URL for password reset links.
    FRONTEND_URL: str = "http://localhost:5173"

    # Connection pool tuning (SQLAlchemy engine).
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 1800  # seconds (30 minutes)

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
