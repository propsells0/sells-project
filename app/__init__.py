"""
Flask application factory
"""
import logging
import os
from flask import Flask, request, session
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


def create_app():
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates"
    )
    app.config.from_object(Config)

    from app.database import init_all_tables
    init_all_tables()

    # Register blueprints
    from app.blueprints.auth_bp import auth_bp
    from app.blueprints.users_bp import users_bp
    from app.blueprints.kpi_bp import kpi_bp
    from app.blueprints.pages_bp import pages_bp
    from app.blueprints.propfinder_bp import propfinder_bp
    from app.blueprints.finance_bp import finance_bp
    from app.blueprints.teams_bp import teams_bp
    from app.blueprints.marketing_bp import marketing_bp
    from app.blueprints.util_bp import util_bp
    from app.blueprints.crm_bp import crm_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(kpi_bp)
    app.register_blueprint(propfinder_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(teams_bp)
    app.register_blueprint(marketing_bp)
    app.register_blueprint(util_bp)
    app.register_blueprint(crm_bp)

    # Error handlers — API paths get structured JSON, browser gets a friendly page
    @app.errorhandler(404)
    def not_found(e):
        from flask import request, jsonify, redirect
        if request.path.startswith("/api/"):
            return jsonify({"error_code": "not_found", "error": "not_found"}), 404
        return redirect("/")

    @app.errorhandler(500)
    def server_error(e):
        log.error(f"500 error: {e}")
        from flask import request, jsonify
        if request.path.startswith("/api/"):
            return jsonify({"error_code": "server", "error": "server"}), 500
        return "Server error. Please try again later.", 500

    @app.errorhandler(405)
    def method_not_allowed(e):
        from flask import request, jsonify
        if request.path.startswith("/api/"):
            return jsonify({"error_code": "forbidden", "error": "method_not_allowed"}), 405
        return "Method not allowed", 405

    # Track last-seen for the Online/Offline status. Throttled at the SQL
    # level via WHERE last_seen < NOW() - INTERVAL '30s' so concurrent
    # requests don't pile up writes. Skips static assets, the /api/auth/me
    # poll, and the login endpoints — these would either hammer the row or
    # fire before a user_id is set.
    _LAST_SEEN_SKIP = {"/api/auth/me", "/api/auth/login", "/api/auth/logout", "/api/auth/csrf"}

    @app.before_request
    def _touch_last_seen():
        if request.endpoint == "static":
            return
        if request.path in _LAST_SEEN_SKIP:
            return
        uid = session.get("user_id")
        if not uid:
            return
        try:
            from app.database import get_conn
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET last_seen = NOW() "
                        "WHERE id = %s AND (last_seen IS NULL OR last_seen < NOW() - INTERVAL '30 seconds')",
                        (uid,),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            # Never fail a request because heartbeat couldn't write — the
            # admin status display is non-critical.
            log.debug("last_seen update failed for uid=%s: %s", uid, e)

    # Security-hardening response headers + freshness guarantees on API
    # responses. Without an explicit Cache-Control, browsers can apply a
    # heuristic cache (typically a fraction of the resource's age) to GET
    # responses — the symptom is "I clicked filter and the data didn't
    # update from the database." `no-store` on /api/* forces every call
    # to round-trip; static assets keep their default caching.
    @app.after_request
    def _sec_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp

    log.info("✅ Flask app ready — all blueprints registered")
    return app
