"""Consensus-vs-model comparison.

Joins the KeepTradeCut community consensus (cached snapshot under
``data/consensus/ktc_latest.json``) to the model's ranking output and
produces a per-format diff table:

  * Model rank (by ``production_score`` desc, position-blind)
  * Consensus rank (KTC ``superflexValues.rank`` or ``oneQBValues.rank``)
  * Delta = model_rank - consensus_rank
        Negative delta = the MODEL is more bullish (ranks the player
                         higher than the crowd).
        Positive delta = the MODEL is more bearish (ranks them lower).

The point of this view is to surface where the dynasty community's
prognostication isn't backed by the production data the engine actually
uses. See ``docs/CONSENSUS-VS-MODEL.md``.

Player matching strategy (cheapest hop first):

  1. KTC publishes ``mflid``; dynastyprocess publishes a static
     ``db_playerids.csv`` that maps ``mfl_id``, ``gsis_id``, and ``ktc_id``
     in one row. We prefer the direct ``ktc_id -> gsis_id`` mapping when
     the crosswalk file is available.
  2. Fallback to ``mfl_id -> gsis_id`` (covers KTC rows where ktc_id is
     absent from the crosswalk).
  3. Fallback to normalized ``(name, position)``. Names are lower-cased,
     stripped of punctuation, suffixes (Jr / Sr / II / III) removed.

Players who never resolve to a model gsis_id are dropped from the table
with a count emitted in the metadata (so we can monitor mapping decay).
"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .sources.keeptradecut import (
    CONSENSUS_DIR,
    KTCPlayer,
    KTCSnapshot,
    load_latest as load_latest_ktc,
)


CROSSWALK_PATH = CONSENSUS_DIR / "dp_playerids.csv"


# ---------------------------------------------------------------------------
# Name normalization for the fallback matcher
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^a-z0-9]+")
# Match a generation suffix at the end of the string. Run AFTER lowercasing
# and AFTER stripping trailing punctuation so we catch both "Marvin Harrison
# Jr." and "Marvin Harrison Jr".
_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv|v)\.?$")


def normalize_name(s: str) -> str:
    """Lower-case, strip diacritics, drop punctuation + suffixes.

    Used as the last-resort matcher when neither ``ktc_id`` nor ``mfl_id``
    yields a crosswalk hit. The output is intentionally compact (no
    spaces, no punctuation) so trivial formatting variants collapse to
    the same key.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    # Strip suffix (handles trailing "." via the inline ``\.?``) before
    # the punctuation pass so "Marvin Harrison Jr." → "marvinharrison".
    s = _SUFFIX_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    return s


# ---------------------------------------------------------------------------
# Crosswalk loading
# ---------------------------------------------------------------------------

@dataclass
class Crosswalk:
    """Multi-key player crosswalk from dynastyprocess ``db_playerids.csv``."""
    ktc_to_gsis: Dict[int, str] = field(default_factory=dict)
    mfl_to_gsis: Dict[str, str] = field(default_factory=dict)
    name_to_gsis: Dict[Tuple[str, str], str] = field(default_factory=dict)

    def resolve(
        self,
        *,
        ktc_id: Optional[int],
        mfl_id: Optional[str],
        name: str,
        position: str,
    ) -> Optional[str]:
        if ktc_id is not None:
            gsis = self.ktc_to_gsis.get(ktc_id)
            if gsis:
                return gsis
        if mfl_id:
            gsis = self.mfl_to_gsis.get(str(mfl_id))
            if gsis:
                return gsis
        key = (normalize_name(name), position.upper())
        return self.name_to_gsis.get(key)


def load_crosswalk(path: Path = CROSSWALK_PATH) -> Crosswalk:
    """Load the dynastyprocess crosswalk into a multi-index lookup.

    Returns an empty Crosswalk if the file is missing; the consensus
    comparison then degrades to name+position matching only and emits a
    metadata warning so we notice.
    """
    cw = Crosswalk()
    if not path.exists():
        return cw
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            gsis = (row.get("gsis_id") or "").strip()
            if not gsis or gsis.upper() == "NA":
                continue
            ktc = (row.get("ktc_id") or "").strip()
            if ktc and ktc.upper() != "NA":
                try:
                    cw.ktc_to_gsis[int(ktc)] = gsis
                except ValueError:
                    pass
            mfl = (row.get("mfl_id") or "").strip()
            if mfl and mfl.upper() != "NA":
                cw.mfl_to_gsis[mfl] = gsis
            name = (row.get("name") or row.get("merge_name") or "").strip()
            pos = (row.get("position") or "").strip().upper()
            if name and pos:
                cw.name_to_gsis.setdefault((normalize_name(name), pos), gsis)
    return cw


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


@dataclass
class ConsensusRow:
    """One row in the consensus-vs-model comparison table."""
    gsis_id: str
    name: str
    position: str
    age: Optional[int]
    team: Optional[str]
    model_rank: int
    consensus_rank: int
    delta: int                                  # model_rank - consensus_rank
    production_score: float
    consensus_value: Optional[int]              # KTC trade value
    consensus_tier: Optional[int]
    consensus_positional_rank: Optional[int]
    consensus_trend: Optional[int]
    consensus_adp: Optional[float]
    slug: Optional[str] = None                  # player-page slug from rankings


@dataclass
class ConsensusComparison:
    """Per-format comparison output."""
    league_format: str               # 'sf_ppr' | '1qb_ppr'
    consensus_source: str            # 'keeptradecut'
    consensus_captured_at: datetime
    rows: List[ConsensusRow]
    n_model_only: int                # in model rankings but not in KTC top-500
    n_consensus_only: int            # in KTC top-500 but not in model rankings
    n_unmatched_consensus: int       # KTC rows we couldn't resolve to a gsis_id


def _format_picker(player: KTCPlayer, league_format: str):
    if league_format in ("sf_ppr", "2qb_ppr", "sf_te_premium", "superflex"):
        return player.superflex
    if league_format in ("1qb_ppr", "half_ppr", "std", "1qb"):
        return player.one_qb
    # Unknown -> fall back to superflex (the dynasty default).
    return player.superflex


def compare_to_consensus(
    *,
    model_rankings: Iterable[Dict],
    ktc_snapshot: Optional[KTCSnapshot] = None,
    crosswalk: Optional[Crosswalk] = None,
    league_format: str = "sf_ppr",
) -> ConsensusComparison:
    """Join the model ranking output with the KTC snapshot.

    Args:
        model_rankings: iterable of dicts in the ``engine_rankings.json``
            shape (player_id, name, position, age, production_score, ...).
        ktc_snapshot: parsed KTC snapshot. Defaults to the latest cached.
        crosswalk: dynastyprocess crosswalk. Defaults to the cached file.
        league_format: 'sf_ppr' (uses KTC superflex ranks) or '1qb_ppr'
            (uses KTC oneQB ranks).
    """
    snap = ktc_snapshot or load_latest_ktc()
    if snap is None:
        raise FileNotFoundError(
            "No KTC consensus snapshot found. Run "
            "`scripts/refresh_ktc_consensus.py` first."
        )
    cw = crosswalk if crosswalk is not None else load_crosswalk()

    # Filter model rankings to skill positions (the engine already does
    # this but be defensive) and stamp the position-blind model rank.
    model_skill = [
        r for r in model_rankings
        if (r.get("position") or "").upper() in SKILL_POSITIONS
    ]
    model_skill.sort(key=lambda r: -float(r.get("production_score") or 0))
    model_rank_by_pid: Dict[str, int] = {}
    for i, r in enumerate(model_skill, 1):
        pid = r.get("player_id") or ""
        if pid:
            model_rank_by_pid[pid] = i
    model_by_pid: Dict[str, Dict] = {r["player_id"]: r for r in model_skill}

    # Build consensus rank-by-gsis_id with the chosen format's ranks.
    consensus_by_gsis: Dict[str, Tuple[KTCPlayer, int]] = {}
    n_unmatched = 0
    for p in snap.players:
        if p.position not in SKILL_POSITIONS:
            continue  # skip RDPs (draft picks) and any non-skill rows
        fmt = _format_picker(p, league_format)
        if fmt.rank is None:
            continue
        gsis = cw.resolve(
            ktc_id=p.ktc_id,
            mfl_id=p.mfl_id,
            name=p.name,
            position=p.position,
        )
        if not gsis:
            n_unmatched += 1
            continue
        # Keep the highest-ranked KTC row if duplicates resolve to the
        # same gsis_id (shouldn't happen, but be safe).
        existing = consensus_by_gsis.get(gsis)
        if existing is None or fmt.rank < existing[1]:
            consensus_by_gsis[gsis] = (p, fmt.rank)

    rows: List[ConsensusRow] = []
    n_model_only = 0
    matched_consensus_gsis: set = set()
    for pid, mrow in model_by_pid.items():
        pair = consensus_by_gsis.get(pid)
        if pair is None:
            n_model_only += 1
            continue
        ktc_player, consensus_rank = pair
        matched_consensus_gsis.add(pid)
        fmt = _format_picker(ktc_player, league_format)
        model_rank = model_rank_by_pid[pid]
        rows.append(ConsensusRow(
            gsis_id=pid,
            name=mrow.get("name") or ktc_player.name,
            position=(mrow.get("position") or ktc_player.position).upper(),
            age=mrow.get("age"),
            team=ktc_player.team,
            model_rank=model_rank,
            consensus_rank=consensus_rank,
            delta=model_rank - consensus_rank,
            production_score=float(mrow.get("production_score") or 0),
            consensus_value=fmt.value,
            consensus_tier=fmt.tier,
            consensus_positional_rank=fmt.positional_rank,
            consensus_trend=fmt.overall_trend,
            consensus_adp=fmt.adp,
            slug=mrow.get("slug"),
        ))
    n_consensus_only = len(consensus_by_gsis) - len(matched_consensus_gsis)

    rows.sort(key=lambda r: r.model_rank)
    return ConsensusComparison(
        league_format=league_format,
        consensus_source="keeptradecut",
        consensus_captured_at=snap.captured_at,
        rows=rows,
        n_model_only=n_model_only,
        n_consensus_only=n_consensus_only,
        n_unmatched_consensus=n_unmatched,
    )


def comparison_to_dict(cmp: ConsensusComparison) -> Dict:
    return {
        "schema": "consensus_vs_model.v1",
        "league_format": cmp.league_format,
        "consensus_source": cmp.consensus_source,
        "consensus_captured_at": cmp.consensus_captured_at.isoformat(),
        "n_model_only": cmp.n_model_only,
        "n_consensus_only": cmp.n_consensus_only,
        "n_unmatched_consensus": cmp.n_unmatched_consensus,
        "rows": [vars(r) for r in cmp.rows],
    }
