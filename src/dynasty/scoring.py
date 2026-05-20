"""Composite scoring — blends source rankings into a single dynasty score.

Approach:
  1. For each source, pull its most-recent ranking per player at a given league_format.
  2. Convert each source's rank to a normalized 0..100 score:
        score_per_source = 100 * (1 - (rank - 1) / max_rank_for_normalization)
     (or use the market_value directly if present, rescaled 0..100).
  3. Weight each source by:
        effective_weight = default_weight * track_record_multiplier
     where the multiplier comes from `source_track_record.spearman_corr`
     (sources without a track record get 1.0).
  4. Composite = weighted average of per-source scores.
  5. Compute a "consensus rank" using only market/aggregator sources
     (representing where the broader fantasy community has the player).
  6. Compute rank_divergence = consensus_rank - model_rank.
     Positive = model is higher on the player than consensus (a "buy" signal).
     Negative = model is lower than consensus (a "sell" signal).
  7. Write CompositeScore rows.

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
    position_modifier,
    years_pro_modifier,
    select_track_record_multiplier,
    corr_to_multiplier,
)


# How many players to consider "in range" for normalization. Above this rank,
# score floors to 0.
DEFAULT_NORMALIZATION_DEPTH = 300

# Source categories that represent "consensus" / "the market".
# Anything outside these is treated as an evaluator opinion.
CONSENSUS_CATEGORIES = {"market", "aggregator"}


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


def _years_pro_for(player: Player | None, score_year: int) -> Optional[int]:
    """How many NFL seasons since the player's draft year (None if unknown)."""
    if player is None or player.draft_year is None:
        return None
    return max(0, int(score_year) - int(player.draft_year))


def compute_composite_scores(
    league_format: str = "sf_ppr",
    depth: int = DEFAULT_NORMALIZATION_DEPTH,
    model_version: str = "0.3.0",
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

        # Aggregate per-player contributions
        contribs: dict[int, list[tuple[str, str, float, float, int | None]]] = defaultdict(list)
        # contribs[player_id] = [(source_slug, category, score, weight, raw_rank), ...]

        # Track raw consensus ranks per player for the divergence calculation
        consensus_ranks: dict[int, list[int]] = defaultdict(list)

        for sid, plr_rankings in per_source.items():
            src = sources.get(sid)
            if src is None:
                continue
            source_tr_by_pos = multipliers_by_pos.get(sid, {})

            for pid, ranking in plr_rankings.items():
                player = players_by_id.get(pid)
                pos = player.position if player else None

                # 1. Backtested track-record multiplier, preferring position-
                #    specific over overall.
                tr_mult = select_track_record_multiplier(source_tr_by_pos, pos)

                # 2. Position modifier (per (slug, pos)).
                pos_mod = position_modifier(src.slug, pos)

                # 3. Years-pro decay (rookie signals decay, market signals
                #    inverse-decay for rookies).
                yrs_pro = _years_pro_for(player, effective_score_year)
                yp_mod = years_pro_modifier(src.slug, yrs_pro)

                weight = src.default_weight * tr_mult * pos_mod * yp_mod

                score = None
                if ranking.market_value is not None and sid in source_max_value:
                    score = _value_to_score(ranking.market_value, source_max_value[sid])
                if score is None:
                    score = _rank_to_score(ranking.overall_rank, depth)
                if score is None:
                    continue

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
            total_w = sum(w for _, _, _, w, _ in items)
            if total_w <= 0:
                continue
            score = sum(s * w for _, _, s, w, _ in items) / total_w

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
