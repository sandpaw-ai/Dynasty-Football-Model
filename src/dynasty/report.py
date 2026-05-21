"""Dynasty Football Model — v1.0 report builder.

Renders the static site from the v1 similarity engine. Mirrors the basketball
model's UI architecture: shared CSS, site header, rankings / league / methodology
/ sources / prospects pages, per-player pages with career-arc comparables.

The v0.x ``generate_site`` API is kept as a thin wrapper that ignores the old
``additional_formats`` parameter; the new site renders every PRESET overlay
into ``league.html`` via client-side JS, no per-format file fanout needed.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .engine.similarity_v1 import EngineResult, OUT_ROOT, run_engine
from .engine.format_overlay import PRESETS, OverlayResult, all_format_overlays


# ---------------------------------------------------------------------------
# Position colour palette (mirrors basketball model)
# ---------------------------------------------------------------------------

POSITION_COLOR = {
    "QB": "#e74c3c",
    "RB": "#27ae60",
    "WR": "#3498db",
    "TE": "#f39c12",
}


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _slug(name: str, pid: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{s}-{pid.replace('-', '')[-6:]}"


def _pos_badge(pos: str) -> str:
    color = POSITION_COLOR.get(pos, "#9ca3af")
    return f'<span class="pos-badge" style="background:{color}">{_esc(pos)}</span>'


# ---------------------------------------------------------------------------
# Shared CSS (mirrors basketball model exactly, with football accent colour)
# ---------------------------------------------------------------------------

def _shared_css() -> str:
    return """
:root {
  --bg: #ffffff; --card: #ffffff; --border: #e5e7eb; --text: #0f172a;
  --muted: #64748b; --accent: #1d4ed8; --accent-dark: #1e3a8a;
  --hover: #eff6ff; --header-bg: #0f172a; --header-text: #f8fafc;
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
  background: var(--header-bg);
  color: var(--header-text); padding: 20px 36px;
  border-bottom: 3px solid var(--accent);
}
header.site .row { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 16px; }
header.site h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.01em; }
header.site h1 a { color: var(--header-text); }
header.site h1 .accent { color: var(--accent); }
header.site nav a {
  color: var(--header-text); opacity: 0.75; margin-left: 22px; font-size: 14px; font-weight: 500;
}
header.site nav a:hover { opacity: 1; text-decoration: none; }
header.site nav a.active { opacity: 1; border-bottom: 2px solid var(--accent); padding-bottom: 4px; }
header.site .meta { opacity: 0.6; font-size: 12px; margin-top: 4px; }
.container { max-width: 1240px; margin: 0 auto; padding: 28px 36px; }
.container.narrow { max-width: 900px; }
h2 { color: var(--text); font-size: 22px; margin-top: 32px; font-weight: 600; }
h2 .accent { color: var(--accent); }
h3 { color: var(--text); font-size: 16px; margin-top: 22px; font-weight: 600; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px 24px; margin-bottom: 18px; }
.lede { font-size: 15px; color: var(--muted); margin: 8px 0 18px 0; max-width: 720px; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 22px; }
.kpi { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; }
.kpi .num { font-size: 24px; font-weight: 700; color: var(--accent); font-variant-numeric: tabular-nums; }
.kpi .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
table { width: 100%; background: var(--card); border-collapse: collapse;
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
th { background: #f8fafc; padding: 11px 14px; text-align: left;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 700; }
td { padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 14px; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr.player-row:hover { background: var(--hover); cursor: pointer; }
td.rank { font-weight: 700; color: var(--accent); width: 50px; }
td.name { font-weight: 600; }
td.score { font-weight: 700; text-align: right; font-variant-numeric: tabular-nums; color: var(--accent); }
td.years, td.team, td.tier, td.consensus { color: var(--muted); font-variant-numeric: tabular-nums; }
.pos-badge { display: inline-block; color: white; padding: 3px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 700; min-width: 32px; text-align: center; }
.controls { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; margin-bottom: 18px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.controls input, .controls select { font: inherit; padding: 7px 11px;
  border: 1px solid var(--border); border-radius: 6px; background: white; }
.controls input { flex: 1; min-width: 220px; }
.controls button { font: inherit; padding: 8px 16px; border: 0; border-radius: 6px;
  background: var(--accent); color: white; font-weight: 600; cursor: pointer; }
.controls button:hover { background: var(--accent-dark); }
.stats { color: var(--muted); font-size: 13px; margin-left: auto; }
.div-chip { display: inline-block; padding: 3px 9px; border-radius: 12px; font-size: 11px;
  font-weight: 600; font-variant-numeric: tabular-nums; }
.div-up { background: #ecfdf5; color: #047857; }
.div-up-big { background: #16a34a; color: white; }
.div-down { background: #fef2f2; color: #b91c1c; }
.div-down-big { background: #dc2626; color: white; }
.div-flat { background: #f3f4f6; color: #6b7280; }
.div-none { background: #f3f4f6; color: var(--muted); font-style: italic; }
.callout { background: #eff6ff; border: 1px solid #93c5fd;
  border-left: 4px solid var(--accent); border-radius: 6px; padding: 14px 18px;
  color: #1e3a8a; margin: 16px 0; font-size: 14px; }
.callout strong { color: var(--accent-dark); }
.player-header { background: var(--header-bg); color: var(--header-text); padding: 28px 36px; border-bottom: 3px solid var(--accent); }
.player-header h1 { margin: 0; font-size: 28px; }
.player-header .sub { opacity: 0.75; font-size: 14px; margin-top: 4px; }
.player-header .metrics { display: flex; gap: 28px; margin-top: 18px; flex-wrap: wrap; }
.player-header .metric .num { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--accent); }
.player-header .metric .label { opacity: 0.75; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
footer { color: var(--muted); font-size: 12px; padding: 32px 40px; text-align: center; border-top: 1px solid var(--border); margin-top: 40px; }
.tag { display: inline-block; padding: 2px 9px; border-radius: 10px;
  font-size: 11px; font-weight: 600; background: #eef2ff; color: #4338ca; }
.tag.tag-retired { background: #fef3c7; color: #92400e; }
.tag.tag-prospect { background: #fdf4ff; color: #86198f; }
.comp-tier-elite { color: #b45309; font-weight: 600; }
.comp-tier-above-avg { color: #047857; font-weight: 600; }
.comp-tier-starter { color: #1d4ed8; }
.style-badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.02em; margin-left: 4px;
  background: rgba(255,255,255,0.15); color: var(--header-text); }
.style-pocket { background: rgba(96, 165, 250, 0.25); }
.style-mobile { background: rgba(167, 139, 250, 0.30); }
.style-dual_threat { background: rgba(250, 204, 21, 0.35); color: #fde68a; }
.comp-tier-deep { color: var(--muted); }
"""


def _site_header(active: str, latest_ts: Optional[datetime], league_label: str) -> str:
    ts = latest_ts.strftime("%B %d, %Y at %I:%M %p UTC") if latest_ts else "—"

    def link(href, label, key):
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<header class="site">
  <div class="row">
    <div>
      <h1><a href="rankings.html">Dynasty Football <span class="accent">Model</span></a></h1>
      <div class="meta">Similarity-driven dynasty rankings · Updated {_esc(ts)} · Default format: {_esc(league_label)}</div>
    </div>
    <nav>
      {link("rankings.html", "Rankings", "rankings")}
      {link("league.html", "League Overlay", "league")}
      {link("methodology.html", "Methodology", "methodology")}
      {link("sources.html", "Sources", "sources")}
      {link("prospects.html", "Prospects", "prospects")}
    </nav>
  </div>
</header>"""


def _footer() -> str:
    return (
        '<footer>'
        'Dynasty Football Model · open source on '
        '<a href="https://github.com/pstiehl/Dynasty-Football-Model">GitHub</a> · '
        'Stats: <a href="https://github.com/nflverse/nflverse-data">nflverse</a> + Pro-Football-Reference'
        '</footer>'
    )


def _page(title: str, header_html: str, body_html: str, css_href: str = "assets/style.css") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="{css_href}">
</head>
<body>
{header_html}
{body_html}
{_footer()}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Rankings page
# ---------------------------------------------------------------------------

def _comp_tier_class(comp_tier: str) -> str:
    if not comp_tier:
        return "comp-tier-deep"
    for k in ("elite", "above-avg", "starter", "deep"):
        if comp_tier.startswith(k):
            return f"comp-tier-{k}"
    return "comp-tier-deep"


def _build_rankings(engine: EngineResult, latest_ts: datetime, league_label: str,
                    team_lookup: Dict[str, str], limit: int = 300) -> str:
    rows_html = ""
    for row in engine.rankings[:limit]:
        slug = _slug(row["name"], row["player_id"])
        comp_class = _comp_tier_class(row["comp_tier"])
        team = team_lookup.get(row["player_id"], "—")
        rows_html += f"""<tr class="player-row" data-name="{_esc(row['name'].lower())}" data-position="{_esc(row['position'])}" onclick="location='players/{slug}.html'">
<td class="rank">{row['overall_rank']}</td>
<td class="name">{_esc(row['name'])}</td>
<td>{_pos_badge(row['position'])}</td>
<td class="team">{_esc(team)}</td>
<td class="years">{row['age']}</td>
<td class="years">{row['projected_years_remaining']:.1f}</td>
<td class="tier">T{row['tier']}</td>
<td class="years"><span class="{comp_class}">{_esc(row['comp_tier'])}</span></td>
<td class="score">{row['production_score']:.0f}</td>
</tr>"""

    body = f"""<div class="container">

<h2>Dynasty Football <span class="accent">Rankings</span></h2>
<p class="lede">Players ranked by projected lifetime fantasy points,
comped to historical players with similar <strong>fantasy production curves</strong>
under modern scoring. Each active player's fp/g arc is matched against the
<strong>long-arc</strong> NFL corpus (retired ∪ 8+ season veterans). Comps' realised
post-snapshot fantasy points (era-pace adjusted to current era, scored under
{_esc(league_label)}) are similarity-weighted and time-discounted into a
projected remaining career.</p>

<div class="kpi-row">
  <div class="kpi"><div class="num">{len(engine.rankings):,}</div><div class="label">Active players ranked</div></div>
  <div class="kpi"><div class="num">{len(engine.long_arc_corpus):,}</div><div class="label">Long-arc comp pool</div></div>
  <div class="kpi"><div class="num">v2.0</div><div class="label">Engine · fantasy-point arc</div></div>
</div>

<div class="callout"><strong>v2.0.0 — fantasy-point-arc methodology.</strong>
v1.x ranked players by per-stat z-score shape: "who do their counting stats
look like?" That structurally buried Josh Allen and the modern dual-threat
elite because z-scoring is scale-invariant within era — it ignored that
rushing TDs score 6 pts each. v2.0 ranks players by the
<strong>fantasy points they actually produce</strong> under modern scoring, comping
them to historical players whose fp/g curves match. Elite-fp QBs cluster at
the top regardless of style. See <a href="methodology.html">Methodology</a>.</div>

<div class="controls">
  <input id="q" placeholder="Search by player name…" type="search">
  <select id="pos">
    <option value="">All positions</option>
    <option value="QB">QB</option><option value="RB">RB</option>
    <option value="WR">WR</option><option value="TE">TE</option>
  </select>
  <span class="stats" id="stats"></span>
</div>

<table>
<thead><tr>
  <th>#</th><th>Player</th><th>Pos</th><th>Team</th>
  <th>Age</th><th>Yrs Left</th><th>Tier</th>
  <th>Comp Tier</th>
  <th style="text-align:right">Value</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<script>
const q = document.getElementById('q');
const pos = document.getElementById('pos');
const stats = document.getElementById('stats');
const rows = document.querySelectorAll('.player-row');
function update() {{
  const qv = (q.value || '').toLowerCase();
  const pv = pos.value || '';
  let shown = 0;
  rows.forEach(r => {{
    const ok = (!qv || r.dataset.name.includes(qv)) && (!pv || r.dataset.position === pv);
    r.style.display = ok ? '' : 'none';
    if (ok) shown++;
  }});
  stats.textContent = shown + ' / ' + rows.length + ' players';
}}
q.addEventListener('input', update); pos.addEventListener('change', update); update();
</script>

</div>"""

    return _page(
        "Dynasty Football Model — Rankings",
        _site_header("rankings", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# League overlay page
# ---------------------------------------------------------------------------

def _build_league(overlays: Dict[str, OverlayResult], latest_ts: datetime,
                  league_label: str, team_lookup: Dict[str, str]) -> str:
    # Precompute overlay data for every preset, embed as JSON.
    overlay_payload = {}
    for fmt, ov in overlays.items():
        overlay_payload[fmt] = {
            "label": ov.label,
            "rankings": [
                {
                    "name": r["name"],
                    "pos": r["position"],
                    "age": r["age"],
                    "team": team_lookup.get(r["player_id"], "—"),
                    "value": r["league_value"],
                    "delta": r["vs_default_delta"],
                }
                for r in ov.rankings[:300]
            ],
        }
    payload_json = json.dumps(overlay_payload)

    preset_buttons = "".join(
        f'<button onclick="setFormat(\'{fmt}\')" id="btn-{fmt}">{_esc(PRESETS[fmt]["label"])}</button> '
        for fmt in PRESETS
    )

    body = f"""<div class="container">

<h2>League <span class="accent">Format Overlay</span></h2>
<p class="lede">Re-rank the engine's projections under your league's exact
scoring + roster rules. Switching format does NOT change which retired comps
each active player has — it just re-scores those comps' careers under your
settings and recomputes positional VORP.</p>

<div class="callout"><strong>Tip.</strong> Compare the <em>vs default</em> column
to see who's overvalued/undervalued in your league relative to the default
Superflex PPR ranking.</div>

<div class="controls">
  Preset: {preset_buttons}
  <span class="stats" id="ov-stats"></span>
</div>

<table>
<thead><tr>
  <th>#</th><th>Player</th><th>Pos</th><th>Team</th>
  <th>Age</th>
  <th style="text-align:right">vs default</th>
  <th style="text-align:right">League value</th>
</tr></thead>
<tbody id="ov-body"></tbody>
</table>

<script>
const OVERLAY = {payload_json};
function chip(d) {{
  if (d > 10) return '<span class="div-chip div-up-big">+'+d+'</span>';
  if (d > 0)  return '<span class="div-chip div-up">+'+d+'</span>';
  if (d < -10) return '<span class="div-chip div-down-big">'+d+'</span>';
  if (d < 0)  return '<span class="div-chip div-down">'+d+'</span>';
  return '<span class="div-chip div-flat">0</span>';
}}
function posBadge(p) {{
  const colors = {{ QB: '#e74c3c', RB: '#27ae60', WR: '#3498db', TE: '#f39c12' }};
  const c = colors[p] || '#9ca3af';
  return '<span class="pos-badge" style="background:'+c+'">'+p+'</span>';
}}
function setFormat(fmt) {{
  const data = OVERLAY[fmt];
  const body = document.getElementById('ov-body');
  body.innerHTML = data.rankings.map((r, i) =>
    '<tr class="player-row"><td class="rank">'+(i+1)+'</td>'+
    '<td class="name">'+r.name+'</td>'+
    '<td>'+posBadge(r.pos)+'</td>'+
    '<td class="team">'+r.team+'</td>'+
    '<td class="years">'+r.age+'</td>'+
    '<td class="years" style="text-align:right">'+chip(r.delta)+'</td>'+
    '<td class="score">'+r.value.toFixed(0)+'</td></tr>'
  ).join('');
  document.getElementById('ov-stats').textContent = data.label + ' · ' + data.rankings.length + ' players';
  Object.keys(OVERLAY).forEach(k => {{
    const b = document.getElementById('btn-'+k);
    if (b) b.style.opacity = (k === fmt) ? '1' : '0.55';
  }});
}}
setFormat('sf_ppr');
</script>

</div>"""

    return _page(
        "Dynasty Football Model — League Overlay",
        _site_header("league", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# Methodology page
# ---------------------------------------------------------------------------

def _build_methodology(engine: EngineResult, latest_ts: datetime,
                       league_label: str) -> str:
    # Render era-pace multiplier table from corpus values.
    eras = (1, 2, 3, 4)
    pace = engine.era_pace
    rows = ""
    for pos in ("QB", "RB", "WR", "TE"):
        stats_for_pos = sorted(pace.multipliers.get(pos, {}).keys())
        for stat in stats_for_pos:
            cells = "".join(
                f"<td class='years'>{pace.get(pos, stat, e):.2f}×</td>"
                for e in eras
            )
            rows += f"<tr><td class='name'>{pos}</td><td>{_esc(stat)}</td>{cells}</tr>"

    body = f"""<div class="container narrow">

<h2>v2.0 <span class="accent">Methodology — Fantasy-Point Arc</span></h2>

<p class="lede">v1.x ranked players by per-stat z-score shape — "who do their
counting stats look like?" That structurally buried elite dual-threats like
Josh Allen because z-scoring is scale-invariant within era and ignored that a
rushing TD scores 6 points. v2.0 replaces the engine: players are matched by
their <strong>fantasy production curves</strong> under modern scoring, then
projected forward against historical players with similar curves.</p>

<h3>1 · Era-pace pre-adjustment of historical stats</h3>
<p>Before any scoring or similarity math, every historical season's raw
stat line is multiplied by an empirically-calibrated
position+stat+era_from→Era-4 ratio. A 2010 Peyton Manning passing-yards
total becomes "what would this season produce if it happened today". The
full table:</p>

<table style="margin-top:8px">
<thead><tr><th>Pos</th><th>Stat</th><th>Era 1→4</th><th>Era 2→4</th><th>Era 3→4</th><th>Era 4→4</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="lede" style="margin-top:8px">Source: <code>{_esc(engine.era_pace.source)}</code> ·
multipliers derived from the median per-game rate within each era × position × stat cell,
clamped to [0.6, 2.0].</p>

<h3>2 · Fantasy-point arc corpus</h3>
<p>Era-adjusted stats are run through {_esc(league_label)} scoring (plus
sf_ppr, 1qb_ppr, 2qb_ppr, half_ppr, std, sf_te_premium variants) to
produce a <code>fp_per_game</code> arc for every player-season. The result
is a per-player, per-format career arc in MODERN-fp-equivalent units —
completely free of stat-shape distortion.</p>

<h3>3 · The long-arc corpus</h3>
<p>The comp pool is restricted to <em>long-arc</em> players: retired through
2022 OR 8+ NFL seasons OR 33+ years old with 6+ seasons. Long-arc active
veterans (Rodgers, Stafford, Russell Wilson) contribute only their COMPLETED
seasons — the in-progress season never leaks.</p>

<h3>4 · Fantasy-arc similarity vector (10-dim, in fp units)</h3>
<ol>
  <li><code>v[0]</code> = fp/g at the current age (weight 1.0)</li>
  <li><code>v[1]</code> = fp/g at age-1 (weight 0.7)</li>
  <li><code>v[2]</code> = fp/g at age-2 (weight 0.5)</li>
  <li><code>v[3]</code> = career-avg fp/g through current age</li>
  <li><code>v[4]</code> = peak-3yr-avg fp/g through current age</li>
  <li><code>v[5]</code> = peak-single-season fp/g (any age through current)</li>
  <li><code>v[6]</code> = career-total fp through current age (scaled / 100)</li>
  <li><code>v[7]</code> = trajectory slope (fp/g per career-season)</li>
  <li><code>v[8]</code> = durability (games / possible_games)</li>
  <li><code>v[9]</code> = career-stage fp percentile within position</li>
</ol>
<p>Similarity is feature-importance-weighted inverse-distance (not cosine
because we want magnitude to matter): two players with similar fp/g
production trajectories under modern scoring are similar, regardless of
how they earned those points.</p>

<h3>5 · Projection pipeline</h3>
<ol>
  <li>For an active player at age <em>A</em>, find top-20 long-arc comps at
      same position, age ±1, career-stage ±1, ranked by
      feature-weighted similarity.</li>
  <li>For each comp, sum their realised post-age fantasy points under the
      target format (already in modern-fp units — era-pace was applied at
      corpus build).</li>
  <li>Time-discount 5%/year, similarity-weight, sum → <code>comp_weighted_fp</code>.</li>
  <li>Compute <code>peak_anchored_fp</code> = target's projection-rate ×
      17 games × expected remaining years × mid-life discount factor.
      Projection-rate = <em>max(recent_3yr × 1.10, peak_3yr × 0.90)</em> —
      blends current form with all-time ceiling so a single down year
      doesn't crash a proven star.</li>
  <li>Take <code>max(comp_weighted_fp, peak_anchored_fp)</code> when the
      target's peak-3yr clears the elite tier (QB ≥18, RB ≥15, WR ≥16,
      TE ≥12). Sub-elite players fall back to comp-weighted.</li>
  <li>For mobile / dual-threat QBs, multiply by 1.05–1.10 (modern
      medicine continues to extend mobile-QB careers; lift on projected
      years remaining matches v1.1 at up to 1.50× for display).</li>
</ol>

<h3>6 · Why this is dynasty-appropriate</h3>
<p>Dynasty value is the projected lifetime fantasy points a player will
score for your roster. v1.x's stat-shape matching answered a different
question ("what shape of NFL career does this player project to have?")
which correlated imperfectly with fantasy production. v2.0 measures the
thing we actually care about directly: <em>fantasy points produced under
modern scoring</em>.</p>

<h3>7 · Format overlay</h3>
<p>The base <a href="rankings.html">Rankings</a> page uses Superflex PPR.
The <a href="league.html">League Overlay</a> page reads per-format
fp totals directly from the pre-computed arc corpus (no re-scoring needed)
and recomputes positional VORP baselines under the target roster rules.</p>

<h3>Known limitations</h3>
<ul>
  <li>Corpus starts in 1999. Pre-1999 retired greats (Jim Brown, Steve Young
      peak, Barry Sanders, Jerry Rice) are not fully represented.</li>
  <li>Birth dates missing for some retired players — we fall back to
      <em>rookie_season + 22</em> as an age estimate (~2% of corpus).</li>
  <li>Sample-of-1 comp pools (e.g. Aaron Rodgers at 41 with only Tom Brady
      as a same-age comp) fall back to comp-weighted only — the
      peak-anchored projection requires ≥3 comps.</li>
</ul>

</div>"""

    return _page(
        "Dynasty Football Model — Methodology",
        _site_header("methodology", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# Sources page (slim)
# ---------------------------------------------------------------------------

def _build_sources(latest_ts: datetime, league_label: str) -> str:
    body = """<div class="container narrow">

<h2>Data <span class="accent">Sources</span></h2>
<p class="lede">v1.0 runs on one primary data source. Auxiliary sources from
the v0.x composite are still synced for metadata but no longer feed the
ranking.</p>

<table>
<thead><tr><th>Source</th><th>Role</th><th>Where it's used</th></tr></thead>
<tbody>
<tr><td class="name">nflverse · player_stats_season</td>
    <td><span class="tag">primary</span></td>
    <td>Every per-season stat line for every NFL skill player back to 1999. The
    retired-only similarity corpus and the era-pace calibration are built
    entirely from this file.</td></tr>
<tr><td class="name">nflverse · players</td>
    <td><span class="tag">primary</span></td>
    <td>Player metadata: positions, birth dates, rookie/last seasons, draft
    info. Used to filter to skill positions and to compute age.</td></tr>
<tr><td class="name">Sleeper API</td>
    <td><span class="tag">metadata</span></td>
    <td>Current roster + team for active players. Powers the team column on
    the rankings page and the league-import flow on
    <a href="league.html">/league.html</a>.</td></tr>
<tr><td class="name">MyFantasyLeague API</td>
    <td><span class="tag">metadata</span></td>
    <td>League-import for MFL leagues. Same overlay engine, just different
    roster-fetch path.</td></tr>
<tr><td class="name">NFL Draft history</td>
    <td><span class="tag">metadata</span></td>
    <td>Draft round/pick for current players (shown on player pages). Not in
    the composite.</td></tr>
</tbody>
</table>

<p class="lede" style="margin-top:18px">v0.x sources (FantasyCalc,
DynastyProcess, FantasyPros, Brainy Ballers, FFC ADP, PFF, RAS, NFL Impact,
DynastyProcess, etc.) have been removed from the composite. The engine no
longer blends external opinions — it produces its own ranking from raw
production history. See <a href="methodology.html">Methodology</a>.</p>

</div>"""
    return _page(
        "Dynasty Football Model — Sources",
        _site_header("sources", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# Prospects page (decoupled)
# ---------------------------------------------------------------------------

def _build_prospects(latest_ts: datetime, league_label: str) -> str:
    body = """<div class="container narrow">

<h2>Draft <span class="accent">Prospects</span></h2>
<p class="lede">Prospects are evaluated separately from the main rankings.
NFL veterans have production data the engine can compare against; prospects
don't, so they live here on their own page.</p>

<div class="callout"><strong>v1.0 note.</strong> The college→NFL similarity
chain shipped in v0.16 is intentionally <em>not</em> wired into the v1.0
launcher — it depended on the old composite pipeline. A clean prospects
engine that mirrors the basketball model's rookie page is on the v1.1
roadmap. For now this page is a placeholder so the IA matches the
basketball model.</p>

<p class="lede" style="margin-top:18px">If you're looking for veteran NFL
rankings, head back to <a href="rankings.html">Rankings</a>. If you want to
rank players under your specific league's scoring, the
<a href="league.html">League Overlay</a> has presets for SF, 1QB, 2QB, and
SF TE-Premium plus a delta column showing how your format reshuffles
things.</p>

</div>"""
    return _page(
        "Dynasty Football Model — Prospects",
        _site_header("prospects", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# Player pages
# ---------------------------------------------------------------------------

def _player_header(row: Dict, team: str, league_label: str) -> str:
    # v2.0: surface QB style classification + fantasy-arc metrics.
    qb_style = row.get("qb_style")
    style_badge = ""
    if row.get("position") == "QB" and qb_style:
        style_label = {
            "pocket": "Pocket",
            "mobile": "Mobile",
            "dual_threat": "Dual-Threat",
        }.get(qb_style, qb_style.title())
        rypg = row.get("qb_career_rypg") or 0.0
        style_badge = (
            f" · <span class=\"style-badge style-{qb_style}\">"
            f"{style_label} ({rypg:.1f} ru/g)</span>"
        )
    lift_yr = row.get("career_length_lift") or 1.0
    lift_fp = row.get("career_length_lift_fp") or 1.0
    lift_panel = ""
    if row.get("position") == "QB" and (lift_yr > 1.0 or lift_fp > 1.0):
        era_note = (
            "modern medicine + RPO scheme adjustment"
            if qb_style == "dual_threat"
            else "mobile-QB longevity adjustment"
        )
        lift_panel = (
            f"<div class=\"callout\" style=\"margin-top:14px\">"
            f"<strong>{('Dual-threat' if qb_style=='dual_threat' else 'Mobile')} "
            f"career-length lift: {lift_fp:.2f}× fp / {lift_yr:.2f}× years</strong> "
            f"— {era_note}. v2.0 retains v1.1's correction for short-career "
            f"bias in the historical comp pool. Pocket lift = 1.00× (no lift)."
            f"</div>"
        )
    peak3 = row.get("peak_3yr_fp_per_game") or 0.0
    return f"""<div class="player-header">
  <h1>{_esc(row['name'])}</h1>
  <div class="sub">{_pos_badge(row['position'])} · {_esc(team)} · Rank #{row['overall_rank']} · Tier T{row['tier']}{style_badge}</div>
  <div class="metrics">
    <div class="metric"><div class="num">{row['production_score']:.0f}</div><div class="label">Projected lifetime fp</div></div>
    <div class="metric"><div class="num">{peak3:.1f}</div><div class="label">Peak 3yr fp/g</div></div>
    <div class="metric"><div class="num">{row['age']}</div><div class="label">Age</div></div>
    <div class="metric"><div class="num">{row['projected_years_remaining']:.1f}</div><div class="label">Yrs remaining</div></div>
    <div class="metric"><div class="num">{row['n_comps']}</div><div class="label">Long-arc comps</div></div>
  </div>
  {lift_panel}
  <div style="margin-top:14px"><a href="../rankings.html" style="color:var(--header-text);opacity:0.8;font-size:13px">← back to rankings</a></div>
</div>"""


def _build_player_page(row: Dict, comps: List[Dict], team: str,
                       league_label: str, latest_ts: datetime) -> str:
    comp_rows = ""
    for c in comps[:10]:
        comp_peak = c.get("peak_3yr_fp_per_game", 0.0)
        comp_rows += (
            f"<tr>"
            f"<td class='name'>{_esc(c['name'])}</td>"
            f"<td>{_pos_badge(c['position'])}</td>"
            f"<td class='years'>{c['last_season']}</td>"
            f"<td class='score'>{c['similarity']:.3f}</td>"
            f"<td class='score'>{comp_peak:.1f}</td>"
            f"<td class='years'>{c['post_age_seasons']}</td>"
            f"<td class='years'>{c['career_ppr']:.0f}</td>"
            f"<td class='score'>{c['post_age_projected_pts']:.0f}</td>"
            f"</tr>"
        )

    body = f"""{_player_header(row, team, league_label)}
<div class="container">

<h2>Fantasy-Point Arc <span class="accent">Comparables</span></h2>
<p class="lede">The top-10 most similar <em>long-arc</em> NFL players matched by
<strong>fantasy-point production curve</strong> at this career stage. Each
comp's "Peak 3yr fp/g" is their best 3-season fp/g average under
{_esc(league_label)} (era-pace-adjusted to modern). "Projected pts" is what
their post-age-{row['age']} career would have produced under modern scoring,
time-discounted 5%/year. The player's projected lifetime fantasy points is
computed as the max of the similarity-weighted comp projection and the
target's own peak-anchored projection (proven elite producers don't get
dragged down by an occasional low-projection comp).</p>

<table>
<thead><tr>
  <th>Comparable</th><th>Pos</th><th>Last season</th>
  <th style="text-align:right">Similarity</th>
  <th style="text-align:right">Peak 3yr fp/g</th>
  <th>Their post-age seasons</th>
  <th>Their career fp</th>
  <th style="text-align:right">Projected pts</th>
</tr></thead>
<tbody>{comp_rows}</tbody>
</table>

<p class="lede" style="margin-top:24px">Want this player ranked under your
league's specific scoring + roster rules? Head to
<a href="../league.html">League Overlay</a>.</p>

</div>"""

    return _page(
        f"Dynasty Football Model — {row['name']}",
        _site_header("rankings", latest_ts, league_label),
        body,
        css_href="../assets/style.css",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _load_sleeper_teams() -> Dict[str, str]:
    """Pull current Sleeper player → team map, keyed by GSIS id where possible.

    Falls back to an empty map if the DB isn't initialised or the Player table
    doesn't carry a GSIS-id column. Team rendering degrades to "—".
    """
    try:
        from .db.session import get_session
        from .db.models import Player
        from sqlalchemy import select
        out: Dict[str, str] = {}
        with get_session() as session:
            for p in session.execute(select(Player)).scalars():
                gsis = getattr(p, "gsis_id", None) or getattr(p, "pfr_id", None)
                team = getattr(p, "team", None) or getattr(p, "nfl_team", None)
                if gsis and team:
                    out[gsis] = team
        return out
    except Exception:
        return {}


def generate_site(
    output_dir: str = "dynasty_site",
    league_format: str = "sf_ppr",
    limit: int = 300,
    additional_formats=None,    # kept for backwards-compat; ignored in v1
    engine: Optional[EngineResult] = None,
) -> str:
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "assets").mkdir(parents=True, exist_ok=True)
    (out_root / "players").mkdir(parents=True, exist_ok=True)

    latest_ts = datetime.now(timezone.utc)

    if engine is None:
        engine = run_engine(persist=True)

    overlays = all_format_overlays(engine)
    team_lookup = _load_sleeper_teams()

    label = PRESETS.get(league_format, PRESETS["sf_ppr"])["label"]

    (out_root / "assets" / "style.css").write_text(_shared_css(), encoding="utf-8")

    # rankings.html — primary landing page (no index.html distinction needed)
    rankings_html = _build_rankings(engine, latest_ts, label, team_lookup, limit=limit)
    (out_root / "rankings.html").write_text(rankings_html, encoding="utf-8")
    (out_root / "index.html").write_text(rankings_html, encoding="utf-8")

    (out_root / "league.html").write_text(
        _build_league(overlays, latest_ts, label, team_lookup),
        encoding="utf-8",
    )
    (out_root / "methodology.html").write_text(
        _build_methodology(engine, latest_ts, label),
        encoding="utf-8",
    )
    (out_root / "sources.html").write_text(
        _build_sources(latest_ts, label),
        encoding="utf-8",
    )
    (out_root / "prospects.html").write_text(
        _build_prospects(latest_ts, label),
        encoding="utf-8",
    )

    # Per-player pages.
    for row in engine.rankings[:limit]:
        slug = _slug(row["name"], row["player_id"])
        comps = engine.comps.get(row["player_id"], [])
        team = team_lookup.get(row["player_id"], "—")
        page = _build_player_page(row, comps, team, label, latest_ts)
        (out_root / "players" / f"{slug}.html").write_text(page, encoding="utf-8")

    # Also drop the engine's master rankings JSON next to the site so the
    # league-import flow can consume it without re-running the engine.
    (out_root / "engine_rankings.json").write_text(
        json.dumps([dict(r) for r in engine.rankings], indent=2, default=float),
        encoding="utf-8",
    )

    return str(out_root.resolve())
