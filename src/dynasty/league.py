"""League import — pull rosters from Sleeper / MFL and evaluate them.

This is the KTC-style "rate my league / rate my team" feature. The point
isn't to recompute the underlying model (that's `scoring.py`); it's to
*apply* the latest composite scores to a user's actual rosters and
surface:

- Per-team total dynasty value
- Per-team position-group breakdown (QB / RB / WR / TE depth)
- Per-team top-5 assets and "weak spots" (rosters with no Tier-1/2 at a
  starting position)
- League-wide power rankings (teams sorted by total value)
- Rookie / pick depth (if pick metadata is exposed)

Both Sleeper and MFL are supported. Both use the canonical Player.id
lookup via their respective external IDs (sleeper_id, mfl_id) — make
sure you've run `sync-players` recently before evaluating an MFL league.

Usage
-----
::

    from dynasty.league import evaluate_sleeper_league, evaluate_mfl_league

    report = evaluate_sleeper_league("968712712272838656")
    print(report["power_rankings"])
    for team in report["teams"]:
        print(team["display_name"], team["total_score"], team["weaknesses"])

Or via the CLI (see PR commit body) — `python -m dynasty.cli league
sleeper 968712712272838656`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable, Optional
import httpx
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db.session import get_session
from .db.models import Player, CompositeScore
from .config import settings


SLEEPER_BASE = "https://api.sleeper.app/v1"
MFL_BASE = "https://api.myfantasyleague.com"

# A Tier-1 starting position means the team has a top-tier (tier <= 2) player
# at that position. Used in weakness flagging.
_STARTING_POSITIONS = ("QB", "RB", "WR", "TE")
_WEAKNESS_TIER_THRESHOLD = 3  # if best player at pos has tier > 3, flag as weak


@dataclass
class TeamReport:
    """One team's evaluation."""
    team_id: str
    display_name: str
    total_score: float
    avg_score: float
    players_evaluated: int
    players_unrated: int
    position_totals: dict[str, float] = field(default_factory=dict)
    top_assets: list[dict] = field(default_factory=list)         # [{name, pos, rank, score, tier}]
    weaknesses: list[str] = field(default_factory=list)
    roster: list[dict] = field(default_factory=list)             # full per-player rows


@dataclass
class LeagueReport:
    """League-wide evaluation."""
    platform: str
    league_id: str
    name: str
    league_format: str
    teams: list[TeamReport] = field(default_factory=list)
    power_rankings: list[dict] = field(default_factory=list)     # [{rank, display_name, total_score, divergence_from_avg}]
    league_avg_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "league_id": self.league_id,
            "name": self.name,
            "league_format": self.league_format,
            "league_avg_score": round(self.league_avg_score, 2),
            "power_rankings": self.power_rankings,
            "teams": [
                {
                    "team_id": t.team_id,
                    "display_name": t.display_name,
                    "total_score": round(t.total_score, 2),
                    "avg_score": round(t.avg_score, 2),
                    "players_evaluated": t.players_evaluated,
                    "players_unrated": t.players_unrated,
                    "position_totals": {k: round(v, 2) for k, v in t.position_totals.items()},
                    "top_assets": t.top_assets,
                    "weaknesses": t.weaknesses,
                }
                for t in self.teams
            ],
        }


# ---------------------------------------------------------------------------
# Player score lookup
# ---------------------------------------------------------------------------

def _latest_composite_by_player(
    session: Session, league_format: str
) -> dict[int, CompositeScore]:
    """Latest CompositeScore per player_id for a league_format."""
    latest_ts = session.execute(
        select(func.max(CompositeScore.generated_at))
        .where(CompositeScore.league_format == league_format)
    ).scalar_one_or_none()
    if latest_ts is None:
        return {}
    rows = session.execute(
        select(CompositeScore)
        .where(CompositeScore.league_format == league_format)
        .where(CompositeScore.generated_at == latest_ts)
    ).scalars().all()
    return {r.player_id: r for r in rows}


# ---------------------------------------------------------------------------
# Sleeper
# ---------------------------------------------------------------------------

def _fetch_sleeper_league(client: httpx.Client, league_id: str) -> tuple[dict, list[dict], list[dict]]:
    league = client.get(f"{SLEEPER_BASE}/league/{league_id}").json()
    users = client.get(f"{SLEEPER_BASE}/league/{league_id}/users").json()
    rosters = client.get(f"{SLEEPER_BASE}/league/{league_id}/rosters").json()
    return league, users, rosters


def evaluate_sleeper_league(
    league_id: str,
    league_format: str = "sf_ppr",
    client: Optional[httpx.Client] = None,
) -> LeagueReport:
    """Pull a Sleeper league and evaluate every team against the latest model.

    Uses the latest composite_scores snapshot for the given league_format.
    Players not present in the model are counted as `players_unrated`.
    """
    own_client = client is None
    client = client or httpx.Client(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )

    try:
        league, users, rosters = _fetch_sleeper_league(client, league_id)
    finally:
        if own_client:
            client.close()

    user_id_to_name: dict[str, str] = {}
    for u in users or []:
        user_id_to_name[u["user_id"]] = u.get("display_name") or u.get("username") or u["user_id"]

    # Build (team_id, owner_name, [sleeper_player_ids]) tuples
    teams_raw: list[tuple[str, str, list[str]]] = []
    for r in rosters or []:
        team_id = str(r.get("roster_id", "?"))
        owner = user_id_to_name.get(r.get("owner_id", ""), f"Team {team_id}")
        players = [str(p) for p in (r.get("players") or []) if p]
        teams_raw.append((team_id, owner, players))

    return _build_report(
        platform="sleeper",
        league_id=league_id,
        league_name=league.get("name", f"Sleeper league {league_id}"),
        league_format=league_format,
        teams_raw=teams_raw,
        id_kind="sleeper_id",
    )


# ---------------------------------------------------------------------------
# MFL
# ---------------------------------------------------------------------------

def _fetch_mfl_league(
    client: httpx.Client, year: int, league_id: str
) -> tuple[dict, dict]:
    league_url = f"{MFL_BASE}/{year}/export?TYPE=league&L={league_id}&JSON=1"
    rosters_url = f"{MFL_BASE}/{year}/export?TYPE=rosters&L={league_id}&JSON=1"
    league = client.get(league_url).json()
    rosters = client.get(rosters_url).json()
    return league, rosters


def evaluate_mfl_league(
    league_id: str,
    year: Optional[int] = None,
    league_format: str = "sf_ppr",
    client: Optional[httpx.Client] = None,
) -> LeagueReport:
    """Pull an MFL league and evaluate every team."""
    from datetime import datetime as _dt
    year = year or _dt.utcnow().year

    own_client = client is None
    client = client or httpx.Client(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )

    try:
        league_payload, rosters_payload = _fetch_mfl_league(client, year, league_id)
    finally:
        if own_client:
            client.close()

    league = league_payload.get("league", {}) or {}
    franchises_meta = {f["id"]: f.get("name", f["id"]) for f in (league.get("franchises", {}).get("franchise") or [])}

    franchises = (rosters_payload.get("rosters", {}).get("franchise") or [])

    teams_raw: list[tuple[str, str, list[str]]] = []
    for franchise in franchises:
        team_id = str(franchise.get("id", "?"))
        owner = franchises_meta.get(team_id, f"Team {team_id}")
        players_entry = franchise.get("player", [])
        if isinstance(players_entry, dict):
            players_entry = [players_entry]
        player_ids = [str(p.get("id")) for p in players_entry if p.get("id")]
        teams_raw.append((team_id, owner, player_ids))

    return _build_report(
        platform="mfl",
        league_id=league_id,
        league_name=league.get("name", f"MFL league {league_id}"),
        league_format=league_format,
        teams_raw=teams_raw,
        id_kind="mfl_id",
    )


# ---------------------------------------------------------------------------
# Shared report builder
# ---------------------------------------------------------------------------

def _build_report(
    *,
    platform: str,
    league_id: str,
    league_name: str,
    league_format: str,
    teams_raw: Iterable[tuple[str, str, list[str]]],
    id_kind: str,  # "sleeper_id" | "mfl_id"
) -> LeagueReport:
    with get_session() as session:
        # Resolve all external ids → Player.
        all_ext_ids: set[str] = set()
        for _, _, ext_ids in teams_raw:
            all_ext_ids.update(ext_ids)

        # Pull all matching players in one query.
        col = Player.sleeper_id if id_kind == "sleeper_id" else Player.mfl_id
        players_by_ext: dict[str, Player] = {}
        if all_ext_ids:
            rows = session.execute(
                select(Player).where(col.in_(all_ext_ids))
            ).scalars().all()
            for p in rows:
                ext = getattr(p, id_kind)
                if ext:
                    players_by_ext[ext] = p

        composite_by_pid = _latest_composite_by_player(session, league_format)

        teams: list[TeamReport] = []
        for team_id, display_name, ext_ids in teams_raw:
            roster_rows: list[dict] = []
            position_totals: dict[str, float] = {}
            best_at_pos: dict[str, dict] = {}
            evaluated = 0
            unrated = 0
            total_score = 0.0

            for ext in ext_ids:
                player = players_by_ext.get(ext)
                cs = composite_by_pid.get(player.id) if player else None
                if player is None:
                    unrated += 1
                    continue
                if cs is None:
                    unrated += 1
                    roster_rows.append({
                        "ext_id": ext, "name": player.full_name,
                        "position": player.position,
                        "score": None, "rank": None, "tier": None,
                    })
                    continue
                evaluated += 1
                total_score += cs.score
                roster_rows.append({
                    "ext_id": ext, "name": player.full_name,
                    "position": player.position,
                    "score": round(cs.score, 2),
                    "rank": cs.overall_rank,
                    "tier": cs.tier,
                    "divergence": cs.rank_divergence,
                })
                pos = player.position
                if pos:
                    position_totals[pos] = position_totals.get(pos, 0.0) + cs.score
                    if pos not in best_at_pos or cs.score > best_at_pos[pos]["score"]:
                        best_at_pos[pos] = {
                            "name": player.full_name,
                            "score": cs.score,
                            "rank": cs.overall_rank,
                            "tier": cs.tier,
                        }

            # Top assets — top 5 by score
            ranked_roster = sorted(
                (r for r in roster_rows if r.get("score") is not None),
                key=lambda r: r["score"],
                reverse=True,
            )
            top_assets = ranked_roster[:5]

            # Weaknesses — flag any starting position whose best player is
            # outside the top 3 tiers.
            weaknesses: list[str] = []
            for pos in _STARTING_POSITIONS:
                best = best_at_pos.get(pos)
                if best is None:
                    weaknesses.append(f"no rated {pos} on roster")
                elif (best.get("tier") or 99) > _WEAKNESS_TIER_THRESHOLD:
                    weaknesses.append(
                        f"weak {pos}: best is {best['name']} (Tier {best['tier']}, rank {best['rank']})"
                    )

            avg_score = (total_score / evaluated) if evaluated else 0.0
            teams.append(TeamReport(
                team_id=team_id,
                display_name=display_name,
                total_score=total_score,
                avg_score=avg_score,
                players_evaluated=evaluated,
                players_unrated=unrated,
                position_totals=position_totals,
                top_assets=top_assets,
                weaknesses=weaknesses,
                roster=roster_rows,
            ))

    # Power rankings: sort teams by total_score, compute divergence from avg.
    league_avg_score = (
        sum(t.total_score for t in teams) / len(teams) if teams else 0.0
    )
    sorted_teams = sorted(teams, key=lambda t: t.total_score, reverse=True)
    power_rankings = [
        {
            "rank": i,
            "team_id": t.team_id,
            "display_name": t.display_name,
            "total_score": round(t.total_score, 2),
            "vs_league_avg": round(t.total_score - league_avg_score, 2),
        }
        for i, t in enumerate(sorted_teams, start=1)
    ]

    return LeagueReport(
        platform=platform,
        league_id=league_id,
        name=league_name,
        league_format=league_format,
        teams=teams,
        power_rankings=power_rankings,
        league_avg_score=league_avg_score,
    )
