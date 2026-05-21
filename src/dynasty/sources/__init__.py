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
from .ffc_adp import FFCAdp
from .ras import RAS
from .cfbd_breakouts import CFBDBreakouts
from .nfl_impact import NFLImpact
from .similarity_career_arc import SimilarityCareerArc

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
        FFCAdp,
        RAS,
        CFBDBreakouts,
        NFLImpact,
        SimilarityCareerArc,
    ]
}

__all__ = [
    "REGISTRY", "BaseSource", "RankingRecord",
    "FantasyCalc", "DynastyProcessValues", "SleeperPlayers",
    "PFF", "FantasyPros", "BrainyBallers", "NFLDraftCapital", "FFCAdp", "RAS",
    "CFBDBreakouts", "NFLImpact", "SimilarityCareerArc",
]
