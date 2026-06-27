"""
Demo-data seeder for Ain KPI.

Creates a richer demo dataset suitable for showing the platform off:
  - 1 Sales Manager
  - 3 Team Leaders (one per team)
  - 1 Data Entry user
  - 1 Marketing Manager
  - 20 Sales reps split across 3 teams with VARIED tenure — some reps
    have a full 12 months of history, others joined recently and only
    have a few months of data
  - 12 months of KPI entries (last full year) so charts have real shape
  - 3 marketing campaigns (mix of completed actuals + template state)

Replacement seeding: any existing sales user NOT in the new SALES_REPS
list is deleted first (with their KPI entries via ON DELETE CASCADE) so
re-running the seed produces a clean, balanced dataset instead of
piling on top of whatever was there before.

Idempotent for the kept rows: re-running upserts users / KPI rows.

Usage:
  # From project root — must have DATABASE_URL in env (or local DB config):
  python scripts/seed_demo.py

  # On Railway:
  railway run python scripts/seed_demo.py
"""
import os
import random
import sys
from datetime import datetime

# Make "app" importable when invoked as a one-off script.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.auth import hash_password
from app.database import get_conn
from app.kpi_logic import KPI_CONFIG, compute_score


DEMO_PASSWORD = "Demo1234!"

# Non-sales staff. Three team leaders match the three teams below.
USERS = [
    # username,         full_name,        role,          email
    ("omar.manager",    "Omar Hassan",    "manager",     "omar.manager@demo.ain"),
    ("ali.tl",          "Ali Ahmed",      "team_leader", "ali.tl@demo.ain"),
    ("sara.tl",         "Sara Ibrahim",   "team_leader", "sara.tl@demo.ain"),
    ("kareem.tl",       "Kareem Mansour", "team_leader", "kareem.tl@demo.ain"),
    ("menna",           "Menna Farouk",   "dataentry",   "menna@demo.ain"),
    ("nour.mkt",        "Nour Mahmoud",   "marketing",   "nour.mkt@demo.ain"),
]

TEAMS = [
    # team_name,    leader_username
    ("Team Alpha",  "ali.tl"),
    ("Team Beta",   "sara.tl"),
    ("Team Gamma",  "kareem.tl"),
]

# 20 sales reps, deliberately distributed so all 3 teams compete closely:
# each team has the same SHAPE of performance (one excellent anchor, two
# vgood, three good) plus or minus one medium rep to keep totals close.
# Tenure is varied PER team too — every team has long-tenured veterans
# AND recent hires — so historical trends look natural for everyone.
#
# Tuple format: (username, full_name, team_idx, join_months_ago, profile)
#
# `join_months_ago = 12` means the rep has KPI rows for the last 12
# months. `join_months_ago = 0` means they only have the current month.
SALES_REPS = [
    # ── Anchors: each team gets one full-year excellent. ────────────
    ("sales.ahmed",   "Ahmed Salah",   0, 12, "excellent"),  # Alpha
    ("sales.youssef", "Youssef Amr",   1, 12, "excellent"),  # Beta
    ("sales.tamer",   "Tamer Wael",    2, 12, "excellent"),  # Gamma

    # ── 11 months — first vgood per team. ───────────────────────────
    ("sales.laila",   "Laila Kamal",   0, 11, "vgood"),
    ("sales.khaled",  "Khaled Naguib", 1, 11, "vgood"),
    ("sales.salma",   "Salma Hassan",  2, 11, "vgood"),

    # ── 9–10 months — second vgood per team. ────────────────────────
    ("sales.heba",    "Heba Aly",      0, 10, "vgood"),
    ("sales.dina",    "Dina Magdy",    1, 10, "vgood"),
    ("sales.bassel",  "Bassel Ezz",    2,  9, "vgood"),

    # ── 7–8 months — first "good" per team. ─────────────────────────
    ("sales.mostafa", "Mostafa Tarek", 0,  8, "good"),
    ("sales.mariam",  "Mariam Hany",   1,  8, "good"),
    ("sales.reem",    "Reem Gamal",    2,  7, "good"),

    # ── 5–6 months — second "good" per team. ────────────────────────
    ("sales.karim",   "Karim Yasser",  0,  6, "good"),
    ("sales.omar",    "Omar Saeed",    1,  6, "good"),
    ("sales.tarek",   "Tarek Maged",   2,  5, "good"),

    # ── 3–4 months — third "good" (Alpha & Beta only). ──────────────
    ("sales.nada",    "Nada Wael",     0,  4, "good"),
    ("sales.farah",   "Farah Sherif",  1,  3, "good"),

    # ── 1–2 months — one medium per team to keep close ratios. ──────
    ("sales.hana",    "Hana Adel",     0,  2, "medium"),
    ("sales.amr",     "Amr Hisham",    1,  2, "medium"),
    ("sales.ziad",    "Ziad Nabil",    2,  1, "medium"),
]


def _months_back(n):
    """Return the last `n` calendar months in chronological order, latest last."""
    today = datetime.utcnow()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
    return list(reversed(out))


# ─── User / team seeding ─────────────────────────────────────────────────

def upsert_user(conn, username, full_name, role, email, password=DEMO_PASSWORD):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        row = cur.fetchone()
        if row:
            uid = row[0]
            cur.execute("""
                UPDATE users SET full_name=%s, role=%s, email=%s,
                    password_hash=%s, active=true, updated_at=NOW()
                WHERE id=%s
            """, (full_name, role, email, hash_password(password), uid))
            return uid
        cur.execute("""
            INSERT INTO users (username, full_name, role, email, password_hash, active)
            VALUES (%s, %s, %s, %s, %s, true)
            RETURNING id
        """, (username, full_name, role, email, hash_password(password)))
        return cur.fetchone()[0]


def upsert_team(conn, name, description, leader_id):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM teams WHERE name=%s", (name,))
        row = cur.fetchone()
        if row:
            tid = row[0]
            cur.execute("UPDATE teams SET description=%s, leader_id=%s WHERE id=%s",
                        (description, leader_id, tid))
            return tid
        cur.execute("""
            INSERT INTO teams (name, description, leader_id)
            VALUES (%s, %s, %s) RETURNING id
        """, (name, description, leader_id))
        return cur.fetchone()[0]


def attach_members(conn, team_id, user_ids):
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET team_id=%s, updated_at=NOW() WHERE id = ANY(%s)",
                    (team_id, user_ids))


def cleanup_stale_sales(conn, keep_usernames):
    """Delete any sales-role users not in `keep_usernames`.

    We're seeding a fresh demo dataset; old sales reps from previous runs
    would otherwise pollute the leaderboard with reps the user explicitly
    asked us to remove. ON DELETE CASCADE on kpi_entries.user_id and the
    other user-keyed tables means deleting the user removes their history
    too — exactly what we want here.
    """
    keep_lower = [u.lower() for u in keep_usernames]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, username FROM users
            WHERE role = 'sales' AND LOWER(username) <> ALL(%s)
            """,
            (keep_lower,),
        )
        stale = cur.fetchall()
        if not stale:
            return 0
        ids = [r[0] for r in stale]
        cur.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
        for _, uname in stale:
            print(f"   ✗ removed stale sales user: {uname}")
        return len(stale)


# ─── KPI seeding ─────────────────────────────────────────────────────────

# Performance profiles — drive how close each rep gets to target.
PROFILES = {
    "excellent": {"lo": 0.92, "hi": 1.05, "passfail_fail_chance": 0.02},
    "vgood":     {"lo": 0.80, "hi": 0.94, "passfail_fail_chance": 0.06},
    "good":      {"lo": 0.60, "hi": 0.82, "passfail_fail_chance": 0.12},
    "medium":    {"lo": 0.45, "hi": 0.65, "passfail_fail_chance": 0.22},
    "weak":      {"lo": 0.25, "hi": 0.50, "passfail_fail_chance": 0.35},
}

# Look up profile from the SALES_REPS table.
SALES_PROFILES = {row[0]: row[4] for row in SALES_REPS}


def seed_kpi_for_rep(conn, user_id, username, month, rng: random.Random):
    profile = PROFILES[SALES_PROFILES.get(username, "good")]
    lo, hi = profile["lo"], profile["hi"]

    fresh_leads = rng.randint(80, 160)

    # For each numeric KPI, aim for profile_factor × target
    numbers = {}
    for key, cfg in KPI_CONFIG.items():
        if cfg.get("input_type") == "passfail":
            numbers[key] = 0 if rng.random() < profile["passfail_fail_chance"] else 100
            continue

        factor = rng.uniform(lo, hi)
        tgt_type = cfg.get("target_type")
        if tgt_type == "fixed":
            target = cfg["target"]
        elif tgt_type == "leads_pct":
            target = fresh_leads * cfg["target_pct"]
        else:
            target = 100

        actual = target * factor
        if cfg.get("input_type") == "percent":
            actual = round(min(actual, 100.0), 1)
        else:
            actual = round(actual)
        numbers[key] = actual

    params = {
        "user_id": user_id,
        "month": month,
        "fresh_leads": fresh_leads,
        "calls":        int(numbers.get("calls", 0)),
        "meetings":     int(numbers.get("meetings", 0)),
        "crm_pct":      float(numbers.get("crm_pct", 0)),
        "deals":        int(numbers.get("deals", 0)),
        "reports":      int(numbers.get("reports", 0)),
        "reservations": int(numbers.get("reservations", 0)),
        "followup_pct": float(numbers.get("followup_pct", 0)),
        "attendance_pct": float(numbers.get("attendance_pct", 0)),
        "attitude":     int(numbers.get("attitude", 100)),
        "presentation": int(numbers.get("presentation", 100)),
        "behaviour":    int(numbers.get("behaviour", 100)),
        "appearance":   int(numbers.get("appearance", 100)),
        "hr_roles":     int(numbers.get("hr_roles", 100)),
    }

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO kpi_entries (user_id, month,
                fresh_leads, calls, meetings, crm_pct, deals,
                reports, reservations, followup_pct, attendance_pct,
                attitude, presentation, behaviour, appearance, hr_roles,
                sales_submitted_at, dataentry_submitted_at)
            VALUES (%(user_id)s, %(month)s,
                %(fresh_leads)s, %(calls)s, %(meetings)s, %(crm_pct)s, %(deals)s,
                %(reports)s, %(reservations)s, %(followup_pct)s, %(attendance_pct)s,
                %(attitude)s, %(presentation)s, %(behaviour)s, %(appearance)s, %(hr_roles)s,
                NOW(), NOW())
            ON CONFLICT (user_id, month) DO UPDATE SET
                fresh_leads = EXCLUDED.fresh_leads,
                calls = EXCLUDED.calls,
                meetings = EXCLUDED.meetings,
                crm_pct = EXCLUDED.crm_pct,
                deals = EXCLUDED.deals,
                reports = EXCLUDED.reports,
                reservations = EXCLUDED.reservations,
                followup_pct = EXCLUDED.followup_pct,
                attendance_pct = EXCLUDED.attendance_pct,
                attitude = EXCLUDED.attitude,
                presentation = EXCLUDED.presentation,
                behaviour = EXCLUDED.behaviour,
                appearance = EXCLUDED.appearance,
                hr_roles = EXCLUDED.hr_roles,
                sales_submitted_at = NOW(),
                dataentry_submitted_at = NOW(),
                updated_at = NOW()
            RETURNING id
        """, params)

    total, rating, _ = compute_score(params)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE kpi_entries SET total_score=%s, rating=%s, updated_at=NOW()
            WHERE user_id=%s AND month=%s
        """, (total, rating, user_id, month))


def seed_tl_manual_eval(conn, tl_id, month, rng: random.Random):
    """TL's own manual fields (what Sales Manager fills via /tl-evaluation)."""
    params = {
        "user_id": tl_id,
        "month": month,
        "crm_pct":          round(rng.uniform(80, 100), 1),
        "reports":          rng.randint(3, 5),
        "clients_pipeline": round(rng.uniform(60, 95), 1),
        "attitude":         100,
        "presentation":     100 if rng.random() > 0.1 else 0,
        "behaviour":        100,
        "appearance":       100,
        "attendance_pct":   100 if rng.random() > 0.1 else 0,
        "hr_roles":         100,
    }
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO kpi_entries (user_id, month,
                crm_pct, reports, clients_pipeline,
                attitude, presentation, behaviour, appearance, attendance_pct, hr_roles,
                dataentry_submitted_at)
            VALUES (%(user_id)s, %(month)s,
                %(crm_pct)s, %(reports)s, %(clients_pipeline)s,
                %(attitude)s, %(presentation)s, %(behaviour)s, %(appearance)s,
                %(attendance_pct)s, %(hr_roles)s,
                NOW())
            ON CONFLICT (user_id, month) DO UPDATE SET
                crm_pct          = EXCLUDED.crm_pct,
                reports          = EXCLUDED.reports,
                clients_pipeline = EXCLUDED.clients_pipeline,
                attitude         = EXCLUDED.attitude,
                presentation     = EXCLUDED.presentation,
                behaviour        = EXCLUDED.behaviour,
                appearance       = EXCLUDED.appearance,
                attendance_pct   = EXCLUDED.attendance_pct,
                hr_roles         = EXCLUDED.hr_roles,
                dataentry_submitted_at = NOW(),
                updated_at       = NOW()
        """, params)


# ─── Marketing campaigns ─────────────────────────────────────────────────

def upsert_campaign(conn, user_id, name, avg_price, comm_input, ctype, cr, budget,
                    actuals=None, month=None):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM marketing_campaigns WHERE campaign_name=%s", (name,))
        row = cur.fetchone()
        if row:
            cid = row[0]
            cur.execute("""
                UPDATE marketing_campaigns SET
                    avg_unit_price=%s, commission_input=%s, commission_type=%s,
                    expected_close_rate=%s, campaign_budget=%s, month=%s,
                    tax_rate=0.19, recommended_scenario='balanced',
                    updated_at=NOW()
                WHERE id=%s
            """, (avg_price, comm_input, ctype, cr, budget, month, cid))
        else:
            cur.execute("""
                INSERT INTO marketing_campaigns
                    (user_id, campaign_name, avg_unit_price, commission_input, commission_type,
                     tax_rate, expected_close_rate, campaign_budget, recommended_scenario, month)
                VALUES (%s, %s, %s, %s, %s, 0.19, %s, %s, 'balanced', %s)
                RETURNING id
            """, (user_id, name, avg_price, comm_input, ctype, cr, budget, month))
            cid = cur.fetchone()[0]

        if actuals is not None:
            cur.execute("""
                INSERT INTO marketing_actuals (campaign_id,
                    actual_spend, actual_leads, actual_qualified_leads,
                    actual_meetings, actual_follow_ups, actual_deals)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (campaign_id) DO UPDATE SET
                    actual_spend = EXCLUDED.actual_spend,
                    actual_leads = EXCLUDED.actual_leads,
                    actual_qualified_leads = EXCLUDED.actual_qualified_leads,
                    actual_meetings = EXCLUDED.actual_meetings,
                    actual_follow_ups = EXCLUDED.actual_follow_ups,
                    actual_deals = EXCLUDED.actual_deals,
                    updated_at = NOW()
            """, (cid,
                  actuals.get("spend", 0), actuals.get("leads", 0),
                  actuals.get("ql", 0), actuals.get("meetings", 0),
                  actuals.get("fu", 0), actuals.get("deals", 0)))
    return cid


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    rng = random.Random(42)  # stable seeds for reproducible demo
    months = _months_back(12)
    print(f"→ Seeding {len(months)} months: {months[0]} → {months[-1]}")

    conn = get_conn()
    conn.autocommit = False

    try:
        # 0. Cleanup — drop any sales user not in the new roster so the
        #    leaderboard isn't polluted by leftovers from older seeds.
        new_sales_usernames = [r[0] for r in SALES_REPS]
        removed = cleanup_stale_sales(conn, new_sales_usernames)
        if removed:
            print(f"   ✓ removed {removed} stale sales user(s) (with their KPI history)")

        # 1. Non-sales users (manager, team leaders, dataentry, marketing).
        user_ids = {}
        for username, full_name, role, email in USERS:
            uid = upsert_user(conn, username, full_name, role, email)
            user_ids[username] = uid
            print(f"   ✓ user {username:<16} → id {uid}")

        # 2. Sales reps (all 20).
        for username, full_name, _, _, _ in SALES_REPS:
            uid = upsert_user(conn, username, full_name, "sales",
                              f"{username}@demo.ain")
            user_ids[username] = uid
        print(f"   ✓ {len(SALES_REPS)} sales reps upserted")

        # 3. Teams + membership.
        team_ids = {}
        for team_name, leader_username in TEAMS:
            leader_id = user_ids[leader_username]
            tid = upsert_team(conn, team_name, f"{team_name} demo team", leader_id)
            team_ids[team_name] = tid
            # Sales reps belonging to this team
            members = [r[0] for r in SALES_REPS
                       if TEAMS[r[2]][0] == team_name]
            member_ids = [user_ids[u] for u in members]
            attach_members(conn, tid, member_ids)
            print(f"   ✓ team {team_name} (leader={leader_username}, members={len(member_ids)})")

        conn.commit()

        # 4. KPI entries — each rep gets the months they were active for.
        total_kpi_rows = 0
        for username, _, _, join_months_ago, _ in SALES_REPS:
            uid = user_ids[username]
            # join_months_ago = 12 → last 12 months; = 0 → last 1 month only
            n = max(1, int(join_months_ago) + 1)
            rep_months = months[-n:]
            for m in rep_months:
                seed_kpi_for_rep(conn, uid, username, m, rng)
                total_kpi_rows += 1
        print(f"   ✓ KPI entries: {total_kpi_rows} rows across {len(SALES_REPS)} reps × varied tenure")

        # 5. TL manual evaluations × 12 months.
        for _, leader_username in TEAMS:
            tl_id = user_ids[leader_username]
            for m in months:
                seed_tl_manual_eval(conn, tl_id, m, rng)
        print(f"   ✓ TL manual evaluations × {len(months)} months × {len(TEAMS)} TLs")

        conn.commit()

        # 6. Marketing campaigns
        mk_uid = user_ids["nour.mkt"]
        current_month = months[-1]
        prev_month = months[-3] if len(months) >= 3 else months[0]

        upsert_campaign(
            conn, mk_uid,
            name="North Coast Summer 2026",
            avg_price=8_500_000, comm_input=4.5, ctype="percentage",
            cr=0.02, budget=450_000, month=current_month,
            actuals={"spend": 380_000, "leads": 2100, "ql": 520,
                     "meetings": 140, "fu": 1600, "deals": 42},
        )
        upsert_campaign(
            conn, mk_uid,
            name="New Cairo Launch",
            avg_price=6_200_000, comm_input=3.5, ctype="percentage",
            cr=0.025, budget=300_000, month=current_month,
            actuals={"spend": 310_000, "leads": 1650, "ql": 380,
                     "meetings": 95, "fu": 1100, "deals": 28},
        )
        upsert_campaign(
            conn, mk_uid,
            name="Sahel Premium",
            avg_price=12_000_000, comm_input=5.0, ctype="percentage",
            cr=0.015, budget=600_000, month=prev_month,
            actuals=None,
        )
        print("   ✓ 3 marketing campaigns (2 with actuals, 1 template)")

        conn.commit()

        print("\n✅ Demo data seeded.\n")
        _print_credentials_table()

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Seed failed: {e}")
        raise
    finally:
        conn.close()


def _print_credentials_table():
    print("=" * 78)
    print("DEMO CREDENTIALS")
    print("=" * 78)
    print(f"{'Username':<22} {'Password':<14} {'Role':<14} {'Full Name'}")
    print("-" * 78)
    for username, full_name, role, _ in USERS:
        print(f"{username:<22} {DEMO_PASSWORD:<14} {role:<14} {full_name}")
    for username, full_name, _, _, _ in SALES_REPS:
        print(f"{username:<22} {DEMO_PASSWORD:<14} {'sales':<14} {full_name}")
    print("=" * 78)
    print()
    print("Login at: /login")
    print("Admin account was created on first deploy — keep using that for admin access.")
    print()


if __name__ == "__main__":
    main()
