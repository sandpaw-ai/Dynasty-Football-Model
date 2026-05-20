"""FantasyCalc adapter.

FantasyCalc exposes a free public API at api.fantasycalc.com. Values are derived
from ~1M+ real fantasy trades and update multiple times per day. This is our
closest legal substitute for KeepTradeCut (whose ToS forbids scraping).

Endpoint:
    GET https://api.fantasycalc.com/values/current
    Params:
        isDynasty   true | false
        numQbs      1 | 2          (2 = Superflex)
        numTeams    8 | 10 | 12 | 14 | 16
        ppr         0 | 0.5 | 1
"""
from __future__ import annotations
from typing import Iterator
from .base import BaseSource, RankingRecord


class FantasyCalc(BaseSource):
    slug = "fantasycalc"
    name = "FantasyCalc — crowdsourced dynasty values"
    category = "market"
    update_frequency = "daily"
    tos_compliant = True
    default_weight = 1.0
    homepage = "https://fantasycalc.com/"
    notes = "Open API; values derived from real trade data."

    BASE_URL = "https://api.fantasycalc.com/values/current"

    # League formats to fetch on each sync. Add/remove as needed.
    FORMATS = [
        # (is_dynasty, num_qbs, num_teams, ppr, our_label)
        (True,  2, 12, 1, "sf_ppr"),
        (True,  1, 12, 1, "1qb_ppr"),
        (False, 2, 12, 1, "sf_ppr_redraft"),
    ]

    def fetch(self) -> Iterator[RankingRecord]:
        for is_dynasty, num_qbs, num_teams, ppr, label in self.FORMATS:
            params = {
                "isDynasty": str(is_dynasty).lower(),
                "numQbs": num_qbs,
                "numTeams": num_teams,
                "ppr": ppr,
            }
            resp = self._client.get(self.BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            for row in data:
                p = row.get("player", {}) or {}
                yield RankingRecord(
                    source_slug=self.slug,
                    sleeper_id=p.get("sleeperId"),
                    mfl_id=str(p["mflId"]) if p.get("mflId") is not None else None,
                    full_name=p.get("name", ""),
                    position=p.get("position"),
                    overall_rank=row.get("overallRank"),
                    position_rank=row.get("positionRank"),
                    market_value=row.get("value"),
                    trend_30d=row.get("trend30Day"),
                    league_format=label,
                    is_dynasty=is_dynasty,
                )
