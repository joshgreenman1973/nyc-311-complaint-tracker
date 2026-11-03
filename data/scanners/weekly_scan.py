#!/usr/bin/env python3
"""
NYC Open Data Weekly Scanner
Fetches recently updated datasets and key metrics from NYC Open Data (Socrata API).
Run weekly to generate a consolidated insights report + append to trend CSV.
"""

import csv
import json
import os
import sys
import time
import requests
from datetime import datetime, timedelta
from collections import Counter

BASE = "https://data.cityofnewyork.us/resource"
CATALOG = "https://api.us.socrata.com/api/catalog/v1"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

CSV_COLUMNS = [
    "week_ending", "crashes", "persons_injured", "persons_killed",
    "pedestrians_injured", "pedestrians_killed", "cyclists_injured",
    "shooting_incidents", "shooting_victims", "shooting_murders",
    "hpd_complaints", "hpd_heat_hot_water", "hpd_heat_pct",
    "hpd_bronx_pct", "hpd_open_pct", "hpd_immediate_emergency",
    "vacate_orders_new", "vacate_orders_active",
    "restaurant_inspections", "critical_violations", "restaurant_closures",
    "dob_filings", "dob_large_projects_over_1m",
]


def fetch_json(url, params=None, attempts=4):
    """GET JSON from Socrata, retrying the failures that are worth retrying.

    Socrata read-times-out on the big datasets often enough that a single-shot
    request loses the whole weekly scan to one slow query -- 24 calls each with
    one chance means a scan that rarely finishes. Timeouts, dropped connections
    and 5xx get backed off and retried; a 404 or a bad query does not, because
    repeating those just wastes the run. If every attempt fails the exception
    still propagates and the job exits non-zero, which is the point: the scan
    fails loudly rather than publishing a week with holes in it.
    """
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=60,
                             headers={"User-Agent": "NYC-Open-Data-Weekly/1.0"})
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(
                    f"{r.status_code} Server Error for {url}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError,
                json.JSONDecodeError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and status < 500:
                raise
            last = e
            if i == attempts - 1:
                break
            wait = 5 * (2 ** i)
            print(f"  retry {i + 1}/{attempts - 1} in {wait}s: {url} ({type(e).__name__})",
                  file=sys.stderr)
            time.sleep(wait)
    raise last


def get_updated_datasets(days=7, limit=200):
    """Get all datasets updated in the past N days."""
    data = fetch_json(CATALOG, {
        "domains": "data.cityofnewyork.us",
        "order": "updatedAt DESC",
        "limit": limit,
        "only": "datasets",
        "provenance": "official"
    })
    cutoff = datetime.utcnow() - timedelta(days=days)
    recent = []
    for r in data.get("results", []):
        updated = r["resource"].get("updatedAt", "")
        if updated:
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
            if dt > cutoff:
                agency = ""
                for m in r.get("classification", {}).get("domain_metadata", []):
                    if m.get("key") == "Dataset-Information_Agency":
                        agency = m["value"]
                recent.append({
                    "name": r["resource"]["name"],
                    "id": r["resource"]["id"],
                    "updated": updated,
                    "category": r.get("classification", {}).get("domain_category", ""),
                    "agency": agency,
                    "description": r["resource"].get("description", "")[:120],
                    "views_last_week": r["resource"].get("page_views", {}).get("page_views_last_week", 0),
                })
    return recent


# --- Individual dataset scanners ---

def scan_collisions(days=7):
    """NYPD Motor Vehicle Collisions - Crashes (h9gi-nx95)"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = fetch_json(f"{BASE}/h9gi-nx95.json", {
        "$where": f"crash_date>='{since}'",
        "$limit": 5000
    })
    if not rows:
        return None
    return {
        "total_crashes": len(rows),
        "persons_injured": sum(int(r.get("number_of_persons_injured", 0)) for r in rows),
        "persons_killed": sum(int(r.get("number_of_persons_killed", 0)) for r in rows),
        "pedestrians_injured": sum(int(r.get("number_of_pedestrians_injured", 0)) for r in rows),
        "pedestrians_killed": sum(int(r.get("number_of_pedestrians_killed", 0)) for r in rows),
        "cyclists_injured": sum(int(r.get("number_of_cyclist_injured", 0)) for r in rows),
        "borough_breakdown": dict(Counter(r.get("borough", "UNSPECIFIED") for r in rows).most_common()),
        "top_factors": dict(Counter(r.get("contributing_factor_vehicle_1", "Unspecified") for r in rows).most_common(10)),
    }


def scan_hpd_complaints(days=7):
    """HPD Housing Maintenance Complaints (ygpa-z7cr)"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = fetch_json(f"{BASE}/ygpa-z7cr.json", {
        "$where": f"received_date>='{since}'",
        "$select": "count(*) as total",
    })
    total = int(rows[0]["total"]) if rows else 0
    types = fetch_json(f"{BASE}/ygpa-z7cr.json", {
        "$where": f"received_date>='{since}'",
        "$select": "major_category, count(*) as cnt",
        "$group": "major_category",
        "$order": "cnt DESC",
        "$limit": 10,
    })
    boroughs = fetch_json(f"{BASE}/ygpa-z7cr.json", {
        "$where": f"received_date>='{since}'",
        "$select": "borough, count(*) as cnt",
        "$group": "borough",
        "$order": "cnt DESC",
    })
    # Status breakdown
    statuses = fetch_json(f"{BASE}/ygpa-z7cr.json", {
        "$where": f"received_date>='{since}'",
        "$select": "complaint_status, count(*) as cnt",
        "$group": "complaint_status",
        "$order": "cnt DESC",
    })
    return {
        "total_complaints": total,
        "top_categories": {r["major_category"]: int(r["cnt"]) for r in types},
        "borough_breakdown": {r["borough"]: int(r["cnt"]) for r in boroughs},
        "status_breakdown": {r["complaint_status"]: int(r["cnt"]) for r in statuses},
    }


def scan_hpd_vacates(days=7):
    """HPD Order to Repair/Vacate (tb8q-a3ar)"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = fetch_json(f"{BASE}/tb8q-a3ar.json", {
        "$where": f"vacate_effective_date>='{since}'",
        "$order": "vacate_effective_date DESC",
        "$limit": 200,
    })
    active = [r for r in rows if not r.get("actual_rescind_date")]
    return {
        "new_orders": len(rows),
        "still_active": len(active),
        "reasons": dict(Counter(r.get("primary_vacate_reason", "") for r in rows).most_common()),
        "boroughs": dict(Counter(r.get("boro_short_name", "") for r in rows).most_common()),
    }


def scan_restaurant_inspections(days=7):
    """DOHMH Restaurant Inspections (43nn-pn8j)"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = fetch_json(f"{BASE}/43nn-pn8j.json", {
        "$where": f"inspection_date>='{since}'",
        "$limit": 5000,
    })
    grades = Counter(r.get("grade", "No Grade") for r in rows)
    critical = [r for r in rows if r.get("critical_flag") == "Critical"]
    # Count closures (grade P or action includes closure)
    closures = [r for r in rows if r.get("action", "").startswith("Establishment Closed")]
    return {
        "total_inspections": len(rows),
        "grade_distribution": dict(grades.most_common()),
        "critical_violations": len(critical),
        "closures": len(set(r.get("camis", "") for r in closures)),
    }


def scan_shootings(days=7):
    """NYPD Shooting Incidents (5ucz-vwe8), Victims (pztn-9bne)"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    incidents = fetch_json(f"{BASE}/5ucz-vwe8.json", {
        "$where": f"occur_date>='{since}'",
        "$limit": 5000,
    })
    if not incidents:
        return {"total_incidents": 0, "total_victims": 0, "murders": 0}
    keys = [r["incident_key"] for r in incidents if "incident_key" in r]
    victims = []
    if keys:
        victims = fetch_json(f"{BASE}/pztn-9bne.json", {
            "$where": f"incident_key in({','.join(repr(k) for k in keys[:200])})",
            "$limit": 5000,
        })
    murders = sum(1 for v in victims if v.get("stat_murder_flg") == "Y")
    return {
        "total_incidents": len(incidents),
        "total_victims": len(victims),
        "murders": murders,
        "borough_breakdown": dict(Counter(r.get("boro", "UNKNOWN") for r in incidents).most_common()),
    }


def scan_dob_filings(days=7):
    """DOB NOW Job Application Filings (w9ak-ipjd)"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    # Count server-side: the old $limit=2000 fetch silently capped busy weeks at 2,000.
    total = fetch_json(f"{BASE}/w9ak-ipjd.json", {
        "$select": "count(*) as n",
        "$where": f"filing_date>='{since}'",
    })
    big = fetch_json(f"{BASE}/w9ak-ipjd.json", {
        "$select": "count(*) as n",
        "$where": f"filing_date>='{since}' AND initial_cost::number > 1000000",
    })
    return {
        "total_filings": int((total or [{}])[0].get("n", 0)),
        "large_projects_over_1m": int((big or [{}])[0].get("n", 0)),
    }


# --- CSV + JSON output ---

def build_week_row(week_ending, collisions, hpd, vacates, restaurants, shootings, dob):
    """Build a single row dict for the weekly trends CSV."""
    heat_hw = hpd["top_categories"].get("HEAT/HOT WATER", 0) if hpd else 0
    hpd_total = hpd["total_complaints"] if hpd else 0
    bronx = hpd["borough_breakdown"].get("BRONX", 0) if hpd else 0
    open_count = hpd["status_breakdown"].get("Open", 0) if hpd else 0
    emergency = hpd["top_categories"].get("GENERAL", 0) if hpd else 0  # approximate

    # For immediate emergency, try to get "EMERGENCY" category
    imm_emergency = hpd["top_categories"].get("EMERGENCY", 0) if hpd else 0

    # Collisions publish 6-10 weeks late and NYPD's shooting file is quarterly, so a
    # "last 7 days" query legitimately returns nothing. Write blanks, not zeros — a
    # zero here reads as "no crashes this week" and produced -100% YoY artifacts.
    # Blank a whole source only when it returned no records; once there are crashes,
    # a zero (e.g. nobody killed) is a real count and stays a zero.
    has_crashes = bool(collisions and collisions["total_crashes"])
    has_shootings = bool(shootings and shootings["total_incidents"])

    def lag(present, v):
        return v if present else ""

    return {
        "week_ending": week_ending,
        "crashes": lag(has_crashes, collisions["total_crashes"] if collisions else 0),
        "persons_injured": lag(has_crashes, collisions["persons_injured"] if collisions else 0),
        "persons_killed": lag(has_crashes, collisions["persons_killed"] if collisions else 0),
        "pedestrians_injured": lag(has_crashes, collisions["pedestrians_injured"] if collisions else 0),
        "pedestrians_killed": lag(has_crashes, collisions["pedestrians_killed"] if collisions else 0),
        "cyclists_injured": lag(has_crashes, collisions["cyclists_injured"] if collisions else 0),
        "shooting_incidents": lag(has_shootings, shootings["total_incidents"] if shootings else 0),
        "shooting_victims": lag(has_shootings, shootings["total_victims"] if shootings else 0),
        "shooting_murders": lag(has_shootings, shootings["murders"] if shootings else 0),
        "hpd_complaints": hpd_total,
        "hpd_heat_hot_water": heat_hw,
        "hpd_heat_pct": round(heat_hw / hpd_total * 100, 1) if hpd_total else 0,
        "hpd_bronx_pct": round(bronx / hpd_total * 100, 1) if hpd_total else 0,
        "hpd_open_pct": round(open_count / hpd_total * 100, 1) if hpd_total else 0,
        "hpd_immediate_emergency": imm_emergency,
        "vacate_orders_new": vacates["new_orders"] if vacates else 0,
        "vacate_orders_active": vacates["still_active"] if vacates else 0,
        "restaurant_inspections": restaurants["total_inspections"] if restaurants else 0,
        "critical_violations": restaurants["critical_violations"] if restaurants else 0,
        "restaurant_closures": restaurants["closures"] if restaurants else 0,
        "dob_filings": dob["total_filings"] if dob else 0,
        "dob_large_projects_over_1m": dob["large_projects_over_1m"] if dob else 0,
    }


def load_existing_csv():
    """Load existing weekly trends CSV."""
    csv_path = os.path.join(OUTPUT_DIR, "weekly-trends.csv")
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, "r") as f:
        return list(csv.DictReader(f))


def save_csv(rows):
    """Save weekly trends CSV."""
    csv_path = os.path.join(OUTPUT_DIR, "weekly-trends.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {len(rows)} weeks to {csv_path}")


def compute_aggregates(rows):
    """Compute monthly and YTD aggregates from weekly rows."""
    if not rows:
        return {"monthly": {}, "ytd": {}}

    numeric_cols = [c for c in CSV_COLUMNS if c != "week_ending"]

    # Group by month
    monthly = {}
    for row in rows:
        month_key = row["week_ending"][:7]  # YYYY-MM
        if month_key not in monthly:
            monthly[month_key] = {c: 0 for c in numeric_cols}
            monthly[month_key]["weeks"] = 0
        monthly[month_key]["weeks"] += 1
        for c in numeric_cols:
            val = float(row.get(c, 0) or 0)
            # For percentages, we'll average rather than sum
            if c.endswith("_pct"):
                monthly[month_key][c] += val
            else:
                monthly[month_key][c] += val

    # Average the percentages
    for month_key, data in monthly.items():
        weeks = data["weeks"]
        for c in numeric_cols:
            if c.endswith("_pct") and weeks > 0:
                data[c] = round(data[c] / weeks, 1)

    # YTD: current year
    current_year = datetime.now().strftime("%Y")
    ytd_rows = [r for r in rows if r["week_ending"].startswith(current_year)]
    ytd = {c: 0 for c in numeric_cols}
    ytd["weeks"] = len(ytd_rows)
    for row in ytd_rows:
        for c in numeric_cols:
            val = float(row.get(c, 0) or 0)
            if c.endswith("_pct"):
                ytd[c] += val
            else:
                ytd[c] += val
    # Average percentages
    if ytd["weeks"] > 0:
        for c in numeric_cols:
            if c.endswith("_pct"):
                ytd[c] = round(ytd[c] / ytd["weeks"], 1)

    return {"monthly": monthly, "ytd": ytd}


def generate_flags(row, existing_rows, collisions, hpd, vacates, restaurants, shootings, datasets):
    """Generate editorial flags — anomalies, context, and things worth investigating."""
    flags = []
    prev_rows = [r for r in existing_rows if r["week_ending"] != row["week_ending"]]

    # Helper: compute average of a field from previous weeks
    def avg(field):
        vals = [float(r.get(field, 0) or 0) for r in prev_rows]
        return sum(vals) / len(vals) if vals else None

    def pct_change(current, baseline):
        if not baseline or baseline == 0:
            return None
        return round((current - baseline) / baseline * 100, 1)

    # --- Crash anomalies ---
    if collisions and row["crashes"] != "":
        crash_avg = avg("crashes")
        killed = int(row.get("persons_killed", 0))
        killed_avg = avg("persons_killed")
        ped_inj = int(row.get("pedestrians_injured", 0))
        ped_avg = avg("pedestrians_injured")

        if crash_avg and pct_change(int(row["crashes"]), crash_avg) and abs(pct_change(int(row["crashes"]), crash_avg)) > 15:
            chg = pct_change(int(row["crashes"]), crash_avg)
            direction = "up" if chg > 0 else "down"
            flags.append({
                "category": "Public Safety",
                "severity": "high" if abs(chg) > 25 else "medium",
                "headline": f"Crashes {'spike' if chg > 0 else 'drop'}: {int(row['crashes'])} this week",
                "detail": f"{'Up' if chg > 0 else 'Down'} {abs(chg)}% vs {int(crash_avg)}/week average. {int(row['persons_injured'])} injured, {killed} killed.",
                "direction": direction,
            })

        if killed >= 5:
            context = f" (avg: {killed_avg:.1f}/week)" if killed_avg else ""
            flags.append({
                "category": "Public Safety",
                "severity": "high",
                "headline": f"{killed} killed in traffic this week",
                "detail": f"{killed} people killed in {int(row['crashes'])} crashes{context}. {int(row.get('pedestrians_killed', 0))} were pedestrians.",
                "direction": "up",
            })

        if ped_avg and ped_inj > ped_avg * 1.2:
            flags.append({
                "category": "Public Safety",
                "severity": "medium",
                "headline": f"Pedestrian injuries elevated: {ped_inj}",
                "detail": f"Up from {ped_avg:.0f}/week average. That's {ped_inj / 7:.0f}/day heading into {'spring' if datetime.now().month in (3,4,5) else 'the season'} when foot traffic increases.",
                "direction": "up",
            })

        # Top contributing factor
        if collisions.get("top_factors"):
            top_factor = max(collisions["top_factors"].items(), key=lambda x: x[1] if x[0] != "Unspecified" else 0)
            if top_factor[0] != "Unspecified":
                pct = round(top_factor[1] / collisions["total_crashes"] * 100, 1)
                flags.append({
                    "category": "Public Safety",
                    "severity": "info",
                    "headline": f"Top crash cause: {top_factor[0]}",
                    "detail": f"Accounts for {pct}% of this week's {collisions['total_crashes']} crashes ({top_factor[1]} incidents).",
                    "direction": "neutral",
                })

    # --- HPD anomalies ---
    if hpd:
        hpd_total = hpd["total_complaints"]
        hpd_avg = avg("hpd_complaints")
        heat_pct = float(row.get("hpd_heat_pct", 0))
        bronx_pct = float(row.get("hpd_bronx_pct", 0))

        if hpd_avg and pct_change(hpd_total, hpd_avg) and abs(pct_change(hpd_total, hpd_avg)) > 15:
            chg = pct_change(hpd_total, hpd_avg)
            flags.append({
                "category": "Housing",
                "severity": "high" if abs(chg) > 25 else "medium",
                "headline": f"HPD complaints {'surge' if chg > 0 else 'drop'}: {hpd_total:,}",
                "detail": f"{'Up' if chg > 0 else 'Down'} {abs(chg)}% vs {int(hpd_avg):,}/week average.",
                "direction": "up" if chg > 0 else "down",
            })

        # Heat/hot water in context
        month = datetime.now().month
        season_note = ""
        if month in (3, 4, 5):
            season_note = " Heating season should be winding down — this suggests landlords cutting heat prematurely or chronic system failures."
        elif month in (10, 11):
            season_note = " Heating season is starting — watch for buildings slow to fire up boilers."
        if heat_pct > 25:
            flags.append({
                "category": "Housing",
                "severity": "high" if heat_pct > 35 else "medium",
                "headline": f"Heat/hot water at {heat_pct}% of complaints",
                "detail": f"{int(row.get('hpd_heat_hot_water', 0)):,} heat/hot water complaints out of {hpd_total:,} total.{season_note}",
                "direction": "up",
            })

        # Bronx disproportionality
        if bronx_pct > 30:
            flags.append({
                "category": "Housing",
                "severity": "high" if bronx_pct > 35 else "medium",
                "headline": f"Bronx: {bronx_pct}% of housing complaints (14% of population)",
                "detail": f"The Bronx accounts for {bronx_pct}% of all HPD complaints with only 14% of the city's population — a {bronx_pct / 14:.1f}x overrepresentation.",
                "direction": "up",
            })

    # --- Vacate orders ---
    if vacates and vacates["new_orders"] > 0:
        vac_avg = avg("vacate_orders_new")
        detail_parts = []
        if vacates.get("reasons"):
            top_reason = max(vacates["reasons"].items(), key=lambda x: x[1])
            detail_parts.append(f"Top reason: {top_reason[0]}")
        if vacates.get("boroughs"):
            top_boro = max(vacates["boroughs"].items(), key=lambda x: x[1])
            detail_parts.append(f"{top_boro[0]} leads with {top_boro[1]}")
        context = f" (avg: {vac_avg:.0f}/week)" if vac_avg else ""
        flags.append({
            "category": "Housing",
            "severity": "medium" if vacates["new_orders"] >= 10 else "info",
            "headline": f"{vacates['new_orders']} new vacate orders, {vacates['still_active']} active",
            "detail": f"{vacates['new_orders']} buildings ordered vacated or repaired this week{context}. {'. '.join(detail_parts)}.",
            "direction": "neutral",
        })

    # --- Shootings ---
    if shootings and shootings["total_incidents"] > 0:
        sh_avg = avg("shooting_incidents")
        context = f" (avg: {sh_avg:.0f}/week)" if sh_avg else ""
        boro_detail = ""
        if shootings.get("borough_breakdown"):
            top_boro = max(shootings["borough_breakdown"].items(), key=lambda x: x[1])
            boro_detail = f" {top_boro[0]} leads with {top_boro[1]} incidents."
        flags.append({
            "category": "Public Safety",
            "severity": "high",
            "headline": f"{shootings['total_incidents']} shooting incidents, {shootings['murders']} murders",
            "detail": f"{shootings['total_victims']} victims this week{context}.{boro_detail}",
            "direction": "up",
        })
    elif shootings and shootings["total_incidents"] == 0:
        flags.append({
            "category": "Public Safety",
            "severity": "info",
            "headline": "Shooting data: 0 incidents reported",
            "detail": "NYPD restructured shooting datasets in Feb 2026 (now 3 separate datasets). Zero may reflect reporting lag rather than no incidents.",
            "direction": "neutral",
        })

    # --- Restaurant inspections ---
    if restaurants and restaurants["closures"] > 0:
        flags.append({
            "category": "Health",
            "severity": "medium" if restaurants["closures"] >= 5 else "info",
            "headline": f"{restaurants['closures']} restaurant closures, {restaurants['critical_violations']} critical violations",
            "detail": f"Out of {restaurants['total_inspections']} inspections this week.",
            "direction": "neutral",
        })

    # --- Dataset catalog flags ---
    if datasets:
        high_interest_agencies = {"NYPD", "HPD", "DOB", "DOHMH", "DEP", "DOE", "ACS", "DHS"}
        notable = [d for d in datasets if d["agency"] in high_interest_agencies and d.get("views_last_week", 0) > 100]
        if notable:
            names = [d["name"] for d in sorted(notable, key=lambda x: x.get("views_last_week", 0), reverse=True)[:5]]
            flags.append({
                "category": "Data Updates",
                "severity": "info",
                "headline": f"{len(datasets)} datasets updated this week",
                "detail": f"High-traffic updates: {', '.join(names[:3])}{'...' if len(names) > 3 else ''}",
                "direction": "neutral",
            })

    # Sort: high severity first
    severity_order = {"high": 0, "medium": 1, "info": 2}
    flags.sort(key=lambda f: severity_order.get(f["severity"], 3))

    return flags


def compute_baselines(week_ending_str):
    """Compute 2025 annual averages and same-week-prior-year data for YoY comparison."""
    print("\n[Baselines] Querying prior-year data for comparison...")

    prior_year = str(int(week_ending_str[:4]) - 1)
    # Same week last year: 7-day window ending on same date minus 1 year
    we = datetime.strptime(week_ending_str, "%Y-%m-%d")
    prior_we = we.replace(year=we.year - 1)
    prior_start = (prior_we - timedelta(days=6)).strftime("%Y-%m-%d")
    prior_end = prior_we.strftime("%Y-%m-%d")

    baselines = {"annual_prev_year": {}, "same_week_prior_year": {}, "diverging_trends": []}

    # --- Annual averages (full prior year) ---
    try:
        r = fetch_json(f"{BASE}/h9gi-nx95.json", {
            "$where": f"crash_date between '{prior_year}-01-01' and '{prior_year}-12-31'",
            "$select": "count(*) as total, sum(number_of_persons_killed) as killed, "
                       "sum(number_of_pedestrians_injured) as ped_inj, "
                       "sum(number_of_cyclist_injured) as cyc_inj",
        })
        if r and r[0].get("total"):
            total = int(r[0]["total"])
            baselines["annual_prev_year"]["crashes_per_week"] = round(total / 52)
            baselines["annual_prev_year"]["persons_killed_per_week"] = round(int(r[0].get("killed", 0) or 0) / 52, 1)
            baselines["annual_prev_year"]["pedestrians_injured_per_week"] = round(int(r[0].get("ped_inj", 0) or 0) / 52)
            baselines["annual_prev_year"]["cyclists_injured_per_week"] = round(int(r[0].get("cyc_inj", 0) or 0) / 52)
            print(f"  Crashes {prior_year}: {total}/yr = {total//52}/wk")
    except Exception as e:
        print(f"  [warn] Crash baseline failed: {e}")

    try:
        r = fetch_json(f"{BASE}/ygpa-z7cr.json", {
            "$where": f"received_date between '{prior_year}-01-01T00:00:00' and '{prior_year}-12-31T23:59:59'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            total = int(r[0]["total"])
            baselines["annual_prev_year"]["hpd_complaints_per_week"] = round(total / 52)
            print(f"  HPD {prior_year}: {total}/yr = {total//52}/wk")
        r2 = fetch_json(f"{BASE}/ygpa-z7cr.json", {
            "$where": f"received_date between '{prior_year}-01-01T00:00:00' and '{prior_year}-12-31T23:59:59'"
                       f" AND upper(major_category)='HEAT/HOT WATER'",
            "$select": "count(*) as total",
        })
        if r2 and r2[0].get("total"):
            hw = int(r2[0]["total"])
            baselines["annual_prev_year"]["hpd_heat_hw_per_week"] = round(hw / 52)
            if total:
                baselines["annual_prev_year"]["hpd_heat_pct"] = round(hw / total * 100, 1)
    except Exception as e:
        print(f"  [warn] HPD baseline failed: {e}")

    try:
        r = fetch_json(f"{BASE}/5ucz-vwe8.json", {
            "$where": f"occur_date between '{prior_year}-01-01' and '{prior_year}-12-31'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            baselines["annual_prev_year"]["shooting_incidents_per_week"] = round(int(r[0]["total"]) / 52, 1)
    except Exception as e:
        print(f"  [warn] Shooting baseline failed: {e}")

    try:
        r = fetch_json(f"{BASE}/tb8q-a3ar.json", {
            "$where": f"vacate_effective_date between '{prior_year}-01-01' and '{prior_year}-12-31'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            baselines["annual_prev_year"]["vacate_orders_per_week"] = round(int(r[0]["total"]) / 52)
    except Exception as e:
        print(f"  [warn] Vacate baseline failed: {e}")

    try:
        r = fetch_json(f"{BASE}/43nn-pn8j.json", {
            "$where": f"inspection_date between '{prior_year}-01-01T00:00:00' and '{prior_year}-12-31T23:59:59'"
                       f" AND action like 'Establishment Closed%25'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            baselines["annual_prev_year"]["restaurant_closures_per_week"] = round(int(r[0]["total"]) / 52)
    except Exception as e:
        print(f"  [warn] Restaurant baseline failed: {e}")

    # --- Same week prior year ---
    print(f"  Querying same-week {prior_year} ({prior_start} to {prior_end})...")
    sw = baselines["same_week_prior_year"]
    sw["week_ending"] = prior_end

    try:
        rows = fetch_json(f"{BASE}/h9gi-nx95.json", {
            "$where": f"crash_date between '{prior_start}' and '{prior_end}'",
            "$select": "count(*) as total, sum(number_of_persons_killed) as killed, "
                       "sum(number_of_pedestrians_injured) as ped_inj, "
                       "sum(number_of_cyclist_injured) as cyc_inj",
        })
        if rows and rows[0].get("total"):
            sw["crashes"] = int(rows[0]["total"])
            sw["persons_killed"] = int(rows[0].get("killed", 0) or 0)
            sw["pedestrians_injured"] = int(rows[0].get("ped_inj", 0) or 0)
            sw["cyclists_injured"] = int(rows[0].get("cyc_inj", 0) or 0)
    except Exception as e:
        print(f"  [warn] Same-week crash query failed: {e}")

    try:
        r = fetch_json(f"{BASE}/ygpa-z7cr.json", {
            "$where": f"received_date between '{prior_start}T00:00:00' and '{prior_end}T23:59:59'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            sw["hpd_complaints"] = int(r[0]["total"])
        r2 = fetch_json(f"{BASE}/ygpa-z7cr.json", {
            "$where": f"received_date between '{prior_start}T00:00:00' and '{prior_end}T23:59:59'"
                       f" AND upper(major_category)='HEAT/HOT WATER'",
            "$select": "count(*) as total",
        })
        if r2 and r2[0].get("total"):
            sw["hpd_heat_hot_water"] = int(r2[0]["total"])
            if sw.get("hpd_complaints"):
                sw["hpd_heat_pct"] = round(sw["hpd_heat_hot_water"] / sw["hpd_complaints"] * 100, 1)
    except Exception as e:
        print(f"  [warn] Same-week HPD query failed: {e}")

    try:
        r = fetch_json(f"{BASE}/5ucz-vwe8.json", {
            "$where": f"occur_date between '{prior_start}' and '{prior_end}'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            sw["shooting_incidents"] = int(r[0]["total"])
    except Exception as e:
        print(f"  [warn] Same-week shooting query failed: {e}")

    try:
        r = fetch_json(f"{BASE}/43nn-pn8j.json", {
            "$where": f"inspection_date between '{prior_start}T00:00:00' and '{prior_end}T23:59:59'"
                       f" AND action like 'Establishment Closed%25'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            sw["restaurant_closures"] = int(r[0]["total"])
    except Exception as e:
        print(f"  [warn] Same-week restaurant query failed: {e}")

    try:
        r = fetch_json(f"{BASE}/tb8q-a3ar.json", {
            "$where": f"vacate_effective_date between '{prior_start}' and '{prior_end}'",
            "$select": "count(*) as total",
        })
        if r and r[0].get("total"):
            sw["vacate_orders_new"] = int(r[0]["total"])
    except Exception as e:
        print(f"  [warn] Same-week vacate query failed: {e}")

    print(f"  Baselines computed: {len(baselines['annual_prev_year'])} annual, {len(sw)-1} same-week")
    return baselines


def compute_diverging_trends(current_row, baselines):
    """Flag metrics that are diverging significantly from prior year patterns."""
    diverging = []
    ann = baselines.get("annual_prev_year", {})
    sw = baselines.get("same_week_prior_year", {})

    def num(k):
        v = current_row.get(k, 0)
        return None if v in ("", None) else float(v)

    comparisons = [
        ("Crashes", num("crashes"), sw.get("crashes"), ann.get("crashes_per_week")),
        ("Killed in traffic", num("persons_killed"), sw.get("persons_killed"), ann.get("persons_killed_per_week")),
        ("Pedestrian injuries", num("pedestrians_injured"), sw.get("pedestrians_injured"), ann.get("pedestrians_injured_per_week")),
        ("Cyclist injuries", num("cyclists_injured"), sw.get("cyclists_injured"), ann.get("cyclists_injured_per_week")),
        ("HPD complaints", num("hpd_complaints"), sw.get("hpd_complaints"), ann.get("hpd_complaints_per_week")),
        ("Shooting incidents", num("shooting_incidents"), sw.get("shooting_incidents"), ann.get("shooting_incidents_per_week")),
        ("Restaurant closures", num("restaurant_closures"), sw.get("restaurant_closures"), ann.get("restaurant_closures_per_week")),
        ("Vacate orders", num("vacate_orders_new"), sw.get("vacate_orders_new"), ann.get("vacate_orders_per_week")),
    ]

    for metric, this_week, same_week, avg_week in comparisons:
        if this_week is None or (same_week is None and avg_week is None):
            continue  # lagged feed with no data this week -- not a -100% drop
        yoy_pct = round((this_week - same_week) / same_week * 100, 1) if same_week and same_week > 0 else None
        vs_avg_pct = round((this_week - avg_week) / avg_week * 100, 1) if avg_week and avg_week > 0 else None
        # Flag if >15% YoY change OR >20% vs annual avg
        if (yoy_pct is not None and abs(yoy_pct) > 15) or (vs_avg_pct is not None and abs(vs_avg_pct) > 20):
            direction = "up" if (yoy_pct or 0) > 0 else "down"
            note = _divergence_note(metric, this_week, same_week, avg_week, yoy_pct, vs_avg_pct, direction)
            diverging.append({
                "metric": metric,
                "this_week": this_week,
                "same_week_prior_year": same_week,
                "annual_avg_prior_year": avg_week,
                "yoy_pct": yoy_pct,
                "vs_avg_pct": vs_avg_pct,
                "direction": direction,
                "note": note,
            })

    # Sort by absolute YoY change
    diverging.sort(key=lambda d: abs(d.get("yoy_pct") or 0), reverse=True)
    return diverging


def _divergence_note(metric, this_week, same_week, avg_week, yoy_pct, vs_avg_pct, direction):
    """Generate a human-readable note about a diverging trend."""
    m = metric.lower()
    if yoy_pct is not None and abs(yoy_pct) > 40:
        if direction == "down":
            return f"{metric} down sharply vs same week last year — possible data lag or genuine improvement"
        return f"{metric} well above same week last year — worth investigating"
    if yoy_pct is not None and vs_avg_pct is not None:
        if (yoy_pct > 0) != (vs_avg_pct > 0):
            return f"{metric} diverging: {'above' if yoy_pct > 0 else 'below'} same week last year but {'above' if vs_avg_pct > 0 else 'below'} annual average"
        if direction == "up":
            return f"{metric} trending above both last year and annual average"
        return f"{metric} trending below both last year and annual average"
    if yoy_pct is not None:
        return f"{metric} {'up' if direction == 'up' else 'down'} {abs(yoy_pct):.0f}% vs same week last year"
    return f"{metric} {'above' if direction == 'up' else 'below'} annual average by {abs(vs_avg_pct):.0f}%"


def save_dashboard_json(rows, aggregates, flags=None, datasets=None, detail=None, baselines=None):
    """Save JSON for the dashboard to consume."""
    json_path = os.path.join(OUTPUT_DIR, "weekly-trends.json")
    with open(json_path, "w") as f:
        json.dump({
            "weekly": rows,
            "baselines": baselines or {},
            "monthly": aggregates["monthly"],
            "ytd": aggregates["ytd"],
            "flags": flags or [],
            "detail": detail or {},
            "datasets_updated": datasets or [],
            "generated": datetime.now().isoformat(),
        }, f)
    print(f"  Saved dashboard JSON to {json_path}")


def run_scan():
    """Run the full weekly scan, append to CSV, and generate dashboard data."""
    week_ending = datetime.now().strftime("%Y-%m-%d")
    print(f"NYC Open Data Weekly Scan — week ending {week_ending}")
    print("=" * 60)

    print("\n[Collisions] Scanning motor vehicle crashes...")
    collisions = scan_collisions()
    if collisions:
        print(f"  {collisions['total_crashes']} crashes, {collisions['persons_killed']} killed")

    print("\n[Shootings] Scanning shooting incidents...")
    shootings = scan_shootings()
    print(f"  {shootings['total_incidents']} incidents, {shootings['murders']} murders")

    print("\n[HPD] Scanning housing complaints...")
    hpd = scan_hpd_complaints()
    print(f"  {hpd['total_complaints']} complaints")

    print("\n[HPD Vacates] Scanning vacate orders...")
    vacates = scan_hpd_vacates()
    print(f"  {vacates['new_orders']} new, {vacates['still_active']} active")

    print("\n[Restaurants] Scanning restaurant inspections...")
    restaurants = scan_restaurant_inspections()
    print(f"  {restaurants['total_inspections']} inspections, {restaurants['critical_violations']} critical")

    print("\n[DOB] Scanning job filings...")
    dob = scan_dob_filings()
    print(f"  {dob['total_filings']} filings")

    print("\n[Catalog] Scanning recently updated datasets...")
    datasets = get_updated_datasets()
    by_agency = Counter(d["agency"] for d in datasets)
    print(f"  {len(datasets)} datasets updated in past 7 days")

    # Build this week's row
    row = build_week_row(week_ending, collisions, hpd, vacates, restaurants, shootings, dob)

    # Load existing data and append (avoid duplicates)
    existing = load_existing_csv()
    existing_weeks = {r["week_ending"] for r in existing}
    if week_ending in existing_weeks:
        # Replace this week's data
        existing = [r for r in existing if r["week_ending"] != week_ending]
    existing.append(row)
    existing.sort(key=lambda r: r["week_ending"])

    # Save
    print("\n[Output] Writing trend data...")
    save_csv(existing)

    aggregates = compute_aggregates(existing)

    # Generate editorial flags
    print("\n[Flags] Generating editorial intelligence...")
    flags = generate_flags(row, existing, collisions, hpd, vacates, restaurants, shootings, datasets)
    print(f"  Generated {len(flags)} flags ({sum(1 for f in flags if f['severity']=='high')} high, {sum(1 for f in flags if f['severity']=='medium')} medium)")
    for f in flags:
        icon = {"high": "!!", "medium": "!", "info": "-"}.get(f["severity"], " ")
        print(f"  [{icon}] {f['headline']}")

    # Build detail breakdowns for the dashboard
    detail = {}
    if collisions:
        detail["crashes"] = {
            "borough_breakdown": collisions.get("borough_breakdown", {}),
            "top_factors": collisions.get("top_factors", {}),
        }
    if hpd:
        detail["hpd"] = {
            "borough_breakdown": hpd.get("borough_breakdown", {}),
            "top_categories": hpd.get("top_categories", {}),
            "status_breakdown": hpd.get("status_breakdown", {}),
        }
    if vacates:
        detail["vacates"] = {
            "reasons": vacates.get("reasons", {}),
            "boroughs": vacates.get("boroughs", {}),
        }
    if restaurants:
        detail["restaurants"] = {
            "grade_distribution": restaurants.get("grade_distribution", {}),
        }
    if shootings:
        detail["shootings"] = {
            "borough_breakdown": shootings.get("borough_breakdown", {}),
        }

    # Compute prior-year baselines and diverging trends
    baselines = compute_baselines(week_ending)
    diverging = compute_diverging_trends(row, baselines)
    baselines["diverging_trends"] = diverging
    print(f"\n[Diverging] {len(diverging)} metrics diverging from prior year:")
    for d in diverging:
        arrow = "↑" if d["direction"] == "up" else "↓"
        yoy = f"{d['yoy_pct']:+.0f}% YoY" if d.get("yoy_pct") is not None else ""
        print(f"  {arrow} {d['metric']}: {yoy} — {d['note']}")

    save_dashboard_json(existing, aggregates, flags, datasets, detail, baselines)

    # Save updated datasets list
    datasets_path = os.path.join(OUTPUT_DIR, "updated-datasets.json")
    with open(datasets_path, "w") as f:
        json.dump({"datasets": datasets, "by_agency": dict(by_agency.most_common()),
                    "scanned": datetime.now().isoformat()}, f)
    print(f"  Saved {len(datasets)} datasets to {datasets_path}")

    print("\n" + "=" * 60)
    print("Weekly scan complete.")


if __name__ == "__main__":
    run_scan()
