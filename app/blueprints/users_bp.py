"""
Users management blueprint — admin CRUD for users.
All error responses use structured {error_code} payloads.
"""
import logging

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, jsonify, session

from app.auth import (
    ROLES,
    can_create_role,
    error_response,
    hash_password,
    role_required,
    validate_email,
    validate_password,
    validate_phone,
    validate_username,
)
from app.database import get_conn
from app.mailer import (
    send_mail,
    signup_approved_email,
    signup_rejected_email,
)

log = logging.getLogger(__name__)
users_bp = Blueprint("users", __name__, url_prefix="/api/users")


def _user_to_dict(row):
    d = dict(row)
    for k in ("created_at", "updated_at", "last_login", "last_seen"):
        if d.get(k):
            d[k] = d[k].isoformat()
    d.pop("password_hash", None)
    return d


@users_bp.route("", methods=["GET"])
@role_required("admin", "manager", "dataentry")
def list_users():
    role_filter = request.args.get("role")
    active_only = request.args.get("active_only") == "true"
    team_filter = request.args.get("team_id")
    if role_filter and role_filter not in ROLES:
        return error_response("invalid_role", 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # team_id/team_name surface "the team they lead" (if any),
            # falling back to "the team they belong to". This way TLs
            # always show their own team even when users.team_id wasn't
            # written when the team was created.
            # team_leader_id/team_leader_name: only meaningful for sales reps
            # (their TL is tm.leader_id). NULL for all other roles.
            q = """SELECT u.id, u.username, u.full_name, u.role, u.email, u.phone,
                          u.active, u.avatar_url, u.approval_status,
                          COALESCE(tl.id,   u.team_id) AS team_id,
                          COALESCE(tl.name, tm.name)   AS team_name,
                          CASE WHEN u.role = 'sales' THEN tm.leader_id END AS team_leader_id,
                          CASE WHEN u.role = 'sales' THEN tlu.full_name END AS team_leader_name,
                          u.created_at, u.updated_at, u.last_login, u.last_seen,
                          (u.last_seen IS NOT NULL AND u.last_seen > NOW() - INTERVAL '2 minutes') AS is_online
                   FROM users u
                   LEFT JOIN teams tm ON tm.id        = u.team_id
                   LEFT JOIN teams tl ON tl.leader_id = u.id
                   LEFT JOIN users tlu ON tlu.id      = tm.leader_id
                   WHERE 1=1"""
            params = []
            if role_filter:
                q += " AND u.role = %s"
                params.append(role_filter)
            if active_only:
                q += " AND u.active = true"
            if team_filter:
                if team_filter == "none":
                    q += " AND COALESCE(tl.id, u.team_id) IS NULL"
                else:
                    try:
                        q += " AND COALESCE(tl.id, u.team_id) = %s"
                        params.append(int(team_filter))
                    except ValueError:
                        return error_response("invalid_team_id", 400)
            q += " ORDER BY u.role DESC, u.full_name ASC"
            cur.execute(q, params)
            users = [_user_to_dict(r) for r in cur.fetchall()]
        return jsonify(users)
    except Exception as e:
        log.error("list_users: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/<int:user_id>", methods=["GET"])
@role_required("admin", "manager")
def get_user(user_id):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, username, full_name, role, email, phone, active, avatar_url,
                       approval_status, created_at, updated_at, last_login, last_seen,
                       (last_seen IS NOT NULL AND last_seen > NOW() - INTERVAL '2 minutes') AS is_online
                FROM users WHERE id = %s
            """, (user_id,))
            row = cur.fetchone()
        if not row:
            return error_response("not_found", 404)
        return jsonify(_user_to_dict(row))
    except Exception as e:
        log.error("get_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("", methods=["POST"])
@role_required("admin", "manager", "dataentry")
def create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    full_name = (data.get("full_name") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "sales").strip()
    email = (data.get("email") or "").strip().lower() or None
    phone = (data.get("phone") or "").strip() or None

    if not full_name:
        return error_response("required_fields_missing", 400)
    if role not in ROLES:
        return error_response("invalid_role", 400)
    # Hierarchy guard (DE-04): the creator may only assign roles permitted
    # by their own role. dataentry can create sales/marketing only.
    if not can_create_role(session.get("role"), role):
        return error_response("role_not_allowed", 403)
    if (err := validate_username(username)):
        return error_response(err, 400)
    if (err := validate_email(email, required=True)):
        return error_response(err, 400)
    if (err := validate_phone(phone, required=False)):
        return error_response(err, 400)
    if (err := validate_password(password, username=username)):
        return error_response(err, 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE LOWER(email) = %s LIMIT 1", (email,))
            if cur.fetchone():
                return error_response("email_taken", 409)
            try:
                cur.execute("""
                    INSERT INTO users (username, full_name, password_hash, role, email, phone)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (username, full_name[:150], hash_password(password), role, email, phone))
                new_id = cur.fetchone()[0]
            except psycopg2.IntegrityError:
                conn.rollback()
                return error_response("username_taken", 409)
        conn.commit()
        log.info("✅ User created: %s (%s)", username, role)
        return jsonify({"id": new_id}), 201
    except Exception as e:
        log.error("create_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/<int:user_id>", methods=["PUT"])
@role_required("admin", "manager", "dataentry")
def update_user(user_id):
    data = request.get_json(silent=True) or {}
    actor_role = session.get("role")
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch existing to validate updates contextually
            cur.execute("SELECT username, email, role FROM users WHERE id = %s", (user_id,))
            existing = cur.fetchone()
            if not existing:
                return error_response("not_found", 404)

            # Hierarchy guard: the actor must be allowed to manage the
            # existing role. Prevents dataentry from editing admins/managers etc.
            if not can_create_role(actor_role, existing["role"]):
                return error_response("role_not_allowed", 403)

            fields = []
            params = []

            if "full_name" in data:
                fn = (data["full_name"] or "").strip()
                if not fn:
                    return error_response("required_fields_missing", 400)
                fields.append("full_name = %s")
                params.append(fn[:150])

            if "role" in data:
                r = (data["role"] or "").strip()
                if r not in ROLES:
                    return error_response("invalid_role", 400)
                # Hierarchy guard for the NEW role too — dataentry can't
                # promote a sales rep to manager.
                if not can_create_role(actor_role, r):
                    return error_response("role_not_allowed", 403)
                # Guard against demoting the last admin
                if existing["role"] == "admin" and r != "admin":
                    cur.execute(
                        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = true AND id <> %s",
                        (user_id,),
                    )
                    if cur.fetchone()["count"] == 0:
                        return error_response("cannot_delete_last_admin", 400)
                fields.append("role = %s")
                params.append(r)

            if "email" in data:
                em = (data["email"] or "").strip().lower() or None
                if (err := validate_email(em, required=True)):
                    return error_response(err, 400)
                if em and em != (existing["email"] or "").lower():
                    cur.execute(
                        "SELECT 1 FROM users WHERE LOWER(email) = %s AND id <> %s LIMIT 1",
                        (em, user_id),
                    )
                    if cur.fetchone():
                        return error_response("email_taken", 409)
                fields.append("email = %s")
                params.append(em)

            if "phone" in data:
                ph = (data["phone"] or "").strip() or None
                if (err := validate_phone(ph, required=False)):
                    return error_response(err, 400)
                fields.append("phone = %s")
                params.append(ph)

            if "active" in data:
                fields.append("active = %s")
                params.append(bool(data["active"]))

            if data.get("password"):
                if (err := validate_password(data["password"], username=existing["username"])):
                    return error_response(err, 400)
                fields.append("password_hash = %s")
                params.append(hash_password(data["password"]))

            if not fields:
                return error_response("no_changes", 400)

            fields.append("updated_at = NOW()")
            params.append(user_id)
            cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", params)
            if cur.rowcount == 0:
                return error_response("not_found", 404)
        conn.commit()
        log.info("✅ User %s updated", user_id)
        return jsonify({"ok": True})
    except Exception as e:
        log.error("update_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/<int:user_id>", methods=["DELETE"])
@role_required("admin", "manager", "dataentry")
def delete_user(user_id):
    """Hard delete — removes user and all their KPI entries (CASCADE)."""
    if user_id == session.get("user_id"):
        return error_response("forbidden", 403)
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)
            # Hierarchy guard — actor must be allowed to manage this role.
            if not can_create_role(session.get("role"), row[0]):
                return error_response("role_not_allowed", 403)
            if row[0] == "admin":
                cur.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = true"
                )
                if cur.fetchone()[0] <= 1:
                    return error_response("cannot_delete_last_admin", 400)
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        log.info("✅ User %s deleted", user_id)
        return jsonify({"ok": True})
    except Exception as e:
        log.error("delete_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/<int:user_id>/deactivate", methods=["POST"])
@role_required("admin", "manager", "dataentry")
def deactivate_user(user_id):
    if user_id == session.get("user_id"):
        return error_response("forbidden", 403)
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)
            if not can_create_role(session.get("role"), row[0]):
                return error_response("role_not_allowed", 403)
            if row[0] == "admin":
                cur.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = true"
                )
                if cur.fetchone()[0] <= 1:
                    return error_response("cannot_delete_last_admin", 400)
            cur.execute(
                "UPDATE users SET active = false, updated_at = NOW() WHERE id = %s",
                (user_id,),
            )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("deactivate_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/pending", methods=["GET"])
@role_required("admin", "manager")
def list_pending():
    """Pending signup requests — surfaced in the admin panel."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, username, full_name, email, phone, role, created_at
                FROM users
                WHERE approval_status = 'pending'
                ORDER BY created_at DESC
            """)
            rows = [_user_to_dict(r) for r in cur.fetchall()]
        return jsonify(rows)
    except Exception as e:
        log.error("list_pending: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/<int:user_id>/approve", methods=["POST"])
@role_required("admin", "manager")
def approve_user(user_id):
    """Approve a pending signup. Admin can override the role at approve time
    via the JSON body's "role" field; default keeps whatever the user picked
    at signup."""
    data = request.get_json(silent=True) or {}
    new_role = (data.get("role") or "").strip() or None
    if new_role and new_role not in ROLES:
        return error_response("invalid_role", 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, full_name, email, role, approval_status, preferred_theme
                FROM users WHERE id = %s
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)
            if row["approval_status"] != "pending":
                return error_response("not_pending", 400)
            # Hierarchy guard: actor must be allowed to manage the *resulting* role.
            target_role = new_role or row["role"]
            if not can_create_role(session.get("role"), target_role):
                return error_response("role_not_allowed", 403)

            cur.execute("""
                UPDATE users SET active = true,
                                 approval_status = 'approved',
                                 role = COALESCE(%s, role),
                                 failed_logins = 0,
                                 locked_until = NULL,
                                 updated_at = NOW()
                WHERE id = %s
            """, (new_role, user_id))
        conn.commit()
        log.info("✅ User %s approved by uid=%s as role=%s",
                 user_id, session.get("user_id"), target_role)

        # Surface email status to the admin UI: send_mail returns False when
        # neither RESEND_API_KEY nor SMTP credentials are configured, which is
        # the most common reason approval emails "don't arrive". The frontend
        # turns this into a warning toast so the admin knows to follow up out
        # of band instead of silently leaving the user in the dark.
        email_sent = False
        if row.get("email"):
            try:
                user_theme = (row.get("preferred_theme") or "light").lower()
                subject, text, html = signup_approved_email(row["full_name"] or "", theme=user_theme)
                email_sent = bool(send_mail(row["email"], subject, text, html))
            except Exception as e:
                log.warning("approve email failed for %s: %s", row["email"], e)

        return jsonify({"ok": True, "email_sent": email_sent, "email": row.get("email")})
    except Exception as e:
        log.error("approve_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/<int:user_id>/reject", methods=["POST"])
@role_required("admin", "manager")
def reject_user(user_id):
    """Reject a pending signup. Hard-deletes the row — keeping rejected
    accounts around just clutters the table and they can re-apply if they
    were rejected by mistake."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, full_name, email, role, approval_status, preferred_theme
                FROM users WHERE id = %s
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)
            if row["approval_status"] != "pending":
                return error_response("not_pending", 400)
            if not can_create_role(session.get("role"), row["role"]):
                return error_response("role_not_allowed", 403)
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        log.info("🚫 User %s rejected by uid=%s", user_id, session.get("user_id"))

        email_sent = False
        if row.get("email"):
            try:
                user_theme = (row.get("preferred_theme") or "light").lower()
                subject, text, html = signup_rejected_email(row["full_name"] or "", theme=user_theme)
                email_sent = bool(send_mail(row["email"], subject, text, html))
            except Exception as e:
                log.warning("reject email failed for %s: %s", row["email"], e)

        return jsonify({"ok": True, "email_sent": email_sent, "email": row.get("email")})
    except Exception as e:
        log.error("reject_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()


@users_bp.route("/<int:user_id>/activate", methods=["POST"])
@role_required("admin", "manager", "dataentry")
def activate_user(user_id):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Hierarchy guard before activating
            cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)
            if not can_create_role(session.get("role"), row[0]):
                return error_response("role_not_allowed", 403)
            cur.execute(
                "UPDATE users SET active = true, updated_at = NOW(), "
                "failed_logins = 0, locked_until = NULL WHERE id = %s",
                (user_id,),
            )
            if cur.rowcount == 0:
                return error_response("not_found", 404)
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("activate_user: %s", e)
        return error_response("server", 500)
    finally:
        if conn:
            conn.close()
