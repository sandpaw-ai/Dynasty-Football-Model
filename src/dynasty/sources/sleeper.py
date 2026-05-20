"""Sleeper API adapter — used primarily for canonical player ID + metadata resolution.

The Sleeper player endpoint is the spine that links all sources. It returns a dict
keyed by sleeper_id with fields including mfl_id, espn_id, yahoo_id, etc.

Docs: https://docs.sleeper.com/
This endpoint is ~5MB and changes infrequently — run it weekly, not daily.
"""
from __future__ import annotations
from typing import Iterator
from .base import BaseSource, RankingRecord


class SleeperPlayers(BaseSource):
    slug = "sleeper_players"
    name = "Sleeper — player metadata"
    category = "aggregator"
    update_frequency = "weekly"
    tos_compliant = True
    homepage = "https://docs.sleeper.com/"
    notes = "Used to build the canonical player ID map. Does NOT provide rankings."

    URL = "https://api.sleeper.app/v1/players/nfl"

    def fetch(self) -> Iterator[RankingRecord]:
        # Sleeper doesn't expose rankings; nothing to yield as a RankingRecord.
        # The richer use is `fetch_players_dict()` below, called by the sync layer.
        return iter([])

    def fetch_players_dict(self) -> dict:
        """Returns the full Sleeper player dict keyed by sleeper_id."""
        resp = self._client.get(self.URL)
        resp.raise_for_status()
        return resp.json()
