"""
Utility endpoints for the frontend.

Currently:
  GET /api/util/today  → server's "today" so the date-range picker doesn't
                         drift from the server's clock when shared deep-links
                         span timezones.
"""
import logging
from datetime import date

from flask import Blueprint, Response, json, jsonify

from app.auth import login_required, role_required
from app.mailer import mailer_is_configured

log = logging.getLogger(__name__)
util_bp = Blueprint("util", __name__, url_prefix="/api/util")


@util_bp.route("/today", methods=["GET"])
@login_required
def today():
    """
    Returns the server's current local date and the timezone it's interpreted
    in. The frontend uses this for the date-range picker so "Today" / "This
    Week" presets agree with the server even when the user's clock is off
    or when the link was shared across timezones.

    All date math elsewhere in the app uses Africa/Cairo (the deployment
    locale). TIMESTAMPTZ migration is a separate phase — see CLAUDE.md.

    Cache-Control max-age=60: a fresh `today` once per minute is plenty;
    the picker's first paint hits this once per page load.
    """
    payload = {
        "today": date.today().isoformat(),
        "tz": "Africa/Cairo",
    }
    resp = Response(json.dumps(payload), mimetype="application/json")
    resp.headers["Cache-Control"] = "max-age=60"
    return resp


@util_bp.route("/mailer-status", methods=["GET"])
@role_required("admin", "manager")
def mailer_status():
    """Tells the admin UI whether outbound email is configured. Used to
    surface a persistent banner on /admin so the operator notices that
    password-reset and approval emails will silently no-op until SMTP /
    Resend env vars are set."""
    return jsonify({"configured": mailer_is_configured()})
