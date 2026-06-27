"""
Master V API sync service
Pulls real estate units and stores them in the `units` table.
"""
import os
import json
import time
import logging
import threading
import requests
import schedule
import psycopg2.extras
from datetime import datetime
from typing import List, Dict, Optional

from config import Config
from app.database import get_conn

log = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {Config.MASTER_V_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0"
}

PLACES = {
    "New Cairo": 1, "New Capital": 2, "Al-Mostakbal": 3, "Al-Shorouk": 4,
    "6th October": 5, "North Coast": 6, "Ain Sokhna": 7,
}

TRACKED_FIELDS = [
    "total_price_egp", "total_price_to_egp", "cash_price_from_egp",
    "cash_price_to_egp", "price_per_sqm_egp", "status",
    "payment_plan", "delivery_from_months", "delivery_to_months",
    "maintenance", "club_fees", "parking_fees", "finishing_type",
]

sync_status = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "error": None,
}


def fetch_filters(city_id: int) -> Dict:
    try:
        r = requests.get(f"{Config.MASTER_V_URL}/data/filter", headers=HEADERS,
                         params={"SectionId": 1, "CityId": city_id}, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if not data.get("error"):
                return data.get("data", {})
    except Exception as e:
        log.error(f"fetch_filters error: {e}")
    return {}


def find_developer(compound_id: int, developers: List[Dict], city_id: int) -> Optional[int]:
    start = time.time()
    for dev in developers:
        if time.time() - start > 5:
            return None
        dev_id = dev.get("value")
        try:
            r = requests.get(f"{Config.MASTER_V_URL}/data", headers=HEADERS,
                             params={"CompoundId": compound_id, "DeveloperId": dev_id,
                                     "SectionId": 1, "CityId": city_id, "Currency": 1, "ViewAll": "true"},
                             timeout=5)
            if r.status_code == 200:
                data = r.json()
                if not data.get("error") and data.get("data"):
                    if len(data["data"].get("results", [])) > 0:
                        return dev_id
        except:
            continue
    return None


def fetch_compound_details(compound_id: int, developer_id: int, city_id: int) -> Dict:
    try:
        r = requests.get(f"{Config.MASTER_V_URL}/data", headers=HEADERS,
                         params={"CompoundId": compound_id, "DeveloperId": developer_id,
                                 "SectionId": 1, "CityId": city_id, "Currency": 1, "ViewAll": "true"},
                         timeout=30)
        if r.status_code == 200:
            data = r.json()
            if not data.get("error") and data.get("data"):
                results = data["data"].get("results", [])
                if results:
                    return results[0]
    except Exception as e:
        log.error(f"fetch_details error: {e}")
    return {}


def flatten_compound(compound_info: Dict, compound_data: Dict, city_name: str) -> List[Dict]:
    rows = []
    now = datetime.now()
    # Master V returns a list of plans per compound — keep all of them as
    # JSONB so the UI can render a dropdown of every option, and keep the
    # legacy `payment_plan` text (first plan) for backwards compatibility
    # with any consumer reading it directly.
    raw_plans = compound_data.get("DataPayPlans", []) or []
    plans_list = []
    for plan in raw_plans:
        dp = (plan.get("PayPlanDownPayment", 0) or 0) * 100
        inst = plan.get("PayPlanInstalment", 0) or 0
        plans_list.append({
            "down_pct": round(dp, 2),
            "months": int(inst) if inst else 0,
            "label": f"{dp}% down, {inst} months",
        })
    payment_plan_text = plans_list[0]["label"] if plans_list else ""
    payment_plans_json = json.dumps(plans_list) if plans_list else None
    finishing_info = compound_data.get("DataFinishing", {})
    unit_details = compound_data.get("DataDetails", {})
    for unit_type, units in unit_details.items():
        for unit in units:
            built_up = unit.get("DetailBuiltUpArea")
            price = unit.get("DetailUnitTotalPrice")
            rows.append({
                "city_name": city_name,
                "compound_name": compound_info["name"],
                "compound_id": compound_info["id"],
                "developer_name": compound_info["developer_name"],
                "developer_id": compound_info["developer_id"],
                "phase_name": compound_data.get("DataPhas"),
                "phase_id": compound_data.get("DataPhasId"),
                "unit_type": unit_type,
                "bedrooms": unit.get("DetailBedRooms"),
                "built_up_area_sqm": built_up,
                "total_price_egp": price,
                "price_per_sqm_egp": round(price / built_up, 2) if price and built_up else None,
                "cash_price_from_egp": unit.get("DetailUnitTotalCashFrom"),
                "delivery_from_months": compound_data.get("DataPhasDeliveryFrom"),
                "delivery_to_months": compound_data.get("DataPhasDeliveryTo"),
                "payment_plan": payment_plan_text,
                "payment_plans": payment_plans_json,
                "maintenance": compound_data.get("DataPhasMaintenance"),
                "club_fees": compound_data.get("DataPhasClubFees"),
                "parking_fees": compound_data.get("DataPhasParkingFees"),
                "finishing_type": finishing_info.get(unit_type, "N/A"),
                "cash_discount_percent": compound_data.get("DataPhasCashDiscount"),
                "cash_price_to_egp": unit.get("DetailUnitTotalCashTo"),
                "city_id": compound_data.get("DataCityId"),
                "detail_id": unit.get("DetailId"),
                "outdoor_area": unit.get("DetailOutdoor"),
                "status": compound_data.get("DataStatus"),
                "sub_type": unit.get("DetailSubType"),
                "total_price_to_egp": unit.get("DetailUnitTotalPriceTo"),
                "type_id": unit.get("DetailTypeId"),
                "last_seen": now, "first_seen": now,
                "is_sold": False, "sold_at": None,
            })
    return rows


def ensure_columns(conn):
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE units
                ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP,
                ADD COLUMN IF NOT EXISTS first_seen TIMESTAMP,
                ADD COLUMN IF NOT EXISTS is_sold BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS sold_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS payment_plans JSONB;
        """)
    conn.commit()


def sync_units(conn, fresh_units: List[Dict], existing: Dict[int, Dict]):
    now = datetime.now()
    new_count = updated_count = sold_count = 0
    fresh_ids = {u["detail_id"] for u in fresh_units if u.get("detail_id")}
    # Only mark missing units as sold when the API returned ≥70% of what we
    # already had — a low threshold caused a regression where partial API
    # responses gradually flagged the entire catalogue as sold over multiple
    # runs. 70% is conservative enough to skip sold-marking during outages
    # while still catching genuine catalogue churn.
    safe_to_mark_sold = len(fresh_ids) >= max(1, int(len(existing) * 0.70))

    with conn.cursor() as cur:
        for unit in fresh_units:
            did = unit.get("detail_id")
            if not did:
                continue
            if did not in existing:
                cur.execute("""
                    INSERT INTO units (
                        city_name, compound_name, compound_id, developer_name, developer_id,
                        phase_name, phase_id, unit_type, bedrooms, built_up_area_sqm,
                        total_price_egp, price_per_sqm_egp, cash_price_from_egp,
                        delivery_from_months, delivery_to_months, payment_plan, payment_plans,
                        maintenance, club_fees, parking_fees, finishing_type,
                        cash_discount_percent, cash_price_to_egp, city_id, detail_id,
                        outdoor_area, status, sub_type, total_price_to_egp, type_id,
                        last_seen, first_seen, is_sold, sold_at
                    ) VALUES (
                        %(city_name)s, %(compound_name)s, %(compound_id)s, %(developer_name)s, %(developer_id)s,
                        %(phase_name)s, %(phase_id)s, %(unit_type)s, %(bedrooms)s, %(built_up_area_sqm)s,
                        %(total_price_egp)s, %(price_per_sqm_egp)s, %(cash_price_from_egp)s,
                        %(delivery_from_months)s, %(delivery_to_months)s, %(payment_plan)s, %(payment_plans)s,
                        %(maintenance)s, %(club_fees)s, %(parking_fees)s, %(finishing_type)s,
                        %(cash_discount_percent)s, %(cash_price_to_egp)s, %(city_id)s, %(detail_id)s,
                        %(outdoor_area)s, %(status)s, %(sub_type)s, %(total_price_to_egp)s, %(type_id)s,
                        %(last_seen)s, %(first_seen)s, %(is_sold)s, %(sold_at)s
                    )
                """, unit)
                new_count += 1
            else:
                old = existing[did]
                changed = any(str(unit.get(f)) != str(old.get(f)) for f in TRACKED_FIELDS)
                if changed:
                    cur.execute("""
                        UPDATE units SET
                            total_price_egp = %(total_price_egp)s,
                            total_price_to_egp = %(total_price_to_egp)s,
                            cash_price_from_egp = %(cash_price_from_egp)s,
                            cash_price_to_egp = %(cash_price_to_egp)s,
                            price_per_sqm_egp = %(price_per_sqm_egp)s,
                            status = %(status)s,
                            payment_plan = %(payment_plan)s,
                            payment_plans = %(payment_plans)s,
                            delivery_from_months = %(delivery_from_months)s,
                            delivery_to_months = %(delivery_to_months)s,
                            maintenance = %(maintenance)s,
                            club_fees = %(club_fees)s,
                            parking_fees = %(parking_fees)s,
                            finishing_type = %(finishing_type)s,
                            last_seen = %(last_seen)s,
                            is_sold = FALSE,
                            sold_at = NULL
                        WHERE detail_id = %(detail_id)s
                    """, {**unit, "last_seen": now})
                    updated_count += 1
                else:
                    # Unchanged unit re-appeared — refresh last_seen AND clear
                    # any stale is_sold flag (a previous partial sync may have
                    # incorrectly marked it sold).
                    cur.execute(
                        "UPDATE units SET last_seen = %s, is_sold = FALSE, sold_at = NULL "
                        "WHERE detail_id = %s",
                        (now, did),
                    )

        if safe_to_mark_sold:
            for did in set(existing.keys()) - fresh_ids:
                if not existing[did].get("is_sold"):
                    cur.execute("UPDATE units SET is_sold = TRUE, sold_at = %s WHERE detail_id = %s", (now, did))
                    sold_count += 1
        else:
            log.warning(f"⚠️ Skipping sold-marking: {len(fresh_ids)} fresh vs {len(existing)} existing")

    conn.commit()
    return new_count, updated_count, sold_count


def run_sync():
    if sync_status["running"]:
        log.info("⏭️ Sync already running, skipping")
        return

    sync_status["running"] = True
    sync_status["error"] = None
    start = datetime.now()
    log.info(f"🔄 Sync started at {start.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        conn = get_conn()
        ensure_columns(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM units")
            existing = {row["detail_id"]: dict(row) for row in cur.fetchall()}
        log.info(f"📦 Existing units: {len(existing):,}")

        all_fresh = []
        for city_name, city_id in PLACES.items():
            log.info(f"🏙️ Processing {city_name}...")
            filters = fetch_filters(city_id)
            compounds = filters.get("Compound", [])
            developers = filters.get("Developer", [])
            if not compounds:
                continue
            dev_lookup = {d["value"]: d["label"] for d in developers}
            for i, compound in enumerate(compounds, 1):
                cid = compound.get("value")
                cname = compound.get("label")
                dev_id = find_developer(cid, developers, city_id)
                if not dev_id:
                    continue
                details = fetch_compound_details(cid, dev_id, city_id)
                if not details:
                    continue
                compound_info = {
                    "id": cid, "name": cname,
                    "developer_id": dev_id,
                    "developer_name": dev_lookup.get(dev_id, "Unknown"),
                }
                rows = flatten_compound(compound_info, details, city_name)
                all_fresh.extend(rows)
                log.info(f"  [{i}/{len(compounds)}] {cname}: {len(rows)} units")

        new, updated, sold = sync_units(conn, all_fresh, existing)
        conn.close()

        elapsed = (datetime.now() - start).seconds
        result = f"New: {new}, Updated: {updated}, Sold: {sold}, Time: {elapsed}s"
        sync_status["last_result"] = result
        sync_status["last_run"] = datetime.now().isoformat()
        log.info(f"✅ Sync complete — {result}")
    except Exception as e:
        log.error(f"❌ Sync failed: {e}")
        import traceback; traceback.print_exc()
        sync_status["error"] = str(e)
    finally:
        sync_status["running"] = False


def _scheduler_loop():
    log.info("⏰ Scheduler thread started, waiting 15s before first sync...")
    time.sleep(15)
    run_sync()
    schedule.every(14).days.do(run_sync)
    while True:
        schedule.run_pending()
        time.sleep(60)


def start_sync_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    log.info("✅ Sync scheduler started")
