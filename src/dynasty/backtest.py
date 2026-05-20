"""Backtesting — the only honest accuracy score.

Given a source's historical pre-NFL-Draft rookie rankings for cohort years
(say 2020, 2021, 2022, 2023), and actual NFL fantasy production for those
players over the first N years of their careers, compute:

    - Spearman rank correlation between (source rank) and (production rank)
    - Top-12 / Top-24 hit rate
    - MAE on rank position

Results are persisted to `source_track_record` and feed into the scoring weights.

Methodology notes:
  * We only consider rankings captured BEFORE the NFL Draft each year (cutoff:
    April 1 of the cohort year). That's a strong proxy for pre-draft rankings.
    Adjust `_pre_draft_cutoff()` if your cohort import uses different dates.
  * Outcome metric: total PPR points across the first `window_years` seasons
    starting at `cohort_year`. Players with zero recorded production get 0 points
    (they were ranked but didn't pan out — that's signal, not missing data).
  * We restrict to skill positions (QB, RB, WR, TE) by default.
"""
from __future__ import annotations
from datetime import datetime, date
from typing import Iterable
from sqlalchemy import select, and_
from scipy import stats

from .db.session import get_session
from .db.models import Source, Player, Ranking, Production, SourceTrackRecord


SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}


def _pre_draft_cutoff(cohort_year: int) -> datetime:
    # NFL Draft is late April; use April 1 as a conservative pre-draft cutoff.
    return datetime(cohort_year, 4, 1)


def _get_rookie_rankings(
    session, source_id: int, cohort_year: int, position: str | None
) -> dict[int, int]:
    """Returns {player_id: best_overall_rank} for the source's most-recent
    pre-draft snapshot of the cohort.
    """
    cutoff = _pre_draft_cutoff(cohort_year)
    q = (
        select(Ranking, Player)
        .join(Player, Ranking.player_id == Player.id)
        .where(Ranking.source_id == source_id)
        .where(Ranking.captured_at < cutoff)
        .where(Player.draft_year == cohort_year)
    )
    if position:
        q = q.where(Player.position == position)
    rows = session.execute(q).all()

    # Pick the most-recent pre-draft snapshot per player
    latest: dict[int, Ranking] = {}
    for ranking, player in rows:
        cur = latest.get(player.id)
        if cur is None or ranking.captured_at > cur.captured_at:
            latest[player.id] = ranking
    return {pid: r.overall_rank for pid, r in latest.items() if r.overall_rank}


def _get_production_totals(
    session, player_ids: Iterable[int], cohort_year: int, window_years: int
) -> dict[int, float]:
    """Sum PPR points across seasons cohort_year .. cohort_year+window_years-1
    for the given players. Players with no production rows get 0.
    """
    end_season = cohort_year + window_years - 1
    rows = session.execute(
        select(Production.player_id, Production.fantasy_points_ppr)
        .where(Production.player_id.in_(list(player_ids)))
        .where(Production.season.between(cohort_year, end_season))
        .where(Production.week.is_(None))  # season totals only
    ).all()

    totals: dict[int, float] = {pid: 0.0 for pid in player_ids}
    for pid, pts in rows:
        if pts is not None:
            totals[pid] = totals.get(pid, 0.0) + float(pts)
    return totals


def _rank_dict(values: dict[int, float], reverse: bool = True) -> dict[int, int]:
    """Convert a {key: value} dict to {key: rank}. reverse=True means highest=1."""
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=reverse)
    return {k: i + 1 for i, (k, _) in enumerate(ordered)}


def backtest_source(
    source_slug: str,
    cohort_years: list[int],
    window_years: int = 3,
    position: str | None = None,
) -> dict | None:
    """Backtest a source across cohort years.

    Persists a row in source_track_record and returns a dict of metrics.
    Returns None if there is insufficient data.
    """
    with get_session() as session:
        source = session.execute(
            select(Source).where(Source.slug == source_slug)
        ).scalar_one_or_none()
        if source is None:
            raise KeyError(f"Source not found: {source_slug}")

        # Collect (rank, production) pairs across cohorts
        ranks: list[int] = []
        prods: list[float] = []
        all_player_ranks: list[tuple[int, int, float]] = []  # (pid, rank, production)

        for year in cohort_years:
            rankings = _get_rookie_rankings(session, source.id, year, position)
            if not rankings:
                continue
            prod_totals = _get_production_totals(session, rankings.keys(), year, window_years)
            for pid, rank in rankings.items():
                pts = prod_totals.get(pid, 0.0)
                ranks.append(rank)
                prods.append(pts)
                all_player_ranks.append((pid, rank, pts))

        n = len(ranks)
        if n < 5:
            return None

        # Spearman: source rank vs production (negative correlation expected)
        spearman = stats.spearmanr(ranks, prods)
        spearman_corr = float(spearman.correlation) if spearman.correlation is not None else None

        # Pearson on ranks-of-ranks gives us R^2 (more interpretable to some folks)
        r_squared = (spearman_corr ** 2) if spearman_corr is not None else None

        # MAE on rank: compare source rank to "true" rank by actual production
        prod_ranking = _rank_dict(dict(enumerate(prods)), reverse=True)
        # produces {i_in_list: true_rank}
        source_ranking = _rank_dict(dict(enumerate([-r for r in ranks])), reverse=True)
        # invert so smaller source rank = higher (rank 1 is best)
        diffs = [abs(prod_ranking[i] - source_ranking[i]) for i in range(n)]
        mae = sum(diffs) / n if diffs else None

        # Hit rates: top-N by source vs top-N by production
        by_source = sorted(all_player_ranks, key=lambda x: x[1])             # lowest rank = best
        by_prod = sorted(all_player_ranks, key=lambda x: x[2], reverse=True)  # highest pts = best

        def hit_rate(k: int) -> float | None:
            if n < k:
                return None
            src_top = {pid for pid, _, _ in by_source[:k]}
            prod_top = {pid for pid, _, _ in by_prod[:k]}
            return len(src_top & prod_top) / k

        hit12 = hit_rate(12)
        hit24 = hit_rate(24)

        record = SourceTrackRecord(
            source_id=source.id,
            position=position,
            cohort_year=None,  # aggregated
            is_rookie_eval=True,
            outcome_window_years=window_years,
            sample_size=n,
            spearman_corr=spearman_corr,
            r_squared=r_squared,
            mae=mae,
            hit_rate_top12=hit12,
            hit_rate_top24=hit24,
        )
        session.add(record)

        return {
            "source": source_slug,
            "position": position or "ALL",
            "cohort_years": cohort_years,
            "window_years": window_years,
            "sample_size": n,
            "spearman_corr": spearman_corr,
            "r_squared": r_squared,
            "mae": mae,
            "hit_rate_top12": hit12,
            "hit_rate_top24": hit24,
        }
