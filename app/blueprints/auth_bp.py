"""
Authentication blueprint — login, logout, register, change-password,
forgot-password + reset flow. All error responses use structured
{error_code} payloads; frontend maps them to localized text.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Blueprint, current_app, g, jsonify, request, session

from app.auth import (
    current_user,
    ensure_csrf_token,
    error_response,
    hash_password,
    login_required,
    needs_rehash,
    rate_limit,
    rate_limit_reset,
    role_home,
    validate_email,
    validate_password,
    validate_phone,
    validate_username,
    verify_password,
)
from app.database import get_conn
from app.mailer import (
    password_reset_email,
    signup_pending_email,
    send_mail,
)
from config import Config

log = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


# ─── Helpers ───────────────────────────────────────────────────────────────

def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _client_ip() -> str:
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    )


def _reset_base_url() -> str:
    if Config.APP_BASE_URL:
        return Config.APP_BASE_URL
    return request.url_root.rstrip("/")


# ─── Login ─────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
@rate_limit("login", limit=8, window=60)
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    if not username or not password:
        return error_response("required_fields_missing", 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # approval_status is a new column — guard against deployments where
            # the migration hasn't completed so the existing login flow keeps
            # working even if the DB schema is mid-upgrade.
            try:
                cur.execute("""
                    SELECT id, username, full_name, password_hash, role, active, email,
                           phone, avatar_url, failed_logins, locked_until, approval_status
                    FROM users WHERE LOWER(username) = %s
                """, (username,))
                user = cur.fetchone()
            except psycopg2.errors.UndefinedColumn:
                conn.rollback()
                cur.execute("""
                    SELECT id, username, full_name, password_hash, role, active, email,
                           phone, avatar_url, failed_logins, locked_until
                    FROM users WHERE LOWER(username) = %s
                """, (username,))
                user = cur.fetchone()
                if user is not None:
                    user = dict(user)
                    user["approval_status"] = "approved"

            # If credentials match a pending signup, surface a clear error so
            # the user knows their request is queued — they otherwise get the
            # same "invalid_credentials" as a typo, which sends them in circles.
            # Only revealed on a correct password, so it doesn't leak existence.
            if (user
                and user.get("approval_status") == "pending"
                and verify_password(password, user["password_hash"])):
                return error_response("account_pending_approval", 403)

            # Constant-ish path to avoid username enumeration
            valid = bool(
                user
                and user["active"]
                and (not user.get("locked_until") or user["locked_until"] < datetime.utcnow())
                and verify_password(password, user["password_hash"])
            )

            if not valid:
                if user and user["active"]:
                    # Track failures; soft-lock at 10
                    fails = (user.get("failed_logins") or 0) + 1
                    locked = None
                    if fails >= 10:
                        locked = datetime.utcnow() + timedelta(minutes=15)
                        fails = 0
                    cur.execute(
                        "UPDATE users SET failed_logins = %s, locked_until = %s WHERE id = %s",
                        (fails, locked, user["id"]),
                    )
                    conn.commit()
                return error_response("invalid_credentials", 401)

            # Success — rotate session, transparently upgrade legacy hash
            if needs_rehash(user["password_hash"]):
                cur.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (hash_password(password), user["id"]),
                )
            cur.execute(
                "UPDATE users SET last_login = NOW(), failed_logins = 0, locked_until = NULL WHERE id = %s",
                (user["id"],),
            )
        conn.commit()

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["full_name"] = user["full_name"]
        session["role"] = user["role"]
        session["email"] = user.get("email")
        session["phone"] = user.get("phone")
        # avatar_url is intentionally NOT stored in the cookie session — a
        # data-URL avatar (~30-80KB) blows past the browser's ~4KB cookie cap
        # and the new Set-Cookie gets silently dropped, which is exactly the
        # symptom that broke avatar persistence on reload. current_user()
        # fetches it fresh from the DB and caches per-request via flask.g.
        session.permanent = True
        ensure_csrf_token()
        rate_limit_reset("login")

        log.info("✅ Login: %s (%s)", username, user["role"])
        return jsonify({
            "id": user["id"],
            "username": user["username"],
            "full_name": user["full_name"],
            "role": user["role"],
            "redirect": role_home(user["role"]),
        })
    except Exception as e:
        log.error("Login error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


# Self-registration is enabled but gated by admin approval. New rows land
# with active=false, approval_status='pending'. The user can't log in until
# an admin approves them via /api/users/<id>/approve. Only roles below admin
# (and below manager) are allowed at signup; admins/managers must still be
# created from the admin panel.
_SIGNUP_ALLOWED_ROLES = {"sales", "marketing", "team_leader", "dataentry"}


@auth_bp.route("/register", methods=["POST"])
@rate_limit("register", limit=5, window=600)
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    full_name = (data.get("full_name") or "").strip()
    password = data.get("password") or ""
    email = (data.get("email") or "").strip().lower() or None
    phone = (data.get("phone") or "").strip() or None
    # Role is a hint to the admin during approval; default to 'sales' if the
    # client sent something unexpected so we never hand out elevated access.
    role = (data.get("role") or "sales").strip().lower()
    if role not in _SIGNUP_ALLOWED_ROLES:
        role = "sales"

    # Capture the visual context the user is signing up from. We seed the
    # new row with these so the very first email (the pending-review one)
    # lands in the same skin the user just left in the signup screen, and
    # later transactional mail keeps the same identity until they change it.
    pref_theme = (data.get("theme") or "").strip().lower()
    if pref_theme not in _VALID_THEMES:
        pref_theme = "light"
    pref_lang = (data.get("lang") or "").strip().lower()
    if pref_lang not in _VALID_LANGS:
        pref_lang = "ar"

    if not full_name:
        return error_response("required_fields_missing", 400)
    if (err := validate_username(username)):
        return error_response(err, 400)
    if (err := validate_email(email, required=True)):
        return error_response(err, 400)
    # Phone is required on signup so the admin has a second way to reach the
    # user during approval (email may bounce / land in spam).
    if (err := validate_phone(phone, required=True)):
        return error_response(err, 400)
    if (err := validate_password(password, username=username)):
        return error_response(err, 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Username and email must be globally unique. Pending rows count
            # towards the constraint so a second signup with the same email
            # while an approval is in flight gets a clear error.
            cur.execute("SELECT 1 FROM users WHERE LOWER(username) = %s LIMIT 1", (username,))
            if cur.fetchone():
                return error_response("username_taken", 409)
            cur.execute("SELECT 1 FROM users WHERE LOWER(email) = %s LIMIT 1", (email,))
            if cur.fetchone():
                return error_response("email_taken", 409)
            try:
                cur.execute("""
                    INSERT INTO users (username, full_name, password_hash, role, email, phone,
                                       active, approval_status, preferred_theme, preferred_lang)
                    VALUES (%s, %s, %s, %s, %s, %s, false, 'pending', %s, %s)
                    RETURNING id
                """, (username, full_name[:150], hash_password(password), role, email, phone,
                      pref_theme, pref_lang))
                new_id = cur.fetchone()[0]
            except psycopg2.IntegrityError as ie:
                # Two concurrent signups for the same username/email can race
                # past the SELECT-then-INSERT check. The unique index catches
                # it; surface a clean error instead of a 500.
                conn.rollback()
                msg = str(ie).lower()
                if "email" in msg:
                    return error_response("email_taken", 409)
                return error_response("username_taken", 409)
        conn.commit()
        log.info("📝 Signup pending: %s (%s) id=%s", username, role, new_id)

        # Send the user a confirmation that their request is in the queue.
        # Best-effort: a missing SMTP config shouldn't block account creation.
        try:
            subject, text, html = signup_pending_email(full_name, theme=pref_theme)
            send_mail(email, subject, text, html)
        except Exception as e:
            log.warning("signup_pending_email failed for %s: %s", email, e)

        return jsonify({"ok": True, "pending": True})
    except Exception as e:
        log.error("register error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


# ─── Session endpoints ─────────────────────────────────────────────────────

@auth_bp.route("/logout", methods=["POST"])
def logout():
    # Mark offline immediately by stamping last_seen far enough in the past
    # that the admin "online within 2 minutes" check fails. Using NULL would
    # also work, but a timestamp keeps the column queryable for "last seen X
    # minutes ago" displays.
    uid = session.get("user_id")
    if uid:
        conn = None
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_seen = NOW() - INTERVAL '10 minutes' WHERE id = %s",
                    (uid,),
                )
            conn.commit()
        except Exception as e:
            log.debug("logout last_seen reset failed: %s", e)
        finally:
            if conn:
                conn.close()
    session.clear()
    return jsonify({"ok": True})


@auth_bp.route("/me")
def me():
    u = current_user()
    if not u:
        return error_response("unauthorized", 401)
    # Enrich the session payload with DB-side fields the profile page needs
    # (created_at, last_login). Cheap, single-row lookup; the connection pool
    # absorbs the per-request cost.
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # preferred_theme/preferred_lang let the in-app shell render in the
            # last skin the user picked, even on a fresh device — and they're
            # the source of truth for transactional emails so Pending /
            # Approved / Reset all land in the same colour scheme the user just
            # left in the app.
            cur.execute("""
                SELECT email, phone, avatar_url, created_at, last_login,
                       preferred_theme, preferred_lang
                FROM users WHERE id = %s
            """, (u["id"],))
            row = cur.fetchone()
        if row:
            u["email"] = row.get("email") or u.get("email")
            u["phone"] = row.get("phone") or u.get("phone")
            u["avatar_url"] = row.get("avatar_url") or u.get("avatar_url")
            u["preferred_theme"] = row.get("preferred_theme") or "light"
            u["preferred_lang"] = row.get("preferred_lang") or "ar"
            for k in ("created_at", "last_login"):
                v = row.get(k)
                u[k] = v.isoformat() if v else None
    except Exception as e:
        log.warning("me: enrichment failed: %s", e)
    finally:
        if conn:
            conn.close()
    u["csrf"] = ensure_csrf_token()
    return jsonify(u)


# ─── Display preferences (theme + language) ────────────────────────────────

_VALID_THEMES = {"light", "dark"}
_VALID_LANGS = {"ar", "en"}


@auth_bp.route("/preferences", methods=["POST"])
@login_required
def update_preferences():
    """Persist the user's theme/lang choice. Called from the in-app toggle so
    the next email lands in the same skin the user just picked, and a fresh
    device opens straight into the right palette."""
    data = request.get_json(silent=True) or {}
    theme = (data.get("theme") or "").strip().lower()
    lang = (data.get("lang") or "").strip().lower()

    fields = []
    params = []
    if theme:
        if theme not in _VALID_THEMES:
            return error_response("invalid_input", 400)
        fields.append("preferred_theme = %s")
        params.append(theme)
    if lang:
        if lang not in _VALID_LANGS:
            return error_response("invalid_input", 400)
        fields.append("preferred_lang = %s")
        params.append(lang)

    if not fields:
        return error_response("required_fields_missing", 400)

    fields.append("updated_at = NOW()")
    params.append(session["user_id"])

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE id = %s",
                params,
            )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("update_preferences error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@auth_bp.route("/csrf", methods=["GET"])
@login_required
def csrf():
    return jsonify({"csrf": ensure_csrf_token()})


# ─── Change password ───────────────────────────────────────────────────────

@auth_bp.route("/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    old_pw = data.get("old_password") or ""
    new_pw = data.get("new_password") or ""
    username = session.get("username") or ""

    if (err := validate_password(new_pw, username=username)):
        return error_response(err, 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (session["user_id"],))
            row = cur.fetchone()
            if not row or not verify_password(old_pw, row["password_hash"]):
                return error_response("wrong_current_password", 401)
            # Reusing the existing password as the "new" one is a no-op rotation
            # that gives the user false confidence the credential changed.
            if verify_password(new_pw, row["password_hash"]):
                return error_response("password_unchanged", 400)
            cur.execute("""
                UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s
            """, (hash_password(new_pw), session["user_id"]))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("change-password error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


# ─── Profile updates (self-service) ────────────────────────────────────────

# Cap on the avatar payload AFTER base64 decoding. ~150 KB is plenty for a
# 256×256 JPEG/PNG and keeps the user row from ballooning the DB.
_AVATAR_MAX_BYTES = 200 * 1024
_AVATAR_ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp"}


def _validate_avatar_data_url(data_url: str):
    """Returns (mime, size_bytes, error_code).

    Accepts only data:image/{png,jpeg,webp};base64,<...> URLs and rejects
    anything that isn't a real image of bounded size. Defense-in-depth on
    top of the cap on the column itself.
    """
    if not data_url or not isinstance(data_url, str):
        return None, 0, "avatar_invalid"
    if not data_url.startswith("data:"):
        return None, 0, "avatar_invalid"
    try:
        header, b64 = data_url.split(",", 1)
    except ValueError:
        return None, 0, "avatar_invalid"
    if ";base64" not in header:
        return None, 0, "avatar_invalid"
    mime = header[5:].split(";", 1)[0].strip().lower()
    if mime not in _AVATAR_ALLOWED_MIMES:
        return None, 0, "avatar_unsupported_type"
    # Reject anything that won't decode cleanly
    import base64 as _b64
    try:
        raw = _b64.b64decode(b64, validate=True)
    except Exception:
        return None, 0, "avatar_invalid"
    if len(raw) == 0:
        return None, 0, "avatar_invalid"
    if len(raw) > _AVATAR_MAX_BYTES:
        return None, len(raw), "avatar_too_large"
    return mime, len(raw), None


@auth_bp.route("/avatar", methods=["POST"])
@login_required
def upload_avatar():
    """Save (or replace) the current user's avatar.

    The client resizes to 256×256 in a canvas and posts the resulting data
    URL — keeps payload small and avoids us needing Pillow on the server.
    """
    data = request.get_json(silent=True) or {}
    data_url = data.get("avatar_url") or ""

    mime, size, err = _validate_avatar_data_url(data_url)
    if err:
        return error_response(err, 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET avatar_url = %s, updated_at = NOW() WHERE id = %s",
                (data_url, session["user_id"]),
            )
        conn.commit()
        # Bust any per-request cache so the next current_user() in this
        # request (e.g. response middleware) sees the new avatar. The DB is
        # the source of truth — no session write here on purpose.
        if hasattr(g, "_current_user_cache"):
            g._current_user_cache = None
        log.info("avatar updated for user_id=%s mime=%s size=%d", session["user_id"], mime, size)
        return jsonify({"ok": True, "avatar_url": data_url})
    except Exception as e:
        log.error("upload_avatar error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@auth_bp.route("/avatar", methods=["DELETE"])
@login_required
def delete_avatar():
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET avatar_url = NULL, updated_at = NOW() WHERE id = %s",
                (session["user_id"],),
            )
        conn.commit()
        if hasattr(g, "_current_user_cache"):
            g._current_user_cache = None
        return jsonify({"ok": True})
    except Exception as e:
        log.error("delete_avatar error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@auth_bp.route("/profile", methods=["PATCH"])
@login_required
def update_profile():
    """Self-service edits: full_name, email, phone. Username + role are
    admin-only. Validates each field with the same helpers /api/users uses."""
    data = request.get_json(silent=True) or {}
    fields = []
    params = []

    if "full_name" in data:
        full_name = (data.get("full_name") or "").strip()
        if not full_name:
            return error_response("required_fields_missing", 400)
        fields.append("full_name = %s")
        params.append(full_name[:150])

    if "email" in data:
        email = (data.get("email") or "").strip().lower() or None
        if email:
            if (err := validate_email(email)):
                return error_response(err, 400)
        fields.append("email = %s")
        params.append(email)

    if "phone" in data:
        phone = (data.get("phone") or "").strip() or None
        if (err := validate_phone(phone, required=False)):
            return error_response(err, 400)
        fields.append("phone = %s")
        params.append(phone)

    if not fields:
        return error_response("required_fields_missing", 400)

    fields.append("updated_at = NOW()")
    params.append(session["user_id"])

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE id = %s "
                    f"RETURNING full_name, email, phone",
                    params,
                )
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return error_response("email_taken", 409)
            row = cur.fetchone()
        conn.commit()

        # Mirror back into session so subsequent page renders see the new values.
        if row:
            session["full_name"] = row["full_name"]
            session["email"] = row.get("email")
            session["phone"] = row.get("phone")
        return jsonify({"ok": True, "user": dict(row) if row else {}})
    except Exception as e:
        log.error("update_profile error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


# ─── Forgot password ───────────────────────────────────────────────────────

@auth_bp.route("/forgot-password", methods=["POST"])
@rate_limit("forgot", limit=5, window=600)
def forgot_password():
    """Always respond 200 to prevent email enumeration. If the email exists
    and SMTP is configured, a reset link is emailed."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or validate_email(email, required=True):
        # Still return OK to avoid enumeration
        return jsonify({"ok": True})

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, full_name, active, preferred_theme, preferred_lang
                FROM users WHERE LOWER(email) = %s
            """, (email,))
            user = cur.fetchone()

            if user and user["active"]:
                # Invalidate previous unused tokens for this user
                cur.execute(
                    "UPDATE password_reset_tokens SET used_at = NOW() "
                    "WHERE user_id = %s AND used_at IS NULL",
                    (user["id"],),
                )

                raw_token = secrets.token_urlsafe(32)
                token_hash = _hash_token(raw_token)
                expires = datetime.utcnow() + timedelta(minutes=Config.PASSWORD_RESET_TTL_MINUTES)
                cur.execute("""
                    INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_ip)
                    VALUES (%s, %s, %s, %s)
                """, (user["id"], token_hash, expires, _client_ip()[:64]))
                conn.commit()

                # Carry the user's display prefs through the reset URL so the
                # page they land on opens in the same skin as the email — even
                # if they click the link from a fresh device with no
                # localStorage history.
                user_theme = (user.get("preferred_theme") or "light").lower()
                user_lang = (user.get("preferred_lang") or "ar").lower()
                reset_url = (
                    f"{_reset_base_url()}/reset-password?token={raw_token}"
                    f"&theme={user_theme}&lang={user_lang}"
                )
                subject, text, html = password_reset_email(
                    user["full_name"] or "",
                    reset_url,
                    Config.PASSWORD_RESET_TTL_MINUTES,
                    theme=user_theme,
                )
                send_mail(email, subject, text, html)
    except Exception as e:
        log.error("forgot-password error: %s", e)
    finally:
        if conn:
            conn.close()

    return jsonify({"ok": True})


@auth_bp.route("/reset-password", methods=["POST"])
@rate_limit("reset", limit=10, window=600)
def reset_password():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    new_pw = data.get("new_password") or ""

    if not token or len(token) < 16:
        return error_response("invalid_token", 400)

    token_hash = _hash_token(token)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.id AS token_id, t.user_id, t.expires_at, t.used_at,
                       u.username, u.active, u.password_hash
                FROM password_reset_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = %s
            """, (token_hash,))
            row = cur.fetchone()
            if not row or not row["active"] or row["used_at"] is not None:
                return error_response("invalid_token", 400)
            if row["expires_at"] < datetime.utcnow():
                return error_response("token_expired", 400)

            if (err := validate_password(new_pw, username=row["username"])):
                return error_response(err, 400)

            # A reset where the user types in their CURRENT password is almost
            # always a "I forgot which one I used" mistake — silently accepting
            # it would burn the reset token without actually rotating the
            # credential. Force a different value so the reset achieves what
            # the user asked for.
            if verify_password(new_pw, row["password_hash"]):
                return error_response("password_unchanged", 400)

            cur.execute(
                "UPDATE users SET password_hash = %s, failed_logins = 0, "
                "locked_until = NULL, updated_at = NOW() WHERE id = %s",
                (hash_password(new_pw), row["user_id"]),
            )
            cur.execute(
                "UPDATE password_reset_tokens SET used_at = NOW() WHERE id = %s",
                (row["token_id"],),
            )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("reset-password error: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@auth_bp.route("/reset-password/validate", methods=["GET"])
def validate_reset_token():
    token = (request.args.get("token") or "").strip()
    if not token or len(token) < 16:
        return jsonify({"valid": False})
    token_hash = _hash_token(token)
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT expires_at, used_at
                FROM password_reset_tokens WHERE token_hash = %s
            """, (token_hash,))
            row = cur.fetchone()
            if not row:
                return jsonify({"valid": False})
            expires_at, used_at = row
            if used_at is not None:
                return jsonify({"valid": False})
            if expires_at < datetime.utcnow():
                return jsonify({"valid": False, "reason": "expired"})
            return jsonify({"valid": True})
    except Exception as e:
        log.error("validate_reset_token: %s", e)
        return jsonify({"valid": False})
    finally:
        if conn:
            conn.close()
