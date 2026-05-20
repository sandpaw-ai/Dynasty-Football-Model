"""Source adapter registry."""
from typing import Type
from .base import BaseSource, RankingRecord
from .fantasycalc import FantasyCalc
from .dynastyprocess import DynastyProcessValues
from .sleeper import SleeperPlayers
from .pff import PFF
from .fantasypros import FantasyPros
from .brainy_ballers import BrainyBallers
from .nfl_draft_capital import NFLDraftCapital

REGISTRY: dict[str, Type[BaseSource]] = {
    cls.slug: cls
    for cls in [
        FantasyCalc,
        DynastyProcessValues,
        SleeperPlayers,
        PFF,
        FantasyPros,
        BrainyBallers,
        NFLDraftCapital,
    ]
}

__all__ = [
    "REGISTRY", "BaseSource", "RankingRecord",
    "FantasyCalc", "DynastyProcessValues", "SleeperPlayers",
    "PFF", "FantasyPros", "BrainyBallers", "NFLDraftCapital",
]
