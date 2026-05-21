"""Composite scoring — blends source rankings into a single dynasty score.

Approach (v0.14.0):
  1. For each source, pull its most-recent ranking per player at a given league_format.
  2. Convert each source's rank to a normalized 0..100 score:
        score_per_source = 100 * (1 - (rank - 1) / max_rank_for_normalization)
     (or use the market_value directly if present, rescaled 0..100).
  3. Weight each source by:
        effective_weight = default_weight * track_record_multiplier
     where the multiplier comes from `source_track_record.spearman_corr`
     (sources without a track record get 1.0).
  4. Composite = weighted average of per-source scores.
  5. Apply a COVERAGE PENALTY:
        composite *= min(num_qualifying_sources / COVERAGE_MIN_SOURCES, 1.0)
     A player with 1 source caps at 1/3 of credit, 2 at 2/3, 3+ full. This
     fixes the v0.13 'Luke Grimm' bug where a single source's max value
     could vault a player to #1 with no corroboration.
  6. Apply a BAYESIAN PRIOR pull toward position-tier baseline. Players
     with low coverage are pulled toward the average expected score for
     their position; well-covered players are not. Pull strength decays
     to zero at 3+ qualifying sources.
  7. Compute a "consensus rank" using only market/aggregator sources
     (representing where the broader fantasy community has the player).
  8. Compute rank_divergence = consensus_rank - model_rank.
     Positive = model is higher on the player than consensus (a "buy" signal).
     Negative = model is lower than consensus (a "sell" signal).
  9. Write CompositeScore rows.

Result is written to `composite_scores` as an append-only history.
"""
from __future__ import annotations
import json
from datetime import datetime
from collections import defaultdict
from typing import Optional
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db.session import get_session
from .db.models import Source, Player, Ranking, CompositeScore, SourceTrackRecord
from .weights import (
    select_track_record_multiplier,
    corr_to_multiplier,
    ROOKIE_SIGNAL_SOURCES,
)


# How many players to consider "in range" for normalization. Above this rank,
# score floors to 0.
DEFAULT_NORMALIZATION_DEPTH = 300

# Source categories that represent "consensus" / "the market".
# Anything outside these is treated as an evaluator opinion.
CONSENSUS_CATEGORIES = {"market", "aggregator"}

# v0.14.0 — coverage penalty + Bayesian prior parameters.
#
# COVERAGE_MIN_SOURCES is the threshold above which we trust a player's
# composite at face value. Below it, two things happen:
#   (a) the composite is multiplied by (num_sources / COVERAGE_MIN_SOURCES),
#       capping uncorroborated players' upside.
#   (b) the composite is pulled toward a position-tier baseline at a rate
#       that decays as coverage grows.
#
# We deliberately do NOT count sources whose default_weight has been
# zeroed out (RAS, brainy_ballers are overlay-only in v0.14). Those
# sources still emit RankingRecords for the overlay system, but they
# don't count as "coverage" for the corroboration gate.
COVERAGE_MIN_SOURCES = 3
# Strength of the prior pull when a player has zero qualifying sources.
# Tapers linearly to 0 at COVERAGE_MIN_SOURCES.
BAYESIAN_PRIOR_STRENGTH = 0.6
# Per-position baseline composite score (rough "average rosterable"
# territory). Tuned so the prior pull doesn't dominate well-covered
# players but does cap single-source noise.
POSITION_BASELINE_SCORE = {
    "QB": 20.0,
    "RB": 22.0,
    "WR": 22.0,
    "TE": 18.0,
}
DEFAULT_BASELINE_SCORE = 20.0


def _latest_rankings_by_source(
    session: Session, league_format: str
) -> dict[int, dict[int, Ranking]]:
    """Returns {source_id: {player_id: latest_ranking}}.

    "Latest" = max captured_at per (source, player, league_format).
    """
    # Find the latest captured_at per (source_id, player_id)
    subq = (
        select(
            Ranking.source_id,
            Ranking.player_id,
            func.max(Ranking.captured_at).label("max_cap"),
        )
        .where(Ranking.league_format == league_format)
        .group_by(Ranking.source_id, Ranking.player_id)
        .subquery()
    )

    rows = session.execute(
        select(Ranking)
        .join(
            subq,
            (Ranking.source_id == subq.c.source_id)
            & (Ranking.player_id == subq.c.player_id)
            & (Ranking.captured_at == subq.c.max_cap),
        )
        .where(Ranking.league_format == league_format)
    ).scalars().all()

    out: dict[int, dict[int, Ranking]] = defaultdict(dict)
    for r in rows:
        out[r.source_id][r.player_id] = r
    return out


def _track_record_multipliers(
    session: Session,
) -> dict[int, dict[Optional[str], float]]:
    """Convert backtest results into per-(source, position) weight multipliers.

    Returns ``{source_id: {position_or_None: multiplier}}``. The position-aware
    selector in ``weights.select_track_record_multiplier`` prefers the
    position-specific entry and falls back to the position-None overall row.

    We use |spearman_corr| (strong negative correlation is just as
    informative as strong positive). When multiple records exist for the
    same (source, position) tuple we take the most-recently-calculated one.

    Multiplier mapping is defined in ``weights.corr_to_multiplier``
    (tuned per research §4; tighter than the v0.2 cutoffs).
    """
    rows = session.execute(
        select(SourceTrackRecord)
        .where(SourceTrackRecord.cohort_year.is_(None))
        .order_by(SourceTrackRecord.calculated_at.desc())
    ).scalars().all()

    out: dict[int, dict[Optional[str], float]] = defaultdict(dict)
    for r in rows:
        pos = (r.position.upper() if r.position else None)
        if pos in out[r.source_id]:
            continue  # already took the most-recent for this (source, position)
        out[r.source_id][pos] = corr_to_multiplier(r.spearman_corr)
    return out


def _rank_to_score(rank: int | None, depth: int) -> float | None:
    if rank is None:
        return None
    if rank <= 0:
        return None
    if rank > depth:
        return 0.0
    return 100.0 * (1.0 - (rank - 1) / depth)


def _value_to_score(value: float | None, max_value: float) -> float | None:
    if value is None or max_value <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * value / max_value))


def compute_composite_scores(
    league_format: str = "sf_ppr",
    depth: int = DEFAULT_NORMALIZATION_DEPTH,
    model_version: str = "0.14.0",
    score_year: int | None = None,
) -> int:
    """Run the scoring pipeline. Returns number of CompositeScore rows written."""
    with get_session() as session:
        per_source = _latest_rankings_by_source(session, league_format)
        if not per_source:
            return 0

        sources = {
            s.id: s for s in session.execute(select(Source)).scalars().all()
        }
        multipliers_by_pos = _track_record_multipliers(session)

        # Identify which sources are "consensus" (market/aggregator) for the
        # consensus-rank calculation.
        consensus_source_ids = {
            sid for sid, s in sources.items() if s.category in CONSENSUS_CATEGORIES
        }

        # Find a per-source max market_value for normalization (top-1 = 100).
        source_max_value: dict[int, float] = {}
        for sid, plr_rankings in per_source.items():
            vals = [r.market_value for r in plr_rankings.values() if r.market_value is not None]
            if vals:
                source_max_value[sid] = max(vals)

        # Pre-load all players we'll need so each weighting lookup is one
        # dict access rather than a per-row SQL roundtrip.
        all_pids = set()
        for plr_rankings in per_source.values():
            all_pids.update(plr_rankings.keys())
        players_by_id: dict[int, Player] = {
            p.id: p
            for p in session.execute(
                select(Player).where(Player.id.in_(all_pids))
            ).scalars().all()
        }

        effective_score_year = score_year or datetime.utcnow().year

        # v0.10 weighting model (deterministic, per-source):
        #   effective_weight = default_weight * track_record_multiplier
        # The track-record multiplier is derived from the backtested
        # Spearman correlation between this source's rankings and realized
        # NFL fantasy production. When a position-specific track-record row
        # exists for (source, position) it wins over the overall row; this
        # is the *only* per-player variation, and it's driven by data, not
        # hand-coded constants.
        #
        # Removed in v0.10 (Phil request 2026-05-20):
        #   * position_modifier() — hand-coded per-(source, position) overrides
        #   * years_pro_modifier() — linear decay for rookie-signal sources +
        #     inverse curve for market sources
        # Those caused the same source to display different weight values for
        # different players in the breakdown JSON, which read as inconsistent.
        # See docs/CHANGELOG-model.md § v0.10.0 for the rationale.
        #
        # Pre-compute effective weights per (source, position). For sources
        # with no position-specific track record, this collapses to a single
        # value per source.
        def _weight_for(sid: int, pos: Optional[str]) -> float:
            src = sources[sid]
            tr_mult = select_track_record_multiplier(
                multipliers_by_pos.get(sid, {}), pos
            )
            return src.default_weight * tr_mult

        # Aggregate per-player contributions
        contribs: dict[int, list[tuple[str, str, float, float, int | None]]] = defaultdict(list)
        # contribs[player_id] = [(source_slug, category, score, weight, raw_rank), ...]

        # Track raw consensus ranks per player for the divergence calculation
        consensus_ranks: dict[int, list[int]] = defaultdict(list)

        for sid, plr_rankings in per_source.items():
            src = sources.get(sid)
            if src is None:
                continue

            for pid, ranking in plr_rankings.items():
                player = players_by_id.get(pid)
                pos = player.position if player else None

                weight = _weight_for(sid, pos)

                score = None
                if ranking.market_value is not None and sid in source_max_value:
                    score = _value_to_score(ranking.market_value, source_max_value[sid])
                if score is None:
                    score = _rank_to_score(ranking.overall_rank, depth)
                if score is None:
                    continue

                # v0.14.0: only contributions with strictly-positive
                # weight count as "coverage". Sources whose
                # default_weight has been zeroed (RAS, brainy_ballers —
                # now overlays) are still recorded for the overlay
                # system to consume, but they don't gate the
                # corroboration penalty.
                contribs[pid].append((src.slug, src.category, score, weight, ranking.overall_rank))

                if sid in consensus_source_ids and ranking.overall_rank is not None:
                    consensus_ranks[pid].append(ranking.overall_rank)

        # Average consensus rank per player (None if no consensus sources had them)
        avg_consensus_rank = {
            pid: int(round(sum(ranks) / len(ranks)))
            for pid, ranks in consensus_ranks.items() if ranks
        }

        # Compute weighted-average composite score per player
        generated_at = datetime.utcnow()
        results = []
        for pid, items in contribs.items():
            # Corroboration filter: skip players whose ONLY rankings come
            # from pre-NFL / rookie-signal sources (nfl_draft_capital, ras,
            # cfbd_breakouts). These are typically retired or no-longer-on-
            # roster players who still have draft-day rankings on file but
            # no current consensus / model / market read. They show up to
            # users as "no consensus" outliers, polluting the top of the
            # rankings.
            slugs_present = {slug for slug, _, _, _, _ in items}
            if slugs_present and slugs_present.issubset(ROOKIE_SIGNAL_SOURCES):
                continue

            # v0.14.0: count sources with positive weight as "qualifying".
            # Sources zeroed out for overlay use (RAS, brainy_ballers)
            # don't count toward coverage.
            qualifying_items = [it for it in items if it[3] > 0]
            num_qualifying = len(qualifying_items)

            total_w = sum(w for _, _, _, w, _ in items)
            if total_w <= 0:
                continue
            raw_score = sum(s * w for _, _, s, w, _ in items) / total_w

            # --- COVERAGE PENALTY (v0.14.0) ---
            # Quadratic penalty below COVERAGE_MIN_SOURCES so single-source
            # entries are crushed even harder than the linear curve would
            # imply. Linearly 1-source = 0.33; quadratic 1-source = 0.11.
            # The Luke Grimm bug came from a single source emitting a max
            # market value with no corroboration; this curve makes that
            # bucket near-zero.
            linear_coverage = min(
                num_qualifying / float(COVERAGE_MIN_SOURCES), 1.0
            )
            coverage_mult = linear_coverage ** 2
            penalized = raw_score * coverage_mult

            # --- BAYESIAN PRIOR PULL (v0.14.0) ---
            player = players_by_id.get(pid)
            pos = player.position if player else None
            baseline = POSITION_BASELINE_SCORE.get(
                (pos or "").upper(), DEFAULT_BASELINE_SCORE
            )
            # Prior weight: full at zero coverage, zero at COVERAGE_MIN_SOURCES+.
            prior_w = BAYESIAN_PRIOR_STRENGTH * max(
                0.0,
                1.0 - num_qualifying / float(COVERAGE_MIN_SOURCES),
            )
            score = (1.0 - prior_w) * penalized + prior_w * baseline

            # Build a richer breakdown: source -> {score, weight, raw_rank, category}
            breakdown = {
                slug: {
                    "score": round(s, 2),
                    "weight": round(w, 3),
                    "raw_rank": rank,
                    "category": cat,
                }
                for slug, cat, s, w, rank in items
            }
            # v0.14.0: expose the coverage gate in the breakdown so the UI
            # can show why a player's composite was attenuated.
            breakdown["_meta"] = {
                "qualifying_sources": num_qualifying,
                "coverage_mult": round(coverage_mult, 3),
                "raw_score": round(raw_score, 2),
                "baseline_score": baseline,
                "prior_weight": round(prior_w, 3),
            }
            results.append((pid, score, breakdown))

        # Sort and assign model ranks
        results.sort(key=lambda x: x[1], reverse=True)

        position_counters: dict[str, int] = defaultdict(int)

        count = 0
        for overall_rank, (pid, score, breakdown) in enumerate(results, start=1):
            pos = players_by_id.get(pid).position if pid in players_by_id else None
            pos_rank = None
            if pos:
                position_counters[pos] += 1
                pos_rank = position_counters[pos]

            consensus_r = avg_consensus_rank.get(pid)
            divergence = (consensus_r - overall_rank) if consensus_r is not None else None

            session.add(CompositeScore(
                player_id=pid,
                league_format=league_format,
                score=score,
                overall_rank=overall_rank,
                position_rank=pos_rank or 0,
                tier=_tier_from_rank(overall_rank),
                consensus_rank=consensus_r,
                rank_divergence=divergence,
                breakdown_json=json.dumps(breakdown),
                model_version=model_version,
                generated_at=generated_at,
            ))
            count += 1
        return count


def _tier_from_rank(rank: int) -> int:
    """Simple tier buckets — refine later."""
    if rank <= 6:    return 1
    if rank <= 12:   return 2
    if rank <= 24:   return 3
    if rank <= 36:   return 4
    if rank <= 60:   return 5
    if rank <= 100:  return 6
    if rank <= 150:  return 7
    if rank <= 200:  return 8
    return 9
