"""Sync logic: run a source adapter and persist its records.

Player resolution order: sleeper_id → mfl_id → (full_name + position). If no
match, a minimal Player row is auto-created so we never drop data.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import select

from .db.session import get_session
from .db.models import Source, Player, Ranking
from .sources import REGISTRY
from .sources.base import BaseSource, RankingRecord
from .sources.sleeper import SleeperPlayers


def _ensure_source_row(session, adapter: BaseSource) -> Source:
    row = session.execute(select(Source).where(Source.slug == adapter.slug)).scalar_one_or_none()
    if row is None:
        row = Source(
            slug=adapter.slug,
            name=adapter.name,
            category=adapter.category,
            url=adapter.homepage,
            update_frequency=adapter.update_frequency,
            tos_compliant=adapter.tos_compliant,
            default_weight=adapter.default_weight,
            notes=adapter.notes,
        )
        session.add(row)
        session.flush()
    return row


def _resolve_player(session, rec: RankingRecord) -> Player | None:
    if rec.sleeper_id:
        p = session.execute(select(Player).where(Player.sleeper_id == rec.sleeper_id)).scalar_one_or_none()
        if p:
            return _enrich_player(p, rec)
    if rec.gsis_id:
        p = session.execute(select(Player).where(Player.gsis_id == rec.gsis_id)).scalar_one_or_none()
        if p:
            return _enrich_player(p, rec)
    if rec.mfl_id:
        p = session.execute(select(Player).where(Player.mfl_id == rec.mfl_id)).scalar_one_or_none()
        if p:
            return _enrich_player(p, rec)
    if rec.full_name:
        q = select(Player).where(Player.full_name == rec.full_name)
        if rec.position:
            q = q.where(Player.position == rec.position)
        p = session.execute(q).scalars().first()
        if p:
            return _enrich_player(p, rec)
    # Auto-create minimal record
    p = Player(
        sleeper_id=rec.sleeper_id,
        mfl_id=rec.mfl_id,
        gsis_id=rec.gsis_id,
        pfr_id=rec.pfr_id,
        full_name=rec.full_name or "(unknown)",
        position=rec.position,
        nfl_team=rec.nfl_team,
        draft_year=rec.draft_year,
        draft_round=rec.draft_round,
        draft_pick_overall=rec.draft_pick_overall,
        draft_team=rec.draft_team,
        college=rec.college,
    )
    session.add(p)
    session.flush()
    return p


def _enrich_player(p: Player, rec: RankingRecord) -> Player:
    """Fill in missing fields on an existing Player from a RankingRecord.

    Conservative: only writes a field if it's currently NULL/empty. The one
    exception is `nfl_team` for an active player, where the most-recent ranking
    record's team value tends to be more current than what's stored.
    """
    if rec.draft_year and not p.draft_year:
        p.draft_year = rec.draft_year
    if rec.draft_round and not p.draft_round:
        p.draft_round = rec.draft_round
    if rec.draft_pick_overall and not p.draft_pick_overall:
        p.draft_pick_overall = rec.draft_pick_overall
    if rec.draft_team and not p.draft_team:
        p.draft_team = rec.draft_team
    if rec.gsis_id and not p.gsis_id:
        p.gsis_id = rec.gsis_id
    if rec.pfr_id and not p.pfr_id:
        p.pfr_id = rec.pfr_id
    if rec.college and not p.college:
        p.college = rec.college
    if rec.nfl_team and not p.nfl_team:
        p.nfl_team = rec.nfl_team
    if rec.position and not p.position:
        p.position = rec.position
    return p


def sync_source(slug: str) -> int:
    """Run a source by slug. Returns the number of ranking rows written."""
    if slug not in REGISTRY:
        raise KeyError(f"Unknown source slug: {slug}")
    AdapterCls = REGISTRY[slug]
    adapter = AdapterCls()
    count = 0
    try:
        with get_session() as session:
            source_row = _ensure_source_row(session, adapter)
            for rec in adapter.fetch():
                player = _resolve_player(session, rec)
                if player is None:
                    continue
                session.add(Ranking(
                    source_id=source_row.id,
                    player_id=player.id,
                    overall_rank=rec.overall_rank,
                    position_rank=rec.position_rank,
                    market_value=rec.market_value,
                    tier=rec.tier,
                    trend_30d=rec.trend_30d,
                    league_format=rec.league_format,
                    is_dynasty=rec.is_dynasty,
                    is_rookie_only=rec.is_rookie_only,
                    captured_at=rec.captured_at,
                ))
                count += 1
            source_row.last_synced_at = datetime.utcnow()
            source_row.last_sync_status = "ok"
            source_row.last_sync_error = None
    except Exception as e:
        # Persist the error so the UI/CLI can surface it later
        with get_session() as session:
            row = session.execute(select(Source).where(Source.slug == slug)).scalar_one_or_none()
            if row:
                row.last_sync_status = "error"
                row.last_sync_error = str(e)[:1000]
        raise
    finally:
        adapter.close()
    return count


def sync_sleeper_players() -> int:
    """Pull the Sleeper player dict and upsert into the players table.

    Run this BEFORE other sources to populate the canonical ID map.
    """
    adapter = SleeperPlayers()
    try:
        players_dict = adapter.fetch_players_dict()
    finally:
        adapter.close()

    count = 0
    with get_session() as session:
        for sleeper_id, p in players_dict.items():
            full_name = p.get("full_name")
            if not full_name:
                continue
            existing = session.execute(
                select(Player).where(Player.sleeper_id == sleeper_id)
            ).scalar_one_or_none()

            def _str(key):
                v = p.get(key)
                return str(v) if v is not None else None

            if existing:
                existing.full_name = full_name or existing.full_name
                existing.first_name = p.get("first_name") or existing.first_name
                existing.last_name = p.get("last_name") or existing.last_name
                existing.position = p.get("position") or existing.position
                existing.nfl_team = p.get("team") or existing.nfl_team
                existing.mfl_id = _str("mfl_id") or existing.mfl_id
                existing.espn_id = _str("espn_id") or existing.espn_id
                existing.yahoo_id = _str("yahoo_id") or existing.yahoo_id
                existing.gsis_id = _str("gsis_id") or existing.gsis_id
                existing.pfr_id = _str("pfr_id") or existing.pfr_id
                existing.college = p.get("college") or existing.college
                existing.is_active = p.get("active", existing.is_active)
            else:
                session.add(Player(
                    sleeper_id=sleeper_id,
                    full_name=full_name,
                    first_name=p.get("first_name"),
                    last_name=p.get("last_name"),
                    position=p.get("position"),
                    nfl_team=p.get("team"),
                    mfl_id=_str("mfl_id"),
                    espn_id=_str("espn_id"),
                    yahoo_id=_str("yahoo_id"),
                    gsis_id=_str("gsis_id"),
                    pfr_id=_str("pfr_id"),
                    college=p.get("college"),
                    is_active=p.get("active", True),
                ))
            count += 1
    return count
