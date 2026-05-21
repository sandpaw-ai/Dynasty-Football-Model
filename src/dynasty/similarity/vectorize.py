"""Vectorize NFL and college player-seasons for similarity search.

Per-position feature vectors are z-score normalized within position across
the full historical corpus so that distances are comparable.

NFL features (per position group):
  QB:  pass_yds/G, pass_TD/G, INT/G, rush_yds/G, rush_TD/G, sack_rate,
       fantasy_ppr/G, games_played
  RB:  rush_att/G, rush_yds/G, rush_TD/G, rec/G, rec_yds/G, rec_TD/G,
       yards_per_touch, fantasy_ppr/G, games_played
  WR:  targets/G, rec/G, rec_yds/G, rec_TD/G, target_share, wopr,
       yards_per_target, fantasy_ppr/G, games_played
  TE:  same shape as WR
  (Plus YoY deltas for each: prior-season fantasy_ppr/G change.)

College features (per position) — kept lighter to match cfbd_breakouts
granularity:
  per-game production rates + class_year + dominator proxy

This file is deliberately dependency-light: pure Python + a tiny vector
class so it runs in CI without numpy/scipy installs in every env. (scipy
is available per pyproject.toml but we avoid numpy/sklearn to keep the
similarity engine importable on minimal installs.)
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional

from ..sources.pro_football_reference import (
    load_pfr_seasons,
    load_pfr_players,
    player_age_in_season,
)


# Position groups we vectorize. "FB" lumps into RB; everyone else drops out.
NFL_VECTOR_POSITIONS = {"QB", "RB", "WR", "TE"}

# Per-position feature names — order matters because vectors are tuples.
QB_FEATURES = (
    "pass_yds_pg", "pass_td_pg", "int_pg", "rush_yds_pg", "rush_td_pg",
    "sack_rate", "fppr_pg", "games",
)
RB_FEATURES = (
    "rush_att_pg", "rush_yds_pg", "rush_td_pg", "rec_pg", "rec_yds_pg",
    "rec_td_pg", "yds_per_touch", "fppr_pg", "games",
)
WR_FEATURES = (
    "tgt_pg", "rec_pg", "rec_yds_pg", "rec_td_pg", "target_share", "wopr",
    "yds_per_target", "fppr_pg", "games",
)
TE_FEATURES = WR_FEATURES

FEATURES_BY_POSITION: dict[str, tuple[str, ...]] = {
    "QB": QB_FEATURES,
    "RB": RB_FEATURES,
    "WR": WR_FEATURES,
    "TE": TE_FEATURES,
}


def _f(v) -> float:
    try:
        if v in (None, "", "NA"):
            return 0.0
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


@dataclass(frozen=True)
class PlayerSeason:
    """Lightweight player-season record used by the similarity engine."""
    player_id: str            # gsis_id
    player_name: str
    season: int
    position: str             # QB | RB | WR | TE
    team: str
    age: Optional[float]
    games: int
    raw: dict                 # original row
    features: dict[str, float]
    fantasy_ppr: float
    fantasy_standard: float


def _extract_features(row: dict, pos: str) -> dict[str, float]:
    g = max(_f(row.get("games")), 1.0)
    pass_att = _f(row.get("attempts"))
    sacks = _f(row.get("sacks"))
    pass_drop_back = pass_att + sacks
    rush_att = _f(row.get("carries"))
    rec = _f(row.get("receptions"))
    rec_yds = _f(row.get("receiving_yards"))
    tgt = _f(row.get("targets"))
    touches = rush_att + rec

    common = {
        "pass_yds_pg":    _safe_div(_f(row.get("passing_yards")), g),
        "pass_td_pg":     _safe_div(_f(row.get("passing_tds")), g),
        "int_pg":         _safe_div(_f(row.get("interceptions")), g),
        "rush_att_pg":    _safe_div(rush_att, g),
        "rush_yds_pg":    _safe_div(_f(row.get("rushing_yards")), g),
        "rush_td_pg":     _safe_div(_f(row.get("rushing_tds")), g),
        "rec_pg":         _safe_div(rec, g),
        "rec_yds_pg":     _safe_div(rec_yds, g),
        "rec_td_pg":      _safe_div(_f(row.get("receiving_tds")), g),
        "tgt_pg":         _safe_div(tgt, g),
        "target_share":   _f(row.get("target_share")),
        "wopr":           _f(row.get("wopr")),
        "sack_rate":      _safe_div(sacks, pass_drop_back) if pass_drop_back else 0.0,
        "yds_per_touch":  _safe_div(_f(row.get("rushing_yards")) + rec_yds, touches) if touches else 0.0,
        "yds_per_target": _safe_div(rec_yds, tgt) if tgt else 0.0,
        "fppr_pg":        _safe_div(_f(row.get("fantasy_points_ppr")), g),
        "games":          g,
    }
    keys = FEATURES_BY_POSITION[pos]
    return {k: common[k] for k in keys}


def build_nfl_corpus(min_season: int = 1999, min_games: int = 4) -> list[PlayerSeason]:
    """Materialize player-seasons as PlayerSeason objects.

    Players with fewer than ``min_games`` are excluded — their per-game
    stats are too noisy to comp against.
    """
    seasons = load_pfr_seasons(min_season=min_season)
    players = {p["gsis_id"]: p for p in load_pfr_players() if p.get("gsis_id")}

    out: list[PlayerSeason] = []
    for row in seasons:
        pos = (row.get("position") or "").upper()
        if pos not in NFL_VECTOR_POSITIONS:
            continue
        try:
            season = int(row["season"])
            games = int(_f(row.get("games")))
        except (ValueError, KeyError, TypeError):
            continue
        if games < min_games:
            continue

        pid = row.get("player_id") or ""
        bio = players.get(pid, {})
        age = player_age_in_season(bio.get("birth_date"), season)

        feats = _extract_features(row, pos)

        out.append(PlayerSeason(
            player_id=pid,
            player_name=row.get("player_display_name") or row.get("player_name") or "",
            season=season,
            position=pos,
            team=row.get("recent_team") or "",
            age=age,
            games=games,
            raw=row,
            features=feats,
            fantasy_ppr=_f(row.get("fantasy_points_ppr")),
            fantasy_standard=_f(row.get("fantasy_points")),
        ))
    return out


# ---------------------------------------------------------------------------
# Z-score normalization per position
# ---------------------------------------------------------------------------


def compute_zscore_stats(corpus: list[PlayerSeason]) -> dict[str, dict[str, tuple[float, float]]]:
    """Return per-(position, feature) (mean, stdev) tuples.

    Used to normalize a single PlayerSeason into a z-score vector
    comparable across positions and seasons.
    """
    by_pos: dict[str, list[PlayerSeason]] = {}
    for ps in corpus:
        by_pos.setdefault(ps.position, []).append(ps)

    out: dict[str, dict[str, tuple[float, float]]] = {}
    for pos, group in by_pos.items():
        keys = FEATURES_BY_POSITION[pos]
        per_feature: dict[str, tuple[float, float]] = {}
        for k in keys:
            vals = [ps.features[k] for ps in group]
            mu = statistics.fmean(vals) if vals else 0.0
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            per_feature[k] = (mu, sd if sd > 1e-9 else 1.0)
        out[pos] = per_feature
    return out


def vectorize(ps: PlayerSeason, stats: dict[str, dict[str, tuple[float, float]]]) -> tuple[float, ...]:
    """Return z-score normalized feature vector for the given season."""
    keys = FEATURES_BY_POSITION[ps.position]
    pos_stats = stats[ps.position]
    return tuple(
        (ps.features[k] - pos_stats[k][0]) / pos_stats[k][1]
        for k in keys
    )


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine in [-1, 1]; identical vectors → 1.0."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
