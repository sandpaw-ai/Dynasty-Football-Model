"""FantasyFootballCalculator (FFC) ADP adapter.

Second market-data signal alongside FantasyCalc. FFC publishes live mock-draft
ADP from a large free draft platform; their REST API is explicitly open to
third-party use ("Use this ADP data for free in your website or application
with our REST API").

Why add it
----------
FFC's user base skews *casual* / *redraft*, which means it's a poorly-correlated
second market signal to FantasyCalc (which is dynasty-leaning revealed-
preference from real Sleeper trades). Two noise-uncorrelated market sources
average out to a steadier consensus, and divergence between the two is a
useful signal in itself for rookies.

Endpoints
---------
- `GET https://fantasyfootballcalculator.com/api/v1/adp/{format}?teams=12&year=YYYY`
  where format ∈ {standard, ppr, half-ppr, 2qb, dynasty, rookie}.

We fetch:
  - PPR redraft → label `sf_ppr_redraft` for trend-aware sources
  - 2QB / Superflex → label `sf_ppr`
  - Dynasty → label `sf_ppr` (dynasty-flavored)
  - Rookie → emitted as `is_rookie_only=True`

Weighting
---------
default_weight = 0.7 per research §A3 — lower than FantasyCalc because the user
base skews casual, but still meaningful as a second market signal.

Reference: ``docs/RESEARCH-sources.md`` §A3.
"""
from __future__ import annotations
from datetime import datetime
from typing import Iterator, Optional

from .base import BaseSource, RankingRecord


class FFCAdp(BaseSource):
    slug = "ffc_adp"
    name = "FantasyFootballCalculator ADP"
    category = "market"
    update_frequency = "daily"  # continuous, but we'll sync daily
    tos_compliant = True
    default_weight = 0.7
    homepage = "https://fantasyfootballcalculator.com/"
    notes = (
        "Free public REST API. Live ADP from real mock drafts. User base "
        "skews casual/redraft, complementing FantasyCalc's dynasty-leaning "
        "trade-data signal."
    )

    BASE_URL = "https://fantasyfootballcalculator.com/api/v1/adp"

    # (format_slug, our_label, is_dynasty, is_rookie_only)
    FORMATS = [
        ("ppr",     "1qb_ppr",        False, False),
        ("2qb",     "sf_ppr",         False, False),
        ("dynasty", "sf_ppr",         True,  False),
        ("rookie",  "sf_ppr",         True,  True),
    ]

    DEFAULT_TEAMS = 12

    def __init__(self, *args, year: Optional[int] = None, teams: int = DEFAULT_TEAMS, **kwargs):
        super().__init__(*args, **kwargs)
        self.year = year or datetime.utcnow().year
        self.teams = teams

    def fetch(self) -> Iterator[RankingRecord]:
        for fmt_slug, label, is_dynasty, is_rookie_only in self.FORMATS:
            url = f"{self.BASE_URL}/{fmt_slug}"
            params = {"teams": self.teams, "year": self.year}
            try:
                resp = self._client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
            except Exception:
                # FFC sometimes 404s pre-season for niche formats; skip rather
                # than fail the whole sync.
                continue

            players = payload.get("players", []) if isinstance(payload, dict) else []
            for rank, row in enumerate(players, start=1):
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                pos = (row.get("position") or "").strip().upper() or None
                if pos not in ("QB", "RB", "WR", "TE"):
                    # FFC ADP includes K and DEF; ignore them for fantasy modelling.
                    continue
                team = (row.get("team") or "").strip() or None
                adp = row.get("adp")

                # `adp` is the draft-position float (lower = better). Use the
                # response order as `overall_rank`; if the API ever stops
                # sorting by ADP we'd recompute here.
                yield RankingRecord(
                    source_slug=self.slug,
                    full_name=name,
                    position=pos,
                    nfl_team=team,
                    overall_rank=rank,
                    # FFC doesn't publish a player-value scalar, so we synthesize
                    # a 0..10000-ish "market value" from inverse-ADP so the
                    # value-normalization branch in scoring works too.
                    market_value=float(max(0.0, 300.0 - (adp or 300.0))) if adp is not None else None,
                    league_format=label,
                    is_dynasty=is_dynasty,
                    is_rookie_only=is_rookie_only,
                )
