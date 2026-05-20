"""Manager skill ratings from draft + trade history.

For a given league (Sleeper or MFL), pull every draft pick and every
completed trade, then score each manager on:

  * Draft delta: for each pick, compare player's CURRENT composite score
    to the score one would expect at that overall pick. Positive delta =
    manager outperformed pick-slot expectation.

  * Trade delta: for each completed trade, sum composite scores received
    minus given. Positive delta = net value gained.

  * Combined skill: equal-weight z-score blend of the two, normalized
    within the league.

Caveats:
- Uses CURRENT model values, not contemporaneous. Rewards picks that aged
  well, not what looked smart on draft night.
- Trade volume bias: no-trade managers get z_trade=0. We surface n_trades.
- No FAAB / waiver scoring (noisy).
- Picks for unrated players (deep dynasty / IDP / late-round dart throws)
  are skipped.

See docs/CHANGELOG-model.md v0.11.0.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from collections import defaultdict
from statistics import mean, pstdev
from typing import Optional

import httpx
from sqlalchemy import select

from .db.session import get_session
from .db.models import Player, CompositeScore
from .league import SLEEPER_BASE, MFL_BASE, _latest_composite_by_player
from .config import settings


def expected_score_at_pick(pick: int) -> float:
    """Baseline score expected at overall pick `pick` (1-indexed).

    Anchored so pick 1 -> 99.6, pick 60 -> 76.4, pick 200 -> 20.4,
    pick 250+ -> 0. This is the *expectation* against which we score
    manager draft skill.
    """
    if pick <= 0:
        return 100.0
    if pick > 250:
        return 0.0
    return max(0.0, 100.0 * (1.0 - (pick - 1) / 250.0))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DraftPickRecord:
    pick_no: int
    round_no: int
    franchise_id: str
    player_ext_id: str
    player_name: Optional[str] = None
    position: Optional[str] = None
    draft_year: Optional[int] = None


@dataclass
class TradeRecord:
    transaction_id: str
    timestamp: Optional[int] = None
    # sides[franchise_id] = list of player_ext_ids THIS franchise received.
    sides: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ManagerScore:
    franchise_id: str
    display_name: str
    n_picks: int = 0
    draft_delta_total: float = 0.0
    draft_delta_avg: float = 0.0
    n_trades: int = 0
    trade_delta_total: float = 0.0
    z_draft: float = 0.0
    z_trade: float = 0.0
    skill_rank: int = 0
    skill_score: float = 0.0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sleeper data pulls
# ---------------------------------------------------------------------------


def _fetch_sleeper_drafts(client: httpx.Client, league_id: str) -> list[DraftPickRecord]:
    drafts_resp = client.get(f"{SLEEPER_BASE}/league/{league_id}/drafts")
    drafts_resp.raise_for_status()
    drafts = drafts_resp.json() or []

    picks: list[DraftPickRecord] = []
    for draft in drafts:
        draft_id = draft.get("draft_id") or draft.get("id")
        if not draft_id:
            continue
        season = draft.get("season")
        try:
            draft_year = int(season) if season else None
        except (ValueError, TypeError):
            draft_year = None
        try:
            r = client.get(f"{SLEEPER_BASE}/draft/{draft_id}/picks")
            r.raise_for_status()
            rows = r.json() or []
        except Exception:
            continue
        for row in rows:
            md = row.get("metadata") or {}
            first = md.get("first_name") or ""
            last = md.get("last_name") or ""
            name = (first + " " + last).strip() or None
            picks.append(DraftPickRecord(
                pick_no=int(row.get("pick_no") or 0),
                round_no=int(row.get("round") or 0),
                franchise_id=str(row.get("roster_id") or row.get("picked_by") or "?"),
                player_ext_id=str(row.get("player_id") or ""),
                player_name=name,
                position=md.get("position"),
                draft_year=draft_year,
            ))
    return picks


def _fetch_sleeper_trades(client: httpx.Client, league_id: str) -> list[TradeRecord]:
    """Sleeper transactions are per-week (leg). Walk 0..18."""
    trades: list[TradeRecord] = []
    for week in range(0, 19):
        try:
            r = client.get(f"{SLEEPER_BASE}/league/{league_id}/transactions/{week}")
            r.raise_for_status()
            rows = r.json() or []
        except Exception:
            continue
        for tx in rows:
            if tx.get("type") != "trade":
                continue
            if tx.get("status") != "complete":
                continue
            tx_id = str(tx.get("transaction_id") or tx.get("id") or "")
            ts_raw = tx.get("status_updated") or tx.get("created")
            ts = None
            if ts_raw:
                try:
                    ts_int = int(ts_raw)
                    ts = ts_int // 1000 if ts_int > 1_000_000_000_000 else ts_int
                except (ValueError, TypeError):
                    pass
            adds = tx.get("adds") or {}
            sides: dict[str, list[str]] = defaultdict(list)
            for pid, recipient in adds.items():
                sides[str(recipient)].append(str(pid))
            if not sides:
                continue
            trades.append(TradeRecord(
                transaction_id=tx_id,
                timestamp=ts,
                sides=dict(sides),
            ))
    return trades


# ---------------------------------------------------------------------------
# MFL data pulls
# ---------------------------------------------------------------------------


def _fetch_mfl_drafts(client: httpx.Client, year: int, league_id: str) -> list[DraftPickRecord]:
    url = f"{MFL_BASE}/{year}/export?TYPE=draftResults&L={league_id}&JSON=1"
    try:
        r = client.get(url)
        r.raise_for_status()
        payload = r.json()
    except Exception:
        return []

    root = (payload.get("draftResults", {}) or {}).get("draftUnit")
    if isinstance(root, dict):
        units = [root]
    elif isinstance(root, list):
        units = root
    else:
        units = []

    out: list[DraftPickRecord] = []
    for unit in units:
        unit_year = unit.get("year") or year
        try:
            unit_year_int = int(unit_year)
        except (ValueError, TypeError):
            unit_year_int = year

        picks_entry = unit.get("draftPick") or []
        if isinstance(picks_entry, dict):
            picks_entry = [picks_entry]

        for p in picks_entry:
            try:
                pick_no = int(p.get("pick") or 0) + 1
            except (ValueError, TypeError):
                pick_no = 0
            try:
                round_no = int(p.get("round") or 0) + 1
            except (ValueError, TypeError):
                round_no = 0
            out.append(DraftPickRecord(
                pick_no=pick_no,
                round_no=round_no,
                franchise_id=str(p.get("franchise") or ""),
                player_ext_id=str(p.get("player") or ""),
                draft_year=unit_year_int,
            ))
    return out


def _fetch_mfl_trades(client: httpx.Client, year: int, league_id: str) -> list[TradeRecord]:
    url = (
        f"{MFL_BASE}/{year}/export?TYPE=transactions&L={league_id}"
        f"&JSON=1&TRANS_TYPE=TRADE"
    )
    try:
        r = client.get(url)
        r.raise_for_status()
        payload = r.json()
    except Exception:
        return []

    txs = (payload.get("transactions", {}) or {}).get("transaction")
    if isinstance(txs, dict):
        txs = [txs]
    elif not isinstance(txs, list):
        txs = []

    trades: list[TradeRecord] = []
    for tx in txs:
        if tx.get("type") != "TRADE":
            continue
        tx_id = str(tx.get("transaction_id") or tx.get("timestamp") or "")
        try:
            ts = int(tx.get("timestamp") or 0) or None
        except (ValueError, TypeError):
            ts = None

        f1 = tx.get("franchise") or tx.get("franchise1")
        f2 = tx.get("franchise2")
        side1_blob = (tx.get("franchise1_gave_up") or "").strip().rstrip(",")
        side2_blob = (tx.get("franchise2_gave_up") or "").strip().rstrip(",")

        def _player_ids(blob: str) -> list[str]:
            ids = []
            for t in blob.split(","):
                t = t.strip()
                if not t:
                    continue
                if t.startswith("DP_") or t.startswith("FP_") or t.startswith("BB_"):
                    continue
                ids.append(t)
            return ids

        sides: dict[str, list[str]] = {}
        if f1:
            sides[str(f1)] = _player_ids(side2_blob)
        if f2:
            sides[str(f2)] = _player_ids(side1_blob)
        if not sides:
            continue
        trades.append(TradeRecord(
            transaction_id=tx_id,
            timestamp=ts,
            sides=sides,
        ))
    return trades


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _resolve_scores_for_ext_ids(ext_ids: list[str], id_kind: str) -> dict[str, dict]:
    """Return {ext_id: {name, position, score, rank, tier}} for the latest
    composite snapshot."""
    if not ext_ids:
        return {}
    col = Player.sleeper_id if id_kind == "sleeper_id" else Player.mfl_id
    with get_session() as session:
        players = session.execute(
            select(Player).where(col.in_(list(set(ext_ids))))
        ).scalars().all()
        if not players:
            return {}
        composite_by_pid = _latest_composite_by_player(session, league_format="sf_ppr")
        out: dict[str, dict] = {}
        for p in players:
            ext = getattr(p, id_kind)
            if not ext:
                continue
            cs = composite_by_pid.get(p.id)
            if cs is None:
                continue
            out[str(ext)] = {
                "name": p.full_name,
                "position": p.position,
                "score": float(cs.score),
                "rank": cs.overall_rank,
                "tier": cs.tier,
            }
    return out


def _compute_manager_table(
    franchise_names: dict[str, str],
    picks: list[DraftPickRecord],
    trades: list[TradeRecord],
    score_lookup: dict[str, dict],
) -> list[ManagerScore]:
    by_id: dict[str, ManagerScore] = {}

    def _ensure(fid: str) -> ManagerScore:
        if fid not in by_id:
            by_id[fid] = ManagerScore(
                franchise_id=fid,
                display_name=franchise_names.get(fid, f"Franchise {fid}"),
            )
        return by_id[fid]

    # Pre-populate from known franchise list so every team appears even if
    # they have no rated picks/trades.
    for fid, name in franchise_names.items():
        _ensure(fid)

    for p in picks:
        info = score_lookup.get(p.player_ext_id)
        if not info:
            continue
        if not p.franchise_id or p.franchise_id == "?":
            continue
        manager = _ensure(p.franchise_id)
        expected = expected_score_at_pick(p.pick_no)
        delta = info["score"] - expected
        manager.n_picks += 1
        manager.draft_delta_total += delta

    for m in by_id.values():
        m.draft_delta_avg = (m.draft_delta_total / m.n_picks) if m.n_picks else 0.0

    for tx in trades:
        side_values: dict[str, float] = {}
        for fid, received in tx.sides.items():
            side_values[fid] = sum(
                score_lookup[pid]["score"] for pid in received if pid in score_lookup
            )
        for fid in tx.sides:
            received = side_values.get(fid, 0.0)
            given = sum(v for other, v in side_values.items() if other != fid)
            manager = _ensure(fid)
            manager.n_trades += 1
            manager.trade_delta_total += (received - given)

    def _zscore(value: float, pool: list[float]) -> float:
        if not pool or len(pool) < 2:
            return 0.0
        mu = mean(pool)
        sd = pstdev(pool) or 1.0
        return (value - mu) / sd

    draft_pool = [m.draft_delta_avg for m in by_id.values() if m.n_picks]
    trade_pool = [m.trade_delta_total for m in by_id.values() if m.n_trades]

    for m in by_id.values():
        m.z_draft = _zscore(m.draft_delta_avg, draft_pool) if m.n_picks else 0.0
        m.z_trade = _zscore(m.trade_delta_total, trade_pool) if m.n_trades else 0.0
        m.skill_score = (m.z_draft + m.z_trade) / 2.0
        if not m.n_trades:
            m.notes.append("no trades on record")
        if 0 < m.n_picks < 5:
            m.notes.append(f"only {m.n_picks} rated draft picks (low sample)")
        elif m.n_picks == 0:
            m.notes.append("no rated draft picks")

    ranked = sorted(by_id.values(), key=lambda x: x.skill_score, reverse=True)
    for i, m in enumerate(ranked, start=1):
        m.skill_rank = i
    return ranked


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _serialize_report(
    platform: str,
    league_id: str,
    managers: list[ManagerScore],
    picks: list[DraftPickRecord],
    trades: list[TradeRecord],
    score_lookup: dict[str, dict],
) -> dict:
    enriched_picks = []
    for p in picks:
        info = score_lookup.get(p.player_ext_id)
        expected = expected_score_at_pick(p.pick_no)
        enriched_picks.append({
            "pick_no": p.pick_no,
            "round": p.round_no,
            "draft_year": p.draft_year,
            "franchise_id": p.franchise_id,
            "player_ext_id": p.player_ext_id,
            "player_name": (info or {}).get("name") or p.player_name,
            "position": (info or {}).get("position") or p.position,
            "score": (info or {}).get("score"),
            "expected": round(expected, 2),
            "delta": round((info["score"] - expected), 2) if info else None,
        })
    return {
        "platform": platform,
        "league_id": league_id,
        "n_picks": len(picks),
        "n_trades": len(trades),
        "managers": [
            {
                "franchise_id": m.franchise_id,
                "display_name": m.display_name,
                "skill_rank": m.skill_rank,
                "skill_score": round(m.skill_score, 3),
                "n_picks": m.n_picks,
                "draft_delta_total": round(m.draft_delta_total, 2),
                "draft_delta_avg": round(m.draft_delta_avg, 2),
                "z_draft": round(m.z_draft, 3),
                "n_trades": m.n_trades,
                "trade_delta_total": round(m.trade_delta_total, 2),
                "z_trade": round(m.z_trade, 3),
                "notes": m.notes,
            }
            for m in managers
        ],
        "picks_detail": enriched_picks,
    }


def manager_report_sleeper(
    league_id: str, client: Optional[httpx.Client] = None
) -> dict:
    own_client = client is None
    client = client or httpx.Client(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    try:
        users = client.get(f"{SLEEPER_BASE}/league/{league_id}/users").json() or []
        rosters = client.get(f"{SLEEPER_BASE}/league/{league_id}/rosters").json() or []
        picks = _fetch_sleeper_drafts(client, league_id)
        trades = _fetch_sleeper_trades(client, league_id)
    finally:
        if own_client:
            client.close()

    user_by_id = {
        u["user_id"]: (u.get("display_name") or u.get("username") or u["user_id"])
        for u in users
    }
    franchise_names: dict[str, str] = {}
    for r in rosters:
        rid = str(r.get("roster_id"))
        franchise_names[rid] = user_by_id.get(r.get("owner_id"), f"Team {rid}")

    all_ext = list({p.player_ext_id for p in picks} | {
        pid for tx in trades for ids in tx.sides.values() for pid in ids
    })
    scores = _resolve_scores_for_ext_ids(all_ext, "sleeper_id")
    managers = _compute_manager_table(franchise_names, picks, trades, scores)
    return _serialize_report("sleeper", league_id, managers, picks, trades, scores)


def manager_report_mfl(
    league_id: str, year: int, client: Optional[httpx.Client] = None
) -> dict:
    own_client = client is None
    client = client or httpx.Client(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    try:
        league_url = f"{MFL_BASE}/{year}/export?TYPE=league&L={league_id}&JSON=1"
        league = (client.get(league_url).json() or {}).get("league", {}) or {}
        franchises_meta = (league.get("franchises", {}) or {}).get("franchise") or []
        if isinstance(franchises_meta, dict):
            franchises_meta = [franchises_meta]
        franchise_names = {str(f["id"]): f.get("name", str(f["id"])) for f in franchises_meta}
        picks = _fetch_mfl_drafts(client, year, league_id)
        trades = _fetch_mfl_trades(client, year, league_id)
    finally:
        if own_client:
            client.close()

    all_ext = list({p.player_ext_id for p in picks} | {
        pid for tx in trades for ids in tx.sides.values() for pid in ids
    })
    scores = _resolve_scores_for_ext_ids(all_ext, "mfl_id")
    managers = _compute_manager_table(franchise_names, picks, trades, scores)
    return _serialize_report("mfl", league_id, managers, picks, trades, scores)
