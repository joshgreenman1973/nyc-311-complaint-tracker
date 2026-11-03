# How the weekly build works

The front page of the Neighborhood Story Finder is district-first: pick one of the 59 community districts and see this month's 311 several ways, five other city record systems against the month before, the buildings where records converge, and the leads that cleared a coverage check. Everything is rebuilt every Monday from public data. This document says exactly what each number is, how leads are scored, what was deliberately left out and how it can be wrong. Code: `data/leads/`.

## The district page

- **311, four ways.** *Above seasonal norm* — the anomaly test below. *Biggest by volume* — the district's 15 largest complaint types this month, each shown against its own expected value. *Rising this week* — types whose last 7 days beat the average of the prior three weeks by 50%+ (5+ complaints). *12-week trends* — weekly counts for the largest types. All from `erm2-nwe9`, grouped server-side by community board and complaint type in twelve 7-day buckets.
- **Other record systems, latest window vs the window before** (`district_stats.py`): Housing Preservation and Development (HPD) violations issued (30 days, `wvxf-dwi5`), HPD complaints received (28 days, `ygpa-z7cr`), Department of Buildings (DOB) complaints (30 days, `eabe-havv`), residential marshal evictions (60 days, `6z8x-wfk4`), HPD vacate orders (60 days, `tb8q-a3ar`). Ratios are suppressed when the prior window has fewer than 5 records.
- **Also on the district page:** major felonies (New York Police Department CompStat 28-day precinct counts, area-weighted onto districts via a precinct/district overlap crosswalk — approximate, and labelled with the report week), HPD litigation opened, new-building and demolition filings, DOB safety violations, restaurants closed by the health department (DOHMH), deeds recorded (Automated City Register Information System, or ACRIS: master + legals + parties, mapped to districts through the city's PLUTO tax-lot file; ACRIS runs 2-3 weeks behind, so its window is its own and is printed on the tile) and news attention (Google News headlines naming the district in the last 7 days).
- **Every number on a tile opens the underlying records** — the exact SoQL (Socrata Query Language) query for that district and window — so nothing has to be taken on faith. Every window is printed as a date range, never as "30 days".
- **Buildings, leads, liquor filings, land use, City Record notices** — described below.

## What a lead is

Three kinds. All windows end on the last *full* day of 311 data: the pipeline reads `max(created_date)` and, if that day's newest record is before 20:00, ends on the day before (on Aug 18 2026 the newest record was 02:55 on the 17th; treating the 17th as complete would have undercounted every seven-day figure).

### 1. Buildings (place-level convergence)

The unit is the tax lot (BBL). Eight timely, place-keyed feeds are joined on it:

| Feed | Dataset | Window | Key |
|---|---|---|---|
| 311 service requests | NYC Open Data `erm2-nwe9` | 14 days, vs the prior 84 days | `bbl` |
| HPD housing-code violations | `wvxf-dwi5` | 30 days | boro/block/lot |
| HPD housing complaints | `ygpa-z7cr` | 14 days | borough/block/lot |
| DOB complaints | `eabe-havv` | 30 days | address (DOB publishes BIN, not BBL) |
| OATH/ECB building violations | `6bgk-3dad` | 30 days | boro/block/lot |
| HPD vacate orders | `tb8q-a3ar` | 60 days | `bbl` |
| Marshal evictions | `6z8x-wfk4` | 60 days | `bbl` |
| SLA pending liquor licenses | NY State Open Data `f8i8-k2gm` | 60 days | point-in-polygon to district; address to lot |

A lot enters the candidate pool with six or more 311 calls in 14 days, eight or more HPD violations (or four class C) in 30 days, five or more HPD complaints in 14 days, three or more DOB complaints in 30 days, four or more Office of Administrative Trials and Hearings (OATH) violations, any vacate order or two or more residential evictions.

**Convergence is counted by independent record family.** 311, HPD complaints and DOB complaints are all resident reports — the latter two are largely 311 intake — so they count once, together. The four families are:

- resident reports (311, HPD complaints, DOB complaints)
- inspector findings (HPD violations, OATH violations)
- enforcement and court action (vacate orders, marshal evictions)
- state licensing (pending liquor licenses)

A lot becomes a lead when two or more families agree, or when a single source is extreme (12+ 311 calls in a fortnight, a vacate order, 10+ class C violations, 4+ DOB complaints). Score is a weighted sum of the counts (capped per feed) plus 2 points per additional family. Signal = score / 26, capped at 1.

### 2. District anomalies (seasonal baseline)

For every community district x 311 complaint type, the last 28 days are compared with the **same 28-day window in each of the prior three years** (weekday-aligned by shifting 364 days; scaled by the ratio of citywide volume then vs now) and with the **recent eight-week pace**. Expected = the higher of the two, so a pair is only flagged when it beats both the seasonal norm and the recent trend. Flag if the count is 20+ (10+ for a life-safety type), at least 1.5x expected and z = (observed - expected)/sqrt(expected + 1) is 3 or more. "Accelerating" means the last 7 days are at least 1.3x the average of the prior three weeks.

Complaint types with no citywide track record in the seasonal windows are skipped here (they are renames or new intake categories, and would otherwise all read as 100x spikes); the novelty detector handles them.

Signal = 0.7 x min(z/30, 1) + 0.2 if a life-safety type + 0.15 if accelerating; multiplied by 0.55 for low-editorial-value types (parking, street lights, tree requests and the like) and 0.85 if there is no prior-year history.

### 3. New in 311 (novelty)

Descriptors with 25+ reports in the last 56 days that were absent from, or at least 4x rarer in, the same 56-day window in each of the prior two years (volume-adjusted). Three rename filters, because 311 periodically relabels its own descriptors: a descriptor is set aside as a taxonomy change, not a lead, when (a) its complaint type is itself new and carries 100+ reports (volume that used to be filed under another type), (b) it accounts for 40%+ of a type whose total is flat, or two or more "new" descriptors appear under one flat type at once, or (c) descriptors under the same type that existed in prior years have vanished, in volume at least half the new descriptor's — the signature of a swap. Set-aside items are published in `leads.json` under `taxonomy_changes` so nobody has to rediscover them. In the first run (Aug 2026) this set aside 55 relabels — the Water System → Water Maintenance migration, the Noise descriptor split, "Lead Kit" — and left five new descriptors.

Signal = 0.6 x min(n/200, 1) + 0.4 if the top district holds 30%+ of the reports.

## Coverage check (A) and ranking

Every candidate lead is searched against Google News RSS for the last 42 days — by address (buildings), by neighborhood names plus topic keywords (district anomalies) or by the descriptor phrase (novelty) — and against the RSS feeds of 11 local outlets. Google matches loosely, so a hit only counts if the headline contains the address, a topic keyword or the phrase. Local-feed hits weigh 1.0, Google hits 0.7; coverage = min(1, weight/3).

**Rank = signal x (1 - 0.75 x coverage).** A strong signal nobody has written about outranks a stronger one that already made the papers. When the search backend is unreachable, coverage is recorded as unknown and the lead ranks on signal alone; the page says so.

## Optional summaries

If an `ANTHROPIC_API_KEY` is present, the top 20 leads are sent to Claude (model `claude-opus-5`, with server-side fallbacks enabled) with only the numbers shown on the page, and it returns a one-sentence headline, a "why now" and a confidence grade per lead. Nothing in the detection depends on this step; if it fails, the statistical headlines stand. Weekly cost is on the order of cents.

## District feeds (D)

Shown on each district panel, not ranked: Zoning Application Portal (ZAP) land-use applications with a milestone in the last 60 days (`hgx4-8ukb`), City Record notices from the last 14 days naming the district or one of its neighborhoods (via the City Record Digest in `joshgreenman1973/experiments`), pending liquor licenses located in the district and sidewalk-shed counts from the shed tracker.

## Feeds considered and rejected

Kept out on purpose because they cannot support an early-warning claim:

- **Motor vehicle collisions** (`h9gi-nx95`) publish 6-10 weeks late and the fatality field fills in later still (zero deaths in the newest four weeks when the true figure is about 20). A "this week" crash number is a fabrication.
- **NYPD shootings** are published quarterly; **911 calls** have no 2026 data.
- **DOB stalled sites** (`i296-73x5`) stopped receiving complaints in December 2024.
- **CouncilStat constituent cases** (`b9km-gdpy`) stopped updating.
- **Community board WordPress scraping** returns nothing from CI runners.
- **Reddit** JSON is blocked from CI runners. Reddit also attaches no location to posts; the only geography available is neighborhood-specific subreddits and name-matching in text. Buildable as a local job; not wired.
- **The old 311-only map** (`map.html`) was retired: it colored every district for broad categories, its auto-generated titles leaned on "surge"/"spike", and its data lagged its date stamp.

## Known failure modes

- A building with 40 calls in two weeks may have one persistent caller. The evidence panel shows how many distinct days and complaint types the calls span; a single type on a single day is a weaker lead.
- HPD violations follow inspections; an inspection sweep produces a violation cluster without any change in conditions.
- A district anomaly can be a change in how 311 routes or names complaints. The taxonomy filter catches the citywide cases, not district-level routing changes.
- Google News RSS returns titles only, so the coverage check misses stories that mention the place in the body but not the headline. "No coverage found" means no headline matched, not that nobody has written it.
- Community district assignment for State Liquor Authority (SLA) data is point-in-polygon on the license's coordinates; roughly a fifth of New York City applications carry no coordinates and are dropped.

## Verification

`data/leads/verify.py` re-queries the APIs with independently written SoQL for the top leads (311 counts per BBL and district, HPD violation and class C counts, evictions, vacate orders, novelty counts) and diffs them against what `leads.json` publishes. Run it after every build; it exits non-zero on any mismatch.

## Reproducing

```
pip install -r data/requirements.txt
LEADS_AS_OF=2026-08-17 python data/leads/build_leads.py --no-claude
```

`LEADS_AS_OF` pins the data end date; `LEADS_CACHE=/some/dir` caches Socrata responses for repeat runs. Every number in `leads.json` carries the SoQL query that produced it.
