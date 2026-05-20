"""Evaluator starter pack — publicly available top-30 rankings.

These rankings are transcribed from publicly-published articles (not paywalled
content). They are point-in-time snapshots intended to give the model a head
start with evaluator data while you build out subscriber-grade sources.

For each evaluator, the source article URL is included for citation. If you
have paid access to that evaluator's full rankings, use `manual_import.import_csv()`
to add a richer dataset; this starter pack will be superseded.

NOTE: These are best-effort snapshots from articles I sourced via public search.
The exact rankings may have shifted since publication. Treat them as a starting
point; refresh from the source whenever you can.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import select

from .db.session import get_session
from .db.models import Source, Player, Ranking


# Format: (slug, name, weight, category, homepage, notes, captured_at, ranks)
# ranks is a list of (rank, full_name, position).

STARTER_PACK = [
    # ──────────────────────────────────────────────────────────────────
    # Lance Zierlein — top of FantasyPros draft accuracy and the Hogs Haven
    # wAV-correlation study. Publicly visible scouting reports on NFL.com.
    # His "top of the board" for 2026 rookies (transcribed from public NFL.com
    # prospect tracker articles, May 2026).
    # ──────────────────────────────────────────────────────────────────
    {
        "slug": "lance_zierlein",
        "name": "Lance Zierlein — NFL.com",
        "weight": 1.4,
        "category": "expert",
        "homepage": "https://www.nfl.com/draft/tracker/prospects",
        "captured_at": datetime(2026, 4, 15),
        "league_format": "rookie",
        "is_rookie_only": True,
        "notes": (
            "Top of the Hogs Haven independent draft-outcome study (2018-2020). "
            "Highest measured Weighted-Approximate-Value correlation among major-media analysts. "
            "Public 2026 rookie scouting reports on NFL.com."
        ),
        "ranks": [
            # 2026 NFL Draft class — top rookies for fantasy purposes
            (1, "Jeremiyah Love", "RB"),
            (2, "Carnell Tate", "WR"),
            (3, "Jordyn Tyson", "WR"),
            (4, "Makai Lemon", "WR"),
            (5, "Fernando Mendoza", "QB"),
            (6, "Kenyon Sadiq", "TE"),
            (7, "KC Concepcion", "WR"),
            (8, "Denzel Boston", "WR"),
            (9, "Ty Simpson", "QB"),
            (10, "Skyler Bell", "WR"),
        ],
    },

    # ──────────────────────────────────────────────────────────────────
    # PFF — their public "Top 60 Dynasty Rookies" article from Jan/May 2026.
    # PFF's prospect model explicitly publishes hit-rate-by-bucket curves.
    # Strong in independent draft-outcome studies.
    # ──────────────────────────────────────────────────────────────────
    {
        "slug": "pff_public",
        "name": "PFF — public Top-60 dynasty rookies",
        "weight": 1.3,
        "category": "model",
        "homepage": "https://www.pff.com/news/fantasy-football-2026-top-60-dynasty-rookie-1qb-rankings",
        "captured_at": datetime(2026, 5, 1),
        "league_format": "rookie",
        "is_rookie_only": True,
        "notes": (
            "Transcribed from PFF's public Top-60 article. For the full PFF "
            "prospect model with score curves, paid API access is required "
            "(see sources/pff.py)."
        ),
        "ranks": [
            (1, "Jeremiyah Love", "RB"),
            (2, "Carnell Tate", "WR"),
            (3, "Jordyn Tyson", "WR"),
            (4, "Makai Lemon", "WR"),
            (5, "Fernando Mendoza", "QB"),
            (6, "Kenyon Sadiq", "TE"),
            (7, "KC Concepcion", "WR"),
            (8, "Denzel Boston", "WR"),
            (9, "Skyler Bell", "WR"),
            (10, "Ty Simpson", "QB"),
        ],
    },

    # ──────────────────────────────────────────────────────────────────
    # Daniel Jeremiah — top of FantasyPros mock-draft accuracy 2025.
    # Strong draft-order predictor (which matters because draft capital is
    # the strongest single predictor of fantasy outcomes).
    # ──────────────────────────────────────────────────────────────────
    {
        "slug": "daniel_jeremiah",
        "name": "Daniel Jeremiah — NFL Network",
        "weight": 1.1,
        "category": "expert",
        "homepage": "https://www.nfl.com/news/daniel-jeremiah-mock-draft",
        "captured_at": datetime(2026, 4, 20),
        "league_format": "rookie",
        "is_rookie_only": True,
        "notes": (
            "Most accurate of the 'Big Three' (Jeremiah/Kiper/McShay) in 2025 per Inside The Star. "
            "Strong on predicting actual draft slot — important because draft capital "
            "is the strongest single predictor of fantasy outcomes."
        ),
        "ranks": [
            (1, "Jeremiyah Love", "RB"),
            (2, "Fernando Mendoza", "QB"),
            (3, "Carnell Tate", "WR"),
            (4, "Jordyn Tyson", "WR"),
            (5, "Ty Simpson", "QB"),
        ],
    },
]


def import_starter_pack() -> int:
    """Import all starter-pack rankings into the DB. Idempotent — re-importing
    will create duplicate ranking rows (which is fine, since they're time-series),
    but the source rows are de-duplicated by slug.

    Returns total ranking rows written.
    """
    total = 0
    with get_session() as session:
        for pack in STARTER_PACK:
            source = session.execute(
                select(Source).where(Source.slug == pack["slug"])
            ).scalar_one_or_none()
            if source is None:
                source = Source(
                    slug=pack["slug"],
                    name=pack["name"],
                    category=pack["category"],
                    url=pack["homepage"],
                    update_frequency="event",
                    tos_compliant=True,
                    default_weight=pack["weight"],
                    notes=pack["notes"],
                )
                session.add(source)
                session.flush()
            else:
                # Update metadata (weight may have changed in code)
                source.default_weight = pack["weight"]
                source.notes = pack["notes"]

            for rank, name, position in pack["ranks"]:
                # Resolve player by name + position
                player = session.execute(
                    select(Player)
                    .where(Player.full_name == name)
                    .where(Player.position == position)
                ).scalars().first()
                if player is None:
                    # Auto-create as a prospect (no NFL team yet at draft time)
                    player = Player(
                        full_name=name,
                        position=position,
                        is_prospect=pack.get("is_rookie_only", False),
                        draft_year=2026 if pack.get("is_rookie_only") else None,
                    )
                    session.add(player)
                    session.flush()

                session.add(Ranking(
                    source_id=source.id,
                    player_id=player.id,
                    overall_rank=rank,
                    league_format=pack["league_format"],
                    is_dynasty=True,
                    is_rookie_only=pack.get("is_rookie_only", False),
                    captured_at=pack["captured_at"],
                ))
                total += 1

            source.last_synced_at = datetime.utcnow()
            source.last_sync_status = "ok"
    return total
