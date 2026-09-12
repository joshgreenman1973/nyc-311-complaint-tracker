"""
B. Place-level convergence.

Stories live at a building, not a district. This module clusters the last two
weeks of 311 at the tax lot (BBL) and joins, on the same key, every other
timely place-keyed feed the city publishes:

  311 service requests ............ erm2-nwe9   (bbl, 14 days, vs prior 12 weeks)
  HPD housing-code violations ..... wvxf-dwi5   (boro/block/lot, 30 days, class B/C)
  HPD housing complaints .......... ygpa-z7cr   (borough/block/lot, 14 days)
  DOB complaints .................. eabe-havv   (bin + address, 30 days; joins by address)
  ECB / OATH building violations .. 6bgk-3dad   (boro/block/lot, 30 days)
  HPD vacate orders ............... tb8q-a3ar   (bbl, 60 days)
  Marshal evictions ............... 6z8x-wfk4   (bbl, 60 days)
  SLA pending liquor licenses ..... f8i8-k2gm   (state; point -> district, 60 days)

A place becomes a lead when two or more of these agree, or when a single
source is extreme (a dozen-plus 311 calls in a fortnight, a vacate order,
ten-plus class C violations). Every count carries its source query so a
reporter can verify it in one click.
"""

import re
from collections import defaultdict
from datetime import timedelta

from common import (NYS, DistrictLocator, addr_key, span, bbl_from_parts, cd_from_311, cd_from_3digit,
                    cd_from_num, data_end, iso, soql, soql_all, split_311_address, where_between,
                    window, BORO_NAME, CD_NAMES)

W311 = 14
W311_PRIOR = 84
WHPD_V = 30
WHPD_C = 14
WDOB = 30
WECB = 30
WVAC = 60
WEVI = 60
WSLA = 60

MIN_311 = 6           # 311 calls in 14 days for a BBL to enter the candidate pool

DOB_CATEGORIES = {
    "01": "Accident – construction/plumbing", "03": "Adjacent buildings not protected",
    "04": "After hours work – illegal", "05": "Permit – none (building/PA/demo)",
    "06": "Construction – change grade/watercourse", "09": "Debris – excessive",
    "10": "Debris/building – falling or in danger of falling", "12": "Demolition – unsafe/illegal/mechanical demo",
    "13": "Elevator in (FDNY) readiness – none", "14": "Excavation – undermining adjacent building",
    "15": "Fence – none/inadequate/illegal", "16": "Inadequate support/shoring",
    "18": "Material storage – unsafe", "20": "Landmark building – illegal work",
    "21": "Safety net/guard rail – damaged/inadequate/none", "23": "Sidewalk shed/supported scaffold/inadequate defective/none",
    "29": "Building – vacant, open and unguarded", "30": "Building shaking/vibrating/structural stability affected",
    "31": "Certificate of occupancy – none/illegal/contrary to CO", "35": "Curb cut/driveway/carport – illegal",
    "37": "Egress – locked/blocked/improper/no secondary means", "45": "Illegal conversion",
    "49": "Storefront or business sign/awning/marquee", "50": "Sign falling – danger/sign erection or display in progress",
    "52": "Sprinkler system", "53": "Vent/exhaust – illegal/improper", "54": "Wall/retaining wall – bulging/cracked",
    "55": "Zoning – non-conforming", "56": "Boiler – fumes/smoke/carbon monoxide", "58": "Boiler – defective/inoperative/no permit",
    "59": "Electrical wiring – defective/exposed, in progress", "62": "Debris – excessive/failure to sweep",
    "63": "Elevator – danger condition/shaft open/unguarded", "65": "Cranes and derricks", "66": "Plumbing work – illegal/no permit",
    "67": "Crane – no permit/license/cert/unsafe/illegal", "71": "SRO – illegal work/no PA/illegal use",
    "73": "Failure to maintain", "74": "Illegal commercial/manufacturing use in residential zone",
    "75": "Adult establishment", "76": "Unlicensed/illegal/improper plumbing work in progress",
    "77": "Contrary to LL58/87 (handicapped access)", "78": "Privately owned public space/non-compliance",
    "79": "Lights from parking lot shining on building", "80": "Elevator not inspected/illegal/no permit",
    "81": "Elevator – accident", "82": "Boiler – accident/explosion", "83": "Construction – contrary/beyond approved plans/permits",
    "85": "Failure to retain water/improper drainage", "86": "Work contrary to stop work order",
    "88": "Safety net/guardrail – damaged/inadequate/none (over 6 story/75 ft.)", "89": "Accident – cranes/derricks/suspension scaffold",
    "90": "Unlicensed/illegal activity", "91": "Site conditions endangering workers", "92": "Illegal conversion of manufacturing/industrial space",
    "93": "Request for retaining wall safety inspection", "94": "Plumbing – defective/leaking/not maintained",
    "1A": "Illegal conversion – commercial building/space to dwelling units", "1B": "Illegal tree removal/topo. change in SNAD",
    "1D": "Con Edison referral", "1E": "Suspended (hanging) scaffolds – no permit/license/dangerous/accident",
    "1G": "Stalled construction site", "1K": "Bowstring truss tracking complaint", "1Z": "Enforcement work order (DOB)",
    "2A": "Posted notice or order removed/tampered with", "2B": "Failure to comply with Vacate Order",
    "2C": "Smoking ban – smoking on construction site", "2D": "Smoking signs – 'No Smoking' signs not observed on construction site",
    "2E": "Demolition notification received", "2F": "Building under structural monitoring", "2G": "Advertising sign/billboard/posters/flexible fabric – illegal",
    "2H": "Second avenue subway construction", "2J": "Sandy: building destroyed", "2K": "Structurally compromised building (LL33/08)",
    "2L": "Façade (LL11/98) – unsafe notification", "2M": "Monopole tracking complaint", "3A": "Unlicensed/illegal/improper electrical work in progress",
    "4A": "Illegal hotel rooms in residential buildings", "4B": "SEP – professional certification compliance audit",
    "4C": "Excavation tracking complaint", "4D": "Interior demo tracking complaint", "4F": "SST tracking complaint",
    "4G": "Illegal conversion no access follow-up", "4J": "M.A.R.C.H. program (interagency)", "4K": "CSC – DOB violation – elevator",
    "4L": "CSC – high-rise", "4M": "CSC – low-rise", "4N": "Retaining wall tracking complaint", "4P": "Legal/padlock",
    "4W": "Woodside settlement project", "5A": "Request for joint FDNY/DOB inspection", "5B": "Non-compliance with lightweight materials",
    "5C": "Structural stability impacted – new building under construction", "5E": "Amusement ride accident/incident",
    "5F": "Compliance inspection", "5G": "Unlicensed/illegal activity – gas work",
}

_SLA_BAR_CLASSES = {"252": "Tavern/bar (on-premises)", "340": "Restaurant", "341": "Restaurant/tavern",
                    "241": "Hotel", "242": "Club", "244": "Cabaret", "246": "Catering", "247": "Ballpark",
                    "342": "Eating place (wine/beer)", "343": "Tavern (wine/beer)", "247": "Sports venue"}


def _place(store, key):
    if key not in store:
        store[key] = {"key": key, "bbl": None, "address": None, "cd": None, "sources": {}}
    return store[key]


def collect_places(end=None):
    end = end or data_end()
    places = {}                 # key -> place
    addr_to_key = {}            # addr_key -> place key (bbl)
    locator = DistrictLocator()

    # ---- 311 by BBL, last 14 days --------------------------------------
    # Group by BBL ONLY. Grouping by address too lets a lot with several
    # spellings (or a NYCHA campus with many addresses) slip under the HAVING
    # threshold and undercounts it -- verify.py caught 37 vs 66 at Benchley Pl.
    s, e = window(end, W311)
    rows = soql_all("erm2-nwe9",
                    select="bbl, count(*) as n, "
                           "count(distinct date_trunc_ymd(created_date)) as days, "
                           "count(distinct complaint_type) as types",
                    where=f"{where_between('created_date', s, e)} AND bbl IS NOT NULL",
                    group="bbl", having=f"count(*) >= {MIN_311}", order="count(*) DESC", page=50000)
    print(f"[places] 311: {len(rows)} BBLs with >= {MIN_311} calls in {W311}d")
    by_bbl = {}
    for r in rows:
        by_bbl[r["bbl"]] = {"n": int(r["n"]), "days": int(r.get("days") or 0), "types": int(r.get("types") or 0),
                            "address": None, "cd": None, "boro": None}
    # most-used address + community board per BBL (batched)
    keys = list(by_bbl.keys())
    for i in range(0, len(keys), 150):
        batch = keys[i:i + 150]
        inlist = ",".join(f"'{k}'" for k in batch)
        ar = soql("erm2-nwe9", select="bbl, incident_address, borough, community_board, count(*) as n",
                  where=f"{where_between('created_date', s, e)} AND bbl in({inlist})",
                  group="bbl, incident_address, borough, community_board", order="count(*) DESC", limit=20000)
        for r in ar:
            p = by_bbl.get(r["bbl"])
            if not p:
                continue
            if not p["address"] and r.get("incident_address"):
                p["address"] = r["incident_address"]
                p["boro"] = r.get("borough")
            if not p["cd"]:
                p["cd"] = cd_from_311(r.get("community_board"))
    for b, p in by_bbl.items():
        pl = _place(places, b)
        pl["bbl"] = b
        pl["address"] = p["address"]
        pl["cd"] = p["cd"]
        pl["boro"] = p["boro"]
        pl["sources"]["311"] = {"n14": p["n"], "days": p["days"], "types_n": p["types"]}
        h, st = split_311_address(p["address"])
        ak = addr_key(h, st, p["boro"] or b[0])
        if ak:
            addr_to_key[ak] = b

    # prior 12 weeks + type breakdown for the 311 candidates (batched)
    ps, pe = window(s - timedelta(days=1), W311_PRIOR)
    for i in range(0, len(keys), 150):
        batch = keys[i:i + 150]
        inlist = ",".join(f"'{k}'" for k in batch)
        prior = soql("erm2-nwe9", select="bbl, count(*) as n",
                     where=f"{where_between('created_date', ps, pe)} AND bbl in({inlist})",
                     group="bbl", limit=5000)
        for r in prior:
            places[r["bbl"]]["sources"]["311"]["prior84"] = int(r["n"])
        types = soql("erm2-nwe9", select="bbl, complaint_type, descriptor, count(*) as n",
                     where=f"{where_between('created_date', s, e)} AND bbl in({inlist})",
                     group="bbl, complaint_type, descriptor", order="count(*) DESC", limit=20000)
        tb = defaultdict(list)
        for r in types:
            tb[r["bbl"]].append({"type": r["complaint_type"], "descriptor": r.get("descriptor"), "n": int(r["n"])})
        for b, lst in tb.items():
            places[b]["sources"]["311"]["breakdown"] = lst[:6]
    for b in keys:
        src = places[b]["sources"]["311"]
        prior = src.get("prior84", 0)
        src["prior84"] = prior
        src["prior_per14"] = round(prior / (W311_PRIOR / W311), 1)
        src["ratio"] = round(src["n14"] / max(src["prior_per14"], 1.0), 1)
        src["query"] = (f"https://data.cityofnewyork.us/resource/erm2-nwe9.json?$where=bbl='{b}' AND "
                        f"created_date >= '{iso(s)}'&$order=created_date DESC")

    # ---- HPD violations, last 30 days --------------------------------------
    s, e = window(end, WHPD_V)
    rows = soql_all("wvxf-dwi5", select="boroid, block, lot, class, housenumber, streetname, communityboard, count(*) as n",
                    where=f"{where_between('novissueddate', s, e)}",
                    group="boroid, block, lot, class, housenumber, streetname, communityboard", page=50000)
    agg = defaultdict(lambda: {"total": 0, "C": 0, "B": 0, "A": 0, "addr": None, "cd": None})
    for r in rows:
        b = bbl_from_parts(r.get("boroid"), r.get("block"), r.get("lot"))
        if not b:
            continue
        a = agg[b]
        n = int(r["n"])
        a["total"] += n
        a[r.get("class", "A")] = a.get(r.get("class", "A"), 0) + n
        if not a["addr"] and r.get("housenumber"):
            a["addr"] = f"{r['housenumber']} {r.get('streetname', '')}".strip()
            a["boro"] = r.get("boroid")
        if not a["cd"]:
            a["cd"] = cd_from_num(r.get("boroid"), r.get("communityboard"))
    kept = 0
    for b, a in agg.items():
        if a["total"] < 8 and a["C"] < 4:
            continue
        pl = _place(places, b)
        pl["bbl"] = b
        pl["address"] = pl["address"] or a["addr"]
        pl["cd"] = pl["cd"] or a["cd"]
        pl["boro"] = pl.get("boro") or a.get("boro")
        pl["sources"]["hpd_violations"] = {"n30": a["total"], "class_c": a["C"], "class_b": a["B"],
                                           "query": f"https://data.cityofnewyork.us/resource/wvxf-dwi5.json?$where=boroid='{b[0]}' AND block='{int(b[1:6])}' AND lot='{int(b[6:])}' AND novissueddate >= '{iso(s)}'"}
        if a["addr"]:
            ak = addr_key(*a["addr"].split(" ", 1), a.get("boro") or b[0]) if " " in a["addr"] else None
            if ak:
                addr_to_key.setdefault(ak, b)
        kept += 1
    print(f"[places] HPD violations: {len(agg)} BBLs with violations, {kept} kept")

    # ---- HPD complaints, last 14 days -----------------------------------------
    s, e = window(end, WHPD_C)
    rows = soql_all("ygpa-z7cr", select="borough, block, lot, house_number, street_name, community_board, count(*) as n",
                    where=where_between("received_date", s, e),
                    group="borough, block, lot, house_number, street_name, community_board",
                    having="count(*) >= 5", page=50000)
    kept = 0
    for r in rows:
        b = bbl_from_parts(r.get("borough"), r.get("block"), r.get("lot"))
        if not b:
            continue
        pl = _place(places, b)
        pl["bbl"] = b
        pl["address"] = pl["address"] or f"{r.get('house_number', '')} {r.get('street_name', '')}".strip()
        pl["cd"] = pl["cd"] or cd_from_num(r.get("borough"), r.get("community_board"))
        pl["boro"] = pl.get("boro") or r.get("borough")
        src = pl["sources"].setdefault("hpd_complaints", {"n14": 0})
        src["n14"] += int(r["n"])
        src["query"] = (f"https://data.cityofnewyork.us/resource/ygpa-z7cr.json?$where=borough='{r.get('borough')}' AND "
                        f"block='{r.get('block')}' AND lot='{r.get('lot')}' AND received_date >= '{iso(s)}'")
        kept += 1
    print(f"[places] HPD complaints: {kept} building rows with >= 5 complaints in {WHPD_C}d")

    # ---- DOB complaints, last 30 days (text dates; filter client-side) ----------
    s, e = window(end, WDOB)
    months = sorted({(s + timedelta(days=i)).strftime("%m/%%/%Y") for i in range(WDOB)})
    like = " OR ".join(f"date_entered like '{m}'" for m in months)
    rows = soql_all("eabe-havv", select="bin, house_number, house_street, zip_code, community_board, complaint_category, date_entered, complaint_number",
                    where=f"({like})", page=50000, max_rows=100000)
    dob = defaultdict(lambda: {"n": 0, "cats": defaultdict(int), "cd": None, "addr": None, "nums": []})
    for r in rows:
        try:
            m, d, y = r["date_entered"].split("/")
            from datetime import date as _d
            dt = _d(int(y), int(m), int(d))
        except Exception:
            continue
        if not (s <= dt < e):
            continue
        cd = cd_from_3digit(r.get("community_board"))
        boro = (cd or "?")[0]
        ak = addr_key(r.get("house_number"), r.get("house_street"), boro)
        if not ak:
            continue
        a = dob[ak]
        a["n"] += 1
        a["cats"][r.get("complaint_category") or "?"] += 1
        a["cd"] = a["cd"] or cd
        a["addr"] = a["addr"] or f"{r.get('house_number')} {r.get('house_street')}"
        a["nums"].append(r.get("complaint_number"))
    kept = 0
    for ak, a in dob.items():
        key = addr_to_key.get(ak)
        if not key and a["n"] < 3:
            continue
        pl = _place(places, key or f"A|{ak}")
        pl["address"] = pl["address"] or a["addr"]
        pl["cd"] = pl["cd"] or a["cd"]
        cats = sorted(a["cats"].items(), key=lambda kv: -kv[1])
        pl["sources"]["dob_complaints"] = {
            "n30": a["n"],
            "categories": [{"code": c, "label": DOB_CATEGORIES.get(c, c), "n": n} for c, n in cats[:4]],
            "query": f"https://data.cityofnewyork.us/resource/eabe-havv.json?$where=house_number='{a['addr'].split(' ',1)[0]}' AND upper(house_street)='{a['addr'].split(' ',1)[1].upper() if ' ' in a['addr'] else ''}'&$order=dobrundate DESC",
        }
        kept += 1
    print(f"[places] DOB complaints: {len(dob)} addresses, {kept} joined/kept")

    # ---- ECB violations, last 30 days -------------------------------------------
    s, e = window(end, WECB)
    rows = soql_all("6bgk-3dad", select="boro, block, lot, severity, violation_type, count(*) as n",
                    where=f"issue_date >= '{s.strftime('%Y%m%d')}' AND issue_date < '{e.strftime('%Y%m%d')}'",
                    group="boro, block, lot, severity, violation_type", page=50000)
    ecb = defaultdict(lambda: {"n": 0, "hazardous": 0, "types": defaultdict(int)})
    for r in rows:
        b = bbl_from_parts(r.get("boro"), r.get("block"), r.get("lot"))
        if not b:
            continue
        a = ecb[b]
        n = int(r["n"])
        a["n"] += n
        if (r.get("severity") or "").lower().startswith("haz") or "immediately" in (r.get("severity") or "").lower():
            a["hazardous"] += n
        a["types"][r.get("violation_type") or "?"] += n
    kept = 0
    for b, a in ecb.items():
        if b in places or a["n"] >= 4:
            pl = _place(places, b)
            pl["bbl"] = b
            pl["sources"]["ecb_violations"] = {
                "n30": a["n"], "hazardous": a["hazardous"],
                "types": [{"type": t, "n": n} for t, n in sorted(a["types"].items(), key=lambda kv: -kv[1])[:3]],
                "query": f"https://data.cityofnewyork.us/resource/6bgk-3dad.json?$where=boro='{b[0]}' AND block='{b[1:6]}' AND lot='{b[6:]}' AND issue_date >= '{s.strftime('%Y%m%d')}'",
            }
            kept += 1
    print(f"[places] ECB: {len(ecb)} BBLs, {kept} kept")

    # ---- Vacate orders, effective in last 60 days ------------------------------
    s, e = window(end, WVAC)
    rows = soql_all("tb8q-a3ar", select="bbl, boro_short_name, house_number, street_name, community_board, primary_vacate_reason, vacate_type, vacate_effective_date, number_of_vacated_units, actual_rescind_date",
                    where=where_between("vacate_effective_date", s, e), page=5000)
    for r in rows:
        b = r.get("bbl")
        if not b:
            continue
        pl = _place(places, b)
        pl["bbl"] = b
        pl["address"] = pl["address"] or f"{r.get('house_number', '')} {r.get('street_name', '')}".strip()
        pl["cd"] = pl["cd"] or cd_from_num(r.get("boro_short_name"), r.get("community_board"))
        src = pl["sources"].setdefault("vacate_orders", {"orders": []})
        src["orders"].append({"date": (r.get("vacate_effective_date") or "")[:10], "reason": r.get("primary_vacate_reason"),
                              "type": r.get("vacate_type"), "units": r.get("number_of_vacated_units"),
                              "rescinded": (r.get("actual_rescind_date") or "")[:10] or None})
        src["query"] = f"https://data.cityofnewyork.us/resource/tb8q-a3ar.json?bbl={b}"
    print(f"[places] vacate orders: {len(rows)} in {WVAC}d")

    # ---- Marshal evictions, last 60 days ------------------------------------------
    s, e = window(end, WEVI)
    rows = soql_all("6z8x-wfk4", select="bbl, eviction_address, borough, community_board, executed_date, residential_commercial_ind, ejectment",
                    where=where_between("executed_date", s, e), page=5000)
    ev = defaultdict(list)
    for r in rows:
        if r.get("bbl"):
            ev[r["bbl"]].append(r)
    kept = 0
    for b, lst in ev.items():
        res = [r for r in lst if (r.get("residential_commercial_ind") or "").lower().startswith("res")]
        if b not in places and len(res) < 2:
            continue
        pl = _place(places, b)
        pl["bbl"] = b
        pl["address"] = pl["address"] or lst[0].get("eviction_address")
        pl["cd"] = pl["cd"] or cd_from_num(lst[0].get("borough"), lst[0].get("community_board"))
        pl["sources"]["evictions"] = {"n60": len(lst), "residential": len(res),
                                      "dates": sorted({(r.get("executed_date") or "")[:10] for r in lst}, reverse=True)[:5],
                                      "query": f"https://data.cityofnewyork.us/resource/6z8x-wfk4.json?bbl={b}&$order=executed_date DESC"}
        kept += 1
    print(f"[places] evictions: {len(rows)} executed in {WEVI}d at {len(ev)} BBLs, {kept} kept")

    # ---- SLA pending liquor licenses (state), received last 60 days -------------------
    s, e = window(end, WSLA)
    rows = soql_all("f8i8-k2gm", base=NYS,
                    select="application_id, premises_county, class, description, legalname, actual_address_of_premises, city, zip_code, received_date, status, georeference",
                    where=f"{where_between('received_date', s, e)} AND premises_county in('New York','Kings','Queens','Bronx','Richmond')",
                    page=5000)
    sla_by_cd = defaultdict(list)
    kept = 0
    for r in rows:
        g = r.get("georeference") or {}
        coords = g.get("coordinates") if isinstance(g, dict) else None
        cd = locator.locate(coords[0], coords[1]) if coords else None
        if not cd:
            continue
        item = {"name": r.get("legalname"), "address": r.get("actual_address_of_premises"), "type": r.get("description"),
                "class": r.get("class"), "received": (r.get("received_date") or "")[:10], "status": r.get("status"),
                "application_id": r.get("application_id")}
        sla_by_cd[cd].append(item)
        h, st = split_311_address((r.get("actual_address_of_premises") or "").upper())
        ak = addr_key(h, st, cd[0])
        key = addr_to_key.get(ak) if ak else None
        if key:
            pl = places[key]
            src = pl["sources"].setdefault("sla_pending", {"applications": []})
            src["applications"].append(item)
            src["query"] = f"https://data.ny.gov/resource/f8i8-k2gm.json?application_id={item['application_id']}"
            kept += 1
    print(f"[places] SLA pending: {len(rows)} NYC applications in {WSLA}d, {sum(len(v) for v in sla_by_cd.values())} located, {kept} joined to places")

    # ---- score + prune -----------------------------------------------------------------
    d14, d30, d60 = span(end, W311), span(end, WHPD_V), span(end, WVAC)
    out = []
    for key, pl in places.items():
        srcs = pl["sources"]
        if not pl.get("cd"):
            continue
        score = 0.0
        why = []
        s311 = srcs.get("311")
        if s311:
            score += min(s311["n14"] / 4.0, 5.0)
            if s311.get("ratio", 0) >= 3 and s311["n14"] >= 8:
                score += 1.5
                why.append(f"311 calls {s311['ratio']}x the building's pace over the prior 12 weeks")
            why.append(f"{s311['n14']} 311 calls {d14} ({s311.get('types_n', '?')} types over {s311.get('days', '?')} days)")
        hv = srcs.get("hpd_violations")
        if hv:
            score += min(hv["class_c"] / 3.0, 3.0) + min(hv["n30"] / 10.0, 2.0)
            why.append(f"{hv['n30']} HPD violations issued {d30} ({hv['class_c']} class C)")
        hc = srcs.get("hpd_complaints")
        if hc:
            score += min(hc["n14"] / 4.0, 2.5)
            why.append(f"{hc['n14']} HPD complaints {d14}")
        dc = srcs.get("dob_complaints")
        if dc:
            score += min(dc["n30"] * 1.2, 3.5)
            why.append(f"{dc['n30']} DOB complaints {d30}" + (f" ({dc['categories'][0]['label']})" if dc.get("categories") else ""))
        ec = srcs.get("ecb_violations")
        if ec:
            score += min(ec["n30"] * 0.6, 2.0) + (1.0 if ec["hazardous"] else 0)
            why.append(f"{ec['n30']} OATH/ECB violations {d30}" + (f", {ec['hazardous']} hazardous" if ec["hazardous"] else ""))
        vo = srcs.get("vacate_orders")
        if vo:
            score += 3.5
            o = vo["orders"][0]
            why.append(f"vacate order {o['date']} ({o['reason']}, {o['type']}, {o['units'] or '?'} units)")
        evx = srcs.get("evictions")
        if evx:
            score += 1.0 + min(evx["residential"] * 0.8, 2.5)
            why.append(f"{evx['n60']} marshal evictions {d60} ({evx['residential']} residential)")
        sla = srcs.get("sla_pending")
        if sla:
            score += 1.5
            why.append(f"liquor license pending with the SLA: {sla['applications'][0]['name']} ({sla['applications'][0]['type']}, received {sla['applications'][0]['received']})")
        # Convergence is counted across INDEPENDENT families, not raw feeds:
        # 311, HPD complaints and DOB complaints are all resident reports (the
        # latter two are largely 311 intake), so they count once together.
        FAMILIES = {"reports": ("311", "hpd_complaints", "dob_complaints"),
                    "inspections": ("hpd_violations", "ecb_violations"),
                    "enforcement": ("vacate_orders", "evictions"),
                    "licensing": ("sla_pending",)}
        fams = [f for f, keys in FAMILIES.items() if any(k in srcs for k in keys)]
        n_src = len(fams)
        score += (n_src - 1) * 2.0
        strong_single = (s311 and s311["n14"] >= 12) or vo or (hv and hv["class_c"] >= 10) or (dc and dc["n30"] >= 4)
        if n_src < 2 and not strong_single:
            continue
        pl["families"] = fams
        pl["score"] = round(score, 1)
        pl["n_sources"] = n_src
        pl["why"] = why
        pl["district"] = CD_NAMES[pl["cd"]]
        pl["borough"] = BORO_NAME[pl["cd"][0]]
        pl.pop("boro", None)
        out.append(pl)
    out.sort(key=lambda p: -p["score"])
    # Coordinates for the map: 311's own geocode per BBL, averaged over every
    # request it has for that lot in the last year.
    #
    # The Sept 7 2026 build died here: the one-year group-and-average over 311
    # stalled past the 180s read timeout on all four attempts. Socrata stalls
    # at random on erm2-nwe9 (the same query can take 0.3s or never return), and
    # a year-wide aggregate is the heaviest shape to retry. So ask in two passes:
    # the same average over 30 days, which covers nearly every lot, then page
    # the raw rows for the year and average here for whatever the short window
    # missed. Both take a second or two when the server is healthy, so they use
    # a 60s timeout with more attempts: a stalled request is dropped and re-sent
    # instead of eating three minutes a try. Still fails loud if all attempts
    # stall.
    ys, ye = window(end, 365)
    ys30, ye30 = window(end, 30)
    keys = [p["bbl"] for p in out if p.get("bbl")]
    coords = {}

    def bbl_in(batch):
        return "bbl in(" + ",".join(f"'{k}'" for k in batch) + ")"

    for i in range(0, len(keys), 150):
        batch = keys[i:i + 150]
        rows = soql("erm2-nwe9", select="bbl, avg(latitude) as lat, avg(longitude) as lon",
                    where=f"{where_between('created_date', ys30, ye30)} AND latitude IS NOT NULL AND {bbl_in(batch)}",
                    group="bbl", limit=5000, timeout=60, attempts=6)
        for r in rows:
            try:
                coords[r["bbl"]] = (round(float(r["lat"]), 6), round(float(r["lon"]), 6))
            except (TypeError, ValueError, KeyError):
                pass

    missing = [k for k in keys if k not in coords]
    for i in range(0, len(missing), 150):
        batch = missing[i:i + 150]
        acc = defaultdict(lambda: [0.0, 0.0, 0])
        rows = soql_all("erm2-nwe9", page=5000, max_rows=120000,
                        select="bbl, latitude, longitude",
                        where=f"{where_between('created_date', ys, ye)} AND latitude IS NOT NULL AND {bbl_in(batch)}",
                        order="unique_key", timeout=60, attempts=6)
        for r in rows:
            try:
                a = acc[r["bbl"]]
                a[0] += float(r["latitude"])
                a[1] += float(r["longitude"])
                a[2] += 1
            except (TypeError, ValueError, KeyError):
                pass
        for bbl, (slat, slon, n) in acc.items():
            if n:
                coords[bbl] = (round(slat / n, 6), round(slon / n, 6))
    print(f"[places] coordinates: {len(keys) - len(missing)} from 30d, "
          f"{len(coords) - (len(keys) - len(missing))} from the 365d fallback")

    for p in out:
        c = coords.get(p.get("bbl"))
        if c:
            p["lat"], p["lon"] = c
    print(f"[places] coordinates for {len(coords)} of {len(keys)} places")
    print(f"[places] {len(out)} places kept ({sum(1 for p in out if p['n_sources'] >= 2)} multi-family)")
    return {"as_of": iso(end), "places": out, "sla_by_cd": {k: sorted(v, key=lambda x: x["received"], reverse=True) for k, v in sla_by_cd.items()}}


if __name__ == "__main__":
    import json
    r = collect_places()
    for p in r["places"][:30]:
        print(p["score"], p["n_sources"], p["cd"], p["address"], "|", "; ".join(p["why"])[:160])
    json.dump(r, open("/tmp/places_dev.json", "w"), indent=1)
