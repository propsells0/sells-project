"""
Pages blueprint: serves HTML pages with role-based routing
"""
from flask import Blueprint, abort, render_template, redirect, session
from app.auth import login_required, role_required, role_home, current_user
from app.database import get_conn

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/")
def home():
    if "user_id" in session:
        return redirect(role_home(session["role"]))
    return redirect("/login")


@pages_bp.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(role_home(session["role"]))
    return render_template("login.html")


@pages_bp.route("/register")
def register_page():
    if "user_id" in session:
        return redirect(role_home(session["role"]))
    return render_template("register.html")


@pages_bp.route("/forgot-password")
def forgot_password_page():
    return render_template("forgot_password.html")


@pages_bp.route("/reset-password")
def reset_password_page():
    return render_template("reset_password.html")


@pages_bp.route("/sales")
@login_required
def sales_page():
    # Sales role no longer has a KPI self-entry page; they only browse PropFinder.
    return redirect("/propfinder")


@pages_bp.route("/evaluation")
@role_required("dataentry", "manager", "admin")
def evaluation_page():
    return render_template("evaluation.html", user=current_user())


@pages_bp.route("/data-entry")
@role_required("dataentry", "manager", "admin")
def dataentry_page():
    return redirect("/evaluation")


@pages_bp.route("/dashboard")
@role_required("manager", "dataentry", "admin")
def dashboard_page():
    return render_template("dashboard.html", user=current_user())


@pages_bp.route("/finance")
@role_required("admin", "manager")
def finance_page():
    return render_template("finance.html", user=current_user())


@pages_bp.route("/admin")
@role_required("admin", "manager", "dataentry")
def admin_page():
    # manager/dataentry get the same UI but the backend enforces role
    # hierarchy on every CRUD action (see app/auth.py:can_create_role).
    return render_template("admin.html", user=current_user())


@pages_bp.route("/admin/crm-settings")
@role_required("admin")
def admin_crm_settings_page():
    """Stage + sales-rep mappings CRUD. Admin-only — manager can't add
    mappings because doing so triggers retroactive recalc that rewrites
    historical KPIs and that decision belongs with the system owner."""
    return render_template("admin_crm_settings.html", user=current_user())


@pages_bp.route("/profile")
@login_required
def profile_page():
    return render_template("profile.html", user=current_user())


@pages_bp.route("/marketing")
@role_required("marketing", "manager", "admin")
def marketing_page():
    return render_template("marketing.html", user=current_user())


@pages_bp.route("/crm-reports")
@role_required("marketing", "manager", "admin")
def crm_reports_page():
    return render_template("crm_reports.html", user=current_user())


@pages_bp.route("/marketing/intervention")
@role_required("marketing", "manager", "admin")
def marketing_intervention_inbox():
    """Cross-campaign manager-intervention inbox. The page renders an
    empty shell; the JS bootstraps from /api/crm/intervention so the same
    server template covers all states (zero flags, filtered, etc.)."""
    return render_template("marketing_intervention.html", user=current_user())


@pages_bp.route("/marketing/campaigns/<int:campaign_id>/leads/<int:lead_id>")
@role_required("marketing", "manager", "admin")
def marketing_lead_timeline(campaign_id, lead_id):
    """Per-lead timeline page. We don't verify the lead↔campaign join
    here — the JS hits /api/crm/leads/<id>/timeline which returns the
    campaign_id alongside the lead, and the page uses that to wire the
    back-link. Saves us a Postgres round-trip on the route entry."""
    return render_template(
        "marketing_lead_timeline.html",
        user=current_user(),
        campaign_id=campaign_id,
        lead_id=lead_id,
    )


@pages_bp.route("/marketing/campaigns/<int:campaign_id>")
@role_required("marketing", "manager", "admin")
def marketing_campaign_detail(campaign_id):
    """Per-campaign CRM page. We 404 here (not from the API) so a bookmarked
    URL to a deleted campaign gets a clean redirect to /marketing instead
    of an "Invalid token" page.
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, campaign_name FROM marketing_campaigns WHERE id = %s",
                (campaign_id,),
            )
            row = cur.fetchone()
    finally:
        if conn is not None:
            conn.close()
    if not row:
        abort(404)
    campaign = {"id": row[0], "name": row[1]}
    return render_template(
        "marketing_campaign.html",
        user=current_user(),
        campaign=campaign,
    )


@pages_bp.route("/teams")
@role_required("admin")
def teams_page():
    return render_template("teams.html", user=current_user())


@pages_bp.route("/team-leader")
@role_required("team_leader")
def team_leader_page():
    # Manager/admin see TL data via /tl-evaluation; this page is the TL's own KPI view.
    # @role_required always lets admin through, so admin retains access for support.
    return render_template("team_leader.html", user=current_user())


@pages_bp.route("/tl-evaluation")
@role_required("manager", "admin")
def tl_evaluation_page():
    return redirect("/evaluation")


@pages_bp.route("/propfinder")
@login_required
def propfinder_page():
    return render_template("propfinder.html", user=current_user())
