"""v3.0 PR 3 — Prospect (college → NFL) similarity engine.

Resurrects the v0.16 ``rookie_similarity_chain`` cut in v1.0, rebuilt on
top of the v2.x / v3.0 conventions:

  * 26-season college corpus (``data/historical_ncaa_football/season_*.json``)
    spanning 2000-2025, vs. v0.16's 2014-2025 cfbfastR-only window.
  * **SOS-adjusted** fantasy production: per-team Strength-of-Schedule
    z-scored within season → ``adj_fp = fp * (1 + 0.15 * z_sos)`` clipped
    to ``[0.65, 1.10]``. Mirrors PR 2's SOS corpus.
  * **Strong age weight** (20.0) borrowed from the v2.3.5 age-aware
    similarity fix: a 22-year-old senior and a 19-year-old true sophomore
    with otherwise-identical per-game stats are NOT the same prospect.
  * Conference-tier multiplier (P5 1.00, G5_top 0.85, G5 0.75, FCS 0.65)
    applied to per-game production so an FCS yardage line doesn't
    masquerade as an SEC one.
  * Name-collision-aware bridging to NFL ``gsis_id`` via the existing
    ``data/bridge/ncaa_to_nfl.json``, layered with ``(name, school,
    season ±1)`` for ambiguous SR-slug players (``aaron-jones-1`` family).

What this module is NOT (yet):

  * It does NOT wire into ``similarity_v1.py`` or any projection. PR 4
    does that.
  * It does NOT load any combine / athletic-profile data. Phil's
    production-only directive is enforced — RAS / 40-time / vertical
    columns are deliberately ignored.

Public surface:

  * :class:`ProspectVector`         — one summary row per college career.
  * :func:`build_prospect_corpus`   — load 26 seasons, group by player,
                                       emit one ProspectVector per career.
  * :func:`find_similar_prospects`  — weighted-Euclidean KNN, position-
                                       locked, stage- and age-windowed.
  * :class:`NameCollisionResolver`  — bridge SR-slug → ``gsis_id`` via
                                       (name, school, season ±1) layering.
  * :class:`Comp`                    — comp record (target + similar vector
                                       + similarity + bridged NFL career).
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Skill positions only. The corpus is filtered to these; targets at other
# positions are treated as ineligible.
SKILL_POSITIONS: Tuple[str, ...] = ("QB", "RB", "WR", "TE")

# Conference-tier production multiplier. Mirrors the v0.16 calibration; an
# FCS 1000-yard rusher is roughly 65% the signal of a P5 1000-yard rusher.
CONFERENCE_TIER_MULT: Mapping[str, float] = {
    "P5": 1.00,
    "G5_top": 0.85,
    "G5": 0.75,
    "FCS": 0.65,
}
DEFAULT_CONFERENCE_TIER = "FCS"

# Class-year → ordinal (older = higher). Used when ``class_year`` is
# populated; otherwise the per-player career-stage proxy takes over.
CLASS_TO_ORDINAL: Mapping[str, float] = {"FR": 1.0, "SO": 2.0, "JR": 3.0, "SR": 4.0}

# Minimum career production thresholds to be included in the corpus.
# Drops cup-of-coffee bench players who otherwise dominate the long tail.
MIN_CAREER_THRESHOLDS: Mapping[str, Tuple[str, float]] = {
    # position : (column on raw rows to sum, threshold in yards)
    "RB": ("scrimmage_yds", 200.0),
    "WR": ("rec_yds", 200.0),
    "TE": ("rec_yds", 200.0),
    "QB": ("pass_yds", 200.0),
}

# Minimum games to count a season toward the career vector. A 1-game cameo
# is noise.
MIN_SEASON_GAMES = 4

# SOS adjustment knob (per the PR brief):
#     adj_fp = fp * (1.0 + 0.15 * z_sos)
# clipped to [0.65, 1.10]. The asymmetric clip protects against a weird
# year wiping out a player's signal — at worst they're treated as 65% of
# their raw production, and the upside is capped at +10% so a single
# brutal SOS doesn't catapult a mediocre player into an elite tier.
SOS_BETA = 0.15
SOS_ADJ_FLOOR = 0.65
SOS_ADJ_CEIL = 1.10

# Age fallback: when birth_date is missing (cfbfastR is sparse on this
# for 2000-2013, sometimes 2014+), we infer
#     age = last_season - inferred_freshman_year + 18
# where ``inferred_freshman_year`` is the player's first season in the
# corpus. The +18 anchor is the typical age of an incoming freshman.
DEFAULT_FRESHMAN_AGE = 18.0

# Default age when no inference is possible (single-season seniors with
# no class info). Equal to a typical junior/senior.
DEFAULT_PROSPECT_AGE = 21.0

# Stage-window filter for comp search: a career-length-3 prospect can
# comp against careers of length 2..4.
STAGE_WINDOW = 1
# Age window for comp search.
AGE_WINDOW = 2.0

# Default K for KNN.
DEFAULT_TOP_K = 25

# ---------------------------------------------------------------------------
# Feature weights (weighted Euclidean)
# ---------------------------------------------------------------------------
# Vector layout (per-position, but the weight indices are stable across
# positions — features that don't apply at a position are zeroed):
#
#     v[0]  = adj_fp_per_game_avg         (career-mean adj per-game fp)
#     v[1]  = adj_fp_per_game_peak        (best-season adj per-game fp)
#     v[2]  = adj_fp_per_game_final       (final-season adj per-game fp)
#     v[3]  = career_stage_length         (# seasons in corpus)
#     v[4]  = age_at_last_season          (years)
#     v[5]  = conference_tier_mult_avg    (career-mean tier multiplier)
#     v[6]  = position_ord                (informational; position-lock
#                                           filters out cross-pos comps)
#
# All non-age, non-stage features are z-scored within position. Age and
# stage are kept in RAW units so the weights below represent "how many
# z-units does one year of age cost on the distance" — and that's why
# age is so strongly weighted: in z-space, 1 year of age = ~0.7 z, so
# a 20.0 weight is on the same order as a ~3 z swing on per-game fp.
FEATURE_WEIGHTS: Tuple[float, ...] = (
    3.0,   # v[0]  adj_fp_pg_avg     — primary signal
    2.0,   # v[1]  adj_fp_pg_peak    — ceiling indicator
    2.0,   # v[2]  adj_fp_pg_final   — finishing-trajectory indicator
    1.0,   # v[3]  career_stage_length
    20.0,  # v[4]  age_at_last_season — STRONG per v2.3.5 lesson
    1.5,   # v[5]  conference_tier_mult_avg
    0.0,   # v[6]  position_ord (position-locked filter does the work)
)
VECTOR_DIM = len(FEATURE_WEIGHTS)

# Position ord for v[6] (purely informational; the filter applies upstream).
POSITION_ORD: Mapping[str, float] = {"QB": 1.0, "RB": 2.0, "WR": 3.0, "TE": 4.0}

# ---------------------------------------------------------------------------
# Repo / data roots
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEASONS_ROOT = _REPO_ROOT / "data" / "historical_ncaa_football"
DEFAULT_SOS_ROOT = _REPO_ROOT / "data" / "sos"
DEFAULT_BRIDGE_FILE = _REPO_ROOT / "data" / "bridge" / "ncaa_to_nfl.json"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _f(v) -> float:
    """Tolerant float cast: None / '' / 'NA' → 0.0."""
    try:
        if v in (None, "", "NA", "NaN"):
            return 0.0
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Fantasy point formulas (per game)
# ---------------------------------------------------------------------------
# RB / WR / TE: PPR.
# QB: superflex (SF) scoring (4-pt pass TD, -2 int).
#
# We deliberately use a single per-game fp value across positions because
# the corpus is position-bucketed downstream; comparing 22-fp-per-game
# RBs to 22-fp-per-game WRs is fine. Cross-position comps are blocked by
# the position filter in :func:`find_similar_prospects`.

def _row_per_game_fp(row: Mapping) -> float:
    """Compute fantasy points per game for a row, format-aware by position."""
    games = _f(row.get("games"))
    if games <= 0:
        return 0.0
    pos = (row.get("position") or "").upper()
    if pos == "QB":
        pts = (
            0.04 * _f(row.get("pass_yds"))
            + 4.0 * _f(row.get("pass_td"))
            + 0.1 * _f(row.get("rush_yds"))
            + 6.0 * _f(row.get("rush_td"))
            - 2.0 * _f(row.get("int_thrown"))
        )
    else:
        pts = (
            0.1 * _f(row.get("rush_yds"))
            + 6.0 * _f(row.get("rush_td"))
            + 1.0 * _f(row.get("rec"))
            + 0.1 * _f(row.get("rec_yds"))
            + 6.0 * _f(row.get("rec_td"))
        )
    return pts / games


# ---------------------------------------------------------------------------
# SOS lookup
# ---------------------------------------------------------------------------

class SosIndex:
    """In-memory ``(team, year) → (sos, z_sos)`` lookup.

    z-scoring is computed PER SEASON (each year's SOS distribution
    differs — a -3 SOS in 2008 isn't comparable to -3 in 2020). The
    asymmetric clip lives in :func:`_apply_sos_adjustment`.

    School names are normalized minimally: trailing semicolons (an HTML
    entity artifact in the 2010 SOS CSV) are stripped, and whitespace is
    trimmed. We do NOT do fuzzy team-name matching — that's a separate
    problem; if a team is unmatched, the lookup returns ``(None, 0.0)``
    and the row's adj_fp falls back to its raw fp.
    """

    def __init__(self, sos_by_year: Mapping[int, Mapping[str, float]],
                 z_stats: Mapping[int, Tuple[float, float]]):
        self._sos = sos_by_year
        self._z = z_stats

    @staticmethod
    def _normalize_team(team: str) -> str:
        if not team:
            return ""
        return team.strip().rstrip(";").strip()

    def lookup(self, team: str, year: int) -> Tuple[Optional[float], float]:
        """Return ``(raw_sos, z_sos)`` for ``(team, year)``.

        ``raw_sos`` is None when the team is not in the SOS index.
        ``z_sos`` is 0.0 in that case (no adjustment applied).
        """
        team_n = self._normalize_team(team)
        year_map = self._sos.get(year) or {}
        raw = year_map.get(team_n)
        if raw is None:
            return (None, 0.0)
        mu, sd = self._z.get(year, (0.0, 1.0))
        z = (raw - mu) / sd if sd > 0 else 0.0
        return (raw, z)


def _load_sos_index(sos_root: Path) -> SosIndex:
    """Load ``team_sos_YYYY.csv`` files into a SosIndex."""
    sos_by_year: Dict[int, Dict[str, float]] = {}
    z_stats: Dict[int, Tuple[float, float]] = {}
    if not sos_root.exists():
        log.warning("SOS root %s does not exist; SOS adjustment will be a no-op", sos_root)
        return SosIndex({}, {})
    for path in sorted(sos_root.glob("team_sos_*.csv")):
        try:
            year = int(path.stem.split("_")[-1])
        except (ValueError, IndexError):
            continue
        per_team: Dict[str, float] = {}
        sos_values: List[float] = []
        with path.open() as f:
            header = f.readline().strip().split(",")
            try:
                team_idx = header.index("school")
                sos_idx = header.index("sos")
            except ValueError:
                log.warning("SOS file %s missing 'school'/'sos' columns; skipping", path)
                continue
            for line in f:
                fields = line.rstrip("\n").split(",")
                if len(fields) <= max(team_idx, sos_idx):
                    continue
                team = SosIndex._normalize_team(fields[team_idx])
                try:
                    sos_val = float(fields[sos_idx])
                except ValueError:
                    continue
                per_team[team] = sos_val
                sos_values.append(sos_val)
        sos_by_year[year] = per_team
        if sos_values:
            mu = statistics.fmean(sos_values)
            sd = statistics.pstdev(sos_values) if len(sos_values) > 1 else 1.0
            z_stats[year] = (mu, sd if sd > 1e-9 else 1.0)
        else:
            z_stats[year] = (0.0, 1.0)
    return SosIndex(sos_by_year, z_stats)


def _apply_sos_adjustment(raw_fp_pg: float, z_sos: float) -> float:
    """SOS-adjust a per-game fp:
        adj = raw * clip(1 + 0.15 * z_sos, 0.65, 1.10)

    Sign convention check: in the SOS CSVs, MORE-POSITIVE sos means
    HARDER schedule (Sports-Reference's convention). So a positive
    z_sos should INCREASE the adjusted fp (player produced against a
    tougher slate). The formula above achieves that directly.
    """
    mult = _clip(1.0 + SOS_BETA * z_sos, SOS_ADJ_FLOOR, SOS_ADJ_CEIL)
    return raw_fp_pg * mult


# ---------------------------------------------------------------------------
# ProspectVector + corpus build
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProspectVector:
    """Summary of one college career used by the prospect similarity engine.

    Each field below is a SUMMARY across the player's full set of college
    seasons present in the corpus. The features actually fed to the
    distance function live in :attr:`features` (z-scored across the
    cohort by :func:`_zscore_corpus`) and the raw values in
    :attr:`raw_features`.
    """
    cfb_player_id: str
    player_name: str
    position: str               # QB / RB / WR / TE
    school_last: str            # team in the player's final corpus season
    first_season: int
    last_season: int
    career_stage_length: int    # # seasons present in corpus
    age_at_last_season: float
    age_inferred: bool          # True if class_year / birth_date were absent
    conference_tier_last: str
    raw_features: Dict[str, float] = field(default_factory=dict)
    features: Dict[str, float] = field(default_factory=dict)  # z-scored
    notes: List[str] = field(default_factory=list)


def _canonical_player_key(rows: Sequence[Mapping]) -> Dict[str, str]:
    """Canonicalize cfb_player_ids across the 2013/2014 schema seam.

    The corpus uses ``sr_<slug>`` ids for 2000-2013 (Sports-Reference
    slugs) and bare numeric ids for 2014+ (cfbfastR). A player who
    played in both eras (e.g. Hunter Henry: ``sr_hunter-henry-1`` in
    2013 → ``547233`` in 2014-2015) ends up with TWO separate careers
    unless we stitch them. This function builds a ``raw_pid → canonical_pid``
    map that unifies such cases by ``(normalized_name, normalized_team,
    position)`` when a player has rows in both 2013 and 2014.

    Conservative: only stitches across the 2013→2014 seam, never across
    other year boundaries, and only when there's exactly one sr_-id and
    one cfb-id candidate matching the key. Returns a dict mapping every
    pid (including non-stitched ones, mapping to themselves).
    """
    # First, build (year, name, team, pos) → pids.
    rows_by_year: Dict[int, Dict[Tuple[str, str, str], List[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        try:
            yr = int(r.get("season", 0))
        except (ValueError, TypeError):
            continue
        name = _normalize_name(r.get("name") or "")
        team = _normalize_name(r.get("team") or "")
        pos = (r.get("position") or "").upper()
        pid = r.get("cfb_player_id") or ""
        if not pid or not name:
            continue
        rows_by_year[yr][(name, team, pos)].append(pid)

    canon: Dict[str, str] = {}
    # Stitch sr_ → cfb pairs across the 2013/2014 seam.
    keys_2013 = set(rows_by_year.get(2013, {}).keys())
    keys_2014 = set(rows_by_year.get(2014, {}).keys())
    bridgekeys = keys_2013 & keys_2014
    for k in bridgekeys:
        sr_ids = [p for p in rows_by_year[2013][k] if p.startswith("sr_")]
        cfb_ids = [p for p in rows_by_year[2014][k] if not p.startswith("sr_")]
        if len(sr_ids) == 1 and len(cfb_ids) == 1:
            # Canonical id = the cfb (numeric) id since the bridge file
            # is keyed on those for the 2014+ era.
            canon[sr_ids[0]] = cfb_ids[0]
    return canon


def _aggregate_player_rows(
    rows: Sequence[Mapping],
    sos_index: SosIndex,
) -> Dict[Tuple[str, str], List[Mapping]]:
    """Group rows by ``(canonical_cfb_player_id, position)``.

    A handful of edge cases get filtered here:
      * Position outside SKILL_POSITIONS.
      * Missing / blank cfb_player_id.
      * Per-season ``games`` below :data:`MIN_SEASON_GAMES` (cup-of-coffee).

    The grouping key includes position so a college TE who shifted to
    WR in the NFL stays in the right bucket (we only see college rows
    anyway, so this is mostly defensive — a hypothetical QB→WR conv-
    ert in college would land in two buckets, which we'd report).
    """
    canon = _canonical_player_key(rows)
    grouped: Dict[Tuple[str, str], List[Mapping]] = defaultdict(list)
    for row in rows:
        pos = (row.get("position") or "").upper()
        if pos not in SKILL_POSITIONS:
            continue
        pid = row.get("cfb_player_id") or ""
        if not pid:
            continue
        games = _f(row.get("games"))
        if games < MIN_SEASON_GAMES:
            continue
        # Apply canonicalization (sr_ → cfb across the 2013/2014 seam).
        cpid = canon.get(pid, pid)
        grouped[(cpid, pos)].append(row)
    return grouped


def _passes_career_threshold(rows: Sequence[Mapping], position: str) -> bool:
    """True if the player meets the position's career-yards threshold.

    Drops deep-bench noise — see :data:`MIN_CAREER_THRESHOLDS`.
    """
    col, thresh = MIN_CAREER_THRESHOLDS[position]
    total = sum(_f(r.get(col)) for r in rows)
    return total >= thresh


def _infer_age_at_last_season(
    rows: Sequence[Mapping],
    seasons_in_corpus: Sequence[int],
    last_season: int,
) -> Tuple[float, bool]:
    """Return ``(age, inferred)``.

    Strategy:
      1. If the LAST-season row has a usable ``class_year``
         (FR/SO/JR/SR), age = 18 + (ordinal − 1) → 18/19/20/21. We add
         the class-ordinal-1 to anchor freshmen at 18, seniors at 21.
         This is conservative; real ages skew slightly higher but the
         spread is what matters in the comp distance.
      2. Otherwise fall back to:
         ``age = last_season - first_season_in_corpus + 18``
         which treats the player's first corpus appearance as their
         freshman year.
      3. If both fail (no rows, no seasons), return DEFAULT_PROSPECT_AGE.
    """
    if not rows:
        return (DEFAULT_PROSPECT_AGE, True)
    last_row = next((r for r in rows if int(r.get("season", 0)) == last_season), rows[-1])
    cls = (last_row.get("class_year") or "").strip().upper()
    if cls in CLASS_TO_ORDINAL:
        # FR=18, SO=19, JR=20, SR=21 — modest spread, useful when present.
        return (DEFAULT_FRESHMAN_AGE + (CLASS_TO_ORDINAL[cls] - 1.0), False)
    if seasons_in_corpus:
        first = min(seasons_in_corpus)
        age = float(last_season - first) + DEFAULT_FRESHMAN_AGE
        return (age, True)
    return (DEFAULT_PROSPECT_AGE, True)


def _build_raw_vector(
    rows: Sequence[Mapping],
    position: str,
    sos_index: SosIndex,
) -> Tuple[Dict[str, float], List[str]]:
    """Build the raw (non-z-scored) feature dict for one player career."""
    notes: List[str] = []
    per_season_adj_fp: List[float] = []
    per_season_tier_mult: List[float] = []
    final_season_adj_fp = 0.0
    rows_sorted = sorted(rows, key=lambda r: int(r.get("season", 0)))
    for r in rows_sorted:
        season = int(r.get("season", 0))
        team = r.get("team", "") or ""
        tier = r.get("conference_tier") or DEFAULT_CONFERENCE_TIER
        tier_mult = CONFERENCE_TIER_MULT.get(tier, CONFERENCE_TIER_MULT[DEFAULT_CONFERENCE_TIER])
        raw_pg = _row_per_game_fp(r)
        raw_pg_w_tier = raw_pg * tier_mult
        _, z_sos = sos_index.lookup(team, season)
        adj_pg = _apply_sos_adjustment(raw_pg_w_tier, z_sos)
        per_season_adj_fp.append(adj_pg)
        per_season_tier_mult.append(tier_mult)
        final_season_adj_fp = adj_pg
    if not per_season_adj_fp:
        per_season_adj_fp = [0.0]
        per_season_tier_mult = [CONFERENCE_TIER_MULT[DEFAULT_CONFERENCE_TIER]]

    feats: Dict[str, float] = {
        "adj_fp_pg_avg": statistics.fmean(per_season_adj_fp),
        "adj_fp_pg_peak": max(per_season_adj_fp),
        "adj_fp_pg_final": final_season_adj_fp,
        "career_stage_length": float(len(rows_sorted)),
        "conference_tier_mult_avg": statistics.fmean(per_season_tier_mult),
        "position_ord": POSITION_ORD.get(position, 0.0),
    }
    return feats, notes


def _zscore_corpus(corpus: Sequence[ProspectVector]) -> None:
    """In-place z-score the per-position-pooled features.

    Mutates each ProspectVector's :attr:`features` dict. Age and
    career_stage_length are kept in RAW units (already documented above).
    """
    by_pos: Dict[str, List[ProspectVector]] = defaultdict(list)
    for pv in corpus:
        by_pos[pv.position].append(pv)
    z_keys = ("adj_fp_pg_avg", "adj_fp_pg_peak", "adj_fp_pg_final",
              "conference_tier_mult_avg")
    for pos, group in by_pos.items():
        for k in z_keys:
            vals = [pv.raw_features.get(k, 0.0) for pv in group]
            mu = statistics.fmean(vals) if vals else 0.0
            sd = statistics.pstdev(vals) if len(vals) > 1 else 1.0
            sd = sd if sd > 1e-9 else 1.0
            for pv in group:
                pv.features[k] = (pv.raw_features.get(k, 0.0) - mu) / sd
        # Raw-unit pass-through for non-z features
        for pv in group:
            pv.features["career_stage_length"] = pv.raw_features.get("career_stage_length", 0.0)
            pv.features["age_at_last_season"] = float(pv.age_at_last_season)
            pv.features["position_ord"] = pv.raw_features.get("position_ord", 0.0)


def _load_season_rows(seasons_root: Path) -> List[Mapping]:
    """Load all ``season_YYYY.json`` rows from the seasons directory."""
    out: List[Mapping] = []
    if not seasons_root.exists():
        return out
    for path in sorted(seasons_root.glob("season_*.json")):
        try:
            rows = json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("Could not parse %s; skipping", path)
            continue
        if not isinstance(rows, list):
            continue
        out.extend(rows)
    return out


def build_prospect_corpus(
    seasons_root: Optional[Path] = None,
    sos_root: Optional[Path] = None,
    rows: Optional[Sequence[Mapping]] = None,
) -> List[ProspectVector]:
    """Build the full college-prospect corpus.

    Parameters
    ----------
    seasons_root, sos_root :
        Override the default ``data/historical_ncaa_football/`` and
        ``data/sos/`` directories. Mainly used by tests against
        fixtures.
    rows :
        If supplied, the function skips disk I/O and aggregates these
        rows directly. Useful for tests that want to inject a synthetic
        cohort without touching the real corpus.

    Returns
    -------
    list of :class:`ProspectVector`
        One vector per ``(cfb_player_id, position)`` career meeting the
        position's career-yards threshold and with at least one season
        of ``games >= MIN_SEASON_GAMES``.
    """
    seasons_root = seasons_root or DEFAULT_SEASONS_ROOT
    sos_root = sos_root or DEFAULT_SOS_ROOT
    sos_index = _load_sos_index(Path(sos_root))
    raw_rows = list(rows) if rows is not None else _load_season_rows(Path(seasons_root))
    grouped = _aggregate_player_rows(raw_rows, sos_index)

    corpus: List[ProspectVector] = []
    for (pid, pos), player_rows in grouped.items():
        if not _passes_career_threshold(player_rows, pos):
            continue
        player_rows_sorted = sorted(player_rows, key=lambda r: int(r.get("season", 0)))
        seasons = [int(r.get("season", 0)) for r in player_rows_sorted]
        first = min(seasons)
        last = max(seasons)
        age, age_inferred = _infer_age_at_last_season(player_rows_sorted, seasons, last)
        last_row = next(r for r in player_rows_sorted if int(r.get("season", 0)) == last)
        feats, notes = _build_raw_vector(player_rows_sorted, pos, sos_index)
        if age_inferred:
            notes = list(notes) + ["age_inferred_from_corpus_first_season"]
        pv = ProspectVector(
            cfb_player_id=pid,
            player_name=last_row.get("name") or "",
            position=pos,
            school_last=last_row.get("team") or "",
            first_season=first,
            last_season=last,
            career_stage_length=len(player_rows_sorted),
            age_at_last_season=age,
            age_inferred=age_inferred,
            conference_tier_last=last_row.get("conference_tier") or DEFAULT_CONFERENCE_TIER,
            raw_features=dict(feats),
            features={},
            notes=list(notes),
        )
        corpus.append(pv)

    _zscore_corpus(corpus)
    return corpus


# ---------------------------------------------------------------------------
# Comp search
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Comp:
    """A single comp for a target ProspectVector."""
    target_cfb_player_id: str
    comp_cfb_player_id: str
    comp_player_name: str
    comp_school_last: str
    comp_position: str
    comp_last_season: int
    similarity: float
    distance: float
    # Bridged NFL career — populated by the resolver when available
    nfl_gsis_id: Optional[str] = None
    nfl_display_name: Optional[str] = None
    nfl_position: Optional[str] = None
    nfl_last_college_season: Optional[int] = None
    bridge_match_strategy: Optional[str] = None


def _vector_for_distance(pv: ProspectVector) -> Tuple[float, ...]:
    """Project the ProspectVector's feature dict onto the FEATURE_WEIGHTS-
    indexed tuple used by the distance function.
    """
    return (
        pv.features.get("adj_fp_pg_avg", 0.0),
        pv.features.get("adj_fp_pg_peak", 0.0),
        pv.features.get("adj_fp_pg_final", 0.0),
        pv.features.get("career_stage_length", 0.0),
        pv.features.get("age_at_last_season", 0.0),
        pv.features.get("conference_tier_mult_avg", 0.0),
        pv.features.get("position_ord", 0.0),
    )


def _weighted_distance(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
    """Weighted Euclidean distance over FEATURE_WEIGHTS-indexed vectors."""
    s = 0.0
    for i in range(min(len(a), len(b), len(FEATURE_WEIGHTS))):
        d = a[i] - b[i]
        s += FEATURE_WEIGHTS[i] * d * d
    return math.sqrt(s)


def find_similar_prospects(
    target: ProspectVector,
    corpus: Sequence[ProspectVector],
    top_k: int = DEFAULT_TOP_K,
    resolver: Optional["NameCollisionResolver"] = None,
    exclude_self: bool = True,
) -> List[Comp]:
    """Return the top-``k`` comps for ``target`` from ``corpus``.

    Filters applied (in order):
      1. Position-lock: same position as the target.
      2. Career-stage window: |Δ career_stage_length| ≤ STAGE_WINDOW.
      3. Age window: |Δ age_at_last_season| ≤ AGE_WINDOW.
      4. ``exclude_self``: drop the target itself by cfb_player_id.

    Distance is weighted Euclidean (see :data:`FEATURE_WEIGHTS`).
    Similarity = ``1 / (1 + distance)`` — bounded in (0, 1], monotone
    decreasing in distance.
    """
    qvec = _vector_for_distance(target)
    candidates: List[Tuple[float, ProspectVector]] = []
    for pv in corpus:
        if pv.position != target.position:
            continue
        if exclude_self and pv.cfb_player_id == target.cfb_player_id:
            continue
        if abs(pv.career_stage_length - target.career_stage_length) > STAGE_WINDOW:
            continue
        if abs(pv.age_at_last_season - target.age_at_last_season) > AGE_WINDOW:
            continue
        d = _weighted_distance(qvec, _vector_for_distance(pv))
        candidates.append((d, pv))

    candidates.sort(key=lambda x: x[0])
    top = candidates[:top_k]

    out: List[Comp] = []
    for d, pv in top:
        sim = 1.0 / (1.0 + d)
        bridge_info: Mapping = {}
        if resolver is not None:
            bridge_info = resolver.resolve(pv) or {}
        out.append(Comp(
            target_cfb_player_id=target.cfb_player_id,
            comp_cfb_player_id=pv.cfb_player_id,
            comp_player_name=pv.player_name,
            comp_school_last=pv.school_last,
            comp_position=pv.position,
            comp_last_season=pv.last_season,
            similarity=round(sim, 6),
            distance=round(d, 6),
            nfl_gsis_id=bridge_info.get("nfl_pfr_player_id") or None,
            nfl_display_name=bridge_info.get("nfl_display_name") or None,
            nfl_position=bridge_info.get("nfl_position") or None,
            nfl_last_college_season=bridge_info.get("last_college_season"),
            bridge_match_strategy=bridge_info.get("match_strategy") or None,
        ))
    return out


# ---------------------------------------------------------------------------
# Name-collision-aware bridge resolver
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Lowercase, strip suffixes (Jr / III), squeeze whitespace."""
    if not name:
        return ""
    n = name.lower().strip()
    for suf in (" jr.", " jr", " sr.", " sr", " ii", " iii", " iv", " v"):
        if n.endswith(suf):
            n = n[: -len(suf)].rstrip()
    return " ".join(n.split())


@dataclass(frozen=True)
class _BridgeEntry:
    """One row of the bridge file, indexed for collision resolution."""
    cfb_player_id: str
    nfl_pfr_player_id: Optional[str]
    nfl_display_name: Optional[str]
    nfl_position: Optional[str]
    last_college_season: Optional[int]
    college: Optional[str]
    match_strategy: Optional[str]


class NameCollisionResolver:
    """Resolve ``cfb_player_id → bridge row``, layered with name+school+season.

    The bridge file is keyed by ``cfb_player_id``. For 2014+ cfbfastR
    rows the cfb_player_id is a stable numeric id and a direct lookup
    is unambiguous. For 2000-2013 SR-slug rows (``sr_aaron-jones-1``,
    ``sr_aaron-jones-2``, ...), name collisions are common — the
    bridge may have only mapped one slug and we need to layer a
    ``(normalized_name, school, season ±1)`` match to disambiguate.

    Resolution order, per query ProspectVector:
      1. Direct lookup by ``cfb_player_id``  → match_strategy preserved
         if the bridge has a non-null gsis_id.
      2. ``(normalized_name, normalized_school, season ±1)`` index match.
         Returns the bridge row with ``match_strategy='layered'``.
      3. None.

    The resolver is intentionally simple — it does not crawl across
    bridge rows looking for fuzzy matches. The bridge file itself
    already did the heavy lifting; we're just filling in the SR-slug
    seam.
    """

    def __init__(self, bridge_rows: Mapping[str, Mapping]):
        self._by_pid: Dict[str, _BridgeEntry] = {}
        self._by_name_school: Dict[Tuple[str, str], List[_BridgeEntry]] = defaultdict(list)
        for pid, raw in bridge_rows.items():
            entry = _BridgeEntry(
                cfb_player_id=pid,
                nfl_pfr_player_id=raw.get("nfl_pfr_player_id"),
                nfl_display_name=raw.get("nfl_display_name"),
                nfl_position=raw.get("nfl_position"),
                last_college_season=raw.get("last_college_season"),
                college=raw.get("college"),
                match_strategy=raw.get("match_strategy"),
            )
            self._by_pid[pid] = entry
            if entry.nfl_pfr_player_id and entry.nfl_display_name:
                key = (
                    _normalize_name(entry.nfl_display_name),
                    _normalize_name(entry.college or ""),
                )
                self._by_name_school[key].append(entry)

    @classmethod
    def from_file(cls, bridge_path: Optional[Path] = None) -> "NameCollisionResolver":
        bridge_path = bridge_path or DEFAULT_BRIDGE_FILE
        if not Path(bridge_path).exists():
            return cls({})
        data = json.loads(Path(bridge_path).read_text())
        if not isinstance(data, dict):
            return cls({})
        return cls(data)

    def resolve(self, pv: ProspectVector) -> Optional[Dict]:
        """Return a bridge dict for ``pv``, or None if unmatched."""
        direct = self._by_pid.get(pv.cfb_player_id)
        if direct and direct.nfl_pfr_player_id:
            return {
                "nfl_pfr_player_id": direct.nfl_pfr_player_id,
                "nfl_display_name": direct.nfl_display_name,
                "nfl_position": direct.nfl_position,
                "last_college_season": direct.last_college_season,
                "match_strategy": direct.match_strategy or "direct",
            }

        key = (_normalize_name(pv.player_name), _normalize_name(pv.school_last))
        candidates = self._by_name_school.get(key) or []
        # Filter by last_college_season window (±1).
        scored: List[Tuple[int, _BridgeEntry]] = []
        for c in candidates:
            if c.last_college_season is None:
                # Unknown season — accept but with low priority.
                scored.append((10, c))
                continue
            delta = abs(c.last_college_season - pv.last_season)
            if delta <= 1:
                scored.append((delta, c))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0])
        chosen = scored[0][1]
        return {
            "nfl_pfr_player_id": chosen.nfl_pfr_player_id,
            "nfl_display_name": chosen.nfl_display_name,
            "nfl_position": chosen.nfl_position,
            "last_college_season": chosen.last_college_season,
            "match_strategy": "layered",
        }

    def coverage(self, corpus: Sequence[ProspectVector]) -> Dict[str, float]:
        """Return coverage statistics for a corpus.

        Useful for sanity checks in tests & CLI tooling.
        """
        n = len(corpus)
        if n == 0:
            return {"n": 0, "matched": 0, "rate": 0.0}
        matched = sum(1 for pv in corpus if self.resolve(pv) is not None)
        return {"n": float(n), "matched": float(matched), "rate": matched / n}


__all__ = [
    "ProspectVector",
    "Comp",
    "NameCollisionResolver",
    "build_prospect_corpus",
    "find_similar_prospects",
    "CONFERENCE_TIER_MULT",
    "FEATURE_WEIGHTS",
    "VECTOR_DIM",
    "SKILL_POSITIONS",
    "SOS_BETA",
    "SOS_ADJ_FLOOR",
    "SOS_ADJ_CEIL",
    "STAGE_WINDOW",
    "AGE_WINDOW",
    "DEFAULT_TOP_K",
    "DEFAULT_SEASONS_ROOT",
    "DEFAULT_SOS_ROOT",
    "DEFAULT_BRIDGE_FILE",
]
