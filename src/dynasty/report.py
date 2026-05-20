"""Generate a multi-page HTML site of the dynasty model.

Output structure:
    dynasty_site/
        index.html              — landing page: methodology, sources, divergence highlights
        rankings.html           — full top-300 with consensus delta column
        sources.html            — detailed source-by-source breakdown
        players/<slug>.html     — per-player detail page with full breakdown
        assets/style.css        — shared styles

All HTML is self-contained (no external CDNs), works offline.
"""
from __future__ import annotations
import json
import re
from datetime import datetime
from pathlib import Path
from sqlalchemy import select, func

from .db.session import get_session
from .db.models import Player, CompositeScore, Source, Ranking


# --------------------------------------------------------------------------
# Source descriptions — kept human-readable for the landing page
# --------------------------------------------------------------------------

SOURCE_DESCRIPTIONS = {
    "fantasycalc": {
        "blurb": "Crowdsourced dynasty values derived from ~1M+ real fantasy trades.",
        "type": "Market signal",
        "strength": "Reflects what the broader fantasy community actually pays for players, updated multiple times daily.",
        "weakness": "By definition tracks consensus — won't surface contrarian or evaluator opinions.",
        "weight_justification": (
            "Baseline weight (1.0). Pure market signal; weighted as 'consensus' for the "
            "divergence calculation rather than as an independent evaluator opinion."
        ),
    },
    "dynastyprocess": {
        "blurb": "FantasyPros Expert Consensus Rankings (ECR) aggregated across 70+ industry analysts.",
        "type": "Aggregator",
        "strength": "Broad expert consensus, slow-moving and stable.",
        "weakness": "By averaging 70+ analysts, mutes the signal from the most accurate evaluators.",
        "weight_justification": (
            "Baseline weight (1.0). Treated as 'consensus' alongside FantasyCalc — useful for "
            "establishing what the market thinks but not for divergence."
        ),
    },
    "sleeper_players": {
        "blurb": "Sleeper's canonical player ID map. Used internally — does not contribute to scoring.",
        "type": "Reference data",
        "strength": "Links every source to a single player identity.",
        "weakness": "Not a ranking source.",
        "weight_justification": "N/A — does not contribute to composite scoring.",
    },
    "brainy_ballers": {
        "blurb": "Top-500 dynasty rankings, primarily SPS-driven (Star-Predictor Score) with expert overlays.",
        "type": "Analytics model",
        "strength": (
            "Algorithmic prospect-scoring approach with publicly-cited successes — early calls on Jaxson Dart, "
            "Trey McBride, JSN, McCaffrey. Featured on Pat McAfee, Forbes, Barstool."
        ),
        "weakness": "Rookie-grade detail is paywalled; only the aggregated top-500 is scraped here.",
        "weight_justification": (
            "Default weight 1.3. Elevated above consensus because SPS represents an independent, "
            "transparent algorithmic methodology rather than another consensus aggregator. Track-record "
            "multiplier will adjust this once backtesting against actual NFL outcomes is run."
        ),
    },
    "lance_zierlein": {
        "blurb": "NFL.com lead draft analyst. Public scouting reports on every prospect.",
        "type": "Expert evaluator",
        "strength": (
            "Top performer in the Hogs Haven 2018-2020 wAV correlation study — strongest measured "
            "correlation between pre-draft rankings and actual NFL player performance among major-media analysts."
        ),
        "weakness": "No single consolidated ranking export; starter pack contains transcribed top-10 only.",
        "weight_justification": (
            "Default weight 1.4 — the highest in the model. Justified by the Hogs Haven independent study "
            "which measured analyst pre-draft rankings against Weighted Career Approximate Value (wAV) "
            "over multiple years. Zierlein led that study."
        ),
    },
    "pff_public": {
        "blurb": "Pro Football Focus public Top-60 dynasty rookies (free article tier).",
        "type": "Analytics model",
        "strength": (
            "PFF publishes hit-rate-by-bucket curves for their prospect model. Transparent methodology. "
            "Strong relative performance in independent draft-outcome studies."
        ),
        "weakness": "Full PFF prospect model is paywalled; only the Top-60 article is loaded for free.",
        "weight_justification": (
            "Default weight 1.3. PFF's transparent prospect score-to-fantasy-outcome curves are unique "
            "among major analytics services. For the full PFF API, see sources/pff.py."
        ),
    },
    "pff": {
        "blurb": "Pro Football Focus full prospect models and dynasty rankings (paid API).",
        "type": "Analytics model",
        "strength": "Transparent prospect score-to-outcome curves. Top performer in independent draft-outcome studies.",
        "weakness": "Requires paid API access — not active until credentials are added.",
        "weight_justification": "Default weight 1.3 — same justification as PFF public. Higher granularity when active.",
    },
    "fantasypros": {
        "blurb": "FantasyPros Expert Consensus Rankings via paid API.",
        "type": "Aggregator",
        "strength": "Direct line to the largest expert consensus in the industry, updated daily.",
        "weakness": "Paid; redundant with DynastyProcess unless you want lower latency.",
        "weight_justification": "Default weight 1.2. Slight premium over DynastyProcess due to update frequency.",
    },
    "daniel_jeremiah": {
        "blurb": "NFL Network lead draft analyst. Top of FantasyPros mock-draft accuracy 2025.",
        "type": "Expert evaluator",
        "strength": (
            "Most accurate of the Big Three (Jeremiah / Kiper / McShay) in 2025 per Inside The Star's review. "
            "Strong on predicting actual NFL Draft slot — which matters because draft capital is the "
            "strongest single predictor of fantasy outcomes."
        ),
        "weakness": "Specialty is draft order, not fantasy projection — less directly applicable than evaluators like Harmon or RSP.",
        "weight_justification": (
            "Default weight 1.1. Elevated because draft capital is empirically the strongest single predictor "
            "of fantasy outcome, and Jeremiah is documented as the most accurate predictor of where players "
            "actually get drafted."
        ),
    },
}

# Categories of evaluators you should add via CSV import (paywalled sources)
EVALUATOR_RECOMMENDATIONS = [
    {
        "name": "Matt Harmon — Reception Perception",
        "url": "https://receptionperception.com/",
        "scope": "Wide receivers",
        "why": "Charts every WR's route success rate against man/zone — methodology is transparent and has a public stacked-rankings history going back to 2021, which means you can backtest him directly. Behind a PRIME-tier paywall.",
        "import_as": "matt_harmon",
        "suggested_weight": 1.3,
    },
    {
        "name": "Matt Waldman — Rookie Scouting Portfolio",
        "url": "https://mattwaldmanrsp.com/",
        "scope": "All skill positions, rookies only",
        "why": "21st year. Famously identified Puka Nacua as WR12 in 2023. Skill-position film grades only — adds an independent voice not blended into consensus. Pre-draft PDF (~$25).",
        "import_as": "matt_waldman_rsp",
        "suggested_weight": 1.2,
    },
    {
        "name": "Dane Brugler — The Beast (The Athletic)",
        "url": "https://theathletic.com/",
        "scope": "All NFL Draft prospects",
        "why": "400+ scouting reports per year. Closest thing to an NFL-quality scouting reference in public media. Behind The Athletic paywall.",
        "import_as": "brugler_beast",
        "suggested_weight": 1.1,
    },
    {
        "name": "Hayden Winks — Underdog Fantasy",
        "url": "https://underdognetwork.com/",
        "scope": "All offensive positions, model-driven",
        "why": "Athletic-comp model with public methodology. Strong on RBs in particular. Behind Underdog paywall.",
        "import_as": "hayden_winks",
        "suggested_weight": 1.1,
    },
    {
        "name": "PlayerProfiler",
        "url": "https://www.playerprofiler.com/",
        "scope": "All offensive positions, metric-driven",
        "why": "Breakout Age, College Dominator, SPARQ-x — published metrics-based prospect model. Won FantasyPros accuracy in 2021. Free articles; full data paid.",
        "import_as": "playerprofiler",
        "suggested_weight": 1.1,
    },
]


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

def _slugify(name: str, player_id: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{s}-{player_id}"


def _esc(s) -> str:
    """HTML-escape, with None tolerance."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


POS_COLORS = {
    "QB": "#e74c3c", "RB": "#27ae60", "WR": "#3498db", "TE": "#f39c12",
    "K": "#95a5a6", "DEF": "#7f8c8d",
}


def _pos_badge(pos: str | None) -> str:
    pos = pos or "—"
    color = POS_COLORS.get(pos, "#888")
    return f'<span class="pos-badge" style="background:{color}">{_esc(pos)}</span>'


def _divergence_chip(div: int | None) -> str:
    """Visual indicator for consensus delta. Positive = model higher; negative = lower."""
    if div is None:
        return '<span class="div-chip div-none">no consensus</span>'
    if div == 0:
        return '<span class="div-chip div-flat">aligned</span>'
    if div > 0:
        cls = "div-up-big" if div >= 10 else "div-up"
        return f'<span class="div-chip {cls}">model +{div}</span>'
    cls = "div-down-big" if div <= -10 else "div-down"
    return f'<span class="div-chip {cls}">model {div}</span>'


# --------------------------------------------------------------------------
# Shared HTML scaffolding
# --------------------------------------------------------------------------

def _shared_css() -> str:
    return """
:root {
  --bg: #fafbfc; --card: #fff; --border: #e1e4e8; --text: #1a1f24;
  --muted: #6a737d; --accent: #0a3d62; --accent-light: #1e5a8e;
  --hover: #f6f8fa; --up: #16a34a; --down: #dc2626; --flat: #6b7280;
  --warn-bg: #fef3c7; --warn-border: #fbbf24; --warn-text: #92400e;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.55;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.site {
  background: linear-gradient(135deg, var(--accent), var(--accent-light));
  color: white; padding: 24px 40px;
}
header.site .row { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 16px; }
header.site h1 { margin: 0; font-size: 22px; font-weight: 700; }
header.site h1 a { color: white; }
header.site nav a {
  color: white; opacity: 0.85; margin-left: 18px; font-size: 14px; font-weight: 500;
}
header.site nav a:hover { opacity: 1; text-decoration: none; }
header.site .meta { opacity: 0.75; font-size: 12px; margin-top: 4px; }
.container { max-width: 1280px; margin: 0 auto; padding: 24px 40px; }
.container.narrow { max-width: 920px; }
h2 { color: var(--accent); font-size: 22px; margin-top: 32px; }
h3 { color: var(--accent); font-size: 17px; margin-top: 24px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px 24px; margin-bottom: 20px; }
.card.tight { padding: 14px 18px; }
.kv { display: grid; grid-template-columns: 160px 1fr; gap: 6px 18px; font-size: 14px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; font-weight: 500; }
table { width: 100%; background: var(--card); border-collapse: collapse;
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
th { background: #f6f8fa; padding: 11px 14px; text-align: left;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 600; }
td { padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 14px; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr.player-row:hover { background: var(--hover); cursor: pointer; }
td.rank { font-weight: 700; color: var(--accent); width: 50px; }
td.name { font-weight: 600; }
td.score { font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; }
td.tier, td.pos-rank, td.team, td.consensus { color: var(--muted); font-variant-numeric: tabular-nums; }
.pos-badge { display: inline-block; color: white; padding: 3px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 700; min-width: 32px; text-align: center; }
.controls { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; margin-bottom: 20px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.controls input, .controls select { font: inherit; padding: 7px 11px;
  border: 1px solid var(--border); border-radius: 6px; background: white; }
.controls input { flex: 1; min-width: 220px; }
.stats { color: var(--muted); font-size: 13px; margin-left: auto; }
.div-chip { display: inline-block; padding: 3px 9px; border-radius: 12px; font-size: 11px;
  font-weight: 600; font-variant-numeric: tabular-nums; }
.div-up { background: #ecfdf5; color: #047857; }
.div-up-big { background: #16a34a; color: white; }
.div-down { background: #fef2f2; color: #b91c1c; }
.div-down-big { background: #dc2626; color: white; }
.div-flat { background: #f3f4f6; color: var(--flat); }
.div-none { background: #f3f4f6; color: var(--muted); font-style: italic; }
.callout { background: var(--warn-bg); border: 1px solid var(--warn-border);
  border-left: 4px solid var(--warn-border); border-radius: 6px; padding: 14px 18px;
  color: var(--warn-text); margin: 16px 0; font-size: 14px; }
.callout strong { color: #78350f; }
.breakdown-table { font-size: 13px; }
.breakdown-table .source-name { font-weight: 600; }
.weight-bar { display: inline-block; height: 6px; background: var(--accent);
  border-radius: 3px; vertical-align: middle; margin-right: 8px; min-width: 4px; }
.player-header { background: linear-gradient(135deg, var(--accent), var(--accent-light));
  color: white; padding: 28px 40px; }
.player-header h1 { margin: 0; font-size: 30px; }
.player-header .sub { opacity: 0.85; font-size: 15px; margin-top: 4px; }
.player-header .metrics { display: flex; gap: 32px; margin-top: 18px; }
.player-header .metric { }
.player-header .metric .num { font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; }
.player-header .metric .label { opacity: 0.75; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
.divergence-section .div-explanation { font-size: 14px; margin: 12px 0 18px 0; padding: 12px 16px;
  background: var(--hover); border-radius: 6px; border-left: 3px solid var(--accent); }
.steps { counter-reset: step; padding: 0; list-style: none; }
.steps li { counter-increment: step; padding: 12px 0 12px 44px; position: relative; border-bottom: 1px solid var(--border); }
.steps li:last-child { border-bottom: none; }
.steps li::before { content: counter(step); position: absolute; left: 0; top: 14px;
  background: var(--accent); color: white; width: 28px; height: 28px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 13px; }
.steps li strong { color: var(--accent); }
footer { color: var(--muted); font-size: 12px; padding: 32px 40px; text-align: center; border-top: 1px solid var(--border); margin-top: 40px; }
.tag { display: inline-block; padding: 2px 9px; border-radius: 10px;
  font-size: 11px; font-weight: 600; background: #eef2ff; color: #4338ca; }
.tag.tag-market { background: #ecfdf5; color: #065f46; }
.tag.tag-aggregator { background: #eff6ff; color: #1d4ed8; }
.tag.tag-expert { background: #fef3c7; color: #92400e; }
.tag.tag-model { background: #fae8ff; color: #86198f; }
"""


def _site_header(active: str, latest_ts: datetime | None, league_format: str) -> str:
    league_label = {"sf_ppr": "Superflex PPR", "1qb_ppr": "1QB PPR"}.get(league_format, league_format)
    ts = latest_ts.strftime("%B %d, %Y at %I:%M %p") if latest_ts else "—"

    def link(href, label, key):
        cls = ' style="opacity:1;text-decoration:underline"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<header class="site">
  <div class="row">
    <div>
      <h1><a href="index.html">Dynasty Model</a></h1>
      <div class="meta">{league_label} · Last updated {ts}</div>
    </div>
    <nav>
      {link("index.html", "Overview", "index")}
      {link("rankings.html", "Rankings", "rankings")}
      {link("sources.html", "Sources & Methodology", "sources")}
    </nav>
  </div>
</header>"""


def _footer() -> str:
    return '<footer>Generated locally by the Dynasty Model. All data sourced respectfully.</footer>'


def _page(title: str, header_html: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="assets/style.css">
</head><body>
{header_html}
{body_html}
{_footer()}
</body></html>"""


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------

def _latest_composite(session, league_format: str, limit: int = 300):
    latest_ts = session.execute(
        select(func.max(CompositeScore.generated_at))
        .where(CompositeScore.league_format == league_format)
    ).scalar_one_or_none()
    if latest_ts is None:
        return None, []
    rows = session.execute(
        select(CompositeScore, Player)
        .join(Player, CompositeScore.player_id == Player.id)
        .where(CompositeScore.league_format == league_format)
        .where(CompositeScore.generated_at == latest_ts)
        .order_by(CompositeScore.overall_rank)
        .limit(limit)
    ).all()
    return latest_ts, rows


def _all_sources(session):
    return list(session.execute(select(Source).order_by(Source.slug)).scalars().all())


# --------------------------------------------------------------------------
# Page: index.html — landing page with methodology + divergence highlights
# --------------------------------------------------------------------------

def _build_index(rows, sources, latest_ts, league_format: str) -> str:
    # Compute divergence highlights
    rows_with_div = [(cs, p) for cs, p in rows if cs.rank_divergence is not None]
    biggest_buys = sorted(rows_with_div, key=lambda x: -x[0].rank_divergence)[:8]
    biggest_sells = sorted(rows_with_div, key=lambda x: x[0].rank_divergence)[:8]

    consensus_sources = [s for s in sources if s.category in ("market", "aggregator") and s.last_synced_at]
    evaluator_sources = [s for s in sources if s.category in ("expert", "model") and s.last_synced_at]

    def _div_table(items, label):
        if not items:
            return '<p style="color:var(--muted);font-style:italic">None yet — add evaluator sources to see divergence.</p>'
        rows_html = ""
        for cs, p in items:
            slug = _slugify(p.full_name, p.id)
            rows_html += f"""<tr class="player-row" onclick="location='players/{slug}.html'">
<td class="rank">{cs.overall_rank}</td>
<td class="name">{_esc(p.full_name)}</td>
<td>{_pos_badge(p.position)}</td>
<td class="team">{_esc(p.nfl_team or '—')}</td>
<td class="consensus">{cs.consensus_rank if cs.consensus_rank else '—'}</td>
<td>{_divergence_chip(cs.rank_divergence)}</td>
</tr>"""
        return f"""<table>
<thead><tr><th>Model #</th><th>Player</th><th>Pos</th><th>Team</th><th>Consensus</th><th>Delta</th></tr></thead>
<tbody>{rows_html}</tbody></table>"""

    # Status callout reflecting current model state
    eval_count = len(evaluator_sources)
    if eval_count == 0:
        callout_html = """<div class="callout">
<strong>Consensus-only mode:</strong> only market/aggregator sources are active right now.
Add evaluator sources (Brainy Ballers, PFF, Lance Zierlein, etc.) to see meaningful
divergence from consensus.
</div>"""
    else:
        eval_names = ", ".join(s.name.split(" — ")[0] for s in evaluator_sources[:5])
        callout_html = f"""<div class="callout" style="background:#ecfdf5;border-color:#10b981;color:#065f46;border-left-color:#10b981">
<strong>{eval_count} evaluator source{'' if eval_count == 1 else 's'} active</strong> alongside
{len(consensus_sources)} consensus source{'' if len(consensus_sources) == 1 else 's'}: {_esc(eval_names)}.
The "biggest buys / sells" tables below show where the model's evaluator-weighted ranking
diverges from the market consensus.
</div>"""

    sources_summary_html = ""
    for s in sources:
        desc = SOURCE_DESCRIPTIONS.get(s.slug, {})
        blurb = desc.get("blurb", s.notes or "")
        cat_tag = f'<span class="tag tag-{s.category}">{s.category}</span>'
        status = s.last_synced_at.strftime("%Y-%m-%d") if s.last_synced_at else "not yet synced"
        sources_summary_html += f"""<tr>
<td><strong>{_esc(s.name)}</strong><div style="color:var(--muted);font-size:13px;margin-top:2px">{_esc(blurb)}</div></td>
<td>{cat_tag}</td>
<td style="text-align:right;font-variant-numeric:tabular-nums">{s.default_weight:.2f}</td>
<td style="color:var(--muted);font-size:13px">{status}</td>
</tr>"""

    return _page(
        f"Dynasty Model — Overview",
        _site_header("index", latest_ts, league_format),
        f"""<div class="container narrow">

<div class="card">
<h2 style="margin-top:0">What this is</h2>
<p>This is a dynasty fantasy football ranking model that aggregates multiple sources
into a single composite ranking, weighted by each source's measured accuracy at
predicting actual NFL fantasy production. It is designed to surface where the
model <em>disagrees</em> with the consensus market — those gaps are where
edge lives.</p>
</div>

{callout_html}

<div class="card">
<h2 style="margin-top:0">How the model works</h2>
<ol class="steps">
<li><strong>Collect rankings from every source</strong> and store them as a time series — never overwrite, always append. This means we can backtest historical rankings against actual outcomes later, and detect movement.</li>
<li><strong>Normalize each source to a 0–100 scale.</strong> If the source publishes market values (FantasyCalc), use those directly. Otherwise convert the player's rank position to a score so the depth-1 player gets ~100 and depth-300 gets ~0.</li>
<li><strong>Weight each source two ways:</strong> first by a default weight reflecting its category (evaluators with strong methodology get more), then by a track-record multiplier from backtested accuracy (Spearman correlation between the source's pre-NFL-Draft rankings and actual production over the player's first three NFL seasons).</li>
<li><strong>Compute the weighted average</strong> per player — that's the model score, sorted into the overall ranking.</li>
<li><strong>Compute consensus rank separately</strong> using <em>only</em> market and aggregator sources — that represents where the broader fantasy community has the player.</li>
<li><strong>Compute divergence = consensus_rank − model_rank.</strong> Positive numbers mean the model rates the player higher than consensus (a "buy" signal); negative means lower (a "sell" signal).</li>
</ol>
</div>

<h2>Where the model disagrees with consensus</h2>
<p>The interesting players aren't the ones everyone agrees on. They're the ones where the model and the market diverge.</p>

<h3 style="margin-top:18px">Model is higher than consensus (buys)</h3>
{_div_table(biggest_buys, "buys")}

<h3 style="margin-top:30px">Model is lower than consensus (sells)</h3>
{_div_table(biggest_sells, "sells")}

<h2>Sources contributing to today's rankings</h2>
<table>
<thead><tr><th>Source</th><th>Type</th><th style="text-align:right">Weight</th><th>Last Sync</th></tr></thead>
<tbody>{sources_summary_html}</tbody>
</table>
<p style="margin-top:12px"><a href="sources.html">View detailed source-by-source methodology →</a></p>

</div>""")


# --------------------------------------------------------------------------
# Page: rankings.html — full top-300
# --------------------------------------------------------------------------

def _build_rankings(rows, latest_ts, league_format: str) -> str:
    rows_html = ""
    for cs, p in rows:
        slug = _slugify(p.full_name, p.id)
        pos_rank_str = f'{p.position}{cs.position_rank}' if cs.position_rank else '—'
        cons_str = str(cs.consensus_rank) if cs.consensus_rank else '—'
        rows_html += f"""<tr class="player-row" data-name="{_esc(p.full_name.lower())}" data-position="{_esc(p.position or '')}" onclick="location='players/{slug}.html'">
<td class="rank">{cs.overall_rank}</td>
<td class="name">{_esc(p.full_name)}</td>
<td>{_pos_badge(p.position)}</td>
<td class="team">{_esc(p.nfl_team or '—')}</td>
<td class="pos-rank">{pos_rank_str}</td>
<td class="tier">T{cs.tier or '—'}</td>
<td class="consensus">{cons_str}</td>
<td>{_divergence_chip(cs.rank_divergence)}</td>
<td class="score">{cs.score:.1f}</td>
</tr>"""

    return _page(
        "Dynasty Model — Rankings",
        _site_header("rankings", latest_ts, league_format),
        f"""<div class="container">

<div class="controls">
  <input type="text" id="search" placeholder="Search by player name…">
  <select id="pos-filter">
    <option value="">All positions</option>
    <option value="QB">QB</option><option value="RB">RB</option>
    <option value="WR">WR</option><option value="TE">TE</option>
  </select>
  <span class="stats" id="stats">{len(rows)} players</span>
</div>

<table>
<thead><tr>
  <th>#</th><th>Player</th><th>Pos</th><th>Team</th>
  <th>Pos Rank</th><th>Tier</th><th>Consensus</th><th>Delta</th>
  <th style="text-align:right">Score</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<p style="color:var(--muted);font-size:13px;margin-top:14px">
Click any row to see how the model arrived at this player's ranking, including
per-source contributions and where it diverges from consensus.
</p>

</div>
<script>
const search = document.getElementById('search');
const posFilter = document.getElementById('pos-filter');
const rows = document.querySelectorAll('.player-row');
const stats = document.getElementById('stats');
function apply() {{
  const q = search.value.toLowerCase().trim();
  const pos = posFilter.value;
  let n = 0;
  rows.forEach(r => {{
    const matchName = !q || r.dataset.name.includes(q);
    const matchPos = !pos || r.dataset.position === pos;
    const show = matchName && matchPos;
    r.style.display = show ? '' : 'none';
    if (show) n++;
  }});
  stats.textContent = n + ' players';
}}
search.addEventListener('input', apply);
posFilter.addEventListener('change', apply);
</script>""")


# --------------------------------------------------------------------------
# Page: sources.html — methodology and how to add evaluators
# --------------------------------------------------------------------------

def _build_sources_page(sources, latest_ts, league_format: str) -> str:
    active_sources_html = ""
    for s in sources:
        desc = SOURCE_DESCRIPTIONS.get(s.slug, {})
        status = s.last_synced_at.strftime("%Y-%m-%d %H:%M") if s.last_synced_at else "—"
        wjust = desc.get("weight_justification", "")
        active_sources_html += f"""<div class="card">
<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px">
  <div>
    <h3 style="margin:0">{_esc(s.name)} <span class="tag tag-{s.category}">{s.category}</span></h3>
    <div style="color:var(--muted);font-size:13px;margin-top:4px">Last synced: {status} · Weight: <strong>{s.default_weight:.2f}</strong></div>
  </div>
  {f'<a href="{_esc(s.url)}" target="_blank" style="font-size:13px">visit →</a>' if s.url else ''}
</div>
<p style="margin:14px 0 8px 0">{_esc(desc.get('blurb', s.notes or ''))}</p>
{f'<p style="font-size:13px;margin:4px 0"><strong style="color:var(--accent)">Strength:</strong> {_esc(desc.get("strength", ""))}</p>' if desc.get("strength") else ''}
{f'<p style="font-size:13px;margin:4px 0"><strong style="color:#b91c1c">Limitation:</strong> {_esc(desc.get("weakness", ""))}</p>' if desc.get("weakness") else ''}
{f'<p style="font-size:13px;margin:10px 0 4px 0;padding:10px 12px;background:var(--hover);border-radius:6px;border-left:3px solid var(--accent)"><strong>Why this weight:</strong> {_esc(wjust)}</p>' if wjust else ''}
</div>"""

    eval_html = ""
    for ev in EVALUATOR_RECOMMENDATIONS:
        sw = ev.get("suggested_weight")
        eval_html += f"""<div class="card">
<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px">
  <h3 style="margin:0">{_esc(ev['name'])}</h3>
  <a href="{_esc(ev['url'])}" target="_blank" style="font-size:13px">visit →</a>
</div>
<div style="color:var(--muted);font-size:13px;margin-top:2px">Scope: {_esc(ev['scope'])}{f' · Suggested weight: <strong>{sw:.1f}</strong>' if sw else ''}</div>
<p style="margin:12px 0">{_esc(ev['why'])}</p>
<div style="background:var(--hover);padding:10px 14px;border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--accent)">
Import as: <strong>{_esc(ev['import_as'])}</strong>
</div>
</div>"""

    return _page(
        "Dynasty Model — Sources & Methodology",
        _site_header("sources", latest_ts, league_format),
        f"""<div class="container narrow">

<div class="card">
<h2 style="margin-top:0">Source weighting</h2>
<p>Each source contributes to the composite score through a weight calculated as:</p>
<pre style="background:var(--hover);padding:14px;border-radius:6px;font-size:13px;overflow:auto">effective_weight = default_weight × track_record_multiplier</pre>
<p><strong>Default weight</strong> is set per source based on documented evidence of historical accuracy. The weighting hierarchy:</p>
<ul>
<li><strong>1.4</strong> — Documented top performer in independent multi-year accuracy studies (e.g., Lance Zierlein in the Hogs Haven wAV correlation study)</li>
<li><strong>1.3</strong> — Transparent algorithmic models with published methodology and verifiable hit-rate curves (e.g., PFF, Brainy Ballers SPS)</li>
<li><strong>1.1–1.2</strong> — Strong evaluators with measured industry-level accuracy (e.g., Daniel Jeremiah, Matt Harmon's Reception Perception)</li>
<li><strong>1.0</strong> — Consensus aggregators (FantasyCalc, DynastyProcess, FantasyPros ECR)</li>
<li><strong>0.6–0.9</strong> — Sources with documented underperformance in independent studies</li>
</ul>
<p><strong>Track-record multiplier</strong> is computed from backtest results once you load historical pre-NFL-Draft rankings and actual fantasy production. Multiplier mapping:</p>
<ul>
<li><code>|spearman_corr| ≥ 0.7</code> → multiplier <strong>1.5</strong> (proven elite evaluator)</li>
<li><code>|spearman_corr| ≥ 0.5</code> → multiplier <strong>1.2</strong> (above-average)</li>
<li><code>|spearman_corr| ≥ 0.3</code> → multiplier <strong>1.0</strong> (baseline)</li>
<li><code>|spearman_corr| &lt; 0.3</code> → multiplier <strong>0.6</strong> (de-weighted)</li>
<li>No backtest yet → multiplier <strong>1.0</strong> (neutral)</li>
</ul>
<p>Sources with proven accuracy thus compound their influence over time, while sources that have systematically been wrong get progressively de-weighted as backtest data accumulates.</p>
</div>

<div class="card">
<h2 style="margin-top:0">Research backing the default weights</h2>
<p>The default weights aren't arbitrary. They're grounded in publicly-available accuracy research:</p>
<ul>
<li><strong>Hogs Haven (2022 study)</strong> — correlated draft analysts' pre-NFL-Draft rankings against Weighted Career Approximate Value (wAV) for the 2018-2020 drafts. Lance Zierlein and Mel Kiper led; PFF and CBS trailed for draft-order prediction.</li>
<li><strong>Inside The Star 2025 mock-draft accuracy review</strong> — Jeremiah edged out Kiper and McShay for Round 1 prediction accuracy.</li>
<li><strong>FantasyPros Most Accurate Expert Awards</strong> — multi-year industry-standard accuracy scoring across all rankings. Cited where applicable on individual source pages.</li>
<li><strong>PFF's own published prospect score-to-outcome curves</strong> — they publish hit-rate buckets by prospect-model-score band, making their model uniquely auditable among major analytics services.</li>
<li><strong>Brainy Ballers SPS</strong> — publicly cited successes on Jaxson Dart, JSN, Trey McBride, Saquon, Kelce, McCaffrey. Featured on Pat McAfee, Forbes, Barstool. Independent backtest pending in this model.</li>
</ul>
<p style="color:var(--muted);font-size:13px;margin-top:14px"><em>Important caveat:</em> the default weights are starting points. The track-record multiplier will dominate them once you backtest each source against actual NFL outcomes using <code>cli.py backtest</code>. Self-reported accuracy claims (including from highly-cited sources) get scrutinized by the backtest like any other.</p>
</div>

<h2>Active sources</h2>
{active_sources_html}

<h2>Add these evaluators to expand model coverage</h2>
<div class="callout">
<strong>Beyond what's already loaded:</strong> these evaluators have strong track records but
publish behind paywalls. If you subscribe to any of them, you can export their rankings
to a CSV and import them here. See the README for the exact <code>manual_import.import_csv</code>
command. Each evaluator below shows a suggested weight based on their documented track record.
</div>

{eval_html}

</div>""")


# --------------------------------------------------------------------------
# Page: players/<slug>.html — individual player detail
# --------------------------------------------------------------------------

def _build_player_page(cs, p, all_sources, latest_ts, league_format: str) -> str:
    try:
        breakdown = json.loads(cs.breakdown_json) if cs.breakdown_json else {}
    except Exception:
        breakdown = {}

    # Build breakdown rows, sorted by weight (highest contribution first)
    sources_by_slug = {s.slug: s for s in all_sources}
    items = []
    total_w = sum(b.get("weight", 0) for b in breakdown.values())
    for slug, b in breakdown.items():
        items.append({
            "slug": slug,
            "name": sources_by_slug.get(slug).name if slug in sources_by_slug else slug,
            "category": b.get("category", sources_by_slug.get(slug).category if slug in sources_by_slug else "—"),
            "score": b.get("score"),
            "raw_rank": b.get("raw_rank"),
            "weight": b.get("weight", 0),
            "pct": (b.get("weight", 0) / total_w * 100) if total_w else 0,
        })
    items.sort(key=lambda x: -x["weight"])

    breakdown_html = ""
    for it in items:
        bar_width = min(220, it["pct"] * 2.2)
        rank_str = f"#{it['raw_rank']}" if it["raw_rank"] else "—"
        breakdown_html += f"""<tr>
<td class="source-name">{_esc(it['name'])}</td>
<td><span class="tag tag-{it['category']}">{it['category']}</span></td>
<td style="text-align:right;font-variant-numeric:tabular-nums">{rank_str}</td>
<td style="text-align:right;font-variant-numeric:tabular-nums">{it['score']:.1f}</td>
<td style="text-align:right;font-variant-numeric:tabular-nums"><span class="weight-bar" style="width:{bar_width}px"></span>{it['weight']:.2f}</td>
<td style="text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)">{it['pct']:.0f}%</td>
</tr>"""

    # Build the "why model diverges" explanation
    div_explanation = ""
    if cs.rank_divergence is None:
        div_explanation = "<p style='color:var(--muted)'>No consensus rank available for this player — likely outside the depth that market/aggregator sources cover.</p>"
    elif cs.rank_divergence == 0:
        div_explanation = "<p>The model and consensus are aligned on this player.</p>"
    else:
        direction = "higher" if cs.rank_divergence > 0 else "lower"
        magnitude_word = "significantly" if abs(cs.rank_divergence) >= 10 else "modestly"
        # Identify which non-consensus sources are pulling the model
        non_consensus = [it for it in items if it["category"] not in ("market", "aggregator")]
        consensus_items = [it for it in items if it["category"] in ("market", "aggregator")]

        explanation_parts = [
            f"The model ranks this player <strong>{magnitude_word} {direction}</strong> than consensus "
            f"(model #{cs.overall_rank} vs consensus #{cs.consensus_rank})."
        ]
        if non_consensus:
            sn = ", ".join(it["name"] for it in non_consensus[:3])
            explanation_parts.append(
                f"This divergence is driven by evaluator sources outside the consensus stream: <em>{sn}</em>."
            )
        else:
            explanation_parts.append(
                "<strong>No evaluator sources are currently active</strong> — the divergence here is small "
                "and only reflects modest differences between the consensus aggregators themselves. To see meaningful "
                "model-vs-consensus signal, add evaluator sources (see the Sources page)."
            )
        div_explanation = "<p>" + " ".join(explanation_parts) + "</p>"

    # Header metrics
    metrics_html = f"""<div class="metrics">
<div class="metric"><div class="num">#{cs.overall_rank}</div><div class="label">Model Rank</div></div>
<div class="metric"><div class="num">{f'#{cs.consensus_rank}' if cs.consensus_rank else '—'}</div><div class="label">Consensus</div></div>
<div class="metric"><div class="num">{cs.score:.1f}</div><div class="label">Score</div></div>
<div class="metric"><div class="num">T{cs.tier}</div><div class="label">Tier</div></div>
{f'<div class="metric"><div class="num">{p.position}{cs.position_rank}</div><div class="label">Pos Rank</div></div>' if cs.position_rank else ''}
</div>"""

    body = f"""<div class="player-header">
<a href="../rankings.html" style="color:white;opacity:0.8;font-size:13px">← back to rankings</a>
<h1 style="margin-top:8px">{_esc(p.full_name)} {_pos_badge(p.position)}</h1>
<div class="sub">{_esc(p.nfl_team or 'Free agent')} · {_esc(p.position or '—')}</div>
{metrics_html}
</div>

<div class="container narrow">

<div class="card divergence-section">
<h2 style="margin-top:0">Model vs. Consensus</h2>
<div style="display:flex;gap:16px;align-items:center;margin:10px 0">
<div style="font-size:32px;font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums">#{cs.overall_rank}</div>
<div style="color:var(--muted)">model</div>
<div style="font-size:24px;color:var(--muted);margin:0 8px">vs</div>
<div style="font-size:32px;font-weight:700;color:var(--muted);font-variant-numeric:tabular-nums">{f'#{cs.consensus_rank}' if cs.consensus_rank else '—'}</div>
<div style="color:var(--muted)">consensus</div>
<div style="margin-left:auto;font-size:18px">{_divergence_chip(cs.rank_divergence)}</div>
</div>
<div class="div-explanation">{div_explanation}</div>
</div>

<div class="card">
<h2 style="margin-top:0">Source breakdown</h2>
<p style="color:var(--muted);font-size:14px">How each source contributed to this player's composite score. Sources with higher weights drive more of the final ranking.</p>
<table class="breakdown-table">
<thead><tr>
<th>Source</th><th>Type</th><th style="text-align:right">Their Rank</th>
<th style="text-align:right">Score</th><th style="text-align:right">Weight</th><th style="text-align:right">% of Total</th>
</tr></thead>
<tbody>{breakdown_html}</tbody>
</table>
</div>

</div>"""

    return _page(
        f"{p.full_name} — Dynasty Model",
        _site_header("rankings", latest_ts, league_format),
        body,
    )


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------

def generate_site(
    output_dir: str = "dynasty_site",
    league_format: str = "sf_ppr",
    limit: int = 300,
) -> str:
    """Generate the multi-page site. Returns the absolute path to index.html."""
    out_root = Path(output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "assets").mkdir(exist_ok=True)
    (out_root / "players").mkdir(exist_ok=True)

    # Shared CSS
    (out_root / "assets" / "style.css").write_text(_shared_css(), encoding="utf-8")

    with get_session() as session:
        latest_ts, rows = _latest_composite(session, league_format, limit)
        sources = _all_sources(session)

        if not rows:
            (out_root / "index.html").write_text(_page(
                "Dynasty Model — No Data",
                _site_header("index", None, league_format),
                """<div class="container narrow">
<div class="callout"><strong>No rankings have been generated yet.</strong>
Run the launcher again — make sure the sync step completes successfully.</div>
</div>""",
            ), encoding="utf-8")
            return str(out_root / "index.html")

        # Landing page
        (out_root / "index.html").write_text(
            _build_index(rows, sources, latest_ts, league_format), encoding="utf-8"
        )

        # Full rankings
        (out_root / "rankings.html").write_text(
            _build_rankings(rows, latest_ts, league_format), encoding="utf-8"
        )

        # Sources & methodology
        (out_root / "sources.html").write_text(
            _build_sources_page(sources, latest_ts, league_format), encoding="utf-8"
        )

        # Per-player pages
        for cs, p in rows:
            slug = _slugify(p.full_name, p.id)
            (out_root / "players" / f"{slug}.html").write_text(
                _build_player_page(cs, p, sources, latest_ts, league_format),
                encoding="utf-8"
            )

    return str(out_root / "index.html")


# Backwards-compat: keep the old single-file generator as a thin wrapper.
def generate_report(output_path: str = "dynasty_rankings.html",
                    league_format: str = "sf_ppr", limit: int = 300) -> str:
    """Legacy single-file output. Calls generate_site under a parent dir."""
    out_dir = Path(output_path).resolve().parent / "dynasty_site"
    return generate_site(str(out_dir), league_format, limit)
