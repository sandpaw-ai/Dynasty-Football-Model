"""Engine entry point — v2.0 fantasy-point-arc methodology.

NOTE: the module name remains ``similarity_v1`` for back-compat with all
existing callers (``format_overlay``, ``report``, sources, tests). The
IMPLEMENTATION is the v2.0 fantasy-point-arc engine; the v1.x per-stat
z-score machinery has been removed.

Pipeline:

    1.  Load player careers from nflverse season CSV.
    2.  Classify each player as ``long_arc`` (retired before
        ``LONG_ARC_THROUGH_SEASON``, or 8+ NFL seasons, or 33+ years old
        with 6+ seasons).
    3.  Build a corpus-derived era-pace table (era 1→4, 2→4, 3→4
        multipliers per (position, stat)).
    4.  Build a FANTASY-POINT ARC for every player under every supported
        scoring format (sf_ppr, 1qb_ppr, 2qb_ppr, half_ppr, std,
        sf_te_premium). Each season's stat line is era-pace-adjusted
        BEFORE scoring, so every fp value in the arc is in
        modern-fp-equivalent units.
    5.  Build a per-(position, career_stage) percentile table from the
        long-arc corpus' career-total fp.
    6.  For each ACTIVE player, build their 10-dim arc vector at the
        current age, cosine-match against same-position long-arc players
        in an age ±1 / career-stage ±1 window, take top-20 by cosine.
    7.  Project each comp's realised post-age fantasy points
        (modern-fp-equivalent), discount 5%/yr, similarity-weight, sum.
    8.  For QBs, apply v1.1's career-length era lift on
        projected_remaining_seasons AND projected_remaining_fp (mobile /
        dual-threat only; pocket lift = 1.0). This is the ONE piece of
        v1.x machinery v2.0 keeps — modern medicine extends mobile-QB
        careers and the long-arc comp pool is still half-retired-from-
        era-3 dual-threats.

No z-scoring. No style cohort. No per-stat-shape vectors. The 10-dim
vector lives in raw fantasy-point space.
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
from .career_length_era import (
    CareerLengthEraTable,
    STYLE_DUAL_THREAT,
    STYLE_MOBILE,
    STYLE_POCKET,
    apply_lift,
    build_career_length_era_table,
    career_rushing_rate,
    style_for_career,
)
from .fantasy_arc import (
    CareerArc,
    SUPPORTED_FORMATS,
    build_career_arc,
    persist_corpus,
)
from .fantasy_arc_similarity import (
    AGE_WINDOW,
    BASE_FORMAT,
    CAREER_STAGE_WINDOW,
    CompMatch,
    CareerStagePercentileTable,
    DISCOUNT_PER_YEAR,
    MIN_GAMES_PER_SEASON,
    TOP_K_COMPS,
    build_arc_vector,
    build_career_stage_percentile_table,
    find_comps as arc_find_comps,
    project_remaining as arc_project_remaining,
    project_player as arc_project_player,
)
from .rookie_nfl_fp_arc import (
    FULL_CONFIDENCE_GAMES,
    POSITION_ENCODING as ROOKIE_POSITIONS,
    RookieCompMatch,
    RookieProfile,
    RookieProjectionResult,
    build_rookie_corpus,
    project_rookie,
)
from ..scoring_rules import LEAGUE_SCORING

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_ROOT = Path("data/nflverse")
OUT_ROOT = Path("data/engine_v1")
OUT_ROOT.mkdir(parents=True, exist_ok=True)
OUT_ROOT_V2 = Path("data/engine_v2")
OUT_ROOT_V2.mkdir(parents=True, exist_ok=True)

# Long-arc corpus thresholds — UNCHANGED from v1.1.
RETIRED_THROUGH_SEASON = 2022
LONG_ARC_THROUGH_SEASON = 2022
LONG_ARC_MIN_SEASONS = 8
LONG_ARC_VETERAN_AGE = 33
LONG_ARC_VETERAN_SEASONS = 6

SKILL_POSITIONS: Tuple[str, ...] = ("QB", "RB", "WR", "TE")

# Fantasy scoring (sf_ppr default) — used only as the "default scoring"
# parameter callers expect. The real scoring lives in scoring_rules.LEAGUE_SCORING.
DEFAULT_SCORING = dict(LEAGUE_SCORING[BASE_FORMAT])


# ---------------------------------------------------------------------------
# Data containers (kept for back-compat with format_overlay + tests)
# ---------------------------------------------------------------------------

@dataclass
class PlayerSeason:
    player_id: str
    season: int
    age: int
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

    def is_long_arc(
        self,
        through: int = LONG_ARC_THROUGH_SEASON,
        min_seasons: int = LONG_ARC_MIN_SEASONS,
        veteran_age: int = LONG_ARC_VETERAN_AGE,
        veteran_seasons: int = LONG_ARC_VETERAN_SEASONS,
    ) -> bool:
        if self.is_retired(through=through):
            return True
        n_seasons = len(self.seasons)
        if n_seasons >= min_seasons:
            return True
        last_age = 0
        for s in self.seasons:
            if s.age is not None and s.age > last_age:
                last_age = s.age
        if last_age >= veteran_age and n_seasons >= veteran_seasons:
            return True
        return False

    def career_total(self, stat: str) -> float:
        return float(sum(s.stats.get(stat, 0.0) for s in self.seasons))

    def career_ppr(self) -> float:
        return float(sum(s.fantasy_points_ppr for s in self.seasons))

    def seasons_through_age(self, age_cap: int) -> List[PlayerSeason]:
        return [s for s in self.seasons if s.age is not None and s.age <= age_cap]

    def seasons_after_age(self, age_floor: int) -> List[PlayerSeason]:
        return [s for s in self.seasons if s.age is not None and s.age > age_floor]

    def with_completed_seasons_only(self, through_season: int) -> "PlayerCareer":
        kept = [s for s in self.seasons if s.season <= through_season]
        last = kept[-1].season if kept else None
        return PlayerCareer(
            player_id=self.player_id,
            name=self.name,
            position=self.position,
            birth_year=self.birth_year,
            rookie_season=self.rookie_season,
            last_season=last,
            seasons=kept,
        )


# ---------------------------------------------------------------------------
# Loaders (unchanged from v1.x)
# ---------------------------------------------------------------------------

def _load_players_meta(path: Path) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        for row in r:
            gid = row.get("gsis_id") or ""
            if not gid:
                continue
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

    for c in careers.values():
        c.seasons.sort(key=lambda s: s.season)
        if c.last_season is None and c.seasons:
            c.last_season = c.seasons[-1].season
        if c.rookie_season is None and c.seasons:
            c.rookie_season = c.seasons[0].season

    return careers


# ---------------------------------------------------------------------------
# Era-pace calibration (corpus-derived) — unchanged from v1.x
# ---------------------------------------------------------------------------

def build_era_pace_table(careers: Dict[str, PlayerCareer]) -> EraPaceTable:
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
                ratio = max(0.6, min(2.0, ratio))
                mults[pos][stat][era_from] = ratio
    return EraPaceTable(multipliers=mults, source="corpus")


# ---------------------------------------------------------------------------
# CareerArc construction
# ---------------------------------------------------------------------------

def _career_to_arc_seasons(career: PlayerCareer) -> List[Dict]:
    """Convert a PlayerCareer's seasons into the dict-of-dicts shape that
    ``fantasy_arc.build_career_arc`` expects."""
    out: List[Dict] = []
    for s in career.seasons:
        if s.games < MIN_GAMES_PER_SEASON:
            continue
        out.append({
            "season": s.season,
            "age": s.age,
            "games": s.games,
            "era": s.era,
            "stats": s.stats,
        })
    return out


def _build_arcs(
    careers: Iterable[PlayerCareer],
    pace: EraPaceTable,
) -> Dict[str, CareerArc]:
    arcs: Dict[str, CareerArc] = {}
    for c in careers:
        seasons = _career_to_arc_seasons(c)
        if not seasons:
            continue
        retired = c.is_retired()
        is_long_arc = c.is_long_arc()
        arc = build_career_arc(
            player_id=c.player_id,
            name=c.name,
            position=c.position,
            last_season=c.last_season,
            rookie_season=c.rookie_season,
            retired=retired,
            is_long_arc=is_long_arc,
            seasons=seasons,
            pace=pace,
            formats=SUPPORTED_FORMATS,
        )
        arcs[c.player_id] = arc
    return arcs


# ---------------------------------------------------------------------------
# Top-level engine
# ---------------------------------------------------------------------------

@dataclass
class EngineResult:
    rankings: List[Dict]
    comps: Dict[str, List[Dict]]
    era_pace: EraPaceTable
    careers: Dict[str, PlayerCareer]
    long_arc_corpus: List[PlayerCareer]
    active_players: List[PlayerCareer]
    career_length_era: Optional[CareerLengthEraTable] = None
    # v2.0 — fantasy-arc data
    arcs: Dict[str, CareerArc] = field(default_factory=dict)
    long_arc_arcs: List[CareerArc] = field(default_factory=list)
    percentile_table: Optional[CareerStagePercentileTable] = None
    base_format: str = BASE_FORMAT
    # Removed-in-v2 fields kept as None for downstream readers that hardcode them.
    znorm: object = None
    cohort_index: object = None
    cohort_diag: object = None

    @property
    def retired_corpus(self) -> List[PlayerCareer]:
        return self.long_arc_corpus

    def as_player_dict(self, pid: str) -> Optional[Dict]:
        for row in self.rankings:
            if row["player_id"] == pid:
                return row
        return None


def _is_active(career: PlayerCareer, current_season: int) -> bool:
    if not career.seasons:
        return False
    if career.last_season is None:
        return False
    return career.last_season >= (current_season - 1)


def _comp_tier_label(top_comp: CareerArc, top_comp_career_fp: float) -> str:
    """Compact label like 'elite (Megatron)' or 'above-avg (Boldin)'.

    Tiers re-anchored to fp_total (sf_ppr) since v2.0 ranks by projected
    fantasy points, not PPR-only points.
    """
    tier = "deep"
    if top_comp_career_fp >= 1800:
        tier = "elite"
    elif top_comp_career_fp >= 1100:
        tier = "above-avg"
    elif top_comp_career_fp >= 600:
        tier = "starter"
    return f"{tier} ({top_comp.name})"


def _completed_nfl_seasons(career: PlayerCareer) -> int:
    """Number of completed NFL seasons (games >= MIN_GAMES_PER_SEASON).

    A "partial current season" (the in-progress year, or an injury-shortened
    rookie year < 4 games) is NOT counted as completed. This drives the v2.1
    cohort dispatcher: 1 = rookie engine, 2+ = v2.0 cumulative engine.

    Note: the v2.0 corpus loader (load_corpus) already filters seasons with
    games < MIN_GAMES_PER_SEASON=4, so every season on the career counts.
    Travis Hunter (7G) and Malik Nabers (4G in 2024) DO count as 1
    completed rookie season because they meet the 4-game floor.
    """
    return sum(1 for s in career.seasons if s.games >= MIN_GAMES_PER_SEASON)


def _raw_stats_by_pid_season(careers: Dict[str, PlayerCareer]) -> Dict[Tuple[str, int], Dict[str, float]]:
    """Pid+season → raw stat-line dict. Used by rookie_nfl_fp_arc to read
    rookie-year per-category yards/TDs (the fp/G dimension comes from the
    era-pace-adjusted arc corpus, but the per-category vector dims come
    from raw stats).
    """
    out: Dict[Tuple[str, int], Dict[str, float]] = {}
    for c in careers.values():
        for s in c.seasons:
            out[(c.player_id, s.season)] = s.stats
    return out


def _rookie_comp_records(
    comps: Sequence[RookieCompMatch], league_format: str,
) -> List[Dict]:
    """Convert RookieCompMatch list to the comp-record dict shape that
    format_overlay + report.py expect. Sets snapshot_age = comp's
    rookie_age so that format_overlay's re-projection sums fp for
    seasons at age > rookie_age (i.e. year 2+).
    """
    records: List[Dict] = []
    for m in comps:
        arc = m.profile.arc
        # post-age stats projected from comp's year-2+
        pts = 0.0
        n_seasons = 0
        for s in arc.career_arc:
            if s.season <= m.profile.rookie_season:
                continue
            if s.games < MIN_GAMES_PER_SEASON:
                continue
            decay = (1.0 - DISCOUNT_PER_YEAR) ** n_seasons
            pts += s.fp_total.get(league_format, 0.0) * decay
            n_seasons += 1
        records.append({
            "player_id": arc.player_id,
            "name": arc.name,
            "position": arc.position,
            "last_season": arc.last_season,
            "similarity": round(float(m.similarity), 4),
            "career_ppr": round(arc.career_total_fp.get(league_format, 0.0), 1),
            "post_age_projected_pts": round(pts, 1),
            "post_age_seasons": n_seasons,
            # snapshot_age = comp's rookie age so format_overlay re-projects
            # year-2+ correctly under any format.
            "snapshot_age": m.profile.rookie_age,
            "rookie_year": m.profile.rookie_season,
            "peak_3yr_fp_per_game": round(arc.peak_3yr_fp_per_game.get(league_format, 0.0), 2),
            "peak_season_fp_per_game": round(arc.peak_season_fp_per_game.get(league_format, 0.0), 2),
            "career_arc_fp_per_game": [
                {"age": s.age, "fp_per_game": round(s.fp_per_game.get(league_format, 0.0), 2)}
                for s in arc.career_arc
            ],
        })
    return records


def run_engine(
    current_season: int = 2025,
    retired_through: int = RETIRED_THROUGH_SEASON,
    scoring: Optional[Dict[str, float]] = None,
    top_k: int = TOP_K_COMPS,
    persist: bool = True,
) -> EngineResult:
    # NOTE: the ``scoring`` parameter is retained for API back-compat but is
    # not used by the v2.0 engine — the fantasy arc corpus is pre-computed
    # across SUPPORTED_FORMATS and the ranking always uses BASE_FORMAT.
    # Per-format ranks are produced downstream by format_overlay.
    careers = load_corpus()
    pace = build_era_pace_table(careers)

    # v1.1 long-arc corpus selection.
    long_arc_corpus: List[PlayerCareer] = []
    for c in careers.values():
        if len(c.seasons) < 2:
            continue
        if not c.is_long_arc(through=retired_through):
            continue
        if c.is_retired(through=retired_through):
            long_arc_corpus.append(c)
        else:
            trimmed = c.with_completed_seasons_only(current_season)
            if len(trimmed.seasons) >= 2:
                long_arc_corpus.append(trimmed)

    # Career-length era multipliers (corpus-derived).
    career_length_era = build_career_length_era_table(
        long_arc_corpus, era_for_season,
    )

    # Build fantasy-point arcs for the entire corpus + actives.
    arcs = _build_arcs(careers.values(), pace)
    # The long-arc-arc list. For long-arc-but-still-active members, we must
    # use a TRIMMED arc (completed seasons only). Build a parallel set keyed
    # by player_id → trimmed arc.
    long_arc_arcs: List[CareerArc] = []
    for c in long_arc_corpus:
        # ``c`` is already trimmed for active veterans in the loop above.
        seasons = _career_to_arc_seasons(c)
        if not seasons:
            continue
        arc = build_career_arc(
            player_id=c.player_id,
            name=c.name,
            position=c.position,
            last_season=c.last_season,
            rookie_season=c.rookie_season,
            retired=c.is_retired(through=retired_through),
            is_long_arc=True,
            seasons=seasons,
            pace=pace,
            formats=SUPPORTED_FORMATS,
        )
        long_arc_arcs.append(arc)

    # Percentile table from long-arc corpus.
    percentile_table = build_career_stage_percentile_table(
        long_arc_arcs, league_format=BASE_FORMAT,
    )

    active_players = [
        c for c in careers.values()
        if _is_active(c, current_season=current_season)
    ]

    # v2.1: build the historical rookie corpus from the FULL arc set
    # (long-arc/retired vets + active vets with completed rookie years).
    # Each entry is a player's actual rookie-year profile vector +
    # reference to their full v2.0 arc (for year-2+ projection).
    #
    # Exclude the current 2025 draft class from the corpus — we don't
    # want current 1-season rookies comping against each other (they
    # have no year-2+ realised career yet).
    raw_stats = _raw_stats_by_pid_season(careers)
    rookie_season_by_pid = {
        pid: c.rookie_season for pid, c in careers.items()
        if c.rookie_season is not None
    }
    rookie_corpus = build_rookie_corpus(
        arcs=arcs.values(),
        raw_stats_by_pid_season=raw_stats,
        rookie_season_by_pid=rookie_season_by_pid,
        league_format=BASE_FORMAT,
        exclude_rookie_seasons={current_season},
    )

    rankings: List[Dict] = []
    comps_map: Dict[str, List[Dict]] = {}
    CURRENT_ERA = 4

    for ap in active_players:
        target_arc = arcs.get(ap.player_id)
        if target_arc is None or not target_arc.career_arc:
            continue
        last_season = ap.seasons[-1]
        age_now = last_season.age

        # ------------------------------------------------------------------
        # v2.1 cohort dispatcher — by completed NFL seasons.
        #   0 seasons: 2026 draft class (drafted, not yet played). EXCLUDED
        #              from main rankings; deferred to v2.2's college chain.
        #   1 season:  2025 draft class (one NFL season under their belt).
        #              Use the 1-NFL-season rookie engine (rookie_nfl_fp_arc).
        #   2+ seasons: 2024 class and earlier. Use the v2.0 cumulative-arc
        #              engine — their data is rich enough to comp against
        #              full-career retired veterans.
        # ------------------------------------------------------------------
        n_completed = _completed_nfl_seasons(ap)
        if n_completed == 0:
            # Should not happen — load_corpus already filters seasons with
            # games < MIN_GAMES_PER_SEASON, so any active player with at
            # least one row in careers has >= 1 completed season. Kept as
            # a safety net for future schema changes.
            continue

        # v2.1 rookie-engine eligibility: must have EXACTLY 1 completed
        # season AND that season must be the player's actual rookie
        # year (i.e. the draft class within the last ~2 NFL years). This
        # avoids routing perennial backups (Sam Howell: 17G in 2023,
        # benched 2024-25) into the rookie engine — their "1 completed
        # season" isn't a recent rookie campaign.
        is_recent_rookie = False
        if n_completed == 1:
            rookie_year = ap.rookie_season
            first_completed = target_arc.career_arc[0].season
            # Treat the player as a v2.1 rookie if EITHER:
            #   * their first completed season is the current or previous
            #     NFL season (most common case — 2025 draft class), OR
            #   * their actual draft year is within the last 2 seasons
            #     AND they only played 1 completed year (handles late
            #     bloomers who debuted in year 2 like Brock Purdy did,
            #     though Purdy played enough that he's not in this bucket).
            if first_completed >= current_season - 1:
                is_recent_rookie = True
            elif rookie_year is not None and rookie_year >= current_season - 1:
                is_recent_rookie = True

        if is_recent_rookie:
            # ---- v2.1 1-NFL-season rookie engine ----
            rookie_season = target_arc.career_arc[0]
            rookie_stats = raw_stats.get(
                (ap.player_id, rookie_season.season), {}
            )
            rproj = project_rookie(
                target_arc=target_arc,
                target_rookie_stats=rookie_stats,
                target_rookie_age=rookie_season.age,
                target_rookie_games=rookie_season.games,
                rookie_corpus=rookie_corpus,
                league_format=BASE_FORMAT,
                k=top_k,
            )
            if not rproj.comps:
                continue
            weighted_points = rproj.projected_year_2_plus_fp
            weighted_seasons = rproj.projected_year_2_plus_seasons

            # No career-length lift for rookies — too small a sample
            # to know if they're mobile/dual-threat (also, the comp pool
            # already implicitly captures style: a mobile rookie's comps
            # are mobile rookies).
            qb_style = STYLE_POCKET
            qb_rypg = 0.0
            lift_fp = 1.0
            lift_years = 1.0
            if ap.position == "QB":
                qb_style = style_for_career(ap)
                qb_rypg = career_rushing_rate(ap)

            top_comp = rproj.comps[0].profile.arc
            comp_records = _rookie_comp_records(rproj.comps, BASE_FORMAT)
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
                "comp_tier": _comp_tier_label(
                    top_comp, top_comp.career_total_fp.get(BASE_FORMAT, 0.0),
                ),
                "n_comps": len(rproj.comps),
                "qb_style": qb_style if ap.position == "QB" else None,
                "qb_career_rypg": round(qb_rypg, 1) if ap.position == "QB" else None,
                "career_length_lift": round(lift_years, 3),
                "career_length_lift_fp": round(lift_fp, 3),
                # v2.0 projection diagnostics — rookie engine has its own
                # comp-weighted vs peak-anchored split (peak-anchored =
                # rookie_rate × expected_horizon × discount).
                "comp_weighted_fp": round(rproj.comp_weighted_fp * rproj.confidence_factor, 1),
                "peak_anchored_fp": round(rproj.peak_anchored_fp * rproj.confidence_factor, 1),
                "projection_path": (
                    "rookie_peak_anchored"
                    if rproj.peak_anchored_fp > rproj.comp_weighted_fp
                    else "rookie_comp_weighted"
                ),
                # v2.1 rookie-engine fields
                "engine": "rookie_nfl_fp_arc",
                "rookie_year": rproj.rookie_year,
                "rookie_games": rproj.rookie_games,
                "rookie_fp_per_game": round(rproj.rookie_fp_per_game, 2),
                "rookie_confidence_factor": round(rproj.confidence_factor, 3),
                # Arc-summary fields for the UI (rookies have only 1 season,
                # so peak == single-season == career_avg).
                "peak_3yr_fp_per_game": round(target_arc.peak_3yr_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "peak_season_fp_per_game": round(target_arc.peak_season_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "career_avg_fp_per_game": round(target_arc.career_avg_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "career_total_fp_to_date": round(target_arc.career_total_fp.get(BASE_FORMAT, 0.0), 1),
                # cohort fields retained for downstream readers
                "style_cohort": None,
                "cohort_pool_size": None,
                "cohort_widened": None,
                "cohort_styles_used": None,
            })
            comps_map[ap.player_id] = comp_records
            continue

        # ---- v2.0 cumulative-arc engine (2+ NFL seasons) ----
        proj = arc_project_player(
            target=target_arc,
            long_arc_corpus=long_arc_arcs,
            target_age=age_now,
            league_format=BASE_FORMAT,
            percentile_table=percentile_table,
            k=top_k,
        )
        if not proj.comps:
            continue

        weighted_points = proj.projected_remaining_fp
        weighted_seasons = proj.projected_remaining_seasons

        # v1.1.0 career-length era lift — v2.0 keeps it as a MILD lift on
        # projected fantasy points for mobile/dual-threat QBs only.
        #
        # Why milder than v1.1: the v2.0 fantasy-arc methodology already
        # surfaces long-career comps (Manning, Brees, Rodgers, Stafford,
        # Cam) for any high-fp dual-threat target via their actual
        # similarity score — we are no longer comp-pool-starved. The
        # 1.50× multiplier from v1.1 was correcting for a v1.x sample-
        # bias bug that this methodology doesn't have. We retain a
        # smaller lift (1.10 dual-threat, 1.05 mobile) to acknowledge
        # that modern medicine + rule changes do extend mobile careers
        # AT THE TAIL, but we don't over-compound it on top of an
        # already-fantasy-points-anchored projection.
        #
        # The display ``projected_years_remaining`` keeps the FULL v1.1
        # lift so the UI accurately reflects "these QBs play longer".
        qb_style = STYLE_POCKET
        qb_rypg = 0.0
        lift_fp = 1.00     # applied to fp projection (mild)
        lift_years = 1.00  # applied to display years_remaining (full v1.1 lift)
        if ap.position == "QB":
            qb_style = style_for_career(ap)
            qb_rypg = career_rushing_rate(ap)
            lift_years = career_length_era.get_lift(qb_style, CURRENT_ERA)
            if qb_style == STYLE_DUAL_THREAT:
                lift_fp = 1.10
            elif qb_style == STYLE_MOBILE:
                lift_fp = 1.05
        weighted_points = apply_lift(weighted_points, lift_fp)
        weighted_seasons = apply_lift(weighted_seasons, lift_years)

        top_comp = proj.comps[0].arc
        # Record comp list — keep the same shape as v1.x for format_overlay
        # / report.py.
        comp_records: List[Dict] = []
        for c in proj.comps:
            pts, n_seasons = arc_project_remaining(
                c.arc, age_floor=c.snapshot_age, league_format=BASE_FORMAT,
            )
            comp_records.append({
                "player_id": c.arc.player_id,
                "name": c.arc.name,
                "position": c.arc.position,
                "last_season": c.arc.last_season,
                "similarity": round(float(c.similarity), 4),
                "career_ppr": round(c.arc.career_total_fp.get(BASE_FORMAT, 0.0), 1),
                "post_age_projected_pts": round(pts, 1),
                "post_age_seasons": n_seasons,
                # v2.0 extras for the player page UI
                "snapshot_age": c.snapshot_age,
                "peak_3yr_fp_per_game": round(c.arc.peak_3yr_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "peak_season_fp_per_game": round(c.arc.peak_season_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "career_arc_fp_per_game": [
                    {"age": s.age, "fp_per_game": round(s.fp_per_game.get(BASE_FORMAT, 0.0), 2)}
                    for s in c.arc.career_arc
                ],
            })

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
            "comp_tier": _comp_tier_label(top_comp, top_comp.career_total_fp.get(BASE_FORMAT, 0.0)),
            "n_comps": len(proj.comps),
            "qb_style": qb_style if ap.position == "QB" else None,
            "qb_career_rypg": round(qb_rypg, 1) if ap.position == "QB" else None,
            "career_length_lift": round(lift_years, 3),
            "career_length_lift_fp": round(lift_fp, 3),
            # v2.0 projection diagnostics
            "comp_weighted_fp": round(proj.comp_weighted_fp, 1),
            "peak_anchored_fp": round(proj.peak_anchored_fp, 1),
            "projection_path": (
                "peak_anchored" if proj.peak_anchored_fp > proj.comp_weighted_fp
                else "comp_weighted"
            ),
            # v2.0 player-arc metrics
            "peak_3yr_fp_per_game": round(target_arc.peak_3yr_fp_per_game.get(BASE_FORMAT, 0.0), 2),
            "peak_season_fp_per_game": round(target_arc.peak_season_fp_per_game.get(BASE_FORMAT, 0.0), 2),
            "career_avg_fp_per_game": round(target_arc.career_avg_fp_per_game.get(BASE_FORMAT, 0.0), 2),
            "career_total_fp_to_date": round(target_arc.career_total_fp.get(BASE_FORMAT, 0.0), 1),
            # v2.1 dispatcher field: which engine produced this row
            "engine": "fantasy_arc_v2",
            # v1.2 cohort fields retained as None for downstream readers
            "style_cohort": None,
            "cohort_pool_size": None,
            "cohort_widened": None,
            "cohort_styles_used": None,
        })
        comps_map[ap.player_id] = comp_records

    rankings.sort(key=lambda r: r["production_score"], reverse=True)
    n = len(rankings)
    for i, row in enumerate(rankings):
        row["overall_rank"] = i + 1
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
        careers=careers,
        long_arc_corpus=long_arc_corpus,
        active_players=active_players,
        career_length_era=career_length_era,
        arcs=arcs,
        long_arc_arcs=long_arc_arcs,
        percentile_table=percentile_table,
        base_format=BASE_FORMAT,
    )

    if persist:
        _persist(result, current_season)

    return result


def _persist(result: EngineResult, current_season: int) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_season": current_season,
        "engine_version": "v2.1",
        "methodology": "three-tier cohort dispatcher (rookie-NFL + cumulative-arc)",
        "n_active": len(result.active_players),
        "n_long_arc": len(result.long_arc_corpus),
        "n_retired": len(result.long_arc_corpus),  # back-compat
        "n_ranked": len(result.rankings),
        "era_pace_source": result.era_pace.source,
        "era_pace": result.era_pace.multipliers,
        "career_length_era_source": (
            result.career_length_era.source if result.career_length_era else None
        ),
        "career_length_era_lift": (
            result.career_length_era.lift if result.career_length_era else None
        ),
        "career_length_era_median_seasons": (
            result.career_length_era.median_seasons if result.career_length_era else None
        ),
        "rankings": result.rankings,
    }
    (OUT_ROOT / "rankings.json").write_text(
        json.dumps(payload, indent=2, default=float), encoding="utf-8"
    )
    (OUT_ROOT / "comps.json").write_text(
        json.dumps(result.comps, indent=2, default=float), encoding="utf-8"
    )
    long_arc = [
        {
            "player_id": c.player_id,
            "name": c.name,
            "position": c.position,
            "last_season": c.last_season,
            "career_ppr": round(c.career_ppr(), 1),
            "n_seasons": len(c.seasons),
        }
        for c in result.long_arc_corpus
    ]
    (OUT_ROOT / "retired_corpus.json").write_text(
        json.dumps(long_arc, indent=2, default=float), encoding="utf-8"
    )
    (OUT_ROOT / "long_arc_corpus.json").write_text(
        json.dumps(long_arc, indent=2, default=float), encoding="utf-8"
    )
    # v2.0 fantasy-arc corpus sidecar.
    persist_corpus(result.long_arc_arcs, out_path=OUT_ROOT_V2 / "fantasy_arc_corpus.json")


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
