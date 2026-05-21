"""Similarity v1 engine — the single source of truth for player rankings.

Pipeline:
    1. Build a RETIRED-only corpus (last_season ≤ RETIRED_THROUGH_SEASON, by
       default 2022 — three years inactive).
    2. Bucket every player-season into an era (era_pace.era_for_season).
    3. Compute per-position, per-era z-score normalisations of per-game stats
       and store an era-adjusted "shape vector" per player-season.
    4. For each ACTIVE player, build a cumulative-through-age vector (career
       totals through their current age) and find top-K nearest neighbours
       in the retired corpus, restricted to:
           - same position
           - age within ±1 of the current player's age
           - era-normalised cosine similarity
    5. For each comp, take their realised post-age career, rescale every stat
       through era-pace multipliers to era 4 (current), and aggregate the
       weighted projected fantasy points (PPR-default, format-tweakable
       later via format_overlay).
    6. Apply a 5%/year present-value discount and emit production_score.

The engine is deliberately self-contained: it reads
``data/nflverse/player_stats_season.csv.gz`` + ``data/nflverse/players.csv.gz``
and writes per-player JSON sidecars + the master rankings JSON under
``data/engine_v1/``.

No network calls. All inputs are committed.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .era_pace import (
    ERA_BOUNDS,
    EraPaceTable,
    FALLBACK_MULTIPLIERS,
    era_for_season,
    fallback_table,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_ROOT = Path("data/nflverse")
OUT_ROOT = Path("data/engine_v1")
OUT_ROOT.mkdir(parents=True, exist_ok=True)

# Player is considered "retired" if their last_season is on or before this year.
# Three years inactive is the brief's threshold and keeps Calvin Johnson,
# Megatron, Andre Johnson, Larry Fitzgerald etc. in the corpus while excluding
# in-progress careers (Rodgers retired after 2024 → he's borderline; we err on
# the side of keeping the corpus *clean* and exclude him as 'recently active').
RETIRED_THROUGH_SEASON = 2022

# Skill positions we model.
SKILL_POSITIONS: Tuple[str, ...] = ("QB", "RB", "WR", "TE")

# Per-position feature set: per-game rate stats only (the era z-score lives in
# rate-space so volume differences from era inflation are normalised away).
FEATURES: Dict[str, Tuple[str, ...]] = {
    "QB": (
        "passing_yards", "passing_tds", "interceptions",
        "rushing_yards", "rushing_tds",
    ),
    "RB": (
        "rushing_yards", "rushing_tds",
        "receptions", "receiving_yards", "receiving_tds",
    ),
    "WR": (
        "receptions", "receiving_yards", "receiving_tds",
        "rushing_yards", "rushing_tds",  # WR-rushing trickle (jet sweeps)
    ),
    "TE": (
        "receptions", "receiving_yards", "receiving_tds",
    ),
}

# Fantasy scoring (SF-PPR default).
DEFAULT_SCORING = {
    "passing_yards":   0.04,   # 1 pt / 25 yds
    "passing_tds":     4.0,
    "interceptions":   -2.0,
    "rushing_yards":   0.10,   # 1 pt / 10 yds
    "rushing_tds":     6.0,
    "receptions":      1.0,    # PPR
    "receiving_yards": 0.10,
    "receiving_tds":   6.0,
}

# Discount future seasons.
DISCOUNT_PER_YEAR = 0.05

# Neighbourhood size.
TOP_K_COMPS = 20
AGE_WINDOW = 1
MIN_GAMES_PER_SEASON = 4   # filter cup-of-coffee seasons


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class PlayerSeason:
    player_id: str
    season: int
    age: int                       # may be None-ish (-1) if birth_date missing
    position: str
    games: int
    stats: Dict[str, float]
    fantasy_points_ppr: float

    @property
    def era(self) -> int:
        return era_for_season(self.season)


@dataclass
class PlayerCareer:
    player_id: str
    name: str
    position: str
    birth_year: Optional[int]
    rookie_season: Optional[int]
    last_season: Optional[int]
    seasons: List[PlayerSeason] = field(default_factory=list)

    def is_retired(self, through: int = RETIRED_THROUGH_SEASON) -> bool:
        return self.last_season is not None and self.last_season <= through

    def career_total(self, stat: str) -> float:
        return float(sum(s.stats.get(stat, 0.0) for s in self.seasons))

    def career_ppr(self) -> float:
        return float(sum(s.fantasy_points_ppr for s in self.seasons))

    def seasons_through_age(self, age_cap: int) -> List[PlayerSeason]:
        return [s for s in self.seasons if s.age is not None and s.age <= age_cap]

    def seasons_after_age(self, age_floor: int) -> List[PlayerSeason]:
        return [s for s in self.seasons if s.age is not None and s.age > age_floor]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_players_meta(path: Path) -> Dict[str, Dict[str, str]]:
    """Return gsis_id -> row from players.csv.gz."""
    out: Dict[str, Dict[str, str]] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        for row in r:
            gid = row.get("gsis_id") or ""
            if not gid:
                continue
            # Skip ID collisions (the LB Justin Jefferson case): keep the
            # skill-position row preferentially.
            existing = out.get(gid)
            pos = (row.get("position") or "").upper()
            if existing is None or pos in SKILL_POSITIONS:
                out[gid] = row
    return out


def _birth_year(meta_row: Optional[Dict[str, str]]) -> Optional[int]:
    if not meta_row:
        return None
    bd = (meta_row.get("birth_date") or "").strip()
    if not bd or len(bd) < 4:
        return None
    try:
        return int(bd[:4])
    except ValueError:
        return None


def _age_for_season(birth_year: Optional[int], season: int) -> Optional[int]:
    if birth_year is None:
        return None
    # NFL season starts in September → player's age during the season.
    return season - birth_year


def _safe_float(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_corpus(
    stats_path: Path = DATA_ROOT / "player_stats_season.csv.gz",
    players_path: Path = DATA_ROOT / "players.csv.gz",
) -> Dict[str, PlayerCareer]:
    """Load every player career into PlayerCareer objects keyed by gsis_id."""
    meta = _load_players_meta(players_path)
    careers: Dict[str, PlayerCareer] = {}

    with gzip.open(stats_path, "rt", encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        for row in r:
            if (row.get("season_type") or "REG") != "REG":
                continue
            pid = row.get("player_id") or ""
            if not pid:
                continue
            position = (row.get("position") or "").upper()
            if position not in SKILL_POSITIONS:
                continue
            try:
                season = int(row.get("season") or 0)
            except ValueError:
                continue
            if season < 1980:
                continue
            games = int(_safe_float(row.get("games") or 0))
            if games < MIN_GAMES_PER_SEASON:
                continue

            stats = {
                "passing_yards":   _safe_float(row.get("passing_yards")),
                "passing_tds":     _safe_float(row.get("passing_tds")),
                "interceptions":   _safe_float(row.get("interceptions")),
                "rushing_yards":   _safe_float(row.get("rushing_yards")),
                "rushing_tds":     _safe_float(row.get("rushing_tds")),
                "receptions":      _safe_float(row.get("receptions")),
                "receiving_yards": _safe_float(row.get("receiving_yards")),
                "receiving_tds":   _safe_float(row.get("receiving_tds")),
                "games":           float(games),
            }
            fp_ppr = _safe_float(row.get("fantasy_points_ppr"))
            m = meta.get(pid)
            by = _birth_year(m)
            age = _age_for_season(by, season)
            if age is None:
                # Estimate age from rookie_season + 22 as a fallback so that
                # we don't lose retired greats with missing birth dates.
                rs = None
                if m and (m.get("rookie_season") or "").isdigit():
                    rs = int(m["rookie_season"])
                if rs is not None:
                    age = 22 + (season - rs)
            if age is None:
                continue
            ps = PlayerSeason(
                player_id=pid,
                season=season,
                age=age,
                position=position,
                games=games,
                stats=stats,
                fantasy_points_ppr=fp_ppr,
            )

            c = careers.get(pid)
            if c is None:
                name = (
                    (m.get("display_name") if m else None)
                    or row.get("player_display_name")
                    or row.get("player_name")
                    or pid
                )
                rookie_season = None
                last_season = None
                if m:
                    if (m.get("rookie_season") or "").isdigit():
                        rookie_season = int(m["rookie_season"])
                    if (m.get("last_season") or "").isdigit():
                        last_season = int(m["last_season"])
                c = PlayerCareer(
                    player_id=pid,
                    name=name,
                    position=position,
                    birth_year=by,
                    rookie_season=rookie_season,
                    last_season=last_season,
                    seasons=[],
                )
                careers[pid] = c
            c.seasons.append(ps)

    # Sort seasons chronologically; derive last_season if metadata missing.
    for c in careers.values():
        c.seasons.sort(key=lambda s: s.season)
        if c.last_season is None and c.seasons:
            c.last_season = c.seasons[-1].season
        if c.rookie_season is None and c.seasons:
            c.rookie_season = c.seasons[0].season

    return careers


# ---------------------------------------------------------------------------
# Era-z-scoring
# ---------------------------------------------------------------------------

@dataclass
class EraZNorm:
    """Per-position, per-era, per-stat mean/std on per-game rate."""

    means: Dict[Tuple[str, int, str], float]
    stds: Dict[Tuple[str, int, str], float]

    def z(self, position: str, era: int, stat: str, per_game_value: float) -> float:
        key = (position, era, stat)
        mu = self.means.get(key, 0.0)
        sd = self.stds.get(key, 1.0)
        if sd <= 1e-9:
            return 0.0
        return (per_game_value - mu) / sd


def build_era_z_norm(careers: Dict[str, PlayerCareer]) -> EraZNorm:
    bucket: Dict[Tuple[str, int, str], List[float]] = defaultdict(list)
    for c in careers.values():
        for s in c.seasons:
            if s.games < MIN_GAMES_PER_SEASON:
                continue
            for stat in FEATURES[c.position]:
                per_game = s.stats.get(stat, 0.0) / max(s.games, 1)
                bucket[(c.position, s.era, stat)].append(per_game)
    means: Dict[Tuple[str, int, str], float] = {}
    stds: Dict[Tuple[str, int, str], float] = {}
    for k, vals in bucket.items():
        if not vals:
            means[k] = 0.0
            stds[k] = 1.0
            continue
        mu = sum(vals) / len(vals)
        var = sum((v - mu) ** 2 for v in vals) / max(len(vals) - 1, 1)
        sd = math.sqrt(var) if var > 0 else 1.0
        means[k] = mu
        stds[k] = sd
    return EraZNorm(means=means, stds=stds)


def player_career_vector(
    career: PlayerCareer,
    znorm: EraZNorm,
    through_age: Optional[int] = None,
) -> Optional[List[float]]:
    """Compute an era-normalised, cumulative-through-age vector.

    For each feature: average the per-game era z-scores across all qualifying
    seasons up to ``through_age``, weighted by games played.
    """
    feats = FEATURES[career.position]
    seasons = career.seasons_through_age(through_age) if through_age is not None else career.seasons
    seasons = [s for s in seasons if s.games >= MIN_GAMES_PER_SEASON]
    if not seasons:
        return None
    vec: List[float] = []
    for stat in feats:
        num = 0.0
        den = 0.0
        for s in seasons:
            per_game = s.stats.get(stat, 0.0) / max(s.games, 1)
            z = znorm.z(career.position, s.era, stat, per_game)
            num += z * s.games
            den += s.games
        vec.append(num / den if den > 0 else 0.0)
    return vec


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 1e-9 or nb <= 1e-9:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Era-pace calibration (corpus-derived)
# ---------------------------------------------------------------------------

def build_era_pace_table(careers: Dict[str, PlayerCareer]) -> EraPaceTable:
    """Empirically calibrate era_from→4 multipliers from the corpus.

    For each (position, stat, era_from): take the median per-game rate among
    qualifying seasons in era_from and in era 4. Multiplier = median_era4 /
    median_era_from. Fall back to documented values when a cell is empty
    (e.g., fewer than 20 era-1 QB seasons in our 1999+ window).
    """
    samples: Dict[Tuple[str, str, int], List[float]] = defaultdict(list)
    for c in careers.values():
        for s in c.seasons:
            if s.games < MIN_GAMES_PER_SEASON:
                continue
            for stat in FALLBACK_MULTIPLIERS.get(c.position, {}).keys():
                per_game = s.stats.get(stat, 0.0) / max(s.games, 1)
                samples[(c.position, stat, s.era)].append(per_game)

    def median(xs: List[float]) -> Optional[float]:
        if not xs:
            return None
        sx = sorted(xs)
        m = len(sx) // 2
        if len(sx) % 2 == 1:
            return sx[m]
        return 0.5 * (sx[m - 1] + sx[m])

    mults: Dict[str, Dict[str, Dict[int, float]]] = {}
    for pos, stat_map in FALLBACK_MULTIPLIERS.items():
        mults.setdefault(pos, {})
        for stat in stat_map:
            mults[pos].setdefault(stat, {})
            med4 = median(samples.get((pos, stat, 4), []))
            for era_from in (1, 2, 3, 4):
                med_from = median(samples.get((pos, stat, era_from), []))
                if med4 is None or med_from is None or med_from <= 1e-6:
                    mults[pos][stat][era_from] = FALLBACK_MULTIPLIERS[pos][stat].get(era_from, 1.0)
                    continue
                ratio = med4 / med_from
                # Clamp to sane band [0.6, 2.0] to avoid one-off seasons
                # blowing the ratio up.
                ratio = max(0.6, min(2.0, ratio))
                mults[pos][stat][era_from] = ratio
    return EraPaceTable(multipliers=mults, source="corpus")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _project_comp_post_age(
    comp: PlayerCareer,
    age_floor: int,
    pace: EraPaceTable,
    scoring: Dict[str, float],
) -> Tuple[float, int]:
    """Re-score a retired comp's post-age career through modern era-pace
    and the supplied fantasy scoring rules.

    Returns (total_projected_points, n_seasons).
    """
    total = 0.0
    n = 0
    for s in comp.seasons_after_age(age_floor):
        if s.games < MIN_GAMES_PER_SEASON:
            continue
        season_pts = 0.0
        era_from = s.era
        for stat, weight in scoring.items():
            raw = s.stats.get(stat, 0.0)
            mult = pace.get(comp.position, stat, era_from)
            season_pts += raw * mult * weight
        # Time-discount by years out from current (n=0 is first projected year)
        season_pts *= (1.0 - DISCOUNT_PER_YEAR) ** n
        total += season_pts
        n += 1
    return total, n


def find_comps(
    target: PlayerCareer,
    retired_corpus: List[PlayerCareer],
    znorm: EraZNorm,
    through_age: Optional[int],
    k: int = TOP_K_COMPS,
    age_window: int = AGE_WINDOW,
) -> List[Tuple[PlayerCareer, float]]:
    """Find top-k similar RETIRED comps for a target player at a given age."""
    tv = player_career_vector(target, znorm, through_age=through_age)
    if tv is None:
        return []
    candidates: List[Tuple[PlayerCareer, float]] = []
    target_age = through_age if through_age is not None else (
        target.seasons[-1].age if target.seasons else 25
    )
    for comp in retired_corpus:
        if comp.position != target.position:
            continue
        if comp.player_id == target.player_id:
            continue
        # Comp must have played past `target_age` so there's a future to project
        if not comp.seasons_after_age(target_age):
            continue
        # Comp must have meaningful career THROUGH the same age
        comp_v = player_career_vector(comp, znorm, through_age=target_age + age_window)
        if comp_v is None:
            continue
        # Age-window filter: the comp must have at least one season inside the
        # target age window — i.e., they were active at a comparable age.
        ages_in_window = any(
            abs(s.age - target_age) <= age_window for s in comp.seasons
        )
        if not ages_in_window:
            continue
        sim = _cosine(tv, comp_v)
        if sim <= 0:
            continue
        candidates.append((comp, sim))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:k]


# ---------------------------------------------------------------------------
# Top-level engine
# ---------------------------------------------------------------------------

@dataclass
class EngineResult:
    rankings: List[Dict]                             # sorted production list
    comps: Dict[str, List[Dict]]                    # player_id -> comp list
    era_pace: EraPaceTable
    znorm: EraZNorm
    careers: Dict[str, PlayerCareer]
    retired_corpus: List[PlayerCareer]
    active_players: List[PlayerCareer]

    def as_player_dict(self, pid: str) -> Optional[Dict]:
        for row in self.rankings:
            if row["player_id"] == pid:
                return row
        return None


def _is_active(career: PlayerCareer, current_season: int) -> bool:
    """Active = last_season >= current_season - 1, AND at least one qualifying
    NFL season on file."""
    if not career.seasons:
        return False
    if career.last_season is None:
        return False
    return career.last_season >= (current_season - 1)


def _comp_tier_label(top_comp: PlayerCareer, top_comp_career_ppr: float) -> str:
    """Compact label like 'elite (Megatron)' or 'above-avg (Boldin)'."""
    # Tier bands chosen from corpus distribution: top 5% ≈ "elite"; top 20%
    # ≈ "above-avg"; top 40% ≈ "starter"; else "deep".
    tier = "deep"
    if top_comp_career_ppr >= 1800:
        tier = "elite"
    elif top_comp_career_ppr >= 1100:
        tier = "above-avg"
    elif top_comp_career_ppr >= 600:
        tier = "starter"
    return f"{tier} ({top_comp.name})"


def run_engine(
    current_season: int = 2024,
    retired_through: int = RETIRED_THROUGH_SEASON,
    scoring: Optional[Dict[str, float]] = None,
    top_k: int = TOP_K_COMPS,
    persist: bool = True,
) -> EngineResult:
    scoring = scoring or DEFAULT_SCORING

    careers = load_corpus()
    znorm = build_era_z_norm(careers)
    pace = build_era_pace_table(careers)

    retired_corpus = [
        c for c in careers.values()
        if c.is_retired(through=retired_through) and len(c.seasons) >= 2
    ]
    active_players = [
        c for c in careers.values()
        if _is_active(c, current_season=current_season)
    ]

    rankings: List[Dict] = []
    comps_map: Dict[str, List[Dict]] = {}

    for ap in active_players:
        # Use the player's most recent age as the projection age floor.
        # If birth_date is missing, fall back to estimated age from rookie+22.
        last_season = ap.seasons[-1]
        age_now = last_season.age
        comps = find_comps(
            ap, retired_corpus, znorm,
            through_age=age_now,
            k=top_k,
        )
        if not comps:
            continue
        # Weighted projection.
        total_sim = sum(s for _, s in comps) or 1.0
        weighted_points = 0.0
        weighted_seasons = 0.0
        comp_records: List[Dict] = []
        for comp, sim in comps:
            pts, nseasons = _project_comp_post_age(
                comp, age_floor=age_now, pace=pace, scoring=scoring,
            )
            w = sim / total_sim
            weighted_points += pts * w
            weighted_seasons += nseasons * w
            comp_records.append({
                "player_id": comp.player_id,
                "name": comp.name,
                "position": comp.position,
                "last_season": comp.last_season,
                "similarity": round(float(sim), 4),
                "career_ppr": round(comp.career_ppr(), 1),
                "post_age_projected_pts": round(pts, 1),
                "post_age_seasons": nseasons,
            })

        top_comp = comps[0][0]
        rankings.append({
            "player_id": ap.player_id,
            "name": ap.name,
            "position": ap.position,
            "age": age_now,
            "last_season": ap.last_season,
            "production_score": round(weighted_points, 1),
            "projected_years_remaining": round(weighted_seasons, 1),
            "top_comp": top_comp.name,
            "top_comp_id": top_comp.player_id,
            "comp_tier": _comp_tier_label(top_comp, top_comp.career_ppr()),
            "n_comps": len(comps),
        })
        comps_map[ap.player_id] = comp_records

    rankings.sort(key=lambda r: r["production_score"], reverse=True)
    # Assign tiers (T1-T9 on production_score quantile buckets).
    n = len(rankings)
    for i, row in enumerate(rankings):
        row["overall_rank"] = i + 1
        # Roughly even-weight tiers, but log-weighted so T1 is small (top ~12).
        # Simple bucket: T1=top12, T2=13-24, T3=25-48, T4=49-72, T5=73-108,
        # T6=109-144, T7=145-200, T8=201-260, T9=261+
        thresholds = [12, 24, 48, 72, 108, 144, 200, 260]
        tier = 9
        for t_idx, th in enumerate(thresholds, start=1):
            if i + 1 <= th:
                tier = t_idx
                break
        row["tier"] = tier

    result = EngineResult(
        rankings=rankings,
        comps=comps_map,
        era_pace=pace,
        znorm=znorm,
        careers=careers,
        retired_corpus=retired_corpus,
        active_players=active_players,
    )

    if persist:
        _persist(result, current_season)

    return result


def _persist(result: EngineResult, current_season: int) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    # rankings.json
    payload = {
        "generated_at_season": current_season,
        "n_active": len(result.active_players),
        "n_retired": len(result.retired_corpus),
        "n_ranked": len(result.rankings),
        "era_pace_source": result.era_pace.source,
        "era_pace": result.era_pace.multipliers,
        "rankings": result.rankings,
    }
    (OUT_ROOT / "rankings.json").write_text(
        json.dumps(payload, indent=2, default=float), encoding="utf-8"
    )
    # comps.json (sidecar map)
    (OUT_ROOT / "comps.json").write_text(
        json.dumps(result.comps, indent=2, default=float), encoding="utf-8"
    )
    # retired_corpus summary (lightweight)
    retired = [
        {
            "player_id": c.player_id,
            "name": c.name,
            "position": c.position,
            "last_season": c.last_season,
            "career_ppr": round(c.career_ppr(), 1),
            "n_seasons": len(c.seasons),
        }
        for c in result.retired_corpus
    ]
    (OUT_ROOT / "retired_corpus.json").write_text(
        json.dumps(retired, indent=2, default=float), encoding="utf-8"
    )


# Convenience accessor for tests / debugging.
def comp_names_for(result: EngineResult, player_name: str) -> List[str]:
    pid: Optional[str] = None
    for ap in result.active_players:
        if ap.name == player_name:
            pid = ap.player_id
            break
    if pid is None:
        return []
    return [c["name"] for c in result.comps.get(pid, [])]
