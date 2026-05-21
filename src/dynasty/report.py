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
from .sources.similarity_career_arc import load_comps_cache


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
    "nfl_draft_capital": {
        "blurb": "Every NFL draft pick since 1980 from the public nflverse CSV. Treated as an objective evaluator opinion.",
        "type": "Analytics model (draft signal)",
        "strength": (
            "NFL draft capital is the single strongest predictor of rookie fantasy production "
            "(r ≈ 0.4–0.6 vs. 3-year fantasy points). Implicitly captures medicals, character, and "
            "private scouting that NFL teams paid for. Free, ToS-clean, no scraping."
        ),
        "weakness": (
            "Static between drafts — doesn't update mid-season. Less useful for veterans whose "
            "NFL production has already overwritten the draft-day signal (PR #6 — v0.7 weighting refactor — "
            "decays this signal as years-pro increases)."
        ),
        "weight_justification": (
            "Default weight 1.5 (highest in the registry). At QB, position modifier of 1.2x boosts "
            "it further because team draft-capital commitment correlates with opportunity. Years-pro "
            "decay floors it at 0.3x for Year-5+ veterans."
        ),
    },
    "ffc_adp": {
        "blurb": "FantasyFootballCalculator ADP across PPR / 2QB / Dynasty / Rookie formats. Live from real mock drafts.",
        "type": "Market signal (secondary)",
        "strength": (
            "Second market signal alongside FantasyCalc, deliberately uncorrelated: FFC's user base "
            "skews casual and redraft, so it catches sentiment the dynasty-trader crowd lags. Rookie "
            "ADP especially valuable — it moves within hours of draft night."
        ),
        "weakness": "Casual user base means more noise and higher variance than FantasyCalc.",
        "weight_justification": (
            "Default weight 0.7 — lower than FantasyCalc because the underlying drafters are less serious. "
            "Treated as 'consensus' for the divergence calculation."
        ),
    },
    "ras": {
        "blurb": "Relative Athletic Score (Kent Lee Platte). Position-adjusted composite of Combine/Pro Day testing.",
        "type": "Analytics model (athleticism)",
        "strength": (
            "Best free single-number athleticism composite. Most useful as a *bust filter*: prospects "
            "with RAS < 5 in positions that demand athleticism (WR/TE) substantially underperform "
            "their draft capital."
        ),
        "weakness": (
            "Modest standalone signal at WR (r ≈ 0.10–0.15). Near-zero predictive value for QB. "
            "Requires a local CSV file at data/ras/ras_database.csv — yields zero rows when missing."
        ),
        "weight_justification": (
            "Default weight 0.8. Position modifier amplifies to 1.5x for WR/TE, 1.2x for RB, drops to "
            "0.3x at QB. Years-pro decay treats RAS strictly as a pre-NFL signal that fades for vets."
        ),
    },
    "cfbd_breakouts": {
        "blurb": "Engineered college signals: Breakout Age + Best College Dominator Rating.",
        "type": "Analytics model (college production)",
        "strength": (
            "The two highest-signal college production metrics in the public literature. Breakout Age "
            "shows r ≈ 0.43 with NFL fantasy points at WR; College Dominator captures 'was this guy his "
            "college team's go-to player?'. Together they replicate ~80% of PlayerProfiler's paid signal."
        ),
        "weakness": (
            "Requires a local CSV at data/cfbd/breakouts.csv with pre-computed features. Live CFBD API "
            "integration is a planned follow-up."
        ),
        "weight_justification": (
            "Default weight 0.9. Position modifier 1.5x at WR, 1.3x at TE, 1.0x at RB, 0.4x at QB. "
            "Like RAS, decays with years-pro because actual NFL production should dominate vets."
        ),
    },
    "similarity_career_arc": {
        "blurb": "KNN comparables over the PFR / nflverse player-season corpus (1999–2024). "
                 "For each active NFL player, the top 20 historical seasons at the same "
                 "position and age are aggregated into a projected remaining-career value "
                 "(time-discounted 5%/yr).",
        "type": "Analytics model (similarity engine)",
        "strength": (
            "Encodes both current skill (via per-game production / efficiency / usage "
            "features) and longevity (via comp future careers). Surfaces the \"young "
            "players are more valuable\" principle directly: a 22yo whose comps averaged "
            "8 productive seasons after their comp year scores far above a 32yo with the "
            "same current production whose comps averaged 2."
        ),
        "weakness": (
            "Only covers active NFL players (rookies still rely on draft capital + CFBD "
            "breakouts in v0.14; a college→NFL similarity chain is planned for PR #15). "
            "Comps with sparse age windows (e.g. very young or very old players) get "
            "smaller pools."
        ),
        "weight_justification": (
            "Default weight 1.8 — the DOMINANT signal in the v0.14 composite per Phil's "
            "directive to put similarity scores at the heart of the model."
        ),
    },
    "nfl_impact": {
        "blurb": "DARKO-style current-skill signal: per-position efficiency formulas "
                 "computed from the PFR / nflverse corpus, normalized 0–100 within position.",
        "type": "Analytics model (current skill)",
        "strength": (
            "Independent of any market signal — derived purely from on-field production "
            "and efficiency. ANY/A + TD%-INT% + sack rate for QBs; yards-per-touch + TD "
            "rate + target share for RBs; YPRR proxy + aDOT + TD rate for WR/TE."
        ),
        "weakness": (
            "Single-season snapshot. The similarity engine carries the longevity weight "
            "(NFL Impact is the 'how good are they right now' signal)."
        ),
        "weight_justification": (
            "Default weight 0.8. Strong but not dominant — paired with the similarity "
            "engine (1.8) which encodes longevity."
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
      {link("league.html", "Rate My League", "league")}
      {link("sources.html", "Sources", "sources")}
      {link("methodology.html", "Methodology", "methodology")}
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

def _build_rankings(rows, latest_ts, league_format: str, comps_cache: dict | None = None) -> str:
    rows_html = ""
    comps_cache = comps_cache or {}
    for cs, p in rows:
        slug = _slugify(p.full_name, p.id)
        pos_rank_str = f'{p.position}{cs.position_rank}' if cs.position_rank else '—'
        cons_str = str(cs.consensus_rank) if cs.consensus_rank else '—'
        # v0.14.0: hover tooltip showing the top 3 historical comps
        title = ""
        ce = comps_cache.get(p.gsis_id) if p.gsis_id else None
        if ce and ce.get("comparables"):
            top = ce["comparables"][:3]
            title = "Most similar: " + "; ".join(
                f"{c.get('name', '')} ({c.get('season', '')}, sim {c.get('similarity', 0):.2f})"
                for c in top
            )
        title_attr = f' title="{_esc(title)}"' if title else ""
        rows_html += f"""<tr class="player-row"{title_attr} data-name="{_esc(p.full_name.lower())}" data-position="{_esc(p.position or '')}" onclick="location='players/{slug}.html'">
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
  <span style="margin-left:auto;font-size:13px;color:var(--muted)">
    Hover a row for top historical comps ·
    <a href="methodology.html">customize overlays →</a>
  </span>
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
# Page: league.html — client-side Sleeper league evaluator
# --------------------------------------------------------------------------

def _build_league_page(latest_ts, league_format: str) -> str:
    import os
    title = "Dynasty Model — Rate My League"
    header = _site_header("league", latest_ts, league_format)
    # PROXY_URL is the deployed Cloudflare Worker URL (see
    # scripts/cf-worker/README.md). When set at build time, the MFL form
    # on the page actually works against the user's league. When unset,
    # the form switches to "add to leagues.json" guidance.
    raw_proxy = os.environ.get("PROXY_URL", "").rstrip("/")
    # Defensive sanitization: only pass through a simple https URL.
    import re as _re
    if _re.match(r"^https?://[A-Za-z0-9.\-_/]+$", raw_proxy):
        proxy_url = raw_proxy
    else:
        proxy_url = ""
    body = """<div class="container narrow">
<h1>Rate My League</h1>
<p>Two ways in:</p>
<ul style="margin:0 0 24px 0;padding-left:24px">
  <li><strong>Pre-fetched leagues</strong> below (Sleeper + MFL) — includes manager skill rankings from draft + trade history.</li>
  <li><strong>Any Sleeper league</strong> by ID using the form below the pre-fetched section (live, team rankings only).</li>
</ul>

<div id="prefetched-section" style="margin:24px 0"></div>

<h2 style="margin-top:48px">Evaluate any league</h2>

<form id="league-form" onsubmit="return evalLeague(event)" style="margin:24px 0" data-proxy-url="__PROXY_URL__">
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:stretch">
    <select id="platform" onchange="onPlatformChange()"
            style="padding:10px 14px;border:1px solid var(--border);border-radius:6px;font-size:15px;background:white">
      <option value="sleeper">Sleeper</option>
      <option value="mfl">MyFantasyLeague</option>
    </select>
    <input id="league-id" type="text" placeholder="e.g. 968712712272838656"
           style="flex:1;min-width:240px;padding:10px 14px;border:1px solid var(--border);border-radius:6px;font-size:15px"
           required>
    <input id="mfl-year" type="number" placeholder="Year" min="2000" max="2099"
           style="width:90px;padding:10px 14px;border:1px solid var(--border);border-radius:6px;font-size:15px;display:none">
    <button type="submit"
            style="padding:10px 18px;background:#1d4ed8;color:white;border:0;border-radius:6px;font-weight:600;font-size:15px;cursor:pointer">
      Evaluate league
    </button>
  </div>
  <label style="display:flex;align-items:center;gap:8px;font-size:14px;color:var(--muted);margin-bottom:16px">
    <input id="include-managers" type="checkbox" checked>
    <span>Also compute manager skill rankings from draft + trade history (slower — ~20 API calls)</span>
  </label>
  <div id="mfl-proxy-warning" style="display:none;font-size:13px;padding:12px;border-radius:6px;background:#fef3c7;border:1px solid #fde68a;color:#92400e;margin-bottom:16px"></div>

  <details style="margin:8px 0 0 0;padding:14px;border:1px solid var(--border);border-radius:6px">
    <summary style="cursor:pointer;font-weight:600">League settings (optional)</summary>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:14px">
      <label>
        <div style="font-size:13px;color:var(--muted);margin-bottom:4px">QB format</div>
        <select id="qb-format" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:4px">
          <option value="auto" selected>Auto-detect from league</option>
          <option value="1qb">1QB</option>
          <option value="sf">Superflex (1QB + 1 SF)</option>
          <option value="2qb">2QB (two QB starting spots)</option>
        </select>
      </label>
      <label>
        <div style="font-size:13px;color:var(--muted);margin-bottom:4px">TE premium</div>
        <select id="te-prem" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:4px">
          <option value="auto" selected>Auto-detect from league</option>
          <option value="none">No TE premium (1.0 PPR)</option>
          <option value="low">Light TE premium (1.25 PPR)</option>
          <option value="high">Heavy TE premium (1.5 PPR)</option>
        </select>
      </label>
      <label>
        <div style="font-size:13px;color:var(--muted);margin-bottom:4px">Scoring</div>
        <select id="ppr" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:4px">
          <option value="auto" selected>Auto-detect from league</option>
          <option value="full">Full PPR (1.0)</option>
          <option value="half">Half PPR (0.5)</option>
          <option value="standard">Standard / non-PPR</option>
        </select>
      </label>
    </div>
    <p style="margin:14px 0 0 0;color:var(--muted);font-size:12px">
      The base model rates players for <strong>Superflex full-PPR</strong>. These settings apply
      position-value multipliers on top so the team totals and weakness flags match your league's actual
      scarcity. Auto-detect reads <code>roster_positions</code> and <code>scoring_settings</code> from
      the Sleeper league response.
    </p>
  </details>
</form>

<div class="callout" style="font-size:13px">
<strong>Where to find your league ID:</strong>
<ul style="margin:6px 0 0 0;padding-left:20px">
  <li><strong>Sleeper:</strong> open your league → the long number after <code>/leagues/</code> in the URL
      (e.g. <code>sleeper.com/leagues/<strong>968712712272838656</strong>/team</code>).</li>
  <li><strong>MFL:</strong> the 5-digit number in the URL after <code>/YYYY/home/</code>
      (e.g. <code>www48.myfantasyleague.com/2026/home/<strong>12345</strong></code>).</li>
</ul>
</div>

<div id="detected-settings" style="margin:12px 0;font-size:13px;color:var(--muted)"></div>
<div id="league-status" style="margin:16px 0;color:var(--muted);font-size:14px"></div>
<div id="league-results"></div>

<p style="margin-top:40px;color:var(--muted);font-size:13px">
For MFL leagues, the live API has no CORS so the browser can't reach it directly. Add the league to
<code>leagues.json</code> in the repo and it'll appear in the pre-fetched section after the next
daily build.
</p>
</div>

<script>
const STARTING_POSITIONS = ["QB", "RB", "WR", "TE"];
const WEAKNESS_TIER_THRESHOLD = 3;
let MODEL = null;

// Position-value multipliers for league-settings adjustments. Applied to each
// player's base score before computing team totals. These are industry
// conventions, not backtested — if you want precision, run the CLI which has
// access to the full composite model + backtest history.
const QB_MULT  = {"1qb":{"QB":0.65,"RB":1.0,"WR":1.0,"TE":1.0},
                  "sf": {"QB":1.0, "RB":1.0,"WR":1.0,"TE":1.0},
                  "2qb":{"QB":1.25,"RB":0.95,"WR":0.95,"TE":0.95}};
const TE_MULT  = {"none":1.0,"low":1.15,"high":1.25};
const PPR_MULT = {"full":  {"QB":1.0,"RB":1.0, "WR":1.0, "TE":1.0},
                  "half":  {"QB":1.0,"RB":0.97,"WR":0.95,"TE":0.92},
                  "standard":{"QB":1.0,"RB":0.93,"WR":0.88,"TE":0.82}};

async function loadModel() {
  if (MODEL) return MODEL;
  const resp = await fetch("assets/model_scores.json");
  if (!resp.ok) throw new Error("Could not load model scores (assets/model_scores.json)");
  MODEL = await resp.json();
  return MODEL;
}

function detectFromLeague(league) {
  // QB format: count QB-slot starters vs SF slots in roster_positions.
  const rp = league.roster_positions || [];
  const qbCount = rp.filter(p => p === "QB").length;
  const sfCount = rp.filter(p => p === "SUPER_FLEX" || p === "SF").length;
  let qb;
  if (qbCount >= 2) qb = "2qb";
  else if (sfCount >= 1) qb = "sf";
  else qb = "1qb";

  // TE premium: scoring_settings.bonus_rec_te (extra points-per-reception
  // for TEs above the base WR rate).
  const ss = league.scoring_settings || {};
  const bonusTE = (ss.bonus_rec_te || 0) + 0;
  let te;
  if (bonusTE >= 0.5) te = "high";
  else if (bonusTE >= 0.25) te = "low";
  else te = "none";

  // PPR: scoring_settings.rec (points per reception).
  const recPts = (ss.rec || 0) + 0;
  let ppr;
  if (recPts >= 0.9) ppr = "full";
  else if (recPts >= 0.4) ppr = "half";
  else ppr = "standard";

  return { qb, te, ppr, qbCount, sfCount, bonusTE, recPts };
}

function effectiveSettings(detected) {
  const get = (id, fallback) => {
    const v = document.getElementById(id).value;
    return v === "auto" ? fallback : v;
  };
  return {
    qb:  get("qb-format", detected.qb),
    te:  get("te-prem",   detected.te),
    ppr: get("ppr",       detected.ppr),
  };
}

function positionMultiplier(pos, settings) {
  const p = (pos || "").toUpperCase();
  const qbm  = (QB_MULT[settings.qb]  || QB_MULT.sf)[p]  || 1.0;
  const pprm = (PPR_MULT[settings.ppr] || PPR_MULT.full)[p] || 1.0;
  const tem  = p === "TE" ? (TE_MULT[settings.te] || 1.0) : 1.0;
  return qbm * pprm * tem;
}

function getProxyUrl() {
  const form = document.getElementById("league-form");
  const u = (form && form.dataset.proxyUrl) || "";
  return u && u !== "__PROXY_URL__" ? u.replace(/\/$/, "") : "";
}

function onPlatformChange() {
  const plat = document.getElementById("platform").value;
  const yearInput = document.getElementById("mfl-year");
  const warning = document.getElementById("mfl-proxy-warning");
  const idInput = document.getElementById("league-id");
  if (plat === "mfl") {
    yearInput.style.display = "";
    yearInput.value = yearInput.value || String(new Date().getFullYear());
    idInput.placeholder = "e.g. 12345";
    const proxy = getProxyUrl();
    if (!proxy) {
      warning.style.display = "";
      warning.innerHTML = `
        <strong>MFL leagues require a proxy worker.</strong>
        MFL's API doesn't allow direct fetches from <code>github.io</code>.
        Deploy the proxy (see <code>scripts/cf-worker/README.md</code>) and set
        <code>PROXY_URL</code> in the build environment.
        <br><br>
        In the meantime, add your MFL league to <code>leagues.json</code> and it'll appear
        in the pre-fetched section after the next daily build.
      `;
    } else {
      warning.style.display = "none";
    }
  } else {
    yearInput.style.display = "none";
    warning.style.display = "none";
    idInput.placeholder = "e.g. 968712712272838656";
  }
}

// Routed fetcher: routes Sleeper directly (CORS-friendly) and MFL through
// the configured proxy worker (required because MFL's CORS blocks GH Pages).
async function platformFetch(platform, path, params) {
  const proxy = getProxyUrl();
  if (platform === "sleeper") {
    // Always direct for Sleeper. Proxy is only useful for edge caching.
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return fetch(`https://api.sleeper.app${path}${qs}`);
  }
  if (platform === "mfl") {
    if (!proxy) throw new Error("MFL proxy not configured. See scripts/cf-worker/README.md.");
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return fetch(`${proxy}${path}${qs}`);
  }
  throw new Error("unknown platform: " + platform);
}

async function evalLeague(ev) {
  ev.preventDefault();
  const platform = document.getElementById("platform").value;
  const leagueId = document.getElementById("league-id").value.trim();
  const year = (document.getElementById("mfl-year").value || new Date().getFullYear()).toString();
  const includeManagers = document.getElementById("include-managers").checked;
  const status = document.getElementById("league-status");
  const detectedDiv = document.getElementById("detected-settings");
  const results = document.getElementById("league-results");
  results.innerHTML = "";
  detectedDiv.innerHTML = "";
  status.textContent = `Loading model + ${platform.toUpperCase()} league data...`;

  try {
    if (platform === "sleeper") {
      await evalSleeperLeague(leagueId, includeManagers, { status, detectedDiv, results });
    } else if (platform === "mfl") {
      await evalMflLeague(leagueId, year, includeManagers, { status, detectedDiv, results });
    } else {
      throw new Error("unknown platform: " + platform);
    }
  } catch (err) {
    status.textContent = "";
    results.innerHTML = `<div class="callout" style="background:#fef2f2;border-color:#fecaca;color:#991b1b">${escapeHtml(err.message)}</div>`;
  }
  return false;
}

async function evalSleeperLeague(leagueId, includeManagers, { status, detectedDiv, results }) {
  const [model, leagueResp, usersResp, rostersResp] = await Promise.all([
    loadModel(),
    fetch(`https://api.sleeper.app/v1/league/${leagueId}`),
    fetch(`https://api.sleeper.app/v1/league/${leagueId}/users`),
    fetch(`https://api.sleeper.app/v1/league/${leagueId}/rosters`),
  ]);
  if (!leagueResp.ok) throw new Error("Sleeper league not found. Double-check the league ID.");
  const [league, users, rosters] = await Promise.all([
    leagueResp.json(), usersResp.json(), rostersResp.json(),
  ]);

  const detected = detectFromLeague(league);
  const settings = effectiveSettings(detected);
  detectedDiv.innerHTML = renderDetected(detected, settings);

  const userById = {};
  (users || []).forEach(u => { userById[u.user_id] = u.display_name || u.username || u.user_id; });
  const franchiseNames = {};
  (rosters || []).forEach(r => {
    franchiseNames[String(r.roster_id)] = userById[r.owner_id] || ("Team " + r.roster_id);
  });

  const teams = (rosters || []).map(r => evaluateTeam(r, userById, model, settings));
  const leagueAvg = teams.length ? teams.reduce((a, t) => a + t.total, 0) / teams.length : 0;
  teams.forEach(t => { t.vsAvg = t.total - leagueAvg; });
  const sorted = [...teams].sort((a, b) => b.total - a.total);
  status.textContent = `${league.name || "Sleeper league " + leagueId} — ${teams.length} teams — league avg ${leagueAvg.toFixed(1)} (settings-adjusted)`;
  results.innerHTML = renderResults(league.name, sorted);

  if (includeManagers) {
    status.textContent = status.textContent + " — fetching draft + trade history...";
    const mgr = await computeSleeperManagerReport(leagueId, franchiseNames, model);
    results.innerHTML += renderManagerReport(mgr);
    status.textContent = `${league.name || "Sleeper league " + leagueId} — ${teams.length} teams — ${mgr.managers.length} managers ranked`;
  }
}

async function evalMflLeague(leagueId, year, includeManagers, { status, detectedDiv, results }) {
  const proxy = getProxyUrl();
  if (!proxy) {
    throw new Error("MFL leagues require a proxy worker. See scripts/cf-worker/README.md, or add the league to leagues.json for the next daily build.");
  }
  const model = await loadModel();
  const leagueResp = await fetch(`${proxy}/mfl/${year}/export?TYPE=league&L=${leagueId}&JSON=1`);
  if (!leagueResp.ok) throw new Error("MFL league not found. Check league ID + year.");
  const leaguePayload = await leagueResp.json();
  const league = (leaguePayload && leaguePayload.league) || {};
  const franchisesMeta = ((league.franchises || {}).franchise) || [];
  const franchiseArr = Array.isArray(franchisesMeta) ? franchisesMeta : [franchisesMeta];
  const franchiseNames = {};
  franchiseArr.forEach(f => { franchiseNames[String(f.id)] = f.name || String(f.id); });

  const rostersResp = await fetch(`${proxy}/mfl/${year}/export?TYPE=rosters&L=${leagueId}&JSON=1`);
  const rostersPayload = await rostersResp.json();
  const franchisesEntry = ((rostersPayload.rosters || {}).franchise) || [];
  const franchisesList = Array.isArray(franchisesEntry) ? franchisesEntry : [franchisesEntry];

  // Build pseudo-Sleeper-shaped rosters so we can reuse evaluateTeam().
  const fakeRosters = franchisesList.map(f => {
    let playerEntry = f.player || [];
    if (!Array.isArray(playerEntry)) playerEntry = [playerEntry];
    return {
      roster_id: f.id,
      owner_id: f.id,
      players: playerEntry.map(p => String(p.id)).filter(Boolean),
    };
  });

  // MFL rosters use mfl_id which our model_scores.json doesn't index. We need
  // an mfl_id -> entry lookup. Since our pre-built model JSON is keyed by
  // sleeper_id, MFL evaluations only work for players who happen to ALSO have
  // a sleeper_id match. To avoid an extra build artifact for now, we attempt
  // a Sleeper crosswalk: walk the model and build {mfl_id_normalized: entry}.
  // The mfl_id correspondence isn't trivially available client-side, so the
  // first cut here matches by NAME+POSITION (which the pre-fetcher path
  // already does server-side via Player.mfl_id). It's a known limitation;
  // surfaced in the status text below.
  // TODO(v2): emit a separate assets/mfl_scores.json keyed by mfl_id.
  const nameLookup = {};
  Object.values(model).forEach(p => {
    const key = (p.name || "").toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
    if (key) nameLookup[key] = p;
  });

  // For each MFL player_id in rosters, attempt to look up the player's name
  // via MFL's player export (one shot for the whole league).
  const playersResp = await fetch(`${proxy}/mfl/${year}/export?TYPE=players&L=${leagueId}&JSON=1`);
  const playersPayload = await playersResp.json();
  let mflPlayers = ((playersPayload.players || {}).player) || [];
  if (!Array.isArray(mflPlayers)) mflPlayers = [mflPlayers];
  const mflIdToEntry = {};
  let matched = 0;
  mflPlayers.forEach(mp => {
    if (!mp.id) return;
    // MFL name format: "Last, First". Flip it to match our "First Last".
    let n = mp.name || "";
    if (n.includes(",")) {
      const [last, first] = n.split(",").map(s => s.trim());
      n = `${first} ${last}`;
    }
    const key = n.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
    const hit = nameLookup[key];
    if (hit) { mflIdToEntry[String(mp.id)] = hit; matched++; }
  });
  status.textContent = `${league.name || "MFL league " + leagueId} (\u00a7${year}) — ${fakeRosters.length} teams — ${matched} of ${mflPlayers.length} MFL players matched to model`;

  // Now evaluate each franchise using mflIdToEntry as the model lookup.
  const detected = { qb: "sf", te: "none", ppr: "full", qbCount: 0, sfCount: 0, bonusTE: 0, recPts: 1 };
  const settings = effectiveSettings(detected);
  detectedDiv.innerHTML = `<em>Settings auto-detect not yet wired for MFL — using Superflex / full PPR / no TE premium defaults. Use the dropdowns above to override.</em>`;

  const teams = fakeRosters.map(r => evaluateTeam(r, franchiseNames, mflIdToEntry, settings));
  const leagueAvg = teams.length ? teams.reduce((a, t) => a + t.total, 0) / teams.length : 0;
  teams.forEach(t => { t.vsAvg = t.total - leagueAvg; });
  const sorted = [...teams].sort((a, b) => b.total - a.total);
  results.innerHTML = renderResults(league.name, sorted);

  if (includeManagers) {
    status.textContent += " — fetching draft + trade history...";
    const mgr = await computeMflManagerReport(leagueId, year, franchiseNames, mflIdToEntry, proxy);
    results.innerHTML += renderManagerReport(mgr);
  }
}

// ---------------------------------------------------------------------------
// Client-side manager-rankings port. Mirrors src/dynasty/manager.py.
// ---------------------------------------------------------------------------

function expectedScoreAtPick(pick) {
  if (pick <= 0) return 100.0;
  if (pick > 250) return 0.0;
  return Math.max(0.0, 100.0 * (1.0 - (pick - 1) / 250.0));
}

function zscore(value, pool) {
  if (!pool || pool.length < 2) return 0.0;
  const mu = pool.reduce((a, b) => a + b, 0) / pool.length;
  const variance = pool.reduce((a, b) => a + (b - mu) * (b - mu), 0) / pool.length;
  const sd = Math.sqrt(variance) || 1.0;
  return (value - mu) / sd;
}

function computeManagerTable(franchiseNames, picks, trades, scoreLookup) {
  const byId = {};
  function ensure(fid) {
    if (!byId[fid]) {
      byId[fid] = {
        franchise_id: fid,
        display_name: franchiseNames[fid] || `Franchise ${fid}`,
        n_picks: 0, draft_delta_total: 0, draft_delta_avg: 0,
        n_trades: 0, trade_delta_total: 0,
        z_draft: 0, z_trade: 0, skill_score: 0, skill_rank: 0,
        notes: [],
      };
    }
    return byId[fid];
  }
  // Pre-populate so every franchise appears even with zero activity.
  Object.keys(franchiseNames).forEach(fid => ensure(fid));

  picks.forEach(p => {
    const info = scoreLookup[p.player_ext_id];
    if (!info) return;
    if (!p.franchise_id || p.franchise_id === "?") return;
    const m = ensure(p.franchise_id);
    const expected = expectedScoreAtPick(p.pick_no);
    m.n_picks++;
    m.draft_delta_total += (info.score - expected);
  });
  Object.values(byId).forEach(m => {
    m.draft_delta_avg = m.n_picks ? (m.draft_delta_total / m.n_picks) : 0;
  });

  trades.forEach(tx => {
    const sideValues = {};
    Object.keys(tx.sides).forEach(fid => {
      sideValues[fid] = tx.sides[fid].reduce((a, pid) => a + (scoreLookup[pid] ? scoreLookup[pid].score : 0), 0);
    });
    Object.keys(tx.sides).forEach(fid => {
      const received = sideValues[fid] || 0;
      const given = Object.keys(sideValues).filter(o => o !== fid).reduce((a, o) => a + sideValues[o], 0);
      const m = ensure(fid);
      m.n_trades++;
      m.trade_delta_total += (received - given);
    });
  });

  const draftPool = Object.values(byId).filter(m => m.n_picks).map(m => m.draft_delta_avg);
  const tradePool = Object.values(byId).filter(m => m.n_trades).map(m => m.trade_delta_total);

  Object.values(byId).forEach(m => {
    m.z_draft = m.n_picks ? zscore(m.draft_delta_avg, draftPool) : 0;
    m.z_trade = m.n_trades ? zscore(m.trade_delta_total, tradePool) : 0;
    m.skill_score = (m.z_draft + m.z_trade) / 2.0;
    if (!m.n_trades) m.notes.push("no trades on record");
    if (m.n_picks > 0 && m.n_picks < 5) m.notes.push(`only ${m.n_picks} rated draft picks (low sample)`);
    else if (m.n_picks === 0) m.notes.push("no rated draft picks");
  });

  const ranked = Object.values(byId).sort((a, b) => b.skill_score - a.skill_score);
  ranked.forEach((m, i) => { m.skill_rank = i + 1; });
  return { n_picks: picks.length, n_trades: trades.length, managers: ranked };
}

async function computeSleeperManagerReport(leagueId, franchiseNames, model) {
  // Drafts -> picks.
  const draftsResp = await fetch(`https://api.sleeper.app/v1/league/${leagueId}/drafts`);
  const drafts = (await draftsResp.json()) || [];
  const allPicks = [];
  for (const d of drafts) {
    const did = d.draft_id || d.id;
    if (!did) continue;
    try {
      const r = await fetch(`https://api.sleeper.app/v1/draft/${did}/picks`);
      const rows = (await r.json()) || [];
      rows.forEach(row => {
        allPicks.push({
          pick_no: Number(row.pick_no) || 0,
          round_no: Number(row.round) || 0,
          franchise_id: String(row.roster_id || row.picked_by || "?"),
          player_ext_id: String(row.player_id || ""),
        });
      });
    } catch (e) { /* skip */ }
  }

  // Transactions per week (0..18).
  const allTrades = [];
  const weekFetches = [];
  for (let w = 0; w <= 18; w++) {
    weekFetches.push(
      fetch(`https://api.sleeper.app/v1/league/${leagueId}/transactions/${w}`)
        .then(r => r.ok ? r.json() : [])
        .catch(() => [])
    );
  }
  const weekResults = await Promise.all(weekFetches);
  weekResults.forEach(rows => {
    (rows || []).forEach(tx => {
      if (tx.type !== "trade" || tx.status !== "complete") return;
      const adds = tx.adds || {};
      const sides = {};
      Object.keys(adds).forEach(pid => {
        const fid = String(adds[pid]);
        if (!sides[fid]) sides[fid] = [];
        sides[fid].push(String(pid));
      });
      if (!Object.keys(sides).length) return;
      allTrades.push({ transaction_id: String(tx.transaction_id || tx.id || ""), sides });
    });
  });

  return computeManagerTable(franchiseNames, allPicks, allTrades, model);
}

async function computeMflManagerReport(leagueId, year, franchiseNames, mflIdLookup, proxy) {
  // Drafts.
  const allPicks = [];
  try {
    const r = await fetch(`${proxy}/mfl/${year}/export?TYPE=draftResults&L=${leagueId}&JSON=1`);
    const payload = await r.json();
    let units = ((payload.draftResults || {}).draftUnit) || [];
    if (!Array.isArray(units)) units = [units];
    units.forEach(unit => {
      let picks = unit.draftPick || [];
      if (!Array.isArray(picks)) picks = [picks];
      picks.forEach(p => {
        allPicks.push({
          pick_no: (Number(p.pick) || 0) + 1,
          round_no: (Number(p.round) || 0) + 1,
          franchise_id: String(p.franchise || ""),
          player_ext_id: String(p.player || ""),
        });
      });
    });
  } catch (e) { /* skip */ }

  // Trades.
  const allTrades = [];
  try {
    const r = await fetch(`${proxy}/mfl/${year}/export?TYPE=transactions&L=${leagueId}&JSON=1&TRANS_TYPE=TRADE`);
    const payload = await r.json();
    let txs = ((payload.transactions || {}).transaction) || [];
    if (!Array.isArray(txs)) txs = [txs];
    txs.forEach(tx => {
      if (tx.type !== "TRADE") return;
      const f1 = tx.franchise || tx.franchise1;
      const f2 = tx.franchise2;
      const split = blob => (blob || "").split(",").map(s => s.trim()).filter(s => s && !s.startsWith("DP_") && !s.startsWith("FP_") && !s.startsWith("BB_"));
      const side1 = split(tx.franchise1_gave_up);
      const side2 = split(tx.franchise2_gave_up);
      const sides = {};
      if (f1) sides[String(f1)] = side2;
      if (f2) sides[String(f2)] = side1;
      if (Object.keys(sides).length) allTrades.push({ transaction_id: String(tx.transaction_id || tx.timestamp || ""), sides });
    });
  } catch (e) { /* skip */ }

  return computeManagerTable(franchiseNames, allPicks, allTrades, mflIdLookup);
}

function renderDetected(d, s) {
  const qbLabel = {"1qb":"1QB","sf":"Superflex","2qb":"2QB"}[s.qb];
  const teLabel = {"none":"no TE premium","low":"1.25 PPR TE","high":"1.5 PPR TE"}[s.te];
  const pprLabel = {"full":"full PPR","half":"half PPR","standard":"standard scoring"}[s.ppr];
  return `<strong>Settings:</strong> ${qbLabel} · ${teLabel} · ${pprLabel} ` +
         `<span style="color:var(--muted);font-size:12px">(detected from Sleeper: ${d.qbCount}×QB+${d.sfCount}×SF, bonus_rec_te=${d.bonusTE}, rec=${d.recPts})</span>`;
}

function evaluateTeam(roster, userById, model, settings) {
  const ownerName = userById[roster.owner_id] || ("Team " + roster.roster_id);
  const playerIds = roster.players || [];
  let total = 0;
  let evaluated = 0;
  let unrated = 0;
  const playerRows = [];
  const bestAtPos = {};
  for (const pid of playerIds) {
    const entry = model[String(pid)];
    if (!entry) { unrated++; continue; }
    evaluated++;
    const mult = positionMultiplier(entry.position, settings);
    const adjScore = entry.score * mult;
    const row = Object.assign({}, entry, { adjScore, mult });
    total += adjScore;
    playerRows.push(row);
    const pos = entry.position;
    if (!bestAtPos[pos] || adjScore > bestAtPos[pos].adjScore) bestAtPos[pos] = row;
  }
  playerRows.sort((a, b) => b.adjScore - a.adjScore);
  const weaknesses = [];
  for (const pos of STARTING_POSITIONS) {
    const best = bestAtPos[pos];
    if (!best) weaknesses.push(`no rated ${pos} on roster`);
    else if ((best.tier || 99) > WEAKNESS_TIER_THRESHOLD) {
      weaknesses.push(`weak ${pos}: best is ${best.name} (Tier ${best.tier}, rank ${best.rank})`);
    }
  }
  return {
    teamId: roster.roster_id,
    name: ownerName,
    total,
    avg: evaluated ? total / evaluated : 0,
    evaluated,
    unrated,
    topAssets: playerRows.slice(0, 5),
    weaknesses,
  };
}

function renderResults(leagueName, sorted) {
  let html = `<h2 style="margin-top:32px">Power rankings</h2>`;
  html += `<table style="width:100%;border-collapse:collapse">`;
  html += `<thead><tr><th style="text-align:right;padding:8px;border-bottom:2px solid var(--border)">#</th>`;
  html += `<th style="text-align:left;padding:8px;border-bottom:2px solid var(--border)">Team</th>`;
  html += `<th style="text-align:right;padding:8px;border-bottom:2px solid var(--border)">Total</th>`;
  html += `<th style="text-align:right;padding:8px;border-bottom:2px solid var(--border)">vs Avg</th></tr></thead><tbody>`;
  sorted.forEach((t, i) => {
    const diff = t.vsAvg >= 0 ? `+${t.vsAvg.toFixed(1)}` : t.vsAvg.toFixed(1);
    const diffColor = t.vsAvg >= 0 ? "#065f46" : "#991b1b";
    html += `<tr><td style="text-align:right;padding:8px;border-bottom:1px solid var(--border)">${i + 1}</td>`;
    html += `<td style="padding:8px;border-bottom:1px solid var(--border)"><strong>${escapeHtml(t.name)}</strong></td>`;
    html += `<td style="text-align:right;padding:8px;border-bottom:1px solid var(--border)">${t.total.toFixed(1)}</td>`;
    html += `<td style="text-align:right;padding:8px;border-bottom:1px solid var(--border);color:${diffColor}">${diff}</td></tr>`;
  });
  html += `</tbody></table>`;
  html += `<h2 style="margin-top:48px">Team breakdowns</h2>`;
  sorted.forEach(t => {
    html += `<div style="margin:24px 0;padding:16px;border:1px solid var(--border);border-radius:8px">`;
    html += `<h3 style="margin:0 0 8px 0">${escapeHtml(t.name)}</h3>`;
    html += `<div style="color:var(--muted);font-size:14px;margin-bottom:12px">total=${t.total.toFixed(1)} avg=${t.avg.toFixed(1)} rated=${t.evaluated} unrated=${t.unrated}</div>`;
    if (t.topAssets.length) {
      html += `<div style="font-weight:600;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px">Top 5 assets (settings-adjusted)</div>`;
      html += `<ul style="margin:0 0 12px 0;padding-left:20px">`;
      t.topAssets.forEach(a => {
        const adj = a.adjScore.toFixed(1);
        const base = a.score.toFixed(1);
        const multNote = a.mult === 1.0 ? "" : ` <span style="color:var(--muted);font-size:12px">(×${a.mult.toFixed(2)})</span>`;
        html += `<li>${escapeHtml(a.name)} <span style="color:var(--muted)">(${a.position}, rank ${a.rank}, Tier ${a.tier}) base ${base} → ${adj}${multNote}</span></li>`;
      });
      html += `</ul>`;
    }
    if (t.weaknesses.length) {
      html += `<div style="font-weight:600;font-size:13px;color:#92400e;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px">Weaknesses</div>`;
      html += `<ul style="margin:0;padding-left:20px;color:#92400e">`;
      t.weaknesses.forEach(w => { html += `<li>${escapeHtml(w)}</li>`; });
      html += `</ul>`;
    }
    html += `</div>`;
  });
  return html;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ---------------------------------------------------------------------------
// Pre-fetched leagues (Sleeper + MFL pre-baked into the site at build time).
// ---------------------------------------------------------------------------

async function loadPrefetchedIndex() {
  const section = document.getElementById("prefetched-section");
  try {
    const resp = await fetch("leagues/index.json");
    if (!resp.ok) throw new Error("no index");
    const idx = await resp.json();
    const leagues = idx.leagues || [];
    if (!leagues.length) {
      section.innerHTML = `<div class="callout" style="font-size:13px">No leagues are pre-fetched yet. Use the form below to evaluate any Sleeper league live (with manager rankings if the checkbox is on). MFL leagues need either a proxy worker (see <code>scripts/cf-worker/README.md</code>) or an entry in <code>leagues.json</code> for the next daily build.</div>`;
      return;
    }
    let html = `<h2 style="margin-top:0">Pre-fetched leagues</h2><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-top:12px">`;
    leagues.forEach(L => {
      const yearTag = L.year ? ` ${L.year}` : "";
      html += `<button class="prefetched-card" onclick="loadPrefetched('${L.slug}')" style="text-align:left;padding:14px 16px;border:1px solid var(--border);border-radius:6px;background:white;cursor:pointer">`;
      html += `<div style="font-weight:600;font-size:15px">${escapeHtml(L.name)}</div>`;
      html += `<div style="font-size:12px;color:var(--muted);margin-top:4px">${escapeHtml(L.platform.toUpperCase())}${yearTag} · ${L.n_teams} teams · ${L.n_managers} managers</div>`;
      html += `</button>`;
    });
    html += `</div>`;
    section.innerHTML = html;
  } catch (err) {
    section.innerHTML = `<div style="font-size:13px;color:var(--muted)">(No pre-fetched leagues available.)</div>`;
  }
}

async function loadPrefetched(slug) {
  const status = document.getElementById("league-status");
  const results = document.getElementById("league-results");
  status.textContent = `Loading ${slug}...`;
  results.innerHTML = "";
  try {
    const resp = await fetch(`leagues/${slug}.json`);
    if (!resp.ok) throw new Error("could not load " + slug);
    const payload = await resp.json();
    const team = payload.team_report || {};
    const mgr = payload.manager_report || {};
    status.textContent = `${team.name || slug} — pre-fetched ${payload.fetched_at || ""} · ${(team.teams || []).length} teams`;
    results.innerHTML = renderPrefetchedReport(team, mgr);
  } catch (err) {
    status.textContent = "";
    results.innerHTML = `<div class="callout" style="background:#fef2f2;border-color:#fecaca;color:#991b1b">${err.message}</div>`;
  }
}

function renderPrefetchedReport(team, mgr) {
  let html = renderTeamReport(team);
  html += renderManagerReport(mgr);
  return html;
}

function renderTeamReport(team) {
  const power = team.power_rankings || [];
  const teams = team.teams || [];
  if (!teams.length) return "<p>No team data in pre-fetched report.</p>";
  let html = `<h2 style="margin-top:32px">Power rankings <span style="color:var(--muted);font-size:13px;font-weight:normal">(pre-fetched, base scores)</span></h2>`;
  html += `<table style="width:100%;border-collapse:collapse">`;
  html += `<thead><tr><th style="text-align:right;padding:8px;border-bottom:2px solid var(--border)">#</th>`;
  html += `<th style="text-align:left;padding:8px;border-bottom:2px solid var(--border)">Team</th>`;
  html += `<th style="text-align:right;padding:8px;border-bottom:2px solid var(--border)">Total</th>`;
  html += `<th style="text-align:right;padding:8px;border-bottom:2px solid var(--border)">vs Avg</th></tr></thead><tbody>`;
  power.forEach((row, i) => {
    const diff = row.vs_league_avg >= 0 ? `+${row.vs_league_avg.toFixed(1)}` : row.vs_league_avg.toFixed(1);
    const diffColor = row.vs_league_avg >= 0 ? "#065f46" : "#991b1b";
    html += `<tr><td style="text-align:right;padding:8px;border-bottom:1px solid var(--border)">${row.rank}</td>`;
    html += `<td style="padding:8px;border-bottom:1px solid var(--border)"><strong>${escapeHtml(row.display_name)}</strong></td>`;
    html += `<td style="text-align:right;padding:8px;border-bottom:1px solid var(--border)">${row.total_score.toFixed(1)}</td>`;
    html += `<td style="text-align:right;padding:8px;border-bottom:1px solid var(--border);color:${diffColor}">${diff}</td></tr>`;
  });
  html += `</tbody></table>`;
  return html;
}

function renderManagerReport(mgr) {
  const managers = mgr.managers || [];
  if (!managers.length) return "";
  let html = `<h2 style="margin-top:48px">Manager skill rankings</h2>`;
  html += `<p style="color:var(--muted);font-size:13px">Per-manager skill score (z-score blend of draft delta + trade delta). `;
  html += `Picks=${mgr.n_picks||0}, trades=${mgr.n_trades||0}. Uses current composite values — rewards picks that aged well.</p>`;
  html += `<table style="width:100%;border-collapse:collapse;font-size:14px">`;
  html += `<thead><tr>`;
  html += `<th style="text-align:right;padding:6px;border-bottom:2px solid var(--border)">#</th>`;
  html += `<th style="text-align:left;padding:6px;border-bottom:2px solid var(--border)">Manager</th>`;
  html += `<th style="text-align:right;padding:6px;border-bottom:2px solid var(--border)">Skill</th>`;
  html += `<th style="text-align:right;padding:6px;border-bottom:2px solid var(--border)">Picks</th>`;
  html += `<th style="text-align:right;padding:6px;border-bottom:2px solid var(--border)">Draft Δ</th>`;
  html += `<th style="text-align:right;padding:6px;border-bottom:2px solid var(--border)">Trades</th>`;
  html += `<th style="text-align:right;padding:6px;border-bottom:2px solid var(--border)">Trade Δ</th>`;
  html += `<th style="text-align:left;padding:6px;border-bottom:2px solid var(--border)">Notes</th>`;
  html += `</tr></thead><tbody>`;
  managers.forEach(m => {
    const skill = m.skill_score >= 0 ? `+${m.skill_score.toFixed(2)}` : m.skill_score.toFixed(2);
    const skillColor = m.skill_score >= 0 ? "#065f46" : "#991b1b";
    const dDelta = m.n_picks ? (m.draft_delta_avg >= 0 ? `+${m.draft_delta_avg.toFixed(1)}` : m.draft_delta_avg.toFixed(1)) : "—";
    const tDelta = m.n_trades ? (m.trade_delta_total >= 0 ? `+${m.trade_delta_total.toFixed(1)}` : m.trade_delta_total.toFixed(1)) : "—";
    const notes = (m.notes || []).join(", ");
    html += `<tr>`;
    html += `<td style="text-align:right;padding:6px;border-bottom:1px solid var(--border)">${m.skill_rank}</td>`;
    html += `<td style="padding:6px;border-bottom:1px solid var(--border)"><strong>${escapeHtml(m.display_name)}</strong></td>`;
    html += `<td style="text-align:right;padding:6px;border-bottom:1px solid var(--border);color:${skillColor};font-weight:600">${skill}</td>`;
    html += `<td style="text-align:right;padding:6px;border-bottom:1px solid var(--border)">${m.n_picks}</td>`;
    html += `<td style="text-align:right;padding:6px;border-bottom:1px solid var(--border)">${dDelta}</td>`;
    html += `<td style="text-align:right;padding:6px;border-bottom:1px solid var(--border)">${m.n_trades}</td>`;
    html += `<td style="text-align:right;padding:6px;border-bottom:1px solid var(--border)">${tDelta}</td>`;
    html += `<td style="padding:6px;border-bottom:1px solid var(--border);color:var(--muted);font-size:12px">${escapeHtml(notes)}</td>`;
    html += `</tr>`;
  });
  html += `</tbody></table>`;
  return html;
}

// Auto-load the prefetched index on page load.
loadPrefetchedIndex();
// Initial UI sync for platform selector visibility.
onPlatformChange();
</script>"""
    body = body.replace("__PROXY_URL__", proxy_url)
    return _page(title, header, body)


# --------------------------------------------------------------------------
# Page: players/<slug>.html — individual player detail
# --------------------------------------------------------------------------

def _similar_players_card(comp_entry: dict) -> str:
    """Render the top-5 historical comparables for a player.

    ``comp_entry`` comes from ``data/similarity_comps_cache.json`` and has
    the shape produced by ``similarity_career_arc._build_comps_cache``.
    """
    comps = comp_entry.get("comparables") or []
    if not comps:
        return ""
    age = comp_entry.get("query_age")
    proj_yrs = comp_entry.get("projected_remaining_years")
    proj_ppr = comp_entry.get("projected_total_remaining_ppr")
    n_comps = comp_entry.get("n_comps", 0)
    avg_sim = comp_entry.get("avg_similarity", 0)

    rows = []
    for c in comps[:5]:
        sim = c.get("similarity", 0)
        yrs = c.get("years_played_after", 0)
        rows.append(
            f"<tr><td class='source-name'>{_esc(c.get('name', ''))}</td>"
            f"<td>{c.get('season', '')}</td>"
            f"<td>{c.get('team_or_school', '')}</td>"
            f"<td style='text-align:right'>{c.get('age', '—')}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{sim:.2f}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>+{yrs}</td></tr>"
        )
    return f"""<div class="card">
<h2 style="margin-top:0">Most similar historical players</h2>
<p style="color:var(--muted);font-size:14px">
  At age <strong>{age}</strong> and position, this player’s nearest historical
  neighbors (by per-game production, efficiency, and usage — z-score normalized
  within position). The similarity engine averages their realized future careers,
  time-discounted 5%/yr, to project this player’s remaining dynasty value.
</p>
<p style="font-size:14px">
  <strong>Projected remaining years:</strong> {proj_yrs} ·
  <strong>Projected total remaining fantasy PPR:</strong> {proj_ppr:.0f} ·
  <strong>Comp pool:</strong> top {n_comps} (avg similarity {avg_sim:.2f})
</p>
<table class="breakdown-table">
<thead><tr>
<th>Player</th><th>Season</th><th>Team</th><th style="text-align:right">Age</th>
<th style="text-align:right">Similarity</th><th style="text-align:right">Years after</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
"""


def _build_methodology_page(latest_ts, league_format: str) -> str:
    """v0.14.0 — methodology page explaining the similarity engine + overlays."""
    from .overlays import load_correlation_table
    table = load_correlation_table()
    ras = table.get("ras", {})
    srs = table.get("brainy_ballers_srs", {})
    methodology = table.get("methodology", "")

    def _row(pos: str) -> str:
        ras_v = float(ras.get(pos, 0.0))
        srs_v = float(srs.get(pos, 0.0))
        return (
            f"<tr><td><strong>{pos}</strong></td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{ras_v:+.3f}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{srs_v:+.3f}</td></tr>"
        )

    body = f"""<div class="container narrow">
<h1>Methodology</h1>
<p style="color:var(--muted)">v0.14.0 — similarity-based career arc overhaul.</p>

<div class="card">
<h2 style="margin-top:0">Similarity engine</h2>
<p>For each NFL player we vectorize their most recent productive season into
per-game production, efficiency, and usage features (z-score normalized within
position). We then KNN-search the historical corpus (1999–2024 · nflverse /
PFR) for the 20 nearest neighbors at the same position and age (±1 year).</p>
<p>For each comp we know their realized future career, so the weighted
aggregate becomes a projection of this player’s remaining dynasty value:</p>
<ul>
  <li><strong>Projected remaining years</strong> — weighted median of comp careers</li>
  <li><strong>Projected remaining PPR</strong> — weighted average of comp future totals</li>
  <li><strong>Time discount</strong> — 5% per year (dynasty owners value sooner production)</li>
  <li><strong>Dynasty value</strong> — rescaled 0–100 within position</li>
</ul>
<p>This is the dominant weight in the v0.14 composite (1.8). It encodes
both current skill (via the vector) and longevity (via the comps’ careers).</p>
</div>

<div class="card">
<h2 style="margin-top:0">Coverage penalty + Bayesian prior</h2>
<p>The v0.13 model could vault a player to #1 on a single source’s max value
(see: Luke Grimm). v0.14 applies a quadratic coverage penalty
(composite × (min(n_sources/3, 1))²) and pulls low-coverage players toward a
position-tier baseline (Bayesian prior). Players with 3+ qualifying sources are
unaffected; single-source entries are crushed to ~11% of raw plus a baseline
pull.</p>
</div>

<div class="card">
<h2 style="margin-top:0">Overlays — RAS &amp; Brainy Ballers SRS</h2>
<p>RAS and Brainy Ballers’ Star-Predictor Score now sit OUTSIDE the composite
as user-toggleable overlays. The default slider value is the historical Pearson
correlation between the signal and a player’s first 3 NFL seasons of
fantasy PPR.</p>
<table class="breakdown-table">
<thead><tr><th>Position</th><th style="text-align:right">RAS × first-3yr PPR</th>
<th style="text-align:right">SRS × first-3yr PPR</th></tr></thead>
<tbody>
{_row('QB')}{_row('RB')}{_row('WR')}{_row('TE')}
</tbody>
</table>
<p style="color:var(--muted);font-size:13px;margin-top:8px"><em>{_esc(methodology)}</em></p>
<p style="font-size:14px">RAS correlates ~0.23 with RB first-3-year production and ~0.14 with WR,
so enabling the RAS overlay for RBs will meaningfully reshuffle your rankings;
for QBs it’s closer to noise. Brainy Ballers’ SRS uses a low-confidence prior
until a historical archive becomes available.</p>
</div>

<div class="card">
<h2 style="margin-top:0">Source weights (v0.14.0)</h2>
<table class="breakdown-table">
<thead><tr><th>Source</th><th>Category</th><th style="text-align:right">Default weight</th></tr></thead>
<tbody>
<tr><td class="source-name">Similarity Career Arc</td><td>model</td><td style="text-align:right">1.8</td></tr>
<tr><td class="source-name">NFL Impact (DARKO)</td><td>model</td><td style="text-align:right">0.8</td></tr>
<tr><td class="source-name">FantasyCalc</td><td>market</td><td style="text-align:right">0.6</td></tr>
<tr><td class="source-name">FFC ADP</td><td>market</td><td style="text-align:right">0.4</td></tr>
<tr><td class="source-name">FantasyPros</td><td>expert</td><td style="text-align:right">0.4</td></tr>
<tr><td class="source-name">PFF</td><td>expert</td><td style="text-align:right">0.4</td></tr>
<tr><td class="source-name">NFL Draft Capital</td><td>model</td><td style="text-align:right">1.5 (rookies)</td></tr>
<tr><td class="source-name">CFBD Breakouts</td><td>model</td><td style="text-align:right">0.9 (rookies)</td></tr>
<tr><td class="source-name">DynastyProcess</td><td>aggregator</td><td style="text-align:right">0.3</td></tr>
<tr><td class="source-name">RAS</td><td>overlay</td><td style="text-align:right">overlay only</td></tr>
<tr><td class="source-name">Brainy Ballers SRS</td><td>overlay</td><td style="text-align:right">overlay only</td></tr>
</tbody>
</table>
</div>

</div>
"""
    return _page(
        "Methodology — Dynasty Model v0.14",
        _site_header("methodology", latest_ts, league_format),
        body,
    )


def _build_player_page(cs, p, all_sources, latest_ts, league_format: str, comps_cache: dict | None = None) -> str:
    try:
        breakdown = json.loads(cs.breakdown_json) if cs.breakdown_json else {}
    except Exception:
        breakdown = {}
    comps_cache = comps_cache or {}
    comp_entry = comps_cache.get(p.gsis_id) if p.gsis_id else None

    # Build breakdown rows, sorted by weight (highest contribution first)
    sources_by_slug = {s.slug: s for s in all_sources}
    items = []
    # v0.14.0: skip the internal _meta block (coverage / prior diagnostics)
    # — it's not a source contribution.
    coverage_meta = breakdown.get("_meta") if isinstance(breakdown.get("_meta"), dict) else None
    breakdown_rows = {k: v for k, v in breakdown.items() if k != "_meta"}
    total_w = sum(b.get("weight", 0) for b in breakdown_rows.values())
    for slug, b in breakdown_rows.items():
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
        score_val = it["score"] if it["score"] is not None else 0.0
        breakdown_html += f"""<tr>
<td class="source-name">{_esc(it['name'])}</td>
<td><span class="tag tag-{it['category']}">{it['category']}</span></td>
<td style="text-align:right;font-variant-numeric:tabular-nums">{rank_str}</td>
<td style="text-align:right;font-variant-numeric:tabular-nums">{score_val:.1f}</td>
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

{_similar_players_card(comp_entry) if comp_entry else ''}

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

        # v0.14.0: surface comparables from the similarity engine. Loaded
        # once here, used for both rankings hover tooltips and per-player
        # pages below.
        comps_cache = load_comps_cache()

        # Full rankings
        (out_root / "rankings.html").write_text(
            _build_rankings(rows, latest_ts, league_format, comps_cache=comps_cache), encoding="utf-8"
        )

        # Sources & methodology
        (out_root / "sources.html").write_text(
            _build_sources_page(sources, latest_ts, league_format), encoding="utf-8"
        )

        # Rate-My-League page + the model-scores JSON that powers it
        # client-side. Keyed by sleeper_id so the Sleeper league API joins
        # without any server-side work. Use an UNBOUNDED query so deep
        # rosters (12 teams x ~35 = 420) all resolve, not just the top-300
        # we render on rankings.html.
        all_rows_for_json = session.execute(
            select(CompositeScore, Player)
            .join(Player, CompositeScore.player_id == Player.id)
            .where(CompositeScore.league_format == league_format)
            .where(CompositeScore.generated_at == latest_ts)
            .order_by(CompositeScore.overall_rank)
        ).all()
        scores_lookup: dict[str, dict] = {}
        for cs, p in all_rows_for_json:
            if not p.sleeper_id:
                continue
            scores_lookup[str(p.sleeper_id)] = {
                "name": p.full_name,
                "position": p.position,
                "team": p.nfl_team,
                "score": round(cs.score, 2),
                "rank": cs.overall_rank,
                "tier": cs.tier,
                "position_rank": cs.position_rank,
            }
        (out_root / "assets" / "model_scores.json").write_text(
            json.dumps(scores_lookup, separators=(",", ":")), encoding="utf-8"
        )
        (out_root / "league.html").write_text(
            _build_league_page(latest_ts, league_format), encoding="utf-8"
        )

        # Methodology page (v0.14.0)
        (out_root / "methodology.html").write_text(
            _build_methodology_page(latest_ts, league_format), encoding="utf-8"
        )

        # Per-player pages
        for cs, p in rows:
            slug = _slugify(p.full_name, p.id)
            (out_root / "players" / f"{slug}.html").write_text(
                _build_player_page(cs, p, sources, latest_ts, league_format, comps_cache=comps_cache),
                encoding="utf-8"
            )

    return str(out_root / "index.html")


# Backwards-compat: keep the old single-file generator as a thin wrapper.
def generate_report(output_path: str = "dynasty_rankings.html",
                    league_format: str = "sf_ppr", limit: int = 300) -> str:
    """Legacy single-file output. Calls generate_site under a parent dir."""
    out_dir = Path(output_path).resolve().parent / "dynasty_site"
    return generate_site(str(out_dir), league_format, limit)
