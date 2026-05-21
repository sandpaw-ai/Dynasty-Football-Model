"""Source adapter registry — v1.0 slimmed.

v0.x had 13+ source adapters feeding a composite ranking. v1.0 runs on a
single engine (``dynasty.engine.similarity_v1``) and only needs a couple of
adapters for player-metadata enrichment:

  - SleeperPlayers — current rosters / teams / ages (for site display)
  - NFLDraftCapital — draft round/pick metadata (rendered on player pages,
    NOT in the ranking)

The other v0.x adapters (FantasyCalc, DynastyProcess, BrainyBallers, PFF,
FantasyPros, FFCAdp, RAS, CFBDBreakouts, NFLImpact, SimilarityCareerArc,
RookieSimilarityChain) are stubbed to no-op so any leftover imports
keep working but the launcher does not run them. See
``docs/CHANGELOG-model.md`` v1.0 entry for the rationale.
"""
from typing import Type
from .base import BaseSource, RankingRecord
from .sleeper import SleeperPlayers
from .nfl_draft_capital import NFLDraftCapital

REGISTRY: dict[str, Type[BaseSource]] = {
    cls.slug: cls
    for cls in [
        SleeperPlayers,
        NFLDraftCapital,
    ]
}

__all__ = [
    "REGISTRY", "BaseSource", "RankingRecord",
    "SleeperPlayers", "NFLDraftCapital",
]
