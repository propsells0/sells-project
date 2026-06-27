"""
Configuration for Ain KPI System.
"""
import os
import secrets
from datetime import timedelta


def _get_secret_key():
    """Load SECRET_KEY from env. If not set, generate ephemeral key.
    WARNING: on Railway, set SECRET_KEY env var so sessions survive restarts."""
    key = os.environ.get("SECRET_KEY")
    if key and len(key) >= 32:
        return key
    return secrets.token_hex(32)


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = _get_secret_key()

    # Session security
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Cookies are served over HTTPS behind the Railway proxy; set SESSION_COOKIE_SECURE=true
    # once the site is served at a real HTTPS endpoint, off locally.
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "ain_sess")

    # Database — Railway provides DATABASE_URL automatically
    DATABASE_URL = os.environ.get("DATABASE_URL")

    # Fallback to individual settings if DATABASE_URL is not set
    DB_HOST = os.environ.get("DB_HOST", "caboose.proxy.rlwy.net")
    DB_PORT = int(os.environ.get("DB_PORT", 21778))
    DB_NAME = os.environ.get("DB_NAME", "railway")
    DB_USER = os.environ.get("DB_USER", "postgres")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "AdPVLYioZHOYsrpSswoILIvpkHwIReTz")

    # Master V API (only used if sync not disabled)
    MASTER_V_URL = "https://newapi.masterv.net/api/v3/public"
    MASTER_V_TOKEN = os.environ.get(
        "MASTER_V_TOKEN",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJVc2VySWQiOjMyMTksIlVzZXJFbWFpbCI6Im1vaGFtZWRoYW16YTEzMDNAZ21haWwuY29tIiwiVXNlclBob25lTnVtYmVyIjoiMjAxMDk5MjQ5NDk5IiwiSXNDbGllbnQiOnRydWUsImlhdCI6MTc3MTQyNjgwOCwiZXhwIjoxNzc0MDE4ODA4fQ.S9I6GS6gk96R8BkZwyLP0JNUic7jwwVTzJtjTdt7nkI"
    )

    # Default admin credentials (only used on first run if users table is empty).
    # The default password is weak on purpose so it triggers a forced change on first login.
    DEFAULT_ADMIN_USER = os.environ.get("DEFAULT_ADMIN_USER", "admin")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "ChangeMe1!")
    DEFAULT_ADMIN_EMAIL = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@example.com")

    # Sync control
    DISABLE_SYNC = _env_bool("DISABLE_SYNC", False)

    # ─── Mailer ────────────────────────────────────────────────────────────
    # Resend HTTPS API (preferred on Railway / Fly / Vercel — they block SMTP).
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

    # SMTP fallback — used automatically if RESEND_API_KEY is empty.
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS = _env_bool("SMTP_USE_TLS", True)
    MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER or "no-reply@ain-realestate.local")
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Ain Real Estate")

    # Password reset
    PASSWORD_RESET_TTL_MINUTES = int(os.environ.get("PASSWORD_RESET_TTL_MINUTES", 30))
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")

    # ─── Date-range filtering (cross-cutting feature) ──────────────────────
    DATE_RANGE_ENABLED = _env_bool("DATE_RANGE_ENABLED", True)
    MAX_RANGE_YEARS = int(os.environ.get("MAX_RANGE_YEARS", 5))

    # Audit trail — when on, range-aware endpoints insert a row into
    # query_audit per request. Default off; flip via env without redeploy.
    AUDIT_QUERIES = _env_bool("AUDIT_QUERIES", False)
