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
from ..scoring_rules import score_season


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



# ===========================================================================
# Cumulative-career-arc vector (PR #17, v0.17.0)
# ===========================================================================
#
# The single-season-snapshot vector above answers "what does this player
# *look like* right now, per-game?" That's the wrong question for the
# Puka Nacua / Jarrett Boykin pathology Phil flagged: Boykin had one
# fluky starter stretch at age 24 that ate similar per-game shape to
# Nacua's elite age-24 season, but the two had categorically different
# CAREER-TO-DATE production (Nacua ~4191 receiving yds through 3
# seasons; Boykin ~700).
#
# The cumulative-career-arc vector below answers a different question:
# "Which historical players had THIS MUCH career-to-date production,
# at this age, this many NFL seasons in?"
#
# Features encoded (per position, all z-score normalized within position
# across the cohort of (position, age, career_season_number) tuples):
#
#   QB:  career PassYds, PassTD, Int, RushYds, RushTD, career fantasy,
#        peak season fantasy, career GS, career durability, career-per-
#        season averages, trajectory slope, peak-season age.
#   RB:  career RushAtt, RushYds, RushTD, Rec, RecYds, scrimmage yds,
#        scrimmage TDs, peak fantasy, career GS, career YPC, slope.
#   WR/TE: career Tgt, Rec, RecYds, RecTD, target-share proxy (Tgt/yr),
#        peak fantasy, career GS, career YPR, slope.
#
# Time-decay INSIDE the cumulative aggregation: recent=1.0, prior=0.7,
# prior-2=0.5, prior-3+=0.35. The most recent season weighs the most.
# (Career *totals* are also kept un-decayed so the absolute production
# floor remains visible.)
#
# Production percentile within the (position, age, career_season_number)
# cohort lives in comparables.py because it requires the full corpus
# index; this module just emits the raw features.
# ---------------------------------------------------------------------------


# Cumulative feature names per position group. Order is stable: every
# vector returned for a given position has this exact shape.
QB_CUM_FEATURES = (
    "career_pass_yds", "career_pass_td", "career_int",
    "career_rush_yds", "career_rush_td",
    "career_fantasy", "peak_fantasy",
    "career_gs", "career_durability",
    "fantasy_per_season", "slope", "peak_age_norm",
    "decayed_pass_yds", "decayed_pass_td", "decayed_rush_yds", "decayed_fantasy",
)
RB_CUM_FEATURES = (
    "career_rush_att", "career_rush_yds", "career_rush_td",
    "career_rec", "career_rec_yds",
    "career_scrimmage_yds", "career_scrimmage_td",
    "career_fantasy", "peak_fantasy",
    "career_gs", "career_ypc",
    "fantasy_per_season", "slope", "peak_age_norm",
    "decayed_rush_yds", "decayed_rec_yds", "decayed_fantasy",
)
WR_CUM_FEATURES = (
    "career_tgt", "career_rec", "career_rec_yds", "career_rec_td",
    "career_tgt_per_season",
    "career_fantasy", "peak_fantasy",
    "career_gs", "career_ypr",
    "fantasy_per_season", "slope", "peak_age_norm",
    "decayed_rec_yds", "decayed_rec", "decayed_fantasy",
)
TE_CUM_FEATURES = WR_CUM_FEATURES

CUM_FEATURES_BY_POSITION: dict[str, tuple[str, ...]] = {
    "QB": QB_CUM_FEATURES,
    "RB": RB_CUM_FEATURES,
    "WR": WR_CUM_FEATURES,
    "TE": TE_CUM_FEATURES,
}

# Time-decay weights applied INSIDE the cumulative roll-up: most
# recent NFL season gets 1.0, prior 0.7, prior-2 0.5, prior-3+ 0.35.
# These weights ride alongside the un-decayed career-total features so
# both the absolute career floor and the recency-tilted trajectory are
# encoded simultaneously.
_TIME_DECAY_WEIGHTS = (1.0, 0.7, 0.5, 0.35)


def _time_decay_weight(seasons_back: int) -> float:
    """seasons_back=0 means "this is the most recent season".

    seasons_back=3+ collapses to 0.35.
    """
    if seasons_back < 0:
        return 0.0
    if seasons_back < len(_TIME_DECAY_WEIGHTS):
        return _TIME_DECAY_WEIGHTS[seasons_back]
    return _TIME_DECAY_WEIGHTS[-1]


def _career_through_age_seasons(
    seasons: list[PlayerSeason], age: float
) -> list[PlayerSeason]:
    """Return the subset of a player's seasons through (and including) the
    given age. Seasons must be sorted by season ascending.

    Age comparison is fuzzy: any season whose age is at most ``age``
    counts (i.e. includes the age-A season itself). We rely on the
    corpus's ``min_games=4`` filter for what counts as a "career
    season" — sub-4-GP rows are excluded upstream.
    """
    if not seasons:
        return []
    out = []
    for ps in seasons:
        ps_age = ps.age if ps.age is not None else -1.0
        if ps_age <= age + 0.5:  # +0.5 tolerance for floating-point age math
            out.append(ps)
    return out


def _slope_per_year(values: list[float]) -> float:
    """Linear-regression slope of per-season fantasy points across the
    player's career so far (least-squares, intercept-free formulation
    centered on the season midpoint).

    Returns 0.0 if fewer than 2 seasons are available.
    """
    n = len(values)
    if n < 2:
        return 0.0
    # x = 0..n-1, center at mean for stability
    mx = (n - 1) / 2.0
    my = sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


def _extract_cumulative_features(
    seasons: list[PlayerSeason],
    pos: str,
    league_format: str,
) -> dict[str, float]:
    """Build the cumulative feature dict for a list of seasons (already
    filtered through-age-A) at the given position.

    All career counters use the RAW stat line from ``ps.raw`` so the
    cumulative vector survives any future per-game-normalization
    refactor. Fantasy points are re-scored under ``league_format``.
    """
    n = len(seasons)
    if n == 0:
        # Empty cohort — return all zeros at the correct shape.
        return {k: 0.0 for k in CUM_FEATURES_BY_POSITION[pos]}

    # Re-score fantasy points under the active format so the
    # cumulative-arc vector reflects what the cohort will be worth in
    # the format the user is actually playing.
    fantasy_per_season = [score_season(ps.raw, league_format, position=pos) for ps in seasons]
    peak_fantasy = max(fantasy_per_season) if fantasy_per_season else 0.0
    peak_idx = fantasy_per_season.index(peak_fantasy) if fantasy_per_season else 0
    peak_age = seasons[peak_idx].age if seasons[peak_idx].age is not None else 0.0
    career_fantasy = sum(fantasy_per_season)

    # Career totals (unweighted)
    pass_yds = sum(_f(ps.raw.get("passing_yards")) for ps in seasons)
    pass_td = sum(_f(ps.raw.get("passing_tds")) for ps in seasons)
    interceptions = sum(_f(ps.raw.get("interceptions")) for ps in seasons)
    rush_att = sum(_f(ps.raw.get("carries")) for ps in seasons)
    rush_yds = sum(_f(ps.raw.get("rushing_yards")) for ps in seasons)
    rush_td = sum(_f(ps.raw.get("rushing_tds")) for ps in seasons)
    rec = sum(_f(ps.raw.get("receptions")) for ps in seasons)
    rec_yds = sum(_f(ps.raw.get("receiving_yards")) for ps in seasons)
    rec_td = sum(_f(ps.raw.get("receiving_tds")) for ps in seasons)
    tgt = sum(_f(ps.raw.get("targets")) for ps in seasons)
    career_games = sum(ps.games for ps in seasons)
    # career_gs proxy: PFR sometimes lacks 'games_started'; if missing,
    # fall back to ``games`` so the feature is non-zero.
    career_gs = sum(
        _f(ps.raw.get("games_started")) or _f(ps.games) for ps in seasons
    )
    # Durability: ratio of games played to the theoretical max
    # (17 GP/season post-2021, 16 prior). We use 17 as the modern
    # denominator since the cohort is recency-weighted anyway.
    possible_games = 17.0 * n
    durability = career_games / possible_games if possible_games else 0.0

    scrimmage_yds = rush_yds + rec_yds
    scrimmage_td = rush_td + rec_td

    # Trajectory slope (per-season fantasy)
    slope = _slope_per_year(fantasy_per_season)

    # Decayed (recency-tilted) aggregates: last season has weight 1.0,
    # prior 0.7, prior-2 0.5, prior-3+ 0.35.
    # seasons is sorted oldest → newest, so index from the end.
    decayed_pass_yds = 0.0
    decayed_pass_td = 0.0
    decayed_rush_yds = 0.0
    decayed_rec_yds = 0.0
    decayed_rec = 0.0
    decayed_fantasy = 0.0
    for i, ps in enumerate(reversed(seasons)):
        w = _time_decay_weight(i)
        decayed_pass_yds += w * _f(ps.raw.get("passing_yards"))
        decayed_pass_td += w * _f(ps.raw.get("passing_tds"))
        decayed_rush_yds += w * _f(ps.raw.get("rushing_yards"))
        decayed_rec_yds += w * _f(ps.raw.get("receiving_yards"))
        decayed_rec += w * _f(ps.raw.get("receptions"))
        decayed_fantasy += w * fantasy_per_season[len(seasons) - 1 - i]

    common: dict[str, float] = {
        "career_pass_yds":   pass_yds,
        "career_pass_td":    pass_td,
        "career_int":        interceptions,
        "career_rush_att":   rush_att,
        "career_rush_yds":   rush_yds,
        "career_rush_td":    rush_td,
        "career_rec":        rec,
        "career_rec_yds":    rec_yds,
        "career_rec_td":     rec_td,
        "career_tgt":        tgt,
        "career_tgt_per_season": tgt / max(n, 1),
        "career_scrimmage_yds": scrimmage_yds,
        "career_scrimmage_td": scrimmage_td,
        "career_fantasy":    career_fantasy,
        "peak_fantasy":      peak_fantasy,
        "career_gs":         career_gs,
        "career_durability": durability,
        "career_ypc":        _safe_div(rush_yds, rush_att),
        "career_ypr":        _safe_div(rec_yds, rec),
        "fantasy_per_season": career_fantasy / max(n, 1),
        "slope":             slope,
        # Normalize peak-age into roughly [-1, +1]: subtract 25 (typical
        # NFL peak) and divide by 10 (career span). Keeps it in the
        # same magnitude band as other z-score features pre-normalization.
        "peak_age_norm":     (peak_age - 25.0) / 10.0 if peak_age else 0.0,
        "decayed_pass_yds":  decayed_pass_yds,
        "decayed_pass_td":   decayed_pass_td,
        "decayed_rush_yds":  decayed_rush_yds,
        "decayed_rec_yds":   decayed_rec_yds,
        "decayed_rec":       decayed_rec,
        "decayed_fantasy":   decayed_fantasy,
    }
    keys = CUM_FEATURES_BY_POSITION[pos]
    return {k: common[k] for k in keys}


@dataclass(frozen=True)
class CareerArcVector:
    """A cumulative-career-arc vector at a specific (player, age) state.

    Stored alongside the corpus so KNN at query time is O(N) and the
    historical arcs don't need rebuilding for each query.
    """
    player_id: str
    player_name: str
    position: str
    age: float                       # the age through which this arc accumulates
    career_season_number: int        # 1, 2, 3, … (count of seasons in corpus through age)
    league_format: str
    raw_features: dict[str, float]   # un-normalized features (for percentile math)
    # Most-recent (latest in the cumulative window) PlayerSeason — used
    # by projection.py to compute realized future career outcomes.
    latest_season: PlayerSeason


def build_career_arc_corpus(
    corpus: list[PlayerSeason],
    league_format: str = "sf_ppr",
) -> list[CareerArcVector]:
    """Materialize a cumulative-career-arc record for every (player,
    age-through) checkpoint in the historical corpus.

    For each player with N qualifying seasons we emit N records:
    "through age of season-1", "through age of season-2", …, "through
    age of season-N". Each record exposes the cumulative-through-age
    feature vector.

    The result is the historical corpus the cohort-filtered KNN searches
    over.
    """
    by_pid: dict[str, list[PlayerSeason]] = {}
    for ps in corpus:
        by_pid.setdefault(ps.player_id, []).append(ps)
    for arr in by_pid.values():
        arr.sort(key=lambda x: x.season)

    out: list[CareerArcVector] = []
    for pid, seasons in by_pid.items():
        for i, ps in enumerate(seasons, start=1):
            if ps.age is None:
                continue
            sub = seasons[:i]
            feats = _extract_cumulative_features(sub, ps.position, league_format)
            out.append(CareerArcVector(
                player_id=pid,
                player_name=ps.player_name,
                position=ps.position,
                age=float(ps.age),
                career_season_number=i,
                league_format=league_format,
                raw_features=feats,
                latest_season=ps,
            ))
    return out


def vectorize_career_through_age(
    player_id: str,
    age: float,
    corpus: list[PlayerSeason],
    league_format: str = "sf_ppr",
) -> Optional[CareerArcVector]:
    """Convenience helper exposed at module level for direct callers.

    Builds (on demand) the cumulative-career-arc vector for one
    (player, age) state. Returns ``None`` if the player has no
    qualifying season at or before ``age``.
    """
    arr = [ps for ps in corpus if ps.player_id == player_id and ps.age is not None]
    arr.sort(key=lambda x: x.season)
    sub = _career_through_age_seasons(arr, age)
    if not sub:
        return None
    pos = sub[-1].position
    feats = _extract_cumulative_features(sub, pos, league_format)
    return CareerArcVector(
        player_id=player_id,
        player_name=sub[-1].player_name,
        position=pos,
        age=float(sub[-1].age),
        career_season_number=len(sub),
        league_format=league_format,
        raw_features=feats,
        latest_season=sub[-1],
    )


def compute_cumulative_zscore_stats(
    arcs: list[CareerArcVector],
) -> dict[str, dict[str, tuple[float, float]]]:
    """Per-(position, feature) (mean, stdev) tuples for the cumulative
    corpus, used to z-score-normalize a CareerArcVector for cosine
    similarity. Mirrors :func:`compute_zscore_stats` for the snapshot
    vector.
    """
    by_pos: dict[str, list[CareerArcVector]] = {}
    for a in arcs:
        by_pos.setdefault(a.position, []).append(a)

    out: dict[str, dict[str, tuple[float, float]]] = {}
    for pos, group in by_pos.items():
        keys = CUM_FEATURES_BY_POSITION[pos]
        per_feature: dict[str, tuple[float, float]] = {}
        for k in keys:
            vals = [a.raw_features[k] for a in group]
            mu = statistics.fmean(vals) if vals else 0.0
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            per_feature[k] = (mu, sd if sd > 1e-9 else 1.0)
        out[pos] = per_feature
    return out


def vectorize_cumulative(
    arc: CareerArcVector,
    stats: dict[str, dict[str, tuple[float, float]]],
) -> tuple[float, ...]:
    """Z-score normalized vector for a CareerArcVector — used in KNN."""
    keys = CUM_FEATURES_BY_POSITION[arc.position]
    pos_stats = stats[arc.position]
    return tuple(
        (arc.raw_features[k] - pos_stats[k][0]) / pos_stats[k][1]
        for k in keys
    )
