# NYC Neighborhood Story Finder

**August 2026 overhaul.** The front page is now a weekly ranked list of leads, built by `data/leads/` and documented in [leads-methodology.md](leads-methodology.md):

- **Buildings** — tax lots where independent record families converge (311/HPD/DOB reports; HPD and OATH inspector findings; vacate orders and marshal evictions; pending state liquor licenses).
- **District anomalies** — 311 complaint types running above the *same weeks in the prior three years* and above the recent eight-week pace, so October heat and June noise no longer register as "spikes."
- **New in 311** — descriptors that did not exist in the same window of prior years, with 311's own taxonomy churn set aside.
- **Coverage check** — every lead is searched against Google News and local RSS; rank = signal x (1 - 0.75 x coverage). Uncovered signals rank first.

The front page is itself the map (MapLibre choropleth, colorable by any signal, with the district panel alongside). The old 311-only map (`map.html`, `app.js`) and the citywide "performance tracker" were retired in Aug 2026: the former's category chips lit every district, its theme titles overused "surge/spike", and its data lagged the date stamp. The resolution-times page remains. The weekly citywide scan (`data/scanners/weekly_scan.py`) still appends to `data/output/weekly-trends.csv` for the record.

Live: https://joshgreenman1973.github.io/nyc-311-complaint-tracker/ (repo name predates the rebrand).

---

## Original design (district map)

An early-warning system for neighborhood-level stories across New York City. It scans nine data sources — 311 complaints, community board meetings and budget requests, land use applications, constituent service cases, building permits, Reddit and local news — and synthesizes them into story leads for each of the city's 59 community districts.

The tool assumes that local stories don't appear out of nowhere. Before a neighborhood concern becomes a media story, it leaves traces across multiple city systems — 311 call logs, community board agendas, council casework, building permits, land use filings, local Reddit threads and news coverage. This tool surfaces those traces and highlights where they converge.

## The editorial logic

Each data source tells you something different:

- **311 complaints** tell you what residents are reporting, tested for statistical anomalies against an 8-week rolling baseline. Covers 90 days of data, updated daily.
- **Community board meetings** tell you what is becoming organized — scraped from 59 community board WordPress sites with committee classifications (land use, public safety, housing, transportation, SLA/licensing, etc.). When an issue gets on a committee agenda, someone is trying to do something about it.
- **CB budget requests** tell you what districts keep asking the city to fund — the same request showing up year after year means the problem hasn't been solved.
- **Council constituent-service logs** tell you what is politically sticky — issues that residents have escalated to their elected officials. 90 days of casework by type.
- **ULURP / land use applications** tell you what is about to become a fight — rezonings, shelter sitings, special permits and street redesigns in the pipeline before they hit the news.
- **DOB building permits** tell you where construction is changing a neighborhood — spikes in new building, demolition or alteration filings signal development pressure.
- **Reddit discussions** tell you what the public conversation looks like — ~12 New York City subreddits (r/nyc, r/Brooklyn, r/astoria, r/Queens, etc.) mapped to community districts by neighborhood name extraction.
- **Local news** tells you whether an issue has broken through — RSS feeds from 11+ outlets including THE CITY, Gothamist, Hell Gate, City Limits, Bklyner, QNS, Bronx Times, Staten Island Advance, Patch NYC, Curbed and amNewYork.
- **AI synthesis** (Claude API) combines signals from all sources to identify 3-5 story leads per district, ranked by recency, severity and convergence.

### The convergence signal

The tool looks for convergence. When a 311 spike in noise complaints lines up with a community board SLA/licensing committee meeting, a council constituent casework trend and Reddit threads about the same block, the issue is often about to become a media story. The tool is designed to surface these convergences automatically.

## How it works

### Data pipeline (Python, daily)

A batch pipeline (`data/pipeline.py`) runs once per day via GitHub Actions and collects from all sources:

| Source | What it captures | API / Method |
|--------|-----------------|-------------|
| **311 complaints** | 90 days of service requests, z-score spike detection, 12-week sparklines | Socrata API (`erm2-nwe9`) |
| **Community board meetings** | Upcoming/recent meetings, committee classifications, agenda topics | Scraping 59 CB WordPress sites (JSON-LD + HTML fallback) |
| **CB budget requests** | What districts repeatedly ask the city to fix or fund | Socrata API (`vn4m-mk4t`) |
| **Land use / ULURP** | Active rezonings and development proposals | Socrata API (`hgx4-8ukb`) |
| **Council services** | Constituent complaints escalated to elected officials | Socrata API (`b9km-gdpy`) |
| **DOB permits** | New building, demolition, and major alteration filings | Socrata API (`w9ak-ipjd`) |
| **Reddit** | ~12 NYC subreddits, geo-mapped by neighborhood name matching | Public JSON API |
| **Local news** | 11+ outlets (THE CITY, Gothamist, Hell Gate, City Limits, etc.) | RSS / feedparser |

After collection, an AI synthesis step (Claude Haiku) reads all available data for each district and produces 3-5 story leads with structured scoring. Without an API key, the pipeline falls back to purely statistical theme generation from 311 spike data.

Output is static JSON:
- `districts.json` — Summary for all 59 districts with activity scores, top themes, spike counts
- `districts/{cd}.json` — Full detail per district (themes, spikes, meetings, Reddit, news, budget requests, land use, permits, council cases)
- `trends.json` — Citywide hot spots and "What's Happening Now" top story leads ranked by recency
- `timeseries.json` — 12-week sparkline data for each district's top complaint types
- `geo/community_districts.geojson` — Simplified district boundaries

### Frontend (vanilla JS + MapLibre GL)

A single-page application renders a choropleth map colored by activity score (0-100). The score heavily weights recency:
- This-week spikes count at full weight (1.0x)
- Last-week spikes at 0.7x
- Two weeks ago at 0.4x
- Three weeks ago at 0.2x
- Plus an acceleration component (comparing the last 2 weeks to the prior 2 weeks)
- Plus complaint category diversity

The overview shows:
- **"What's Happening Now"** — top story leads across all districts, ranked by recency then quality
- **Category filter pills** — the most common complaint types (heat/hot water, noise, parking, streets) get their own pills, plus broader categories (housing, safety, transit, sanitation, pests, development, infrastructure, environment, health)
- **Search** with synonym expansion — common terms like "dog poop," "rats," "scaffolding," "trash" map to official 311 complaint type names
- **Hot Spots** view — districts ranked by activity with spike counts

Clicking a district loads its detail JSON and shows:
- **Story leads** with recency badges (this week / last 2 weeks / last month / ongoing) and severity/category tags
- **311 anomalies** with z-scores, explicit comparators ("X complaints vs Y wkly avg"), and age labels
- **12-week sparklines** for top complaint types (orange = spiking)
- **Community board meetings** — upcoming and recent meetings with committee classifications, locations, and agenda topics
- **CB budget requests** — what the district keeps asking for
- **Active land use / ULURP applications** — what's in the pipeline
- **Building permit filings** — new construction, demolitions, major alterations
- **Council constituent cases** — what's being escalated
- **Reddit discussions** with engagement metrics and links
- **Local news** with outlet attribution and links
- **Sources** — direct links to NYC Open Data queries, ZAP, Reddit search, and CB pages

Every data point tracks back to a source. Nothing is paywalled.

## Story scoring

Each story lead is scored across six dimensions:

| Dimension | What it measures |
|-----------|-----------------|
| **Severity** | How serious is this for residents? |
| **Verifiability** | Can this be fact-checked with public data? |
| **Freshness** | Is this emerging now or long-running? |
| **Human interest** | Are there affected people to talk to? |
| **Data richness** | How much quantitative evidence exists? |
| **Editorial potential** | Brief, short, or feature-length story? |

Leads that appear across multiple data sources get higher scores. A 311 spike corroborated by a community board agenda item and a council constituent complaint is a much stronger signal than any one alone. Leads are ranked by recency first (this-week signals before last-month signals), then by quality score.

## Running locally

```bash
cd neighborhood-story-finder
pip install -r data/requirements.txt

# Without Claude API (statistical themes only)
python data/pipeline.py --no-claude

# With Claude API for AI-synthesized themes
export ANTHROPIC_API_KEY=sk-ant-...
python data/pipeline.py

# 311 data only (fastest, for testing)
python data/pipeline.py --311-only

# Skip community board scraping (slow)
python data/pipeline.py --no-cb
```

Output goes to `data/output/`. Open `index.html` in a browser (or run `python serve.py` for a dev server with no-cache headers on port 8803).

## Architecture decisions

- **Static JSON over live API**: Pre-computed output, no server, no API keys in the browser, no rate limits.
- **Community districts over neighborhoods**: 59 official districts with clear boundaries. 311 data uses them natively.
- **MapLibre GL**: Free, no token, same API surface as Mapbox.
- **Claude Haiku for synthesis**: ~$2-6/day for all 59 districts during batch.
- **Z-score spike detection**: Compares each week against an 8-week baseline. Z-scores above 1.8 with 3+ complaints trigger an alert. Scans last 4 weeks with age tracking.
- **Recency-weighted scoring**: Activity scores decay by spike age. Acceleration component rewards districts where complaints are rising right now.
- **Search synonym expansion**: Maps common language ("dog poop," "rats," "scaffolding") to official 311 complaint type names.
- **GitHub Pages**: Zero hosting cost. Daily GitHub Actions cron updates the data.

## What's not yet included

The editorial framework behind this tool identifies several additional source layers that would strengthen the signal but aren't yet automated:

- **Community Education Councils (CECs)** — 32 school-district-level parent councils, each on a different website platform with no standard API
- **NYPD precinct councils** — limited online presence, rarely publish agendas digitally
- **NYCHA resident councils** (~95% of developments have them)
- **School SLTs, PA/PTAs** — what families are quietly worried about
- **Borough president hearings and service cabinets**
- **BIDs and merchant groups** — early detection of corridor deterioration
- **The informal layer**: tenant associations, block associations, precinct community WhatsApps, local Facebook groups, parent listservs, mosque/church/synagogue newsletters, mutual-aid chats

These aren't available through public APIs, but they would carry the most granular early signals.

## File structure

```
neighborhood-story-finder/
  index.html              # Frontend
  app.js                  # Map, sidebar, interactions
  style.css               # Vital City dark theme
  lib/sparkline.js        # Canvas-based micro sparklines
  serve.py                # Dev server (port 8803)
  data/
    pipeline.py           # Orchestrator
    config.py             # Mappings, API config, categories
    requirements.txt      # Python dependencies
    collectors/
      three11.py          # 311 complaints (Socrata)
      reddit.py           # NYC subreddits (public JSON)
      news_rss.py         # Local news RSS feeds
      community_boards.py # CB WordPress sites (JSON-LD + HTML)
      budget_requests.py  # CB budget requests (Socrata)
      land_use.py         # ULURP / ZAP applications (Socrata)
      council_services.py # CouncilStat constituent cases (Socrata)
      dob_permits.py      # DOB NOW building permits (Socrata)
    processors/
      trend_detector.py   # Z-score spike detection, activity scoring
      theme_extractor.py  # Claude API synthesis with recency rules
      data_compressor.py  # JSON output generation
      geo_mapper.py       # Text-to-district mapping
    output/               # Generated data (committed to repo)
  .github/workflows/
    update-data.yml       # Daily cron pipeline
```
