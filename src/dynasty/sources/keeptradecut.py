"""KeepTradeCut consensus rankings adapter.

KTC publishes its community-driven dynasty rankings at
https://keeptradecut.com/dynasty-rankings as a server-rendered page that
embeds the full ``playersArray`` JSON inline. A single GET (~1.3 MB)
returns the top 500 players with:

  * ``playerName``, ``position``, ``team``, ``age``, ``birthday``
  * ``playerID`` (KTC's own id), ``mflid`` (MFL crosswalk id)
  * ``superflexValues.rank`` / ``oneQBValues.rank`` — community consensus
  * ``superflexValues.value`` / ``oneQBValues.value`` — trade values

This adapter is intentionally **not** part of the model ranking composite.
It feeds a separate consensus-vs-model comparison view so we can surface
where the data disagrees with the dynasty crowd.

Usage:

    from dynasty.sources.keeptradecut import KeepTradeCut, load_latest
    with KeepTradeCut() as ktc:
        for record in ktc.fetch():
            ...
    snap = load_latest()  # returns the most recent cached snapshot

Caching: ``scripts/refresh_ktc_consensus.py`` writes
``data/consensus/ktc_YYYY-MM-DD.json`` (one polite scrape per day) and
maintains ``data/consensus/ktc_latest.json``. Site builders read the
latest file rather than hitting KTC on every report build.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .base import BaseSource, RankingRecord


KTC_URL = "https://keeptradecut.com/dynasty-rankings"
CONSENSUS_DIR = Path("data/consensus")


# Regex used to pull the embedded JSON out of the rendered HTML. KTC ships
# the data as ``var playersArray = [ ... ];`` at the top of an inline
# <script> block. We match either the ``var`` form or the bare assignment
# so the extractor is resilient to small markup changes.
_PLAYERS_ARRAY_RE = re.compile(
    r"(?:var\s+)?playersArray\s*=\s*(\[.*?\]);",
    re.DOTALL,
)


@dataclass
class KTCFormatRanking:
    """Per-format consensus snapshot for a single player."""
    rank: Optional[int] = None
    positional_rank: Optional[int] = None
    value: Optional[int] = None
    tier: Optional[int] = None
    positional_tier: Optional[int] = None
    adp: Optional[float] = None
    startup_adp: Optional[float] = None
    trade_count: Optional[int] = None
    overall_trend: Optional[int] = None


@dataclass
class KTCPlayer:
    """Normalized KTC consensus row.

    The two format payloads are kept as separate fields so downstream
    code can pick the one that matches a league config without re-parsing
    the raw payload.
    """
    ktc_id: int
    name: str
    position: str
    team: Optional[str]
    age: Optional[float]
    birthday: Optional[str]
    rookie: bool
    mfl_id: Optional[str]
    superflex: KTCFormatRanking = field(default_factory=KTCFormatRanking)
    one_qb: KTCFormatRanking = field(default_factory=KTCFormatRanking)


@dataclass
class KTCSnapshot:
    """A single daily scrape of the KTC dynasty rankings page."""
    captured_at: datetime
    source_url: str
    players: List[KTCPlayer]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _format_from_payload(payload: Optional[Dict]) -> KTCFormatRanking:
    if not payload or not isinstance(payload, dict):
        return KTCFormatRanking()
    return KTCFormatRanking(
        rank=_safe_int(payload.get("rank")),
        positional_rank=_safe_int(payload.get("positionalRank")),
        value=_safe_int(payload.get("value")),
        tier=_safe_int(payload.get("overallTier")),
        positional_tier=_safe_int(payload.get("positionalTier")),
        adp=_safe_float(payload.get("adp")),
        startup_adp=_safe_float(payload.get("startupAdp")),
        trade_count=_safe_int(payload.get("tradeCount")),
        overall_trend=_safe_int(payload.get("overallTrend")),
    )


def _safe_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _safe_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_players_array(html: str) -> List[Dict]:
    """Pull and JSON-decode the embedded ``playersArray`` from KTC HTML.

    Raises ``ValueError`` when the array cannot be located. Returns the
    raw list of dicts (one per player) for downstream normalization.
    """
    m = _PLAYERS_ARRAY_RE.search(html)
    if not m:
        raise ValueError("Could not locate playersArray in KTC HTML")
    return json.loads(m.group(1))


def parse_ktc_html(html: str, *, source_url: str = KTC_URL) -> KTCSnapshot:
    """Parse a KTC dynasty-rankings HTML page into a ``KTCSnapshot``.

    Pure function: takes HTML, returns the normalized snapshot. Used by
    both the live scraper and the test fixture loader.
    """
    raw = extract_players_array(html)
    players: List[KTCPlayer] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        pid = _safe_int(row.get("playerID"))
        name = (row.get("playerName") or "").strip()
        if pid is None or not name:
            continue
        position = (row.get("position") or "").strip().upper()
        # KTC includes draft picks (e.g. "2026 1st") as ``positionID`` 6.
        # Keep them in the raw snapshot but the consensus comparison
        # code filters by position downstream.
        team = (row.get("team") or None) or None
        mfl_id = row.get("mflid")
        mfl_id = str(mfl_id) if mfl_id not in (None, "", 0, "0") else None
        players.append(KTCPlayer(
            ktc_id=pid,
            name=name,
            position=position,
            team=team if team else None,
            age=_safe_float(row.get("age")),
            birthday=(row.get("birthday") or None),
            rookie=bool(row.get("rookie")),
            mfl_id=mfl_id,
            superflex=_format_from_payload(row.get("superflexValues")),
            one_qb=_format_from_payload(row.get("oneQBValues")),
        ))
    return KTCSnapshot(
        captured_at=datetime.now(timezone.utc),
        source_url=source_url,
        players=players,
    )


# ---------------------------------------------------------------------------
# Snapshot persistence / load
# ---------------------------------------------------------------------------

def snapshot_to_dict(snap: KTCSnapshot) -> Dict:
    """Serialize a snapshot for on-disk caching."""
    return {
        "schema": "ktc.v1",
        "captured_at": snap.captured_at.isoformat(),
        "source_url": snap.source_url,
        "players": [
            {
                "ktc_id": p.ktc_id,
                "name": p.name,
                "position": p.position,
                "team": p.team,
                "age": p.age,
                "birthday": p.birthday,
                "rookie": p.rookie,
                "mfl_id": p.mfl_id,
                "superflex": vars(p.superflex),
                "one_qb": vars(p.one_qb),
            }
            for p in snap.players
        ],
    }


def snapshot_from_dict(data: Dict) -> KTCSnapshot:
    captured_at = data.get("captured_at")
    if isinstance(captured_at, str):
        try:
            ts = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(timezone.utc)
    else:
        ts = datetime.now(timezone.utc)
    players: List[KTCPlayer] = []
    for raw in data.get("players", []) or []:
        players.append(KTCPlayer(
            ktc_id=int(raw.get("ktc_id")),
            name=raw.get("name", ""),
            position=raw.get("position", ""),
            team=raw.get("team"),
            age=_safe_float(raw.get("age")),
            birthday=raw.get("birthday"),
            rookie=bool(raw.get("rookie")),
            mfl_id=raw.get("mfl_id"),
            superflex=KTCFormatRanking(**(raw.get("superflex") or {})),
            one_qb=KTCFormatRanking(**(raw.get("one_qb") or {})),
        ))
    return KTCSnapshot(
        captured_at=ts,
        source_url=data.get("source_url", KTC_URL),
        players=players,
    )


def latest_snapshot_path(consensus_dir: Path = CONSENSUS_DIR) -> Path:
    return consensus_dir / "ktc_latest.json"


def load_latest(consensus_dir: Path = CONSENSUS_DIR) -> Optional[KTCSnapshot]:
    """Load the most recent cached KTC snapshot (or ``None`` if missing)."""
    path = latest_snapshot_path(consensus_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return snapshot_from_dict(data)


def save_snapshot(
    snap: KTCSnapshot,
    *,
    consensus_dir: Path = CONSENSUS_DIR,
    dated: bool = True,
) -> Path:
    """Write a snapshot to ``consensus_dir`` and update ``ktc_latest.json``.

    Returns the path of the canonical "latest" file.
    """
    consensus_dir.mkdir(parents=True, exist_ok=True)
    payload = snapshot_to_dict(snap)
    if dated:
        day = snap.captured_at.astimezone(timezone.utc).date().isoformat()
        dated_path = consensus_dir / f"ktc_{day}.json"
        dated_path.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
    latest = latest_snapshot_path(consensus_dir)
    latest.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    return latest


# ---------------------------------------------------------------------------
# Adapter (BaseSource interface, mostly for parity with other adapters)
# ---------------------------------------------------------------------------

class KeepTradeCut(BaseSource):
    """Single-GET KTC scraper.

    Yields one ``RankingRecord`` per (player, format) pair so the existing
    ``RankingRecord`` schema can be reused. Not registered in the engine
    composite — the consumer is the consensus-vs-model report builder.
    """
    slug = "keeptradecut"
    name = "KeepTradeCut (community consensus)"
    category = "market"
    update_frequency = "daily"
    tos_compliant = True
    default_weight = 0.0  # NOT used in the model composite
    homepage = "https://keeptradecut.com/dynasty-rankings"
    notes = (
        "Community-driven dynasty rankings. Used only for the "
        "consensus-vs-model comparison view, not as a model input."
    )

    def fetch_snapshot(self) -> KTCSnapshot:
        """Hit KTC once and return a parsed snapshot."""
        resp = self._client.get(KTC_URL)
        resp.raise_for_status()
        return parse_ktc_html(resp.text, source_url=KTC_URL)

    def fetch(self) -> Iterator[RankingRecord]:
        snap = self.fetch_snapshot()
        for p in snap.players:
            # Superflex
            if p.superflex.rank is not None:
                yield RankingRecord(
                    source_slug=self.slug,
                    mfl_id=p.mfl_id,
                    full_name=p.name,
                    position=p.position,
                    overall_rank=p.superflex.rank,
                    position_rank=p.superflex.positional_rank,
                    market_value=float(p.superflex.value)
                        if p.superflex.value is not None else None,
                    tier=p.superflex.tier,
                    league_format="sf_ppr",
                    age=p.age,
                    captured_at=snap.captured_at,
                )
            # 1QB
            if p.one_qb.rank is not None:
                yield RankingRecord(
                    source_slug=self.slug,
                    mfl_id=p.mfl_id,
                    full_name=p.name,
                    position=p.position,
                    overall_rank=p.one_qb.rank,
                    position_rank=p.one_qb.positional_rank,
                    market_value=float(p.one_qb.value)
                        if p.one_qb.value is not None else None,
                    tier=p.one_qb.tier,
                    league_format="1qb_ppr",
                    age=p.age,
                    captured_at=snap.captured_at,
                )
