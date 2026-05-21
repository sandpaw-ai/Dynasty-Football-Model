"""nfl_impact — DARKO-style current-skill signal for active NFL players.

This is the "how good are they RIGHT NOW" half of the model. The career-
arc / similarity engine owns "how many productive years remain"; this
source owns "what level are they playing at this season".

Inputs: the cached PFR / nflverse player-season corpus (most recent
season per player, REG only).

Per-position skill formulas (0..100 normalized within position):

  QB:  ANY/A proxy + TD% - INT% + sack rate (lower = better)
  RB:  yards per touch + TD rate + target share
  WR:  yards per route run proxy (Tgt/G × yards per target) + aDOT proxy
       (air yards/target) + TD rate
  TE:  same as WR

The output is emitted as a normalized 0..100 ``market_value``-style
score per (player, league_format). Composite weights this at 0.8 — strong
but not dominant. The similarity engine carries the heavier weight
because it captures longevity, which is the actual dynasty differentiator.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Iterator, Optional

from .base import BaseSource, RankingRecord
from .pro_football_reference import load_pfr_seasons, load_pfr_players


MIN_GAMES_FOR_SKILL = 6     # noise floor — fewer than 6 games is unreliable
SKILL_QUERY_MIN_SEASON = 2023


def _f(v) -> float:
    try:
        if v in (None, "", "NA"):
            return 0.0
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _safe_div(a, b):
    return a / b if b else 0.0


def _qb_skill(row: dict) -> float:
    att = _f(row.get("attempts"))
    pass_yds = _f(row.get("passing_yards"))
    pass_td = _f(row.get("passing_tds"))
    ints = _f(row.get("interceptions"))
    sacks = _f(row.get("sacks"))
    sack_yds = _f(row.get("sack_yards"))
    # Adjusted Net Yards per Attempt (ANY/A) — PFR's standard QB efficiency
    drop_back = att + sacks
    any_a = _safe_div(pass_yds - sack_yds + 20 * pass_td - 45 * ints, drop_back)
    td_pct = _safe_div(pass_td, att) * 100.0
    int_pct = _safe_div(ints, att) * 100.0
    sack_rate = _safe_div(sacks, drop_back) * 100.0
    # Blend, then normalize to ~0..1
    score = any_a + (td_pct - int_pct) - 0.5 * sack_rate
    return score


def _rb_skill(row: dict) -> float:
    g = max(_f(row.get("games")), 1.0)
    carries = _f(row.get("carries"))
    rush_yds = _f(row.get("rushing_yards"))
    rush_td = _f(row.get("rushing_tds"))
    rec = _f(row.get("receptions"))
    rec_yds = _f(row.get("receiving_yards"))
    rec_td = _f(row.get("receiving_tds"))
    targets = _f(row.get("targets"))
    touches = carries + rec
    ypt = _safe_div(rush_yds + rec_yds, touches)
    td_rate = _safe_div(rush_td + rec_td, touches) * 100.0
    tgt_pg = _safe_div(targets, g)
    # Heavier touches mean more impact opportunity
    volume_bonus = _safe_div(touches, g)
    return ypt * 1.5 + td_rate * 3.0 + tgt_pg * 0.5 + volume_bonus * 0.4


def _wr_te_skill(row: dict) -> float:
    g = max(_f(row.get("games")), 1.0)
    tgt = _f(row.get("targets"))
    rec_yds = _f(row.get("receiving_yards"))
    rec = _f(row.get("receptions"))
    rec_td = _f(row.get("receiving_tds"))
    air_yds = _f(row.get("receiving_air_yards"))
    target_share = _f(row.get("target_share"))
    wopr = _f(row.get("wopr"))
    yds_per_tgt = _safe_div(rec_yds, tgt)
    adot = _safe_div(air_yds, tgt)         # average depth of target
    tgt_pg = _safe_div(tgt, g)
    td_rate = _safe_div(rec_td, max(rec, 1.0)) * 100.0
    # Yards per route run proxy: roughly tgt_pg × yards_per_target × 0.1
    yprr_proxy = tgt_pg * yds_per_tgt * 0.05
    return (
        yprr_proxy * 1.5
        + adot * 0.6
        + target_share * 100.0 * 0.4
        + wopr * 10.0 * 0.3
        + td_rate * 0.6
    )


_SKILL_FN_BY_POS = {
    "QB": _qb_skill,
    "RB": _rb_skill,
    "WR": _wr_te_skill,
    "TE": _wr_te_skill,
}


def _normalize_by_position(scored: list[tuple[str, str, str, float]]) -> dict[str, dict[str, float]]:
    """scored = list of (gsis_id, name, pos, raw_score) → {pos: {gsis_id: 0..100}}."""
    by_pos: dict[str, list[tuple[str, float]]] = {}
    for pid, _name, pos, s in scored:
        by_pos.setdefault(pos, []).append((pid, s))

    out: dict[str, dict[str, float]] = {}
    for pos, arr in by_pos.items():
        vals = [s for _, s in arr]
        lo = min(vals)
        hi = max(vals)
        rng = hi - lo if hi > lo else 1.0
        out[pos] = {pid: round(100.0 * (s - lo) / rng, 2) for pid, s in arr}
    return out


def compute_nfl_impact_scores(min_season: int = SKILL_QUERY_MIN_SEASON) -> dict[str, dict]:
    """Return {gsis_id: {name, position, team, season, score_0_100}}.

    Picks each player's most recent qualifying season (>= MIN_GAMES_FOR_SKILL,
    season >= min_season) and computes the position-specific skill score.
    """
    seasons = load_pfr_seasons(min_season=min_season)
    players = {p["gsis_id"]: p for p in load_pfr_players() if p.get("gsis_id")}

    latest_by_pid: dict[str, dict] = {}
    for row in seasons:
        try:
            season = int(row["season"])
            games = int(_f(row.get("games")))
        except (ValueError, KeyError, TypeError):
            continue
        if games < MIN_GAMES_FOR_SKILL:
            continue
        pid = row.get("player_id") or ""
        if not pid:
            continue
        pos = (row.get("position") or "").upper()
        if pos not in _SKILL_FN_BY_POS:
            continue
        prev = latest_by_pid.get(pid)
        if prev and int(prev["season"]) > season:
            continue
        latest_by_pid[pid] = row

    scored: list[tuple[str, str, str, float]] = []
    meta: dict[str, dict] = {}
    for pid, row in latest_by_pid.items():
        pos = (row.get("position") or "").upper()
        fn = _SKILL_FN_BY_POS.get(pos)
        if not fn:
            continue
        raw = fn(row)
        scored.append((pid, row.get("player_display_name") or "", pos, raw))
        bio = players.get(pid, {})
        meta[pid] = {
            "gsis_id": pid,
            "pfr_id": bio.get("pfr_id"),
            "name": row.get("player_display_name") or "",
            "position": pos,
            "team": row.get("recent_team") or "",
            "season": int(row["season"]),
            "raw_score": raw,
        }

    norm = _normalize_by_position(scored)
    for pid, m in meta.items():
        m["score_0_100"] = norm.get(m["position"], {}).get(pid, 0.0)
    return meta


# ---------------------------------------------------------------------------
# Source adapter
# ---------------------------------------------------------------------------


class NFLImpact(BaseSource):
    slug = "nfl_impact"
    name = "NFL Impact (DARKO-style current-skill)"
    category = "model"
    update_frequency = "weekly"
    tos_compliant = True
    # Strong but not dominant — similarity engine owns the longevity signal.
    default_weight = 0.8
    homepage = "internal: src/dynasty/sources/nfl_impact.py"
    notes = (
        "DARKO-style current-skill signal derived from the nflverse / PFR "
        "player-season corpus. Per-position formulas: ANY/A + TD%-INT% + "
        "sack rate (QB); yards-per-touch + TD rate + target share (RB); "
        "YPRR proxy + aDOT + TD rate (WR/TE). Normalized 0..100 within "
        "position. Pairs with the similarity engine which carries the "
        "career-arc / longevity weight."
    )

    def fetch(self) -> Iterator[RankingRecord]:
        scores = compute_nfl_impact_scores()
        # Rank within position by score
        by_pos_sorted: dict[str, list[tuple[str, float]]] = {}
        for pid, m in scores.items():
            by_pos_sorted.setdefault(m["position"], []).append((pid, m["score_0_100"]))
        position_rank_by_pid: dict[str, int] = {}
        overall_pool: list[tuple[str, float]] = []
        for pos, arr in by_pos_sorted.items():
            arr.sort(key=lambda x: x[1], reverse=True)
            for i, (pid, _) in enumerate(arr, start=1):
                position_rank_by_pid[pid] = i
            overall_pool.extend(arr)
        overall_pool.sort(key=lambda x: x[1], reverse=True)
        overall_rank_by_pid = {pid: i + 1 for i, (pid, _) in enumerate(overall_pool)}

        captured = datetime.utcnow()
        for pid, m in scores.items():
            for fmt in ("sf_ppr", "1qb_ppr"):
                yield RankingRecord(
                    source_slug=self.slug,
                    gsis_id=pid,
                    pfr_id=m.get("pfr_id"),
                    full_name=m["name"],
                    position=m["position"],
                    nfl_team=m["team"],
                    overall_rank=overall_rank_by_pid.get(pid),
                    position_rank=position_rank_by_pid.get(pid),
                    market_value=m["score_0_100"],
                    league_format=fmt,
                    is_dynasty=True,
                    captured_at=captured,
                )
