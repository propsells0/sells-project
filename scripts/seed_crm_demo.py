"""
CRM Demo Data Seeder — seeds leads + events for the first campaign
so that Sales Performance and Daily Activity tabs show real charts.

What it creates:
  - 1 fake crm_report_uploads row (so "Last Upload" shows in Overview)
  - 50 demo leads spread across 4-5 sales reps
  - ~300 lead_events over the last 30 days with mixed stages
  - Calls recalc_after_upload() → rebuilds lead_assignments, sales_kpis,
    campaign_kpis, manager_intervention in one shot

Safe to re-run: clears only demo leads/events for the target campaign
before re-inserting (idempotent).

Usage:
  python scripts/seed_crm_demo.py                  # uses first campaign found
  python scripts/seed_crm_demo.py --campaign-id 3  # target specific campaign

Railway:
  railway run python scripts/seed_crm_demo.py
"""

import hashlib
import os
import random
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("DISABLE_SYNC", "true")

from app.database import get_conn
from app.crm_logic import recalc_after_upload

# ── Configurable knobs ───────────────────────────────────────────────
LEADS_PER_REP   = 12     # fresh leads per sales rep
ROTATION_ODDS   = 0.20   # 20 % of leads get rotated to a second rep
EVENTS_PER_LEAD = (1, 4) # random range of events per lead
DAYS_BACK       = 30     # spread events over last N days
# ─────────────────────────────────────────────────────────────────────

STAGES = [
    "NO_ANSWER",    # most common
    "NO_ANSWER",
    "NO_ANSWER",
    "FOLLOWING",
    "FOLLOWING",
    "MEETING",
    "INTERESTED",
    "CANCELLATION",
]

ARABIC_NAMES = [
    "محمد علي", "أحمد حسن", "سارة محمود", "منى إبراهيم", "خالد عبدالله",
    "ريم فاروق", "عمر طارق", "نور الدين", "ياسمين حامد", "أسامة رشاد",
    "هالة سليم", "كريم الجوهري", "دينا مصطفى", "وليد صابر", "إسراء نصر",
    "طارق عبدالحميد", "مريم شوقي", "أيمن البدوي", "رانيا الشافعي", "حسام جابر",
    "نادية فوزي", "بلال منصور", "لمياء حسين", "شريف الغزالي", "هبة يوسف",
    "عادل سعيد", "أميرة القاضي", "زياد الحلبي", "تامر العتيق", "فاطمة كمال",
    "إبراهيم صالح", "نرمين العبد", "مصطفى الرفاعي", "ولاء حمدي", "أنور السيد",
    "شيماء حجازي", "لؤي نبيل", "مايسة درويش", "جمال شحاتة", "سمر الشرقاوي",
    "باسم عوض", "إيناس الكيلاني", "يوسف البنا", "صفاء قاسم", "حمدي الديب",
    "سوسن بكر", "ربيع عمار", "هند الحجار", "ماهر رضا", "عزة الشرقاوي",
]

def _random_mobile(seed: str) -> str:
    """Deterministic mobile from seed so re-runs produce same lead mobiles."""
    h = hashlib.md5(seed.encode()).hexdigest()
    return "010" + str(int(h[:8], 16))[-8:]

def _event_hash(campaign_id: int, lead_mobile: str, rep_id: int,
                stage: str, when: datetime) -> str:
    raw = f"demo|{campaign_id}|{lead_mobile}|{rep_id}|{stage}|{when.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()

def run(campaign_id_override: int | None = None):
    conn = get_conn()

    # ── 1. Resolve campaign ───────────────────────────────────────────
    with conn.cursor() as cur:
        if campaign_id_override:
            cur.execute(
                "SELECT id, name FROM marketing_campaigns WHERE id = %s",
                (campaign_id_override,),
            )
        else:
            cur.execute(
                "SELECT id, name FROM marketing_campaigns ORDER BY id LIMIT 1"
            )
        row = cur.fetchone()

    if not row:
        print("❌  No marketing campaign found. Create one in the Marketing page first.")
        return
    campaign_id, campaign_name = row
    print(f"🎯  Target campaign: [{campaign_id}] {campaign_name}")

    # ── 2. Find sales users ───────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, full_name FROM users
            WHERE role = 'sales' AND active = TRUE
            ORDER BY id
            LIMIT 6
            """
        )
        sales_reps = cur.fetchall()  # list of (id, full_name)

    if not sales_reps:
        print("❌  No active sales users found. Run seed_demo.py first (creates 20 reps).")
        return
    print(f"👥  Using {len(sales_reps)} sales reps: {[r[1] for r in sales_reps]}")

    # ── 3. Clear old demo data for this campaign ──────────────────────
    with conn.cursor() as cur:
        # Delete events first (FK), then leads
        cur.execute(
            """
            DELETE FROM lead_events
            WHERE campaign_id = %s
              AND raw_sales_rep_name LIKE 'DEMO|%'
            """,
            (campaign_id,),
        )
        deleted_events = cur.rowcount
        cur.execute(
            """
            DELETE FROM leads
            WHERE campaign_id = %s
              AND client_name LIKE 'DEMO|%'
            """,
            (campaign_id,),
        )
        deleted_leads = cur.rowcount
    conn.commit()
    print(f"🧹  Cleared {deleted_leads} old demo leads, {deleted_events} old demo events")

    # ── 4. Create a fake upload row ───────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crm_report_uploads
                (campaign_id, file_name, status, total_rows, total_leads,
                 total_events, new_events, duplicate_events, processed_at)
            VALUES (%s, 'demo_data.xlsx', 'COMPLETED', %s, %s, %s, %s, 0, NOW())
            RETURNING id
            """,
            (
                campaign_id,
                len(sales_reps) * LEADS_PER_REP,
                len(sales_reps) * LEADS_PER_REP,
                len(sales_reps) * LEADS_PER_REP * 2,
                len(sales_reps) * LEADS_PER_REP * 2,
            ),
        )
        upload_id = cur.fetchone()[0]
    conn.commit()
    print(f"📤  Created fake upload row id={upload_id}")

    # ── 5. Build leads + events ───────────────────────────────────────
    today     = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = today - timedelta(days=DAYS_BACK - 1)

    name_pool = list(ARABIC_NAMES)
    random.shuffle(name_pool)
    name_idx  = 0

    total_leads_created  = 0
    total_events_created = 0

    for rep_id, rep_name in sales_reps:
        for lead_seq in range(LEADS_PER_REP):
            # Pick a unique client name
            client_name = f"DEMO|{name_pool[name_idx % len(name_pool)]}"
            name_idx += 1
            mobile = _random_mobile(f"{campaign_id}:{rep_id}:{lead_seq}")

            # Insert lead (ON CONFLICT skip — idempotent on mobile)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO leads (campaign_id, client_name, mobile)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (campaign_id, mobile) DO UPDATE
                        SET client_name = EXCLUDED.client_name,
                            updated_at  = NOW()
                    RETURNING id
                    """,
                    (campaign_id, client_name, mobile),
                )
                lead_id = cur.fetchone()[0]
            total_leads_created += 1

            # Generate 1-4 events for this lead with this rep (fresh window)
            n_events = random.randint(*EVENTS_PER_LEAD)
            for ev_idx in range(n_events):
                stage = random.choice(STAGES)
                # Spread events across the date range
                ev_day_offset = random.randint(0, DAYS_BACK - 1)
                ev_time = day_start + timedelta(
                    days=ev_day_offset,
                    hours=random.randint(8, 18),
                    minutes=random.randint(0, 59),
                )
                ev_hash = _event_hash(campaign_id, mobile, rep_id, stage, ev_time)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO lead_events
                            (lead_id, campaign_id, sales_user_id,
                             raw_sales_rep_name, raw_stage, normalized_stage,
                             follow_date, source_upload_id, source_row_number,
                             event_hash)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (event_hash) DO NOTHING
                        """,
                        (
                            lead_id, campaign_id, rep_id,
                            f"DEMO|{rep_name}", stage, stage,
                            ev_time, upload_id, total_events_created + 1,
                            ev_hash,
                        ),
                    )
                    total_events_created += cur.rowcount

            # Optionally rotate to a different rep
            if random.random() < ROTATION_ODDS and len(sales_reps) > 1:
                other_reps = [r for r in sales_reps if r[0] != rep_id]
                rot_rep_id, rot_rep_name = random.choice(other_reps)
                # 1-2 rotation events, later in time
                for rev_idx in range(random.randint(1, 2)):
                    stage = random.choice(STAGES)
                    ev_day_offset = random.randint(DAYS_BACK // 2, DAYS_BACK - 1)
                    ev_time = day_start + timedelta(
                        days=ev_day_offset,
                        hours=random.randint(8, 18),
                        minutes=random.randint(0, 59),
                    )
                    ev_hash = _event_hash(campaign_id, mobile, rot_rep_id, stage, ev_time)
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO lead_events
                                (lead_id, campaign_id, sales_user_id,
                                 raw_sales_rep_name, raw_stage, normalized_stage,
                                 follow_date, source_upload_id, source_row_number,
                                 event_hash)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (event_hash) DO NOTHING
                            """,
                            (
                                lead_id, campaign_id, rot_rep_id,
                                f"DEMO|{rot_rep_name}", stage, stage,
                                ev_time, upload_id, total_events_created + 1,
                                ev_hash,
                            ),
                        )
                        total_events_created += cur.rowcount

        conn.commit()
        print(f"  ✅  {rep_name}: {LEADS_PER_REP} leads seeded")

    print(f"\n📊  Totals: {total_leads_created} leads · {total_events_created} events")

    # ── 6. Run full recalc pipeline ───────────────────────────────────
    print("🔄  Running recalc pipeline (assignments → campaign_kpis → sales_kpis → intervention)…")
    result = recalc_after_upload(campaign_id, conn)
    conn.commit()
    print(f"✅  Recalc done: {result}")
    print(f"\n🎉  Done! Open /marketing/campaigns/{campaign_id} and check:")
    print(     "     • Overview     — KPI tiles + stage donut should show data")
    print(     "     • Sales Perf   — stacked bars per rep (fresh vs rotation)")
    print(     "     • Daily Activ  — line charts over last 30 days")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign-id", type=int, default=None,
                    help="ID of the campaign to seed (default: first campaign)")
    args = ap.parse_args()
    run(args.campaign_id)
