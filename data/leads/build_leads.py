"""
Assemble the weekly leads: places (B) + district anomalies and novelty (C) +
district feeds (D), each checked for coverage (A), scored and ranked.

    python data/leads/build_leads.py            # full run
    python data/leads/build_leads.py --no-claude
    LEADS_AS_OF=2026-08-17 LEADS_CACHE=/tmp/c python data/leads/build_leads.py

Outputs (all committed):
    data/output/leads.json                  everything the site needs
    data/output/leads/YYYY-MM-DD.json       history snapshot (leads only)
    data/output/leads/YYYY-MM-DD.md         readable brief
    data/output/leads/latest.md

Every lead carries: the evidence (numbers + the exact source query),
a coverage verdict (articles found or "none found"), and a signal score.
Rank = signal x (1 - 0.75 x coverage). Where coverage could not be checked,
rank = signal and the lead says so.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (BORO_NAME, CD_NAMES, OUTPUT_DIR, cd_search_names, data_end, date_range, iso, neighborhoods_for,
                    save_json, span, window)  # noqa: E402
from coverage import CoverageChecker, TOPIC_KEYWORDS, query_for_place, query_for_topic  # noqa: E402
from feeds import collect_feeds  # noqa: E402
from places import collect_places  # noqa: E402
from seasonal import detect_novelty, detect_seasonal  # noqa: E402
from district_stats import collect_district_stats  # noqa: E402
from attention import collect_attention  # noqa: E402

N_PLACE_LEADS = 45
N_DISTRICT_LEADS = 45
N_NOVELTY = 10
COVERAGE_PENALTY = 0.75

LOW_VALUE_TYPES = {"Illegal Parking", "Blocked Driveway", "Request Large Bulky Item Collection",
                   "Street Light Condition", "Traffic Signal Condition", "Missed Collection",
                   "Missed Collection (All Materials)", "Derelict Vehicles", "Abandoned Vehicle",
                   "Broken Parking Meter", "Broken Muni Meter", "Street Sign - Missing", "Litter Basket Complaint",
                   "Litter Basket Request", "Dead/Dying Tree", "New Tree Request", "Overgrown Tree/Branches",
                   "Root/Sewer/Sidewalk Condition", "Electronics Waste Appointment", "Sweeping/Missed",
                   "Sweeping/Inadequate", "Lost Property", "Ferry Inquiry", "Water Conservation",
                   "Highway Sign - Damaged", "Highway Sign - Missing", "Municipal Parking Facility"}


def _place_signal(p):
    return min(1.0, p["score"] / 26.0)


def _district_signal(a):
    s = min(1.0, a["z"] / 30.0) * 0.7
    if a["hazard"]:
        s += 0.1
    if a["accelerating"]:
        s += 0.15
    if a["complaint_type"] in LOW_VALUE_TYPES:
        s *= 0.55
    if a["seasonal_years"] == 0:
        s *= 0.85   # no multi-year history to compare against
    if a.get("concentrated"):
        s *= 0.6    # one address is most of it: could be one caller
    return min(1.0, s)


def _novelty_signal(n):
    s = min(1.0, n["cur56"] / 200.0) * 0.6
    if (n.get("top_cd_share") or 0) >= 0.3:
        s += 0.4
    return min(1.0, s)


def _rank(signal, cov):
    if not cov or cov.get("score") is None:
        return round(signal, 3)
    return round(signal * (1 - COVERAGE_PENALTY * cov["score"]), 3)


def _tidy_addr(a):
    return re.sub(r"\s+", " ", (a or "").strip()).title()


def _place_headline(p):
    s = p["sources"]
    addr = _tidy_addr(p.get("address"))
    parts = []
    if "vacate_orders" in s:
        parts.append("vacate order")
    if "311" in s and s["311"]["n14"] >= 8:
        parts.append(f"{s['311']['n14']} 311 calls in two weeks")
    if "hpd_violations" in s and s["hpd_violations"]["class_c"] >= 3:
        parts.append(f"{s['hpd_violations']['class_c']} class C violations")
    if "evictions" in s and s["evictions"]["residential"] >= 2:
        parts.append(f"{s['evictions']['residential']} evictions")
    if "dob_complaints" in s and s["dob_complaints"]["n30"] >= 2:
        parts.append(f"{s['dob_complaints']['n30']} DOB complaints")
    if "sla_pending" in s:
        parts.append("liquor license pending")
    if not parts:
        parts.append(", ".join(k.replace("_", " ") for k in s))
    return f"{addr}: {'; '.join(parts[:3])}"


def _district_headline(a):
    t = a["complaint_type"]
    how = f"{a['ratio']}x normal" if a["ratio"] < 10 else f"{int(a['ratio'])}x normal"
    return f"{a['district']}: {t} complaints {how} ({a['cur28']} in the four weeks to {END_LABEL})"


def _place_terms(p):
    addr = _tidy_addr(p.get("address"))
    m = re.match(r"([\d\-]+[A-Za-z]?)\s+(.+)", addr)
    if not m:
        return [addr.lower()]
    num, st = m.group(1), m.group(2)
    st_short = re.sub(r"\b(Avenue|Street|Boulevard|Place|Road)\b", lambda x: {"Avenue": "Ave", "Street": "St", "Boulevard": "Blvd", "Place": "Pl", "Road": "Rd"}[x.group(1)], st)
    return [f"{num} {st}".lower(), f"{num} {st_short}".lower()]


def enrich_anomalies(anomalies, start):
    """Concentration check for each district anomaly: how many distinct
    addresses, the top address's share and the top descriptor. One persistent
    caller at one address is a different story from 1,500 addresses filed once
    each (Howard Beach, Aug 2026: 1,527 addresses, 1,547 illegal-conversion
    complaints -- a filing campaign, not a super-caller)."""
    from common import soql
    for a in anomalies:
        cb = f"{int(a['cd'][1:]):02d} {BORO_NAME[a['cd'][0]].upper()}"
        t = a["complaint_type"].replace("'", "''")
        where = f"community_board='{cb}' AND complaint_type='{t}' AND created_date >= '{start}'"
        try:
            tot = soql("erm2-nwe9", select="count(distinct incident_address) as addrs, count(*) as n", where=where)[0]
            top = soql("erm2-nwe9", select="incident_address, count(*) as n", where=where + " AND incident_address IS NOT NULL",
                       group="incident_address", order="count(*) DESC", limit=1)
            desc = soql("erm2-nwe9", select="descriptor, count(*) as n", where=where, group="descriptor", order="count(*) DESC", limit=1)
        except Exception as e:  # enrichment only; never fail the build
            print(f"  [enrich] {a['cd']} {a['complaint_type']}: {e}", file=sys.stderr)
            continue
        n = int(tot.get("n") or 0) or 1
        a["distinct_addresses"] = int(tot.get("addrs") or 0)
        if top:
            a["top_address"] = re.sub(r"\s+", " ", top[0].get("incident_address") or "").title()
            a["top_address_share"] = round(int(top[0]["n"]) / n, 2)
        if desc:
            a["top_descriptor"] = desc[0].get("descriptor")
            a["top_descriptor_share"] = round(int(desc[0]["n"]) / n, 2)
        a["concentrated"] = bool(a.get("top_address_share", 0) >= 0.4 and a["cur28"] >= 20)


END_LABEL = ""


def build(no_claude=False):
    global END_LABEL
    end = data_end()
    END_LABEL = f"{end.strftime('%b')} {end.day}"
    D28, D56, D14 = span(end, 28), span(end, 56), span(end, 14)
    print(f"=== leads build as of {iso(end)} ===")
    seasonal = detect_seasonal(end)
    enrich_anomalies(seasonal["anomalies"], seasonal["window"]["start"])
    novelty = detect_novelty(end)
    places = collect_places(end)
    feeds = collect_feeds(end)
    dstats = collect_district_stats(end)
    attention = collect_attention()

    checker = CoverageChecker()
    leads = []

    # ---- place leads --------------------------------------------------------
    for p in places["places"][:N_PLACE_LEADS]:
        boro = BORO_NAME[p["cd"][0]]
        q = query_for_place(p.get("address"), boro, p["cd"])
        cov = checker.check(q, place_terms=_place_terms(p), topic_terms=(), must_match=_place_terms(p))
        sig = _place_signal(p)
        leads.append({
            "id": f"place:{p['key']}",
            "kind": "place",
            "headline": _place_headline(p),
            "cd": p["cd"], "district": p["district"], "borough": boro,
            "address": _tidy_addr(p.get("address")), "bbl": p.get("bbl"),
            "signal": round(sig, 3), "coverage": cov, "rank": _rank(sig, cov),
            "families": p.get("families", []), "n_sources": p["n_sources"],
            "why": p["why"], "sources": p["sources"],
        })

    # ---- district anomalies -----------------------------------------------------
    for a in seasonal["anomalies"][:N_DISTRICT_LEADS]:
        q = query_for_topic(a["cd"], a["complaint_type"])
        kws = TOPIC_KEYWORDS.get(a["complaint_type"]) or [a["complaint_type"].lower()]
        cov = checker.check(q, place_terms=[n for n in cd_search_names(a["cd"])], topic_terms=kws, must_match=[k.lower() for k in kws])
        sig = _district_signal(a)
        leads.append({
            "id": f"district:{a['cd']}:{a['complaint_type']}",
            "kind": "district",
            "headline": _district_headline(a),
            "cd": a["cd"], "district": a["district"], "borough": BORO_NAME[a["cd"][0]],
            "complaint_type": a["complaint_type"],
            "signal": round(sig, 3), "coverage": cov, "rank": _rank(sig, cov),
            "why": [f"{a['cur28']} complaints {D28} vs an expected {a['expected']}"
                    + (f" (same weeks in prior years, drift-adjusted: {a['exp_seasonal']}; recent 8-week pace: {a['exp_recent']})" if a['exp_seasonal'] is not None else f" (recent 8-week pace: {a['exp_recent']}; no prior-year history for this type)"),
                    f"{span(end, 7)}: {a['last7']} vs {a['prior3wk_avg']}/week over the three weeks before" + (" — accelerating" if a["accelerating"] else ""),
                    ] + ([f"spread across {a['distinct_addresses']} addresses" + (f"; top address {a['top_address']} has {int(round(a['top_address_share']*100))}%" if a.get("top_address") else "") + (" — likely one caller" if a.get("concentrated") else "") + (f"; top descriptor: {a['top_descriptor']} ({int(round(a['top_descriptor_share']*100))}%)" if a.get("top_descriptor") else "")] if a.get("distinct_addresses") is not None else []),
            "stats": a,
            "query": (f"https://data.cityofnewyork.us/resource/erm2-nwe9.json?$where=complaint_type='{a['complaint_type']}' AND "
                      f"community_board='{int(a['cd'][1:]):02d} {BORO_NAME[a['cd'][0]].upper()}' AND created_date >= '{seasonal['window']['start']}'&$order=created_date DESC"),
        })

    # ---- novelty ---------------------------------------------------------------
    for n in novelty["novel"][:N_NOVELTY]:
        top = n["by_cd"][0] if n.get("by_cd") else None
        phrase = re.sub(r"\s*\(.*?\)\s*", " ", n["descriptor"]).strip()
        q = f'"{phrase}" "New York"'
        cov = checker.check(q, place_terms=(), topic_terms=[phrase.lower()], must_match=[phrase.lower()])
        sig = _novelty_signal(n)
        leads.append({
            "id": f"novel:{n['complaint_type']}:{n['descriptor']}",
            "kind": "novelty",
            "headline": f"New in 311: \"{n['descriptor']}\" ({n['complaint_type']}) — {n['cur56']} reports {D56}" + (f", led by {top['district']}" if top else ""),
            "cd": top["cd"] if top else None, "district": top["district"] if top else "Citywide",
            "borough": BORO_NAME[top["cd"][0]] if top else None,
            "signal": round(sig, 3), "coverage": cov, "rank": _rank(sig, cov),
            "why": [f"{n['cur56']} reports {D56} vs {n['base_same_window']} in the same weeks of the prior two years (volume-adjusted)" + (f" — {n['rate_ratio']}x" if n.get("rate_ratio") else " — not seen in either prior year"),
                    "top districts: " + ", ".join(f"{x['district']} ({x['n']})" for x in n.get("by_cd", [])[:4])],
            "by_cd": n.get("by_cd", []),
        })

    leads.sort(key=lambda l: -l["rank"])
    print(f"[leads] {len(leads)} candidate leads; coverage backend {'ok' if checker.backend_ok else 'DOWN'}; {checker.calls} searches")

    # ---- optional Claude synthesis on the top leads ------------------------------
    synth_meta = {"used": False}
    if not no_claude and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            synth_meta = synthesize(leads[:20])
        except Exception as e:  # never let synthesis kill the run
            print(f"[leads] synthesis failed: {e}", file=sys.stderr)
            synth_meta = {"used": False, "error": str(e)[:200]}

    # ---- district rollup -----------------------------------------------------------
    by_cd = defaultdict(lambda: {"leads": [], "n_places": 0, "n_anomalies": 0, "top_rank": 0.0})
    for l in leads:
        if l.get("cd"):
            d = by_cd[l["cd"]]
            d["leads"].append(l["id"])
            d["top_rank"] = max(d["top_rank"], l["rank"])
    for p in places["places"]:
        by_cd[p["cd"]]["n_places"] += 1
    for a in seasonal["anomalies"]:
        by_cd[a["cd"]]["n_anomalies"] += 1
    districts = {}
    for cd in CD_NAMES:
        d = by_cd.get(cd, {"leads": [], "n_places": 0, "n_anomalies": 0, "top_rank": 0.0})
        districts[cd] = {
            "cd": cd, "name": CD_NAMES[cd], "borough": BORO_NAME[cd[0]],
            "neighborhoods": [n.title() for n in neighborhoods_for(cd)[:6]],
            "lead_ids": d["leads"], "n_places": d["n_places"], "n_anomalies": d["n_anomalies"],
            "top_rank": round(d["top_rank"], 3),
            "anomalies": [a for a in seasonal["anomalies"] if a["cd"] == cd][:12],
            "three11": seasonal["district_311"][cd],
            "trends": {k: v["by_cd"][cd] for k, v in dstats.items() if k != "as_of"},
            "attention": attention.get(cd),
            "places": [{"key": p["key"], "address": _tidy_addr(p.get("address")), "score": p["score"],
                        "n_sources": p["n_sources"], "why": p["why"], "lat": p.get("lat"), "lon": p.get("lon")} for p in places["places"] if p["cd"] == cd][:25],
            "sla_pending": places["sla_by_cd"].get(cd, [])[:12],
            "land_use": feeds["land_use"].get(cd, [])[:8],
            "city_record": feeds["city_record"].get(cd, [])[:8],
            "sheds": feeds["sheds"].get(cd),
        }

    out = {
        "as_of": iso(end),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "week_labels": seasonal["week_labels"],
        "trend_meta": {k: {kk: vv for kk, vv in v.items() if kk != "by_cd"} for k, v in dstats.items() if k != "as_of"},
        "windows": {"place_311_days": 14, "district_days": seasonal["window"]["days"],
                    "dates": {"d7": span(end, 7), "d14": D14, "d28": D28, "d30": span(end, 30), "d56": D56, "d60": span(end, 60),
                              "prior30": date_range(window(end, 30)[0] - __import__('datetime').timedelta(days=30), window(end, 30)[0] - __import__('datetime').timedelta(days=1)),
                              "prior28": date_range(window(end, 28)[0] - __import__('datetime').timedelta(days=28), window(end, 28)[0] - __import__('datetime').timedelta(days=1)),
                              "prior60": date_range(window(end, 60)[0] - __import__('datetime').timedelta(days=60), window(end, 60)[0] - __import__('datetime').timedelta(days=1))},
                    "seasonal_windows": seasonal["seasonal_windows"], "novelty_days": novelty["window"]["days"],
                    "coverage_lookback_days": 42},
        "coverage_backend": "ok" if checker.backend_ok else "unavailable",
        "synthesis": synth_meta,
        "counts": {"leads": len(leads), "places_candidates": len(places["places"]),
                   "district_anomalies": len(seasonal["anomalies"]), "novel_descriptors": len(novelty["novel"]),
                   "sla_pending_located": sum(len(v) for v in places["sla_by_cd"].values())},
        "leads": leads,
        "taxonomy_changes": novelty.get("taxonomy_changes", []),
        "districts": districts,
    }
    save_json(os.path.join(OUTPUT_DIR, "leads.json"), out)
    save_json(os.path.join(OUTPUT_DIR, "leads", f"{iso(end)}.json"),
              {"as_of": iso(end), "generated": out["generated"], "leads": leads})
    md = render_md(out)
    with open(os.path.join(OUTPUT_DIR, "leads", f"{iso(end)}.md"), "w") as f:
        f.write(md)
    with open(os.path.join(OUTPUT_DIR, "leads", "latest.md"), "w") as f:
        f.write(md)
    print(f"[leads] wrote leads.json ({len(leads)} leads) and leads/{iso(end)}.md")
    return out


# --- optional Claude synthesis ----------------------------------------------------

SYNTH_SYSTEM = """You are the desk editor for a New York City local-news early-warning system. You receive machine-detected leads: each is a building or a community district plus the raw evidence (counts from 311, HPD, DOB, OATH, marshal evictions, state liquor-license filings) and a coverage check (news articles found, if any).

Every count in the evidence carries its exact date range. When you cite a number, cite its date range in words (e.g. "between Aug 3 and Aug 16") — never a bare "in 14 days" or "in 30 days". Do not use the words "surge", "spike", "explosion" or "soar"; say what changed and by how much.

For each lead write:
- "headline": one plain sentence, sentence case, no colon-hype, naming the place or district and the specific thing the numbers show, with the date range. Never invent facts; use only the evidence given.
- "why_now": one or two sentences on why a reporter should look this week and what the first call is (the building's owner via HPD registration, the community board district manager, the council office, DOB inspection records). Say plainly when the pattern could be one persistent complainant or a data artifact.
- "confidence": "high", "medium" or "low" for whether this is a real story rather than noise.
Write in a measured newsroom register. No exclamation points. Use "New York City" in full. Straight quotes only."""


def synthesize(leads):
    import anthropic
    client = anthropic.Anthropic()
    payload = []
    for l in leads:
        payload.append({"id": l["id"], "kind": l["kind"], "district": l["district"], "borough": l.get("borough"),
                        "address": l.get("address"), "auto_headline": l["headline"], "evidence": l["why"],
                        "coverage_hits": l["coverage"]["hits"], "coverage_titles": [a["title"] for a in l["coverage"]["articles"][:3]]})
    schema = {"type": "object", "properties": {"leads": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "headline": {"type": "string"},
                                         "why_now": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
        "required": ["id", "headline", "why_now", "confidence"], "additionalProperties": False}}},
        "required": ["leads"], "additionalProperties": False}
    with client.beta.messages.stream(
        model="claude-opus-5-5", max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=SYNTH_SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": "Leads as JSON:\n" + json.dumps(payload, ensure_ascii=False)}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        return {"used": False, "error": "refusal"}
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    by_id = {x["id"]: x for x in data.get("leads", [])}
    n = 0
    for l in leads:
        s = by_id.get(l["id"])
        if s:
            l["headline_auto"] = l["headline"]
            l["headline"] = s["headline"]
            l["why_now"] = s["why_now"]
            l["confidence"] = s["confidence"]
            n += 1
    u = resp.usage
    print(f"[leads] synthesis: {n} leads via {resp.model}; tokens in {u.input_tokens} out {u.output_tokens}")
    return {"used": True, "model": resp.model, "n": n, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens}


# --- markdown brief -------------------------------------------------------------------

def render_md(out):
    L = []
    L.append(f"# Neighborhood leads — week of {out['as_of']}\n")
    L.append(f"Generated {out['generated']}. {out['counts']['leads']} leads from {out['counts']['places_candidates']} candidate places, "
             f"{out['counts']['district_anomalies']} district anomalies and {out['counts']['novel_descriptors']} new complaint descriptors. "
             f"Coverage check: {out['coverage_backend']}.\n")
    L.append("Rank = signal x (1 - 0.75 x coverage). A strong signal nobody has written about outranks a stronger one already in the papers.\n")
    for i, l in enumerate(out["leads"][:40], 1):
        cov = l["coverage"]
        if cov.get("score") is None:
            covs = "coverage: not checked"
        elif cov["hits"] == 0:
            covs = "coverage: none found"
        else:
            covs = f"coverage: {cov['hits']} article(s) — " + "; ".join(f"{a['outlet']}: {a['title'][:70]}" for a in cov["articles"][:2])
        L.append(f"## {i}. {l['headline']}")
        L.append(f"*{l['kind']} · {l['borough'] or ''} · {l['district']} · rank {l['rank']} (signal {l['signal']}) · {covs}*\n")
        if l.get("why_now"):
            L.append(l["why_now"] + (f" (confidence: {l['confidence']})" if l.get("confidence") else "") + "\n")
        for w in l["why"]:
            L.append(f"- {w}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-claude", action="store_true")
    args = ap.parse_args()
    build(no_claude=args.no_claude)
