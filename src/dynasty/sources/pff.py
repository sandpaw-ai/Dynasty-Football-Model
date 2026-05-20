"""PFF adapter — STUB.

PFF doesn't expose a free public API. To enable this:

  1. Acquire API access from PFF (paid partnership) OR
  2. Use authenticated session cookies from a paid account (legality varies by
     ToS — read PFF's Terms before doing this in production).

This stub shows the adapter pattern. Fill in `fetch()` once you have credentials.
The rest of the pipeline (player resolution, time-series storage, composite
scoring, backtest weighting) is already wired up.
"""
from __future__ import annotations
from typing import Iterator
from .base import BaseSource, RankingRecord
from ..config import settings


class PFF(BaseSource):
    slug = "pff"
    name = "Pro Football Focus — dynasty rankings & prospect model"
    category = "model"
    update_frequency = "weekly"
    tos_compliant = True  # via API only; do NOT scrape without partnership
    default_weight = 1.3  # historically strong prospect modeling
    homepage = "https://www.pff.com/"
    notes = "Requires paid API access; stub only."

    def fetch(self) -> Iterator[RankingRecord]:
        if not settings.pff_api_key:
            # No credentials — yield nothing. CLI shows the source but no rows.
            return iter([])
        # TODO: implement PFF API calls
        # resp = self._client.get(URL, headers={"Authorization": f"Bearer {settings.pff_api_key}"})
        # for row in resp.json():
        #     yield RankingRecord(source_slug=self.slug, ...)
        return iter([])
