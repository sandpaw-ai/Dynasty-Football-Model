"""DynastyProcess adapter — reads from the open data repository on GitHub.

DynastyProcess (Tan Ho + Joe Sydlowski) maintains an open repo of dynasty values
aggregated from FantasyPros ECR. Files are published as CSVs.

Repo: https://github.com/dynastyprocess/data

NOTE: Verify the CSV path is still live before relying on this in production.
File names in the repo have changed historically; if this 404s, browse the repo
and update CSV_URL below.
"""
from __future__ import annotations
import csv
import io
from typing import Iterator
from .base import BaseSource, RankingRecord


class DynastyProcessValues(BaseSource):
    slug = "dynastyprocess"
    name = "DynastyProcess — FP-based consensus"
    category = "aggregator"
    update_frequency = "weekly"
    tos_compliant = True
    default_weight = 1.0
    homepage = "https://dynastyprocess.com/"
    notes = "FantasyPros ECR repackaged as open CSV."

    CSV_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/values.csv"

    def fetch(self) -> Iterator[RankingRecord]:
        resp = self._client.get(self.CSV_URL)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))

        for row in reader:
            # Column names from the DynastyProcess values.csv file. If the schema
            # has drifted, adjust these. The principle: pull rank + value + IDs.
            sleeper_id = (row.get("sleeper_id") or "").strip() or None
            mfl_id = (row.get("fp_id") or row.get("mfl_id") or "").strip() or None
            name = (row.get("player") or "").strip()
            pos = (row.get("pos") or "").strip() or None
            team = (row.get("team") or "").strip() or None

            def _intish(k):
                v = row.get(k)
                try:
                    return int(v) if v not in (None, "", "NA") else None
                except (ValueError, TypeError):
                    return None

            def _floatish(k):
                v = row.get(k)
                try:
                    return float(v) if v not in (None, "", "NA") else None
                except (ValueError, TypeError):
                    return None

            draft_year = _intish("draft_year")

            # 1QB format
            yield RankingRecord(
                source_slug=self.slug,
                sleeper_id=sleeper_id,
                mfl_id=mfl_id,
                full_name=name,
                position=pos,
                nfl_team=team,
                draft_year=draft_year,
                overall_rank=_intish("ecr_1qb"),
                market_value=_floatish("value_1qb"),
                league_format="1qb_ppr",
                is_dynasty=True,
            )
            # Superflex format
            yield RankingRecord(
                source_slug=self.slug,
                sleeper_id=sleeper_id,
                mfl_id=mfl_id,
                full_name=name,
                position=pos,
                nfl_team=team,
                draft_year=draft_year,
                overall_rank=_intish("ecr_2qb"),
                market_value=_floatish("value_2qb"),
                league_format="sf_ppr",
                is_dynasty=True,
            )
