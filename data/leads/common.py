"""
Shared helpers for the leads pipeline: Socrata fetch (fail loud), date windows,
community-district code normalization, BBL/address keys and a dependency-free
point-in-polygon against the district GeoJSON.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(HERE)
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
sys.path.insert(0, DATA_DIR)
from config import CD_NAMES, NEIGHBORHOOD_TO_CD  # noqa: E402

NYC = "https://data.cityofnewyork.us/resource"
NYS = "https://data.ny.gov/resource"
UA = {"User-Agent": "NYC-Neighborhood-Story-Finder/2.0 (leads pipeline)"}

APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")
if APP_TOKEN:
    UA["X-App-Token"] = APP_TOKEN

BORO_NUM = {"MANHATTAN": "1", "BRONX": "2", "BROOKLYN": "3", "QUEENS": "4", "STATEN ISLAND": "5",
            "NEW YORK": "1", "KINGS": "3", "RICHMOND": "5",
            "MN": "1", "BX": "2", "BK": "3", "QN": "4", "SI": "5",
            "1": "1", "2": "2", "3": "3", "4": "4", "5": "5"}
BORO_NAME = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}


class FetchError(RuntimeError):
    pass


_CACHE_DIR = os.environ.get("LEADS_CACHE")  # dev only: cache responses on disk


def _cache_path(url, params):
    import hashlib
    key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    return os.path.join(_CACHE_DIR, key + ".json")


def fetch(url, params=None, attempts=4, timeout=180):
    """GET JSON with retry on timeouts/5xx. Raises on final failure — the
    pipeline must fail loudly rather than publish a week with holes."""
    if _CACHE_DIR:
        cp = _cache_path(url, params)
        if os.path.exists(cp):
            return json.load(open(cp))
        data = _fetch(url, params, attempts, timeout)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        json.dump(data, open(cp, "w"))
        return data
    return _fetch(url, params, attempts, timeout)


def _fetch(url, params, attempts, timeout):
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.exceptions.HTTPError(f"{r.status_code} for {url}", response=r)
            if r.status_code == 403 and "authentication_required" in r.text:
                raise requests.exceptions.HTTPError(f"403 throttled for {url}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError, json.JSONDecodeError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            # Keyless Socrata answers a throttled IP with 403
            # "authentication_required", not 429 -- that one is transient.
            throttled = status == 403 and "authentication_required" in (getattr(e.response, "text", "") or "")
            if status is not None and status < 500 and status != 429 and not throttled:
                raise FetchError(f"{status} from {url} params={params}: {getattr(e.response, 'text', '')[:300]}")
            last = e
            if i < attempts - 1:
                wait = 5 * (2 ** i)
                print(f"  retry {i+1}/{attempts-1} in {wait}s: {url} ({type(e).__name__})", file=sys.stderr)
                time.sleep(wait)
    raise FetchError(f"gave up on {url}: {last}")


def soql(dataset, base=NYC, timeout=180, attempts=4, **params):
    """Convenience: soql('erm2-nwe9', select='...', where='...', group='...').
    timeout/attempts: lookups that come back in a second or two when Socrata is
    healthy should pass a short timeout and more attempts, so a stalled request
    is abandoned in a minute rather than three."""
    p = {}
    for k, v in params.items():
        if v is None:
            continue
        p["$" + k] = v
    return fetch(f"{base}/{dataset}.json", p, attempts=attempts, timeout=timeout)


def soql_all(dataset, base=NYC, page=5000, max_rows=200000, **params):
    """Page through a query until short page or max_rows."""
    out = []
    offset = 0
    while True:
        rows = soql(dataset, base=base, limit=page, offset=offset, **params)
        out.extend(rows)
        if len(rows) < page or len(out) >= max_rows:
            break
        offset += page
    return out


# --- dates ---------------------------------------------------------------

def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(d, datetime) else d.strftime("%Y-%m-%d")


_DATA_END = None


def data_end():
    """Latest FULL day of 311 data. Ask the dataset for max(created_date): if the
    newest record is before 20:00 that day, the day is still filling in and we
    end on the day before. (Aug 18 2026: max was 02:55 on the 17th -- treating
    the 17th as complete would have undercounted every 'last 7 days' figure.)"""
    global _DATA_END
    override = os.environ.get("LEADS_AS_OF")
    if override:
        return date.fromisoformat(override)
    if _DATA_END:
        return _DATA_END
    try:
        mx = fetch(f"{NYC}/erm2-nwe9.json", {"$select": "max(created_date) as m"})[0]["m"]
        mdt = datetime.fromisoformat(mx[:19])
        end = mdt.date() if mdt.hour >= 20 else mdt.date() - timedelta(days=1)
    except Exception as e:
        print(f"[common] could not read 311 max date ({e}); using yesterday", file=sys.stderr)
        end = date.today() - timedelta(days=1)
    end = min(end, date.today() - timedelta(days=1))
    print(f"[common] data end = {end}")
    _DATA_END = end
    return end


def window(end, days):
    """[start, end) half-open date range covering `days` days ending on `end` inclusive."""
    start = end - timedelta(days=days - 1)
    return start, end + timedelta(days=1)


def where_between(field, start, end_exclusive):
    return f"{field} >= '{iso(start)}' AND {field} < '{iso(end_exclusive)}'"


# --- community district codes ---------------------------------------------

def cd_from_311(cb):
    """'10 QUEENS' -> '410'. Unassigned/‘0 Unspecified’ -> None."""
    if not cb:
        return None
    m = re.match(r"\s*(\d{1,2})\s+([A-Z ]+)$", cb.strip().upper())
    if not m:
        return None
    n = int(m.group(1))
    boro = BORO_NUM.get(m.group(2).strip())
    if not boro or n == 0:
        return None
    code = f"{boro}{n:02d}"
    return code if code in CD_NAMES else None


def cd_from_num(boro, num):
    """(borough name/abbr/number, '8') -> '308'."""
    b = BORO_NUM.get(str(boro).strip().upper())
    try:
        n = int(str(num).strip())
    except (TypeError, ValueError):
        return None
    if not b or n <= 0:
        return None
    code = f"{b}{n:02d}"
    return code if code in CD_NAMES else None


def cd_from_3digit(code):
    code = str(code or "").strip()
    return code if code in CD_NAMES else None


def cd_label(code):
    return f"{BORO_NAME[code[0]]} CD {int(code[1:])} — {CD_NAMES[code]}"


# --- place keys -----------------------------------------------------------

def bbl_from_parts(boro, block, lot):
    b = BORO_NUM.get(str(boro).strip().upper())
    try:
        blk = int(str(block).strip())
        lt = int(str(lot).strip())
    except (TypeError, ValueError):
        return None
    if not b or blk <= 0 or lt <= 0:
        return None
    return f"{b}{blk:05d}{lt:04d}"


_ADDR_ABBR = [
    (r"\bSTREET\b", "ST"), (r"\bAVENUE\b", "AVE"), (r"\bBOULEVARD\b", "BLVD"),
    (r"\bPLACE\b", "PL"), (r"\bROAD\b", "RD"), (r"\bDRIVE\b", "DR"), (r"\bPARKWAY\b", "PKWY"),
    (r"\bTERRACE\b", "TER"), (r"\bCOURT\b", "CT"), (r"\bLANE\b", "LN"), (r"\bEXPRESSWAY\b", "EXPY"),
    (r"\bEAST\b", "E"), (r"\bWEST\b", "W"), (r"\bNORTH\b", "N"), (r"\bSOUTH\b", "S"),
    (r"\bSAINT\b", "ST"),
]


def addr_key(house, street, boro):
    """Normalized 'boro|house|street' key so DOB (bin-only) rows can join 311/HPD rows."""
    if not house or not street:
        return None
    s = f"{house} {street}".upper()
    s = re.sub(r"[.,#]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for pat, rep in _ADDR_ABBR:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\b(\d+)(ST|ND|RD|TH)\b", r"\1", s)  # 5TH AVE -> 5 AVE
    b = BORO_NUM.get(str(boro).strip().upper(), "?")
    return f"{b}|{s}"


def split_311_address(addr):
    """'1220 RANDALL AVENUE' -> ('1220', 'RANDALL AVENUE')."""
    if not addr:
        return None, None
    m = re.match(r"\s*([\d\-]+[A-Z]?)\s+(.+)$", addr.strip().upper())
    if not m:
        return None, None
    return m.group(1), m.group(2)


# --- point in polygon ------------------------------------------------------

class DistrictLocator:
    def __init__(self, path=None):
        path = path or os.path.join(OUTPUT_DIR, "geo", "community_districts.geojson")
        g = json.load(open(path))
        self.polys = []  # (cd_code, bbox, [rings])
        for f in g["features"]:
            code = str(f["properties"].get("cd_code") or f["properties"].get("BoroCD"))
            if code not in CD_NAMES:
                continue
            geom = f["geometry"]
            parts = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            for poly in parts:
                outer = poly[0]
                xs = [p[0] for p in outer]
                ys = [p[1] for p in outer]
                self.polys.append((code, (min(xs), min(ys), max(xs), max(ys)), poly))

    @staticmethod
    def _in_ring(x, y, ring):
        inside = False
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
                inside = not inside
            j = i
        return inside

    def locate(self, lon, lat):
        try:
            x, y = float(lon), float(lat)
        except (TypeError, ValueError):
            return None
        for code, (x0, y0, x1, y1), poly in self.polys:
            if x < x0 or x > x1 or y < y0 or y > y1:
                continue
            if self._in_ring(x, y, poly[0]):
                if any(self._in_ring(x, y, hole) for hole in poly[1:]):
                    continue
                return code
        return None


# --- neighborhood names per district (for coverage queries + text matching) --

def neighborhoods_for(cd):
    names = [k for k, v in NEIGHBORHOOD_TO_CD.items() if cd in v and len(k) > 3]
    # prefer proper names over abbreviations
    names.sort(key=lambda s: (-len(s.split()), -len(s)))
    return names


def cd_search_names(cd):
    """A couple of good search names for a district: the CD_NAMES halves plus top neighborhoods."""
    parts = [p.strip() for p in CD_NAMES[cd].split("/")]
    extra = [n.title() for n in neighborhoods_for(cd)[:3] if n.title() not in parts]
    seen, out = set(), []
    for p in parts + extra:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out[:4]


def date_range(start, end):
    """'Aug 3–16, 2026' / 'Jul 20–Aug 16, 2026' for human-readable windows (end inclusive)."""
    if start.year != end.year:
        return f"{start.strftime('%b')} {start.day}, {start.year}–{end.strftime('%b')} {end.day}, {end.year}"
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
    return f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}, {end.year}"


def span(end, days):
    """Human-readable label for the `days`-day window ending on `end` (inclusive)."""
    s, _ = window(end, days)
    return date_range(s, end)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
