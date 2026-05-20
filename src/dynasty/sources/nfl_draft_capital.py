"""NFL Draft Capital adapter (nflverse).

The single highest-leverage feature for *rookie* and *2nd-year* dynasty valuation
is NFL Draft capital — where (round, pick) a player was drafted. NFL teams
aggregate information we can't see (medicals, scouting reports, character) and
their picks correlate with fantasy production at r ≈ 0.4–0.6 vs. 3-year fantasy
points (see ``docs/RESEARCH-sources.md`` §2/A1 for citations).

Data source: ``nflverse-data`` GitHub release ``draft_picks/draft_picks.csv``
(MIT/CC0-style license, free, no scraping).

What this adapter does
----------------------
1. Pulls the full draft history CSV.
2. Filters to fantasy-relevant positions: QB / RB / WR / TE.
3. For each player, *enriches the canonical Player row* with:
     - ``gsis_id``, ``pfr_id``, ``college``
     - ``draft_year``, ``draft_round``, ``draft_pick_overall``, ``draft_team``
4. Emits a ``RankingRecord`` whose ``overall_rank = draft_pick_overall``. This
   makes draft capital flow through the normal composite-scoring path — a #1
   overall pick becomes a top-1 ranking from this "source", which then gets
   blended with FantasyCalc, ECR, etc., according to the source's
   ``default_weight`` (1.5 — highest tier for rookies).
5. Limits output to a configurable recent-year window so the scorer isn't fed
   draft data from 1980 about retired players who are still in the Sleeper
   universe via some other channel.

Notes
-----
* Pre-NFL-Draft each spring, "draft capital" is unknown — keep it at last year's
  picks until the draft happens. The composite scorer simply won't have a
  ranking from this source for un-drafted incoming rookies, which is the
  desired behavior (they fall back to other sources / scouting overlays).
* Position is the *NFL* listed position (RB/WR/QB/TE), not the player's college
  position. We pass it through so player resolution can use it as a tie-breaker
  for common-name matches.
"""
from __future__ import annotations
import csv
import io
from datetime import datetime
from typing import Iterator, Optional

from .base import BaseSource, RankingRecord


# Officially curated nflverse release. The asset is stable (versioned releases
# are also available at .../tag/draft_picks).
DRAFT_PICKS_CSV_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "draft_picks/draft_picks.csv"
)

# Fantasy-relevant skill positions only. K and DEF are not modeled here.
_FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "FB"}

# Default: last N draft classes get emitted as rankings. Older classes still
# populate Player draft fields (enrichment), but we don't want a 1995 #1 pick
# polluting the current dynasty rankings.
DEFAULT_EMIT_YEARS_BACK = 6


def _intish(v) -> Optional[int]:
    if v in (None, "", "NA"):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


class NFLDraftCapital(BaseSource):
    slug = "nfl_draft_capital"
    name = "NFL Draft capital (nflverse)"
    category = "model"
    update_frequency = "event"  # updated when the draft happens (April), then static
    tos_compliant = True
    # High weight: best single predictor of rookie fantasy production. The
    # position-/years-pro-aware weighting refactor (PR #5 in the roadmap) will
    # boost this even higher for rookies and decay it for veterans.
    default_weight = 1.5
    homepage = "https://nflreadr.nflverse.com/"
    notes = (
        "Public nflverse CSV of every NFL draft pick since 1980. Free, "
        "open license, no auth needed. Strongest single predictor of "
        "rookie fantasy production."
    )

    # League format label this source emits under. Draft capital is league-
    # format-agnostic, but the scorer indexes by league_format. Pick the most
    # common dynasty format; sync_all could be extended later to fan it out.
    LEAGUE_FORMAT = "sf_ppr"

    def __init__(self, *args, emit_years_back: int = DEFAULT_EMIT_YEARS_BACK, **kwargs):
        super().__init__(*args, **kwargs)
        self.emit_years_back = emit_years_back

    def fetch(self) -> Iterator[RankingRecord]:
        resp = self._client.get(DRAFT_PICKS_CSV_URL)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))

        cutoff_year = datetime.utcnow().year - self.emit_years_back

        for row in reader:
            position = (row.get("position") or "").strip().upper()
            if position not in _FANTASY_POSITIONS:
                continue

            season = _intish(row.get("season"))
            rnd = _intish(row.get("round"))
            pick = _intish(row.get("pick"))
            if season is None or pick is None:
                continue

            name = (row.get("pfr_player_name") or "").strip()
            if not name:
                continue

            gsis_id = (row.get("gsis_id") or "").strip() or None
            pfr_id = (row.get("pfr_player_id") or "").strip() or None
            team = (row.get("team") or "").strip() or None
            college = (row.get("college") or "").strip() or None

            yield RankingRecord(
                source_slug=self.slug,
                gsis_id=gsis_id,
                pfr_id=pfr_id,
                full_name=name,
                position=position if position != "FB" else "RB",
                nfl_team=team,
                draft_year=season,
                draft_round=rnd,
                draft_pick_overall=pick,
                draft_team=team,
                college=college,
                # Score contribution: emit only for recent classes.
                overall_rank=pick if season >= cutoff_year else None,
                league_format=self.LEAGUE_FORMAT,
                is_dynasty=True,
                is_rookie_only=False,
            )
