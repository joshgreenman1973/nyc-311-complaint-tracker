"""
Theme extractor using Claude API.
Synthesizes data from all sources into narrative themes per community district.
Includes story severity, verifiability, and editorial potential scoring.
"""

import os
import json

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CD_NAMES, CATEGORIES


SYNTHESIS_PROMPT = """You are an editor at a NYC local news organization scanning for neighborhood-level stories. Given data from Community District {cd_code} ({cd_name}), identify emerging concerns worth investigating.

IMPORTANT — RECENCY RULES:
- Heavily prioritize issues that are NEW or ACCELERATING in the last 1-2 weeks over long-running patterns.
- A spike from THIS WEEK is far more interesting than a spike from 3 weeks ago.
- In every summary, state the timeframe explicitly: "this week", "over the past 2 weeks", "a 4-week sustained trend", etc. Never leave timing ambiguous.
- When citing a spike, always include the comparator: "X complaints this week vs. Y weekly average over the prior 8 weeks."

311 COMPLAINTS (top types by volume, last 90 days):
{complaints_text}

311 SPIKES (statistically significant deviations from 8-week baseline):
{spikes_text}

REDDIT DISCUSSIONS (recent posts geo-tagged to this district):
{reddit_text}

LOCAL NEWS MENTIONS (recent articles referencing this district):
{news_text}

COMMUNITY BOARD (meeting agendas and topics):
{cb_text}

CB BUDGET REQUESTS (what the district repeatedly asks the city to fund — same request year after year means unsolved problem):
{budget_text}

LAND USE / ULURP APPLICATIONS (active rezonings and development proposals in the pipeline):
{land_use_text}

COUNCIL CONSTITUENT CASES (issues escalated to elected officials, last 90 days):
{council_text}

DOB BUILDING PERMITS (new buildings, demolitions, major alterations filed in last 90 days):
{dob_text}

---

Respond with a JSON array of 3-5 story leads, RANKED BY how recently the signal appeared (newest first). For each, provide:

```json
[
  {{
    "label": "Short label (2-5 words)",
    "summary": "One-sentence summary stating WHAT is happening, WHEN it started or accelerated, and WHY it matters. Always include specific numbers and timeframes.",
    "category": "one of: {categories}",
    "intensity": "low | medium | high",
    "recency": "this week | last 2 weeks | last month | ongoing",
    "story_score": {{
      "severity": "low | medium | high — how serious is this for residents?",
      "verifiability": "low | medium | high — can this be fact-checked with public data?",
      "freshness": "low | medium | high — is this emerging now or long-running?",
      "human_interest": "low | medium | high — are there affected people to talk to?",
      "data_richness": "low | medium | high — how much quantitative evidence exists?",
      "editorial_potential": "brief | short | feature — what length story could this support?"
    }},
    "evidence": ["Key data point 1 (include numbers and timeframe)", "Key data point 2", "Key data point 3"],
    "reporting_angles": ["Suggested angle 1", "Suggested angle 2"]
  }}
]
```

Focus on:
- Concerns that are NEW or ACCELERATING this week or last week — these should rank highest
- Convergence: 311 spike + community board topic + council constituent cases pointing at the same issue = strong signal
- Statistical anomalies (z-scores above 2.0, sustained trends)
- Issues with clear human impact
- Always make comparisons explicit: "X this week vs Y weekly average" or "up Z% from 2 weeks ago"

LABEL DISCIPLINE (very important — this is a measured policy-journalism product, not a tabloid):
- DO NOT use the word "crisis." Ever. It's overused, vague, and inflates routine 311 volume into emergency.
- Also avoid: "epidemic", "out of control", "explosion", "catastrophic", "emergency", "disaster", "nightmare", "war on X", "alarming". These are loaded and unearned.
- Prefer specific, neutral framings: "Heat complaints climb", "Rodent reports double", "Plumbing complaints surge", "Tree damage spike", "Encampment reports up sharply".
- "Spike", "Surge", "Climb", "Rise", "Jump", "Doubled" are acceptable when the data actually supports them.
- A label should describe WHAT and roughly HOW MUCH. It should not pre-judge severity.

Return ONLY valid JSON, no other text."""


def _format_complaints(district_data):
    """Format top complaint types for the prompt."""
    if not district_data:
        return "No data available"

    by_type = district_data.get("by_type", {})
    top = sorted(by_type.items(), key=lambda x: -x[1])[:10]

    lines = []
    for complaint_type, count in top:
        lines.append(f"- {complaint_type}: {count} complaints")

    return "\n".join(lines) if lines else "No complaints recorded"


def _format_spikes(spikes):
    """Format spike data for the prompt with explicit timeframes."""
    if not spikes:
        return "No statistically significant spikes detected"

    age_labels = {0: "THIS WEEK", 1: "last week", 2: "2 weeks ago", 3: "3 weeks ago"}
    lines = []
    for s in spikes[:8]:
        direction = "UP" if s["direction"] == "up" else "DOWN"
        sustained = f" (sustained {s['sustained_weeks']} weeks)" if s.get("sustained_weeks") else ""
        age = age_labels.get(s.get("weeks_ago", 0), "recent")
        lines.append(
            f"- {s['type']}: {direction} {abs(s['pct_change'] or 0):.0f}% vs 8-week avg "
            f"({s['current_week']} complaints {age} vs {s['baseline_avg']} avg) "
            f"(z-score: {s['z_score']}, severity: {s['severity']}){sustained}"
        )

    return "\n".join(lines)


def _format_reddit(posts):
    """Format Reddit posts for the prompt."""
    if not posts:
        return "No relevant Reddit discussions"

    lines = []
    for p in posts[:8]:
        lines.append(
            f"- r/{p['subreddit']}: \"{p['title'][:80]}\" "
            f"({p['score']} upvotes, {p['num_comments']} comments, {p['created_date']})"
        )

    return "\n".join(lines)


def _format_news(articles):
    """Format news articles for the prompt."""
    if not articles:
        return "No recent local news mentions"

    lines = []
    for a in articles[:6]:
        lines.append(f"- {a['outlet']}: \"{a['title'][:80]}\" ({a.get('pub_date', 'unknown')[:10]})")

    return "\n".join(lines)


def _format_cb(cb_info):
    """Format community board info for the prompt."""
    if not cb_info:
        return "No community board data available"

    lines = []
    if cb_info.get("next_meeting"):
        lines.append(f"Next meeting: {cb_info['next_meeting']}")
    if cb_info.get("topics"):
        lines.append(f"Active topics: {', '.join(cb_info['topics'])}")

    return "\n".join(lines) if lines else "No meeting info available"


def _format_budget(budget_reqs):
    """Format budget requests for the prompt."""
    if not budget_reqs:
        return "No budget request data available"
    lines = []
    for req in budget_reqs[:8]:
        lines.append(f"- [{req.get('priority', '?')}] {req.get('request', '')[:100]} (Agency: {req.get('agency', '')})")
    return "\n".join(lines)


def _format_land_use(projects):
    """Format land use projects for the prompt."""
    if not projects:
        return "No active land use applications"
    lines = []
    for p in projects[:5]:
        lines.append(f"- {p.get('name', '')[:80]} ({p.get('type', '')} · {p.get('status', '')} · milestone: {p.get('milestone', '')})")
    return "\n".join(lines)


def _format_council(council_info):
    """Format council constituent services for the prompt."""
    if not council_info:
        return "No constituent services data available"
    lines = [f"Total cases: {council_info.get('total', 0)}"]
    for ctype, count in sorted(council_info.get("complaint_types", {}).items(), key=lambda x: -x[1])[:6]:
        lines.append(f"- {ctype}: {count} cases")
    return "\n".join(lines)


def _format_dob(dob_info):
    """Format DOB permit data for the prompt."""
    if not dob_info:
        return "No building permit data available"
    lines = [f"Total filings: {dob_info.get('total', 0)}"]
    for ptype, count in sorted(dob_info.get("by_type", {}).items(), key=lambda x: -x[1])[:5]:
        lines.append(f"- {ptype}: {count}")
    notable = dob_info.get("notable", [])
    for n in notable[:3]:
        lines.append(f"  → {n.get('type', '')}: {n.get('address', '')} ({n.get('date', '')})")
    return "\n".join(lines)


def synthesize_district(cd_code, district_311, spikes, reddit_posts, news_articles, cb_info,
                        budget_reqs=None, land_use_projects=None, council_info=None, dob_info=None):
    """
    Use Claude API to synthesize themes for a single district.
    Returns list of theme dicts, or fallback themes if API unavailable.
    """
    if not HAS_ANTHROPIC or not os.environ.get("ANTHROPIC_API_KEY"):
        return _fallback_themes(cd_code, district_311, spikes, reddit_posts, news_articles)

    prompt = SYNTHESIS_PROMPT.format(
        cd_code=cd_code,
        cd_name=CD_NAMES.get(cd_code, f"District {cd_code}"),
        complaints_text=_format_complaints(district_311),
        spikes_text=_format_spikes(spikes),
        reddit_text=_format_reddit(reddit_posts),
        news_text=_format_news(news_articles),
        cb_text=_format_cb(cb_info),
        budget_text=_format_budget(budget_reqs),
        land_use_text=_format_land_use(land_use_projects),
        council_text=_format_council(council_info),
        dob_text=_format_dob(dob_info),
        categories=", ".join(CATEGORIES),
    )

    try:
        client = anthropic.Anthropic()
        # Claude Haiku 5.5 thinks by default and thinking counts toward max_tokens:
        # low effort, a roomy cap, and the answer read from the text blocks.
        response = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=8192,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined")

        text = "".join(b.text for b in response.content if b.type == "text").strip()

        # Parse JSON from response (handle markdown code blocks)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        themes = json.loads(text)
        return themes

    except Exception as e:
        print(f"  Warning: Claude API failed for CD {cd_code}: {e}")
        return _fallback_themes(cd_code, district_311, spikes, reddit_posts, news_articles)


def _fallback_themes(cd_code, district_311, spikes, reddit_posts, news_articles):
    """
    Generate themes without Claude API, using purely statistical signals.
    """
    themes = []

    # Theme from top complaint spikes
    age_labels = {0: "this week", 1: "last week", 2: "2 weeks ago", 3: "3 weeks ago"}
    recency_labels = {0: "this week", 1: "last 2 weeks", 2: "last month", 3: "last month"}
    for spike in (spikes or [])[:3]:
        if spike["severity"] in ("high", "medium"):
            direction_word = "surge" if spike["direction"] == "up" else "drop"
            weeks_ago = spike.get("weeks_ago", 0)
            age = age_labels.get(weeks_ago, "recently")
            themes.append({
                "label": f"{spike['type']} {direction_word}",
                "summary": (
                    f"{spike['type']} complaints spiked {age} — "
                    f"{spike['current_week']} complaints vs {spike['baseline_avg']} weekly avg over prior 8 weeks "
                    f"(up {abs(spike['pct_change'] or 0):.0f}%, z-score: {spike['z_score']})."
                ),
                "category": _guess_category(spike["type"]),
                "intensity": spike["severity"],
                "recency": recency_labels.get(weeks_ago, "ongoing"),
                "story_score": {
                    "severity": spike["severity"],
                    "verifiability": "high",
                    "freshness": "high" if weeks_ago <= 1 else "medium",
                    "human_interest": "medium",
                    "data_richness": "high",
                    "editorial_potential": "short" if spike["severity"] == "high" else "brief",
                },
                "evidence": [
                    f"311 data: {spike['current_week']} complaints {age} vs {spike['baseline_avg']} weekly avg (8-week baseline)",
                    f"Z-score: {spike['z_score']} ({spike['severity']} severity)",
                ],
                "reporting_angles": [],
            })

    # Theme from high-volume complaint types
    if district_311 and not themes:
        top_type = max(district_311.get("by_type", {}).items(), key=lambda x: x[1], default=(None, 0))
        if top_type[0]:
            themes.append({
                "label": f"High {top_type[0]} volume",
                "summary": f"{top_type[0]} is the top complaint type with {top_type[1]} complaints in 90 days.",
                "category": _guess_category(top_type[0]),
                "intensity": "low",
                "story_score": {
                    "severity": "low", "verifiability": "high", "freshness": "low",
                    "human_interest": "medium", "data_richness": "high",
                    "editorial_potential": "brief",
                },
                "evidence": [f"311: {top_type[1]} complaints"],
                "reporting_angles": [],
            })

    return themes


def _guess_category(complaint_type):
    """Quick category guess from complaint type text."""
    from config import COMPLAINT_CATEGORY_MAP
    ct_lower = (complaint_type or "").lower()
    for category, keywords in COMPLAINT_CATEGORY_MAP.items():
        for kw in keywords:
            if kw in ct_lower:
                return category
    return "quality-of-life"


def synthesize_all(district_311, spikes, reddit_posts, news_articles, cb_data,
                   budget_data=None, land_use_data=None, council_data=None, dob_data=None):
    """
    Main entry point. Synthesize themes for all districts.
    Returns dict of cd → themes list.
    """
    budget_data = budget_data or {}
    land_use_data = land_use_data or {}
    council_data = council_data or {}
    dob_data = dob_data or {}

    print("[Themes] Synthesizing themes per district...")

    # Group Reddit posts and news by district
    reddit_by_cd = {}
    for post in reddit_posts:
        for cd in post.get("districts", []):
            reddit_by_cd.setdefault(cd, []).append(post)

    news_by_cd = {}
    for article in news_articles:
        for cd in article.get("districts", []):
            news_by_cd.setdefault(cd, []).append(article)

    all_themes = {}
    processed = 0

    for cd in sorted(CD_NAMES.keys()):
        cd_311 = district_311.get(cd, {})
        cd_spikes = spikes.get(cd, [])
        cd_reddit = reddit_by_cd.get(cd, [])
        cd_news = news_by_cd.get(cd, [])
        cd_cb = cb_data.get(cd, {})

        # Skip districts with very little data
        if not cd_311 and not cd_reddit and not cd_news:
            all_themes[cd] = []
            continue

        themes = synthesize_district(
            cd, cd_311, cd_spikes, cd_reddit, cd_news, cd_cb,
            budget_reqs=budget_data.get(cd, []),
            land_use_projects=land_use_data.get(cd, []),
            council_info=council_data.get(cd),
            dob_info=dob_data.get(cd),
        )
        all_themes[cd] = themes
        processed += 1

        if processed % 10 == 0:
            print(f"  Processed {processed} districts...")

    total_themes = sum(len(t) for t in all_themes.values())
    print(f"[Themes] Generated {total_themes} themes across {processed} districts")

    return all_themes
