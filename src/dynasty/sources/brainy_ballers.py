"""Brainy Ballers adapter.

Scrapes their publicly-published Top-500 dynasty rankings. The rankings are
explicitly SPS-driven (Star-Predictor Score): the BB site states
"These rankings are primarily based on the Star-Predictor Score (SPS) but
also incorporate our expert insights and recent performance trends."

The page allows search-engine indexing (robots: index, follow), which signals
that the content is intended for public consumption. We respect that:
 - Rate-limit to one request every few seconds (kind to their server)
 - Identify ourselves with a clear User-Agent
 - Cache locally — never hammer the site repeatedly

We pull the top-500 from three public format pages and tag them appropriately.
The rookie-only SPS grades are paywalled and not scraped here.
"""
from __future__ import annotations
import time
from typing import Iterator
from bs4 import BeautifulSoup
from .base import BaseSource, RankingRecord


class BrainyBallers(BaseSource):
    slug = "brainy_ballers"
    name = "Brainy Ballers — SPS-based dynasty rankings"
    category = "model"
    update_frequency = "weekly"
    tos_compliant = True
    # v0.14.0: demoted from 1.3 → 0.0 — brainy_ballers is now a USER
    # OVERLAY (see src/dynasty/overlays.py). Data still synced; weight is
    # 0 in the composite so the overlay can apply it data-driven.
    default_weight = 0.0
    homepage = "https://brainyballers.com/"
    notes = (
        "Top-500 dynasty rankings, primarily SPS (Star-Predictor Score) "
        "with expert overlays. Publicly published. Rookie-grade detail is paywalled."
    )

    FORMAT_URLS = [
        ("https://brainyballers.com/dynasty-fantasy-football-rankings-superflex-ppr/", "sf_ppr"),
        ("https://brainyballers.com/dynasty-fantasy-football-rankings-1qb-half-ppr/", "1qb_ppr"),
    ]

    def fetch(self) -> Iterator[RankingRecord]:
        for i, (url, league_format) in enumerate(self.FORMAT_URLS):
            if i > 0:
                time.sleep(3)  # be polite between requests
            resp = self._client.get(url)
            if resp.status_code != 200:
                continue
            yield from self._parse(resp.text, league_format)

    def _parse(self, html: str, league_format: str) -> Iterator[RankingRecord]:
        soup = BeautifulSoup(html, "lxml")

        # Find the ranking table. The BB page uses a standard <table> with the
        # columns: Rank | Name | Team | Position. There are several tables on
        # the page (nav, format chooser, the rankings themselves). The rankings
        # table is the one with 'Rank' as the first column header.
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers or "rank" not in headers[0]:
                continue
            # Confirm this is the right table by checking we have name/team/position
            if not any("name" in h for h in headers):
                continue

            rank_idx = 0
            name_idx = next((i for i, h in enumerate(headers) if "name" in h), 1)
            team_idx = next((i for i, h in enumerate(headers) if "team" in h), 2)
            pos_idx = next((i for i, h in enumerate(headers) if "position" in h or h == "pos"), 3)

            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < 4:
                    continue
                try:
                    rank = int(cells[rank_idx].get_text(strip=True))
                except (ValueError, IndexError):
                    continue
                name = cells[name_idx].get_text(strip=True)
                team = cells[team_idx].get_text(strip=True) or None
                position = cells[pos_idx].get_text(strip=True) or None

                if not name:
                    continue

                yield RankingRecord(
                    source_slug=self.slug,
                    full_name=name,
                    position=position,
                    nfl_team=team,
                    overall_rank=rank,
                    league_format=league_format,
                    is_dynasty=True,
                )

            # Found the right table for this format — stop scanning
            return
