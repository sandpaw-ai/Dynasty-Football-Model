"""Manual ranking import — for evaluators without an API or scrapable source.

Use case: Matt Harmon's WR rankings, Matt Waldman's RSP, Dane Brugler's Beast,
Hayden Winks, Reception Perception. You'll typically copy/paste from their
published article or PDF into a CSV like:

    rank,full_name,position,nfl_team,notes
    1,Jeremiyah Love,RB,ARI,top RB in class
    2,Carnell Tate,WR,TEN,first WR off the board
    ...

Then:

    python -c "from dynasty.manual_import import import_csv; \\
               import_csv('matt_harmon', 'WR rankings', './data/harmon_wr.csv', \\
                          category='expert', league_format='dynasty_wr')"

This is also how you'd import historical pre-draft rankings for backtesting.
Save them at the time, never retroactively reconstruct — and always set
`captured_at` to the date the analyst published their list.
"""
from __future__ import annotations
import csv
from datetime import datetime
from sqlalchemy import select

from .db.session import get_session
from .db.models import Source, Player, Ranking
from .sources.base import RankingRecord


def _ensure_source(session, slug: str, name: str, category: str, default_weight: float = 1.0):
    row = session.execute(select(Source).where(Source.slug == slug)).scalar_one_or_none()
    if row is None:
        row = Source(
            slug=slug, name=name, category=category,
            update_frequency="event", tos_compliant=True,
            default_weight=default_weight,
            notes="Imported via manual CSV.",
        )
        session.add(row)
        session.flush()
    return row


def _resolve_player(session, name: str, position: str | None):
    q = select(Player).where(Player.full_name == name)
    if position:
        q = q.where(Player.position == position)
    p = session.execute(q).scalars().first()
    if p:
        return p
    # Auto-create
    p = Player(full_name=name, position=position)
    session.add(p)
    session.flush()
    return p


def import_csv(
    source_slug: str,
    source_name: str,
    csv_path: str,
    category: str = "expert",
    league_format: str = "sf_ppr",
    is_rookie_only: bool = False,
    default_weight: float = 1.0,
    captured_at: datetime | None = None,
) -> int:
    """Import a CSV of rankings. Required columns: rank, full_name.
    Optional: position, nfl_team, market_value, tier, notes.

    `captured_at` should match the date the rankings were published — crucial
    for backtesting (we filter by captured_at < pre-draft cutoff per cohort).
    """
    captured_at = captured_at or datetime.utcnow()
    count = 0
    with get_session() as session:
        source = _ensure_source(session, source_slug, source_name, category, default_weight)
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("full_name") or row.get("player") or "").strip()
                if not name:
                    continue
                rank = int(row["rank"]) if row.get("rank") else None
                pos = (row.get("position") or "").strip() or None
                value = float(row["market_value"]) if row.get("market_value") else None
                tier = int(row["tier"]) if row.get("tier") else None

                player = _resolve_player(session, name, pos)
                session.add(Ranking(
                    source_id=source.id,
                    player_id=player.id,
                    overall_rank=rank,
                    market_value=value,
                    tier=tier,
                    league_format=league_format,
                    is_dynasty=True,
                    is_rookie_only=is_rookie_only,
                    captured_at=captured_at,
                ))
                count += 1
    return count
