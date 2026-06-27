"""
Teams blueprint — CRUD for teams, leader assignment, and member management
"""
import logging
import psycopg2.extras
from flask import Blueprint, request, jsonify, session
from app.database import get_conn
from app.auth import role_required

log = logging.getLogger(__name__)
teams_bp = Blueprint("teams", __name__, url_prefix="/api/teams")


def _team_dict(row):
    d = dict(row)
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    return d


# ─── List all teams with leader + member info ──────────────────────────────────

@teams_bp.route("", methods=["GET"])
@role_required("admin", "manager", "dataentry")
def list_teams():
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT t.id, t.name, t.description, t.created_at,
                           t.leader_id,
                           u.full_name AS leader_name, u.username AS leader_username,
                           u.avatar_url AS leader_avatar_url,
                           (SELECT COUNT(*) FROM users m
                            WHERE m.team_id = t.id AND m.role = 'sales' AND m.active = true
                           ) AS member_count,
                           (SELECT COALESCE(json_agg(json_build_object(
                               'id', m.id,
                               'full_name', m.full_name,
                               'username', m.username,
                               'avatar_url', m.avatar_url
                           ) ORDER BY m.full_name), '[]'::json)
                            FROM users m
                            WHERE m.team_id = t.id AND m.role = 'sales' AND m.active = true
                           ) AS members
                    FROM teams t
                    LEFT JOIN users u ON u.id = t.leader_id
                    ORDER BY t.name
                """)
                teams = [_team_dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return jsonify(teams)
    except Exception as e:
        log.error(f"list_teams: {e}")
        return jsonify({"error": str(e)}), 500


# ─── Get one team with full member list ───────────────────────────────────────

@teams_bp.route("/<int:team_id>", methods=["GET"])
@role_required("admin", "manager")
def get_team(team_id):
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT t.id, t.name, t.description, t.leader_id,
                           u.full_name AS leader_name, u.username AS leader_username,
                           u.avatar_url AS leader_avatar_url
                    FROM teams t
                    LEFT JOIN users u ON u.id = t.leader_id
                    WHERE t.id = %s
                """, (team_id,))
                team = cur.fetchone()
                if not team:
                    return jsonify({"error_code": "not_found", "error": "not_found"}), 404

                cur.execute("""
                    SELECT id, full_name, username, role, active, avatar_url, email, phone
                    FROM users
                    WHERE team_id = %s AND role = 'sales'
                    ORDER BY full_name
                """, (team_id,))
                members = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        result = _team_dict(team)
        result["members"] = members
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Create team ───────────────────────────────────────────────────────────────

@teams_bp.route("", methods=["POST"])
@role_required("admin")
def create_team():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    leader_id = data.get("leader_id") or None
    description = (data.get("description") or "").strip() or None

    if not name:
        return jsonify({"error_code": "campaign_name_required", "error": "required"}), 400

    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO teams (name, leader_id, description)
                    VALUES (%s, %s, %s) RETURNING id
                """, (name, leader_id, description))
                team_id = cur.fetchone()[0]

                if leader_id:
                    cur.execute("""
                        UPDATE users SET team_id = %s, updated_at = NOW()
                        WHERE id = %s AND role = 'team_leader'
                    """, (team_id, leader_id))

            conn.commit()
        finally:
            conn.close()
        log.info(f"✅ Team created: {name} (id={team_id})")
        return jsonify({"id": team_id}), 201
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error_code": "username_taken", "error": "name_taken"}), 409
        return jsonify({"error": str(e)}), 500


# ─── Update team name / leader ─────────────────────────────────────────────────

@teams_bp.route("/<int:team_id>", methods=["PUT"])
@role_required("admin")
def update_team(team_id):
    data = request.get_json() or {}
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT leader_id FROM teams WHERE id = %s", (team_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error_code": "not_found", "error": "not_found"}), 404
                old_leader = row[0]

                fields, params = [], []
                if "name" in data:
                    fields.append("name = %s"); params.append(data["name"].strip())
                if "description" in data:
                    fields.append("description = %s"); params.append(data["description"] or None)
                if "leader_id" in data:
                    new_leader = data["leader_id"] or None
                    fields.append("leader_id = %s"); params.append(new_leader)

                    # Clear old leader's team_id
                    if old_leader and old_leader != new_leader:
                        cur.execute("UPDATE users SET team_id = NULL WHERE id = %s", (old_leader,))

                    # Set new leader's team_id
                    if new_leader:
                        cur.execute("""
                            UPDATE users SET team_id = %s, updated_at = NOW()
                            WHERE id = %s AND role = 'team_leader'
                        """, (team_id, new_leader))

                if fields:
                    params.append(team_id)
                    cur.execute(f"UPDATE teams SET {', '.join(fields)} WHERE id = %s", params)

            conn.commit()
        finally:
            conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Replace all members of a team ────────────────────────────────────────────

@teams_bp.route("/<int:team_id>/members", methods=["PUT"])
@role_required("admin")
def set_members(team_id):
    data = request.get_json() or {}
    member_ids = data.get("member_ids") or []

    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM teams WHERE id = %s", (team_id,))
                if not cur.fetchone():
                    return jsonify({"error_code": "not_found", "error": "not_found"}), 404

                # Remove all current sales members
                cur.execute("""
                    UPDATE users SET team_id = NULL, updated_at = NOW()
                    WHERE team_id = %s AND role = 'sales'
                """, (team_id,))

                # Assign new members
                if member_ids:
                    cur.execute("""
                        UPDATE users SET team_id = %s, updated_at = NOW()
                        WHERE id = ANY(%s) AND role = 'sales'
                    """, (team_id, member_ids))

            conn.commit()
        finally:
            conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Delete team ───────────────────────────────────────────────────────────────

@teams_bp.route("/<int:team_id>", methods=["DELETE"])
@role_required("admin")
def delete_team(team_id):
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Clear members' team_id first
                cur.execute("UPDATE users SET team_id = NULL WHERE team_id = %s", (team_id,))
                cur.execute("DELETE FROM teams WHERE id = %s", (team_id,))
                if cur.rowcount == 0:
                    return jsonify({"error_code": "not_found", "error": "not_found"}), 404
            conn.commit()
        finally:
            conn.close()
        log.info(f"✅ Team {team_id} deleted")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
