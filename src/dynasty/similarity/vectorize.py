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
from ..sources.historical_ncaa_football import (
    load_ncaa_seasons,
    CONFERENCE_MULTIPLIER,
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


# ===========================================================================
# COLLEGE FOOTBALL VECTORIZATION (PR #16 — rookie college→NFL chain)
# ===========================================================================

# Class year → ordinal proxy for "college age". A true freshman is 18.5; we
# add 0.5/yr to step through SO/JR/SR. Plenty of class-year strings are
# blank in the cfbfastR roster, so we fall back to 21.0 (≈ typical Jr/Sr).
_CLASS_TO_AGE = {"FR": 19.0, "SO": 20.0, "JR": 21.0, "SR": 22.0}
DEFAULT_COLLEGE_AGE = 21.0

# Class-year ordinal for the feature vector (older = higher).
_CLASS_TO_ORDINAL = {"FR": 1.0, "SO": 2.0, "JR": 3.0, "SR": 4.0}
DEFAULT_CLASS_ORDINAL = 3.0

CFB_QB_FEATURES = (
    "pass_yds_pg", "pass_td_pg", "int_pg", "completion_pct", "ypa",
    "any_a_proxy", "rush_yds_pg", "rush_td_pg", "class_ord", "conf_mult",
)
CFB_RB_FEATURES = (
    "rush_att_pg", "rush_yds_pg", "ypc", "rush_td_pg",
    "rec_pg", "rec_yds_pg", "scrimmage_td_pg",
    "class_ord", "conf_mult",
)
CFB_WR_TE_FEATURES = (
    "rec_pg", "rec_yds_pg", "rec_td_pg", "ypc",
    "target_share_proxy", "dominator_proxy",
    "class_ord", "conf_mult",
)

CFB_FEATURES_BY_POSITION: dict[str, tuple[str, ...]] = {
    "QB": CFB_QB_FEATURES,
    "RB": CFB_RB_FEATURES,
    "WR": CFB_WR_TE_FEATURES,
    "TE": CFB_WR_TE_FEATURES,
}


@dataclass(frozen=True)
class CollegePlayerSeason:
    """Per-season college football record used by the rookie similarity chain."""
    cfb_player_id: str
    player_name: str
    season: int
    school: str
    conference: str
    conference_tier: str   # P5 / G5_top / G5 / FCS
    position: str          # QB / RB / WR / TE
    class_year: str        # FR / SO / JR / SR / ""
    age_proxy: float       # derived from class_year
    games: int
    raw: dict
    features: dict[str, float]


def _cfb_extract_features(row: dict, pos: str) -> dict[str, float]:
    g = max(_f(row.get("games")), 1.0)
    conf_mult = CONFERENCE_MULTIPLIER.get(row.get("conference_tier", "FCS"), 0.65)
    class_ord = _CLASS_TO_ORDINAL.get(row.get("class_year", ""), DEFAULT_CLASS_ORDINAL)

    pass_att = _f(row.get("pass_att"))
    pass_comp = _f(row.get("pass_comp"))
    pass_yds = _f(row.get("pass_yds"))
    pass_td = _f(row.get("pass_td"))
    int_thrown = _f(row.get("int_thrown"))
    rush_att = _f(row.get("rush_att"))
    rush_yds = _f(row.get("rush_yds"))
    rec = _f(row.get("rec"))
    rec_yds = _f(row.get("rec_yds"))
    rec_td = _f(row.get("rec_td"))
    rush_td = _f(row.get("rush_td"))
    targets = _f(row.get("targets"))

    # ANY/A proxy: (pass_yds + 20*pass_td - 45*int) / pass_att
    if pass_att > 0:
        ypa = pass_yds / pass_att
        completion_pct = pass_comp / pass_att
        any_a_proxy = (pass_yds + 20.0 * pass_td - 45.0 * int_thrown) / pass_att
    else:
        ypa = 0.0
        completion_pct = 0.0
        any_a_proxy = 0.0

    ypc_rush = _safe_div(rush_yds, rush_att) if rush_att > 0 else 0.0
    ypc_rec = _safe_div(rec_yds, rec) if rec > 0 else 0.0

    # Conference-adjusted per-game stats apply the strength multiplier so
    # that, e.g., 1000 rec yds at FCS isn't comparable to 1000 at SEC.
    common = {
        "pass_yds_pg":  _safe_div(pass_yds, g) * conf_mult,
        "pass_td_pg":   _safe_div(pass_td, g) * conf_mult,
        "int_pg":       _safe_div(int_thrown, g),
        "completion_pct": completion_pct,
        "ypa":          ypa,
        "any_a_proxy":  any_a_proxy * conf_mult,
        "rush_att_pg":  _safe_div(rush_att, g),
        "rush_yds_pg":  _safe_div(rush_yds, g) * conf_mult,
        "ypc":          ypc_rush if pos in ("QB", "RB") else ypc_rec,
        "rush_td_pg":   _safe_div(rush_td, g),
        "rec_pg":       _safe_div(rec, g),
        "rec_yds_pg":   _safe_div(rec_yds, g) * conf_mult,
        "rec_td_pg":    _safe_div(rec_td, g),
        "scrimmage_td_pg": _safe_div(rush_td + rec_td, g),
        # target_share_proxy: rec/g normalized by class-level WR norm (12 catches
        # per game is roughly an elite alpha receiver — used as a soft anchor).
        "target_share_proxy": min(1.0, _safe_div(targets, g) / 12.0),
        # dominator_proxy: scrimmage_yds + rec_TD count relative to a generous
        # cap. Not a true team-dominator (we don’t have team totals here),
        # but it captures the same “was this player THE guy” signal.
        "dominator_proxy": min(1.0, (rec_yds + rec_td * 20) / 1600.0) * conf_mult,
        "class_ord":    class_ord,
        "conf_mult":    conf_mult,
    }
    keys = CFB_FEATURES_BY_POSITION[pos]
    return {k: common[k] for k in keys}


def build_college_corpus(
    min_season: int = 2014,
    min_games: int = 4,
) -> list[CollegePlayerSeason]:
    """Materialize college player-seasons from the cached NCAA corpus."""
    rows = load_ncaa_seasons(min_season=min_season)
    out: list[CollegePlayerSeason] = []
    for row in rows:
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

        class_year = row.get("class_year") or ""
        age_proxy = _CLASS_TO_AGE.get(class_year, DEFAULT_COLLEGE_AGE)
        feats = _cfb_extract_features(row, pos)

        out.append(CollegePlayerSeason(
            cfb_player_id=row.get("cfb_player_id") or "",
            player_name=row.get("name") or "",
            season=season,
            school=row.get("team") or "",
            conference=row.get("conference") or "",
            conference_tier=row.get("conference_tier") or "FCS",
            position=pos,
            class_year=class_year,
            age_proxy=age_proxy,
            games=games,
            raw=row,
            features=feats,
        ))
    return out


def compute_college_zscore_stats(
    corpus: list[CollegePlayerSeason],
) -> dict[str, dict[str, tuple[float, float]]]:
    """Per-(position, feature) (mean, stdev) tuples for college vectors."""
    by_pos: dict[str, list[CollegePlayerSeason]] = {}
    for ps in corpus:
        by_pos.setdefault(ps.position, []).append(ps)

    out: dict[str, dict[str, tuple[float, float]]] = {}
    for pos, group in by_pos.items():
        keys = CFB_FEATURES_BY_POSITION[pos]
        per_feature: dict[str, tuple[float, float]] = {}
        for k in keys:
            vals = [ps.features[k] for ps in group]
            mu = statistics.fmean(vals) if vals else 0.0
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            per_feature[k] = (mu, sd if sd > 1e-9 else 1.0)
        out[pos] = per_feature
    return out


def vectorize_college_football_season(
    ps: CollegePlayerSeason,
    stats: dict[str, dict[str, tuple[float, float]]],
) -> tuple[float, ...]:
    """Z-score normalized college feature vector for the given season."""
    keys = CFB_FEATURES_BY_POSITION[ps.position]
    pos_stats = stats[ps.position]
    return tuple(
        (ps.features[k] - pos_stats[k][0]) / pos_stats[k][1]
        for k in keys
    )
