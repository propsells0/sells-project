"""
Auth helpers: password hashing (PBKDF2 w/ per-user salt), decorators,
validation helpers, in-memory rate limiter, and error helpers.

Security notes:
- SHA-256 with a shared salt was replaced by werkzeug's pbkdf2:sha256
  hasher which bundles a per-user random salt into each stored hash.
- Legacy (ain_kpi_2026_salt) hashes are still verified for backwards
  compatibility and transparently upgraded on successful login.
- Username/password/email validation is centralised here so blueprints
  just consume the helpers and return structured error_code payloads.
"""
import hashlib
import hmac
import os
import re
import time
from collections import defaultdict, deque
from functools import wraps
from threading import Lock
from typing import Optional, Tuple

from flask import g, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash


ROLES = ["admin", "manager", "team_leader", "dataentry", "sales", "marketing"]

# Role hierarchy for user CRUD permissions (Section 01 — DE-04). Each entry
# lists the roles a creator can assign on user creation/edit. dataentry sits
# below manager and can only create roles "below Data Entry level" per spec.
_CAN_CREATE_ROLES = {
    "admin":      ["admin", "manager", "team_leader", "dataentry", "sales", "marketing"],
    "manager":    ["team_leader", "dataentry", "sales", "marketing"],
    "dataentry":  ["team_leader", "dataentry", "sales", "marketing"],
}


def can_create_role(creator_role: str, target_role: str) -> bool:
    """True iff a user with `creator_role` may create/edit a user with `target_role`."""
    return target_role in _CAN_CREATE_ROLES.get(creator_role, [])


def allowed_target_roles(creator_role: str) -> list[str]:
    """The set of roles a creator may assign — used by the UI to filter the role picker."""
    return list(_CAN_CREATE_ROLES.get(creator_role, []))

# Legacy salt retained ONLY to verify old accounts; new hashes never use it.
_LEGACY_SALT = "ain_kpi_2026_salt"

# ─── Password hashing ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Generate a salted PBKDF2-SHA256 hash (werkzeug format)."""
    return generate_password_hash(password, method="pbkdf2:sha256:260000")


def _legacy_hash(password: str) -> str:
    return hashlib.sha256((password + _LEGACY_SALT).encode("utf-8")).hexdigest()


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against either a modern werkzeug hash or a legacy
    sha256 hash. Uses constant-time comparison where possible."""
    if not stored:
        return False
    try:
        if stored.startswith("pbkdf2:") or stored.startswith("scrypt:") or stored.startswith("argon2"):
            return check_password_hash(stored, password)
    except Exception:
        return False
    # Legacy path
    return hmac.compare_digest(_legacy_hash(password), stored)


def needs_rehash(stored: str) -> bool:
    """True if the stored hash is in the legacy format and should be upgraded."""
    if not stored:
        return True
    return not stored.startswith(("pbkdf2:", "scrypt:", "argon2"))


# ─── Validation helpers ────────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r"^[a-z0-9_.]{3,50}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-]{6,19}$")


def validate_username(username: Optional[str]) -> Optional[str]:
    if not username:
        return "required_fields_missing"
    if len(username) < 3:
        return "username_too_short"
    if not _USERNAME_RE.match(username):
        return "invalid_input"
    return None


def validate_email(email: Optional[str], required: bool = True) -> Optional[str]:
    if not email:
        return "email_required" if required else None
    if len(email) > 150 or not _EMAIL_RE.match(email):
        return "invalid_email"
    return None


def validate_phone(phone: Optional[str], required: bool = False) -> Optional[str]:
    if not phone:
        return None if not required else "required_fields_missing"
    if not _PHONE_RE.match(phone):
        return "invalid_phone"
    return None


def validate_password(password: Optional[str], username: Optional[str] = None) -> Optional[str]:
    """At least 8 chars, one letter, one digit. Must not equal username."""
    if not password or len(password) < 8 or len(password) > 128:
        return "weak_password"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "weak_password"
    if username and password.lower() == username.lower():
        return "password_username_same"
    return None


def error_response(error_code: str, status: int = 400):
    """Uniform API error payload; frontend maps `error_code` → localised text."""
    return jsonify({"error_code": error_code, "error": error_code}), status


# ─── Rate limiting (in-memory, per-process) ────────────────────────────────────

class _RateLimiter:
    def __init__(self):
        self._buckets: dict = defaultdict(deque)
        self._lock = Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Return False if rate-limited, True if allowed."""
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            q = self._buckets[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str):
        with self._lock:
            self._buckets.pop(key, None)


_limiter = _RateLimiter()


def rate_limit(prefix: str, limit: int = 5, window: int = 60):
    """Decorator: rate-limit per remote IP (or per forwarded IP)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = (
                request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or request.remote_addr
                or "unknown"
            )
            key = f"{prefix}:{ip}"
            if not _limiter.hit(key, limit, window):
                return error_response("rate_limited", 429)
            return fn(*args, **kwargs)
        return wrapper
    return deco


def rate_limit_reset(prefix: str):
    ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )
    _limiter.reset(f"{prefix}:{ip}")


# ─── Session helpers ───────────────────────────────────────────────────────────

def current_user():
    """Returns the current user as a dict for templates and APIs.

    avatar_url is fetched from the DB (not the session cookie) because a
    base64 data-URL avatar is ~30-80KB — far above the ~4KB browser cap
    on cookies. Storing it in the cookie session would cause Set-Cookie
    to be silently dropped by the browser, which is exactly the symptom
    we hit (avatar reverts after reload). The DB hit is cached on
    flask.g for the request so multiple template renders don't pile up.
    """
    if "user_id" not in session:
        return None
    uid = session.get("user_id")

    # Per-request cache
    cached = getattr(g, "_current_user_cache", None)
    if cached and cached.get("id") == uid:
        return cached

    avatar_url = None
    try:
        from app.database import get_conn
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT avatar_url FROM users WHERE id = %s", (uid,))
                row = cur.fetchone()
                if row:
                    avatar_url = row[0]
        finally:
            conn.close()
    except Exception:
        # Avatar fetch is non-critical — fall back to no avatar rather than
        # breaking page renders if the DB momentarily flakes.
        pass

    user = {
        "id": uid,
        "username": session.get("username"),
        "full_name": session.get("full_name"),
        "role": session.get("role"),
        "email": session.get("email"),
        "phone": session.get("phone"),
        "avatar_url": avatar_url,
    }
    g._current_user_cache = user
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return error_response("unauthorized", 401)
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


def role_required(*allowed_roles):
    """Admin always passes. Otherwise must be in allowed_roles."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                if request.path.startswith("/api/"):
                    return error_response("unauthorized", 401)
                return redirect("/login")
            user_role = session.get("role")
            if user_role != "admin" and user_role not in allowed_roles:
                if request.path.startswith("/api/"):
                    return error_response("forbidden", 403)
                return redirect("/")
            return f(*args, **kwargs)
        return wrapper
    return decorator


def role_home(role: str) -> str:
    # Admin lands on the dashboard after login — closer to the day-to-day
    # workflow than the user-management page. /admin remains accessible via
    # the sidebar nav.
    return {
        "admin": "/dashboard",
        "manager": "/dashboard",
        "team_leader": "/team-leader",
        "dataentry": "/data-entry",
        "sales": "/propfinder",
        "marketing": "/marketing",
    }.get(role, "/propfinder")


# ─── CSRF token (double-submit pattern) ────────────────────────────────────────

def ensure_csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = os.urandom(24).hex()
        session["_csrf"] = tok
    return tok


def csrf_protect(f):
    """Require a matching `X-CSRF-Token` header for mutating requests."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            tok = request.headers.get("X-CSRF-Token", "")
            expected = session.get("_csrf", "")
            if not tok or not expected or not hmac.compare_digest(tok, expected):
                return error_response("forbidden", 403)
        return f(*args, **kwargs)
    return wrapper
