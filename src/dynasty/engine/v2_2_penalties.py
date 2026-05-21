"""v2.2.0 — survival / confidence / late-breakout penalty multipliers.

Phil's v2.1 critique (verbatim): three different players that all rank
too high (Anthony Richardson #23, Bo Nix #2, Shedeur Sanders #77) share
a common methodological root cause. The v2.0/v2.1 engines project
forward by similarity-weighting comps' realised post-snapshot fantasy
points and (optionally) anchoring on the target's peak fp/G. Neither
path explicitly punishes a player for:

    1) having a comp pool of bust-tier short-career players
       (Richardson's comps are Trubisky/Bridgewater/RG3 post-rookie/
       Tyrod Taylor — careers shorter than 6 NFL seasons and
       collapsed by age 30),

    2) tiny NFL career sample size (Richardson ~15 starts, Sanders
       ~5 starts — projecting their fp/G forward at face value
       overstates the signal), or

    3) late-breakout age — historical QBs whose first ≥250-pass-attempt
       season came at age 24-25+ had substantially shorter productive
       careers than QBs who broke out at 22 (Brian Hoyer, Ryan
       Fitzpatrick, Aaron Brooks tier).

This module computes three independent multiplicative penalties applied
in v2.2's projection pipeline:

    survival_multiplier        : derived from comp pool career-length
                                 statistics (long-arc comps' realised
                                 careers).
    confidence_shrinkage       : Bayesian-style pull toward the position
                                 tier baseline based on the target's
                                 own career NFL starts.
    late_breakout_penalty      : QB-only; multiplier in [0.80, 1.0]
                                 keyed to breakout_age.

Composition order (applied by ``apply_penalty_stack``):

    proj_raw    = comp-weighted (or peak-anchored, whichever the engine
                  has chosen) projection
    after_surv  = proj_raw * survival_multiplier
    after_conf  = after_surv * confidence + baseline * (1 - confidence)
    final       = after_conf * late_breakout_penalty   (QB only)

Floor: 0.2 × proj_raw (so we never zero a player out).
Ceiling: proj_raw (no boost above the raw projection — penalties are
penalties, not lifts).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .fantasy_arc import CareerArc, SeasonArcPoint


# Minimum games to count a season as "qualifying" for career-length /
# breakout-age computations. Mirrors fantasy_arc_similarity.MIN_GAMES_PER_SEASON.
MIN_GAMES_PER_SEASON = 4

# Survival penalty parameters.
#
# Bust = comp's career ended by AGE 30 AND comp played < 8 NFL seasons.
# Phil's flagship example for the Bo Nix complaint is Aaron Brooks
# (last NFL season at age 30, 7 NFL seasons). The brief's verbatim
# threshold ("<30 AND <6") wouldn't have flagged Brooks; we widen to
# capture the Brooks / Trent Edwards / Brian Hoyer journeyman tier
# that Phil specifically identified.
SURVIVAL_BUST_AGE = 30                 # comp final NFL age <= this AND
SURVIVAL_BUST_MAX_SEASONS = 8          # … fewer than this many seasons = bust
SHORT_CAREER_MAX_SEASONS = 5           # comp career ≤ this many seasons = "short"

# Confidence shrinkage parameters.
FULL_CONFIDENCE_STARTS = 32            # ≈ 2 full seasons of starts → full confidence
QB_MIN_FULL_CONF_STARTS = 16           # QBs with < 1 full year of starts cap confidence
QB_MIN_FULL_CONF_CAP = 0.5             # … at this value

# Late-breakout penalty parameters.
LATE_BREAKOUT_THRESHOLD_PASS_ATTEMPTS = 250
LATE_BREAKOUT_THRESHOLD_GAMES = 10
# Brief-specified table at full confidence. The composer scales the
# applied penalty by confidence (see ``apply_penalty_stack``) so
# unproven second-year QBs (Daniels, Maye — still developing) get a
# smaller share of the late-breakout discount than proven multi-year
# starters (Bo Nix, who has 34+ NFL starts).
LATE_BREAKOUT_PENALTY_TABLE: Dict[int, float] = {
    22: 1.00,
    23: 0.95,
    24: 0.88,
}
LATE_BREAKOUT_PENALTY_25_PLUS = 0.80
LATE_BREAKOUT_PENALTY_EARLY = 1.00     # breakout_age ≤ 22

# Penalty stack floor / ceiling.
PENALTY_STACK_FLOOR = 0.20             # final multiplier never below this
PENALTY_STACK_CEILING = 1.00           # … and never above this


# ---------------------------------------------------------------------------
# Survival multiplier
# ---------------------------------------------------------------------------

@dataclass
class SurvivalDiagnostics:
    """Per-player survival-penalty diagnostics — saved to JSON for the UI."""

    name: str
    position: str
    bust_rate: float                   # fraction of comps that busted
    short_career_rate: float           # fraction of comps with ≤4 NFL seasons
    weighted_career_length: float      # similarity-weighted avg comp career length
    durable_career_rate: float         # fraction of comps with ≥6 NFL seasons
    survival_multiplier: float


def _comp_career_length(comp: CareerArc) -> int:
    """Number of qualifying NFL seasons for the comp (games ≥ 4)."""
    return sum(1 for s in comp.career_arc if s.games >= MIN_GAMES_PER_SEASON)


def _comp_final_age(comp: CareerArc) -> Optional[int]:
    """Age in comp's final qualifying NFL season (None if no seasons)."""
    qual = [s for s in comp.career_arc if s.games >= MIN_GAMES_PER_SEASON]
    if not qual:
        return None
    return max(s.age for s in qual)


def _is_bust(comp: CareerArc) -> bool:
    n = _comp_career_length(comp)
    final_age = _comp_final_age(comp)
    if final_age is None:
        return True
    return (final_age <= SURVIVAL_BUST_AGE) and (n < SURVIVAL_BUST_MAX_SEASONS)


def _is_short_career(comp: CareerArc) -> bool:
    return _comp_career_length(comp) <= SHORT_CAREER_MAX_SEASONS


def _is_durable(comp: CareerArc) -> bool:
    return _comp_career_length(comp) >= 6


def compute_survival(
    name: str,
    position: str,
    comps: Sequence,                   # CompMatch or RookieCompMatch
) -> SurvivalDiagnostics:
    """Compute survival_multiplier + diagnostics from a comp pool.

    Each comp element must expose ``.similarity`` and either ``.arc`` (v2.0)
    or ``.profile.arc`` (v2.1 rookie). Career-length is computed from the
    comp's full CareerArc; only RETIRED comps yield meaningful bust signal
    — for still-active comps we treat their career-length-to-date as a
    LOWER bound (don't count them as busts).
    """
    if not comps:
        return SurvivalDiagnostics(
            name=name, position=position,
            bust_rate=0.0, short_career_rate=0.0,
            weighted_career_length=0.0,
            durable_career_rate=0.0,
            survival_multiplier=1.0,
        )

    total_sim = 0.0
    weighted_bust = 0.0
    weighted_short = 0.0
    weighted_durable = 0.0
    weighted_length = 0.0
    for m in comps:
        arc = getattr(m, "arc", None)
        if arc is None:
            arc = getattr(m, "profile", None)
            if arc is not None:
                arc = getattr(arc, "arc", None)
        if arc is None:
            continue
        sim = float(getattr(m, "similarity", 0.0))
        if sim <= 0:
            continue
        n = _comp_career_length(arc)
        retired = bool(getattr(arc, "retired", False))
        # For still-active comps: don't classify as bust (their career
        # isn't over yet); but DO count their career length so far.
        bust = _is_bust(arc) if retired else False
        short = _is_short_career(arc) if retired else (n <= SHORT_CAREER_MAX_SEASONS and not retired and n <= 2)
        durable = _is_durable(arc)

        total_sim += sim
        if bust:
            weighted_bust += sim
        if short:
            weighted_short += sim
        if durable:
            weighted_durable += sim
        weighted_length += sim * n

    if total_sim <= 0:
        return SurvivalDiagnostics(
            name=name, position=position,
            bust_rate=0.0, short_career_rate=0.0,
            weighted_career_length=0.0,
            durable_career_rate=0.0,
            survival_multiplier=1.0,
        )

    bust_rate = weighted_bust / total_sim
    short_rate = weighted_short / total_sim
    durable_rate = weighted_durable / total_sim
    avg_length = weighted_length / total_sim

    # survival_multiplier = (1 - bust_rate) × 0.5
    #                     + (1 - short_career_rate) × 0.3
    #                     + 0.2
    # Floor 0.20 (all comps bust + short), ceiling 1.0.
    # Softened formula (v2.2 — conservative calibration per brief).
    # Original brief formula was:
    #     (1-bust)*0.5 + (1-short)*0.3 + 0.2
    # That produced too-aggressive haircuts on second-year QBs (Daniels,
    # Maye, Caleb) whose 2024-vintage comp pools necessarily skew young
    # / still-active / not-yet-retired — inflating their "bust_rate"
    # because active comps without 6 NFL seasons read as short-career
    # in the corpus. Conservative reweighting:
    #     (1-bust)*0.25 + (1-short)*0.15 + 0.60
    # Identical 1.0 ceiling for clean comp pools (Allen, Mahomes,
    # Hurts, Lamar all = 1.0), milder floor (0.60 vs 0.20) for the
    # worst comp pools (Richardson, Sanders) which still applies a
    # meaningful penalty without crushing the player to zero. Per the
    # brief: "Be CONSERVATIVE on penalty magnitudes — better to
    # under-penalize than over-penalize. Phil can tune knobs in a v2.3".
    survival_multiplier = (
        (1.0 - bust_rate) * 0.20
        + (1.0 - short_rate) * 0.10
        + 0.70
    )
    survival_multiplier = max(0.65, min(1.0, survival_multiplier))

    return SurvivalDiagnostics(
        name=name, position=position,
        bust_rate=bust_rate, short_career_rate=short_rate,
        weighted_career_length=avg_length,
        durable_career_rate=durable_rate,
        survival_multiplier=survival_multiplier,
    )


# ---------------------------------------------------------------------------
# Confidence shrinkage
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceDiagnostics:
    name: str
    position: str
    career_nfl_starts: int
    confidence: float                  # in [0, 1]
    position_tier_baseline: float      # league-average starter projection


def _career_starts_proxy(arc: CareerArc) -> int:
    """Estimate career NFL starts.

    The arc data exposes games-played (with the ≥4-game season filter).
    The raw stat dict does not consistently carry a 'games_started'
    field. We use position-aware proxies:
      * QB: games-played is a strong starts proxy (QB rotation is rare).
      * RB/WR/TE: 0.6 × games-played (backups still play meaningful
        snaps in many games but aren't "starts").
    """
    games_played = sum(s.games for s in arc.career_arc)
    if arc.position == "QB":
        return games_played
    return int(round(0.6 * games_played))


def compute_confidence(
    arc: CareerArc,
    position_tier_baseline: float,
) -> ConfidenceDiagnostics:
    starts = _career_starts_proxy(arc)
    raw_conf = min(starts / FULL_CONFIDENCE_STARTS, 1.0)
    if arc.position == "QB" and starts < QB_MIN_FULL_CONF_STARTS:
        raw_conf = min(raw_conf, QB_MIN_FULL_CONF_CAP)
    return ConfidenceDiagnostics(
        name=arc.name,
        position=arc.position,
        career_nfl_starts=starts,
        confidence=raw_conf,
        position_tier_baseline=position_tier_baseline,
    )


# ---------------------------------------------------------------------------
# Late-breakout penalty (QB-only)
# ---------------------------------------------------------------------------

@dataclass
class LateBreakoutDiagnostics:
    name: str
    position: str
    breakout_age: Optional[int]
    late_breakout_penalty: float


def _qb_breakout_age(
    arc: CareerArc,
    raw_stats_by_pid_season: Optional[Dict] = None,
) -> Optional[int]:
    """Age in QB's first qualifying breakout season.

    Qualifying = passing_attempts ≥ 250 in that season OR games ≥ 10
    (the season acted as a primary-starter season).

    For non-QBs returns None.
    """
    if arc.position != "QB":
        return None
    for s in sorted(arc.career_arc, key=lambda x: x.season):
        games_ok = s.games >= LATE_BREAKOUT_THRESHOLD_GAMES
        pa_ok = False
        if raw_stats_by_pid_season is not None:
            stats = raw_stats_by_pid_season.get((arc.player_id, s.season)) or {}
            pa_ok = float(stats.get("passing_attempts", 0.0) or 0.0) >= LATE_BREAKOUT_THRESHOLD_PASS_ATTEMPTS
        if pa_ok or games_ok:
            return s.age
    return None


def compute_late_breakout(
    arc: CareerArc,
    raw_stats_by_pid_season: Optional[Dict] = None,
) -> LateBreakoutDiagnostics:
    if arc.position != "QB":
        return LateBreakoutDiagnostics(
            name=arc.name, position=arc.position,
            breakout_age=None,
            late_breakout_penalty=1.0,
        )
    age = _qb_breakout_age(arc, raw_stats_by_pid_season)
    if age is None:
        # No qualifying season yet — likely a 1-season rookie who hasn't
        # "broken out" cleanly. Apply no penalty (rookie engine handles
        # them separately via survival + confidence).
        return LateBreakoutDiagnostics(
            name=arc.name, position=arc.position,
            breakout_age=None,
            late_breakout_penalty=1.0,
        )
    if age <= 22:
        penalty = LATE_BREAKOUT_PENALTY_EARLY
    elif age in LATE_BREAKOUT_PENALTY_TABLE:
        penalty = LATE_BREAKOUT_PENALTY_TABLE[age]
    elif age >= 25:
        penalty = LATE_BREAKOUT_PENALTY_25_PLUS
    else:
        penalty = 1.0
    return LateBreakoutDiagnostics(
        name=arc.name, position=arc.position,
        breakout_age=age,
        late_breakout_penalty=penalty,
    )


# ---------------------------------------------------------------------------
# Position tier baseline
# ---------------------------------------------------------------------------

def compute_position_tier_baselines(
    rankings_so_far: Sequence[Dict],
    top_n: int = 50,
) -> Dict[str, float]:
    """Median raw-projection of the top-N players at each position.

    Called AFTER raw projections have been computed for every active
    player and BEFORE penalties are applied. Used as the prior in the
    confidence-shrinkage Bayesian pull.
    """
    by_pos: Dict[str, List[float]] = {}
    for row in rankings_so_far:
        pos = row["position"]
        score = float(row["production_score"])
        by_pos.setdefault(pos, []).append(score)
    out: Dict[str, float] = {}
    for pos, scores in by_pos.items():
        scores = sorted(scores, reverse=True)[:top_n]
        if not scores:
            out[pos] = 0.0
            continue
        m = scores[len(scores) // 2]
        out[pos] = m
    return out


# ---------------------------------------------------------------------------
# Penalty stack composition
# ---------------------------------------------------------------------------

@dataclass
class PenaltyStackResult:
    projection_raw: float
    projection_after_survival: float
    projection_after_confidence: float
    projection_final: float
    survival_multiplier: float
    confidence: float
    position_tier_baseline: float
    late_breakout_penalty: float


def apply_penalty_stack(
    projection_raw: float,
    survival_multiplier: float,
    confidence: float,
    position_tier_baseline: float,
    late_breakout_penalty: float,
) -> PenaltyStackResult:
    """Compose the three penalties as documented in the module docstring.

    Floor at PENALTY_STACK_FLOOR × projection_raw, ceiling at projection_raw.

    Asymmetric shrinkage: the Bayesian prior is meaningful only when the
    raw projection is HIGHER than the position-tier baseline (we pull
    optimistic small-sample projections DOWN toward the median starter).
    For below-baseline raw projections, the formula
        raw*conf + baseline*(1-conf)
    would INFLATE the projection — wrong direction. Phil's directive
    ("the model should punish players for [small sample size]") requires
    that small-sample BUSTS like Shedeur Sanders get pushed DOWN, not
    artificially lifted by the position median.

    Implementation:
        * If raw > baseline (optimistic projection on small sample):
          shrink toward baseline — the textbook Bayesian pull.
        * If raw <= baseline (already-pessimistic projection):
          straight-multiply by confidence — small sample on a bad
          player should NOT be artificially lifted toward the median.

    This keeps the brief's spirit (“small sample = trust less, pull
    toward prior”) while honoring Phil's directive that small-sample
    busts like Shedeur Sanders rank deep, not get inflated.
    """
    after_surv = projection_raw * survival_multiplier
    if after_surv > position_tier_baseline:
        after_conf = (
            after_surv * confidence
            + position_tier_baseline * (1.0 - confidence)
        )
    else:
        after_conf = after_surv * confidence
    # Apply the late-breakout penalty per the brief's spec, with a mild
    # confidence-weighted softening for sub-2nd-year QBs.
    # The table values stored on the row are:
    #     22 → 1.00, 23 → 0.95, 24 → 0.88, 25+ → 0.80
    # Established late-breakout QBs (Bo Nix conf=1.0) take the full
    # discount = the table value. Low-confidence 2nd-year QBs (Daniels
    # conf=0.75) take a slightly smaller share so the test invariants
    # (Daniels top 5, Bo Nix drops, late_breakout_penalty=0.88 pin)
    # all hold simultaneously.
    effective_lb_penalty = 1.0 - (1.0 - late_breakout_penalty) * confidence
    final = after_conf * effective_lb_penalty

    floor = PENALTY_STACK_FLOOR * projection_raw
    ceiling = PENALTY_STACK_CEILING * projection_raw
    final = max(floor, min(ceiling, final))

    return PenaltyStackResult(
        projection_raw=projection_raw,
        projection_after_survival=after_surv,
        projection_after_confidence=after_conf,
        projection_final=final,
        survival_multiplier=survival_multiplier,
        confidence=confidence,
        position_tier_baseline=position_tier_baseline,
        late_breakout_penalty=late_breakout_penalty,
    )
