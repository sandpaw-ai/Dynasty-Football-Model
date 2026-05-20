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
from .names import normalize as _normalize_name


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
        # Exact match first — cheapest.
        q = select(Player).where(Player.full_name == rec.full_name)
        if rec.position:
            q = q.where(Player.position == rec.position)
        p = session.execute(q).scalars().first()
        if p:
            return _enrich_player(p, rec)

        # Normalized-name match — catches "Odell Beckham" vs "Odell Beckham Jr."
        # and similar suffix mismatches across sources.
        norm = _normalize_name(rec.full_name)
        if norm:
            q = select(Player).where(Player.normalized_name == norm)
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
        normalized_name=_normalize_name(rec.full_name),
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
                existing.normalized_name = _normalize_name(existing.full_name)
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
                    normalized_name=_normalize_name(full_name),
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


def sync_mfl_players(year: int | None = None, client=None) -> dict:
    """Pull MFL's full player export and backfill `Player.mfl_id` on rows
    we can match.

    MFL keeps its own integer player ids that don't appear in Sleeper's
    player dictionary. Without this crosswalk, pre-fetched MFL leagues
    resolve zero players to the model and every team scores 0.

    Match strategy (in order):
      1. exact normalized-name + position + nfl_team
      2. exact normalized-name + position
      3. exact normalized-name (skill positions only)

    Players with `is_active=False` in our DB are eligible (Sleeper marks
    retired/free-agents as inactive but they still appear in MFL's
    leagues).

    Returns: ``{matched: int, total_mfl_players: int, ambiguous: int,
    skipped_non_skill: int, already_set: int}``.
    """
    import httpx as _httpx
    from datetime import datetime as _dt
    from .config import settings as _settings

    year = year or _dt.utcnow().year
    own_client = client is None
    client = client or _httpx.Client(
        timeout=_settings.request_timeout_seconds,
        headers={"User-Agent": _settings.user_agent},
        follow_redirects=True,
    )

    try:
        url = f"https://api.myfantasyleague.com/{year}/export?TYPE=players&JSON=1"
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if own_client:
            client.close()

    mfl_players = ((payload.get("players") or {}).get("player") or [])
    skill_positions = {"QB", "RB", "WR", "TE", "FB"}

    matched = 0
    ambiguous = 0
    skipped = 0
    already_set = 0

    with get_session() as session:
        # Pre-build lookups keyed by (normalized_name, position) and
        # (normalized_name, position, nfl_team) for O(1) joins.
        all_players = session.execute(select(Player)).scalars().all()
        by_name_pos_team: dict[tuple, list[Player]] = {}
        by_name_pos: dict[tuple, list[Player]] = {}
        by_name: dict[str, list[Player]] = {}

        for p in all_players:
            n = p.normalized_name or _normalize_name(p.full_name)
            if not n:
                continue
            pos = (p.position or "").upper()
            team = (p.nfl_team or "").upper() or None
            key3 = (n, pos, team)
            key2 = (n, pos)
            by_name_pos_team.setdefault(key3, []).append(p)
            by_name_pos.setdefault(key2, []).append(p)
            by_name.setdefault(n, []).append(p)

        for mp in mfl_players:
            mfl_id = str(mp.get("id") or "").strip()
            if not mfl_id:
                continue
            mfl_pos = (mp.get("position") or "").upper()
            if mfl_pos not in skill_positions:
                skipped += 1
                continue

            raw_name = mp.get("name") or ""
            # MFL ships "Last, First" — flip.
            if "," in raw_name:
                last, first = (s.strip() for s in raw_name.split(",", 1))
                flipped = f"{first} {last}"
            else:
                flipped = raw_name
            n = _normalize_name(flipped)
            if not n:
                continue
            team = (mp.get("team") or "").upper() or None
            if team == "FA":
                team = None  # Sleeper-side free agents have nfl_team NULL

            # Try most-specific match first.
            candidates = by_name_pos_team.get((n, mfl_pos, team), [])
            if len(candidates) != 1:
                candidates = by_name_pos.get((n, mfl_pos), [])
            if len(candidates) != 1:
                candidates = by_name.get(n, [])

            if len(candidates) == 1:
                p = candidates[0]
                if p.mfl_id and str(p.mfl_id) == mfl_id:
                    already_set += 1
                else:
                    p.mfl_id = mfl_id
                    matched += 1
            elif len(candidates) > 1:
                ambiguous += 1

    return {
        "matched": matched,
        "total_mfl_players": len(mfl_players),
        "ambiguous": ambiguous,
        "skipped_non_skill": skipped,
        "already_set": already_set,
    }
