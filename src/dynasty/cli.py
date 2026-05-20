"""Typer CLI — main entry point.

Usage:
    python -m dynasty.cli init-db
    python -m dynasty.cli sync-players
    python -m dynasty.cli sync fantasycalc
    python -m dynasty.cli sync-all
    python -m dynasty.cli score --league-format sf_ppr
    python -m dynasty.cli top --n 30 --league-format sf_ppr
    python -m dynasty.cli sources
    python -m dynasty.cli backtest fantasycalc --years 2020,2021,2022 --window 3
    python -m dynasty.cli run-scheduler
"""
from __future__ import annotations
import json
from datetime import datetime
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select, func

from .db.session import init_db, get_session
from .db.models import Source, Player, CompositeScore, Ranking
from .sources import REGISTRY
from .sync import sync_source, sync_sleeper_players
from .scoring import compute_composite_scores
from .backtest import backtest_source

app = typer.Typer(help="Dynasty fantasy football composite model CLI", add_completion=False)
console = Console()


@app.command("init-db")
def cli_init_db():
    """Create all database tables."""
    init_db()
    console.print("[green]✓[/green] Database initialized.")


@app.command("sync-players")
def cli_sync_players():
    """Pull the Sleeper player dictionary — required before other sources."""
    console.print("Pulling Sleeper player map (~5MB, may take 30s)...")
    n = sync_sleeper_players()
    console.print(f"[green]✓[/green] Upserted {n} players from Sleeper.")


@app.command("sync")
def cli_sync(slug: str):
    """Sync a single source by slug. E.g. `sync fantasycalc`"""
    if slug not in REGISTRY:
        console.print(f"[red]Unknown source:[/red] {slug}")
        console.print(f"Available: {', '.join(REGISTRY.keys())}")
        raise typer.Exit(1)
    console.print(f"Syncing [cyan]{slug}[/cyan]...")
    n = sync_source(slug)
    console.print(f"[green]✓[/green] Wrote {n} ranking rows from {slug}.")


@app.command("sync-all")
def cli_sync_all():
    """Sync all registered sources (skips Sleeper — use sync-players for that)."""
    for slug in REGISTRY:
        if slug == "sleeper_players":
            continue
        console.print(f"Syncing [cyan]{slug}[/cyan]...")
        try:
            n = sync_source(slug)
            console.print(f"  [green]✓[/green] {n} rows")
        except Exception as e:
            console.print(f"  [red]✗ {e}[/red]")


@app.command("score")
def cli_score(
    league_format: str = typer.Option("sf_ppr", "--league-format", "-f"),
    depth: int = typer.Option(300, "--depth"),
):
    """Compute composite scores for a league format."""
    console.print(f"Scoring [cyan]{league_format}[/cyan] (depth={depth})...")
    n = compute_composite_scores(league_format=league_format, depth=depth)
    console.print(f"[green]✓[/green] Wrote {n} composite score rows.")


@app.command("top")
def cli_top(
    n: int = typer.Option(30, "--n"),
    league_format: str = typer.Option("sf_ppr", "--league-format", "-f"),
    position: str = typer.Option(None, "--position", "-p"),
):
    """Show the most recent composite top-N for a league format."""
    with get_session() as session:
        # Latest generated_at
        latest_ts = session.execute(
            select(func.max(CompositeScore.generated_at))
            .where(CompositeScore.league_format == league_format)
        ).scalar_one_or_none()
        if latest_ts is None:
            console.print("[yellow]No composite scores yet. Run `score` first.[/yellow]")
            raise typer.Exit(0)

        q = (
            select(CompositeScore, Player)
            .join(Player, CompositeScore.player_id == Player.id)
            .where(CompositeScore.league_format == league_format)
            .where(CompositeScore.generated_at == latest_ts)
            .order_by(CompositeScore.overall_rank)
        )
        if position:
            q = q.where(Player.position == position.upper())
        rows = session.execute(q.limit(n)).all()

    table = Table(title=f"Composite Top-{n} — {league_format} — {latest_ts:%Y-%m-%d %H:%M}")
    table.add_column("#", justify="right")
    table.add_column("Player")
    table.add_column("Pos")
    table.add_column("Team")
    table.add_column("Tier", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Pos Rank", justify="right")

    for cs, p in rows:
        table.add_row(
            str(cs.overall_rank),
            p.full_name,
            p.position or "-",
            p.nfl_team or "-",
            str(cs.tier or "-"),
            f"{cs.score:.2f}",
            f"{p.position or '-'}{cs.position_rank}" if cs.position_rank else "-",
        )
    console.print(table)


@app.command("sources")
def cli_sources():
    """Show registered sources and last sync status."""
    with get_session() as session:
        rows = session.execute(select(Source).order_by(Source.slug)).scalars().all()

    table = Table(title="Sources")
    table.add_column("Slug")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Freq")
    table.add_column("ToS OK")
    table.add_column("Weight", justify="right")
    table.add_column("Last Sync")
    table.add_column("Status")

    if not rows:
        console.print("[yellow]No sources in DB yet. Run a `sync` to register one.[/yellow]")
        for slug, cls in REGISTRY.items():
            console.print(f"  [dim]registered (not yet synced):[/dim] {slug} — {cls.name}")
        return

    for s in rows:
        table.add_row(
            s.slug, s.name, s.category, s.update_frequency,
            "✓" if s.tos_compliant else "✗",
            f"{s.default_weight:.2f}",
            s.last_synced_at.strftime("%Y-%m-%d %H:%M") if s.last_synced_at else "-",
            s.last_sync_status or "-",
        )
    console.print(table)


@app.command("backtest")
def cli_backtest(
    source_slug: str,
    years: str = typer.Option(..., "--years", help="Comma-separated cohort years, e.g. 2020,2021,2022"),
    window: int = typer.Option(3, "--window", help="Production window in years"),
    position: str = typer.Option(None, "--position", "-p"),
):
    """Backtest a source against actual NFL production for given cohort years.

    Requires that you've loaded historical pre-draft rankings + production data.
    """
    cohort_years = [int(y.strip()) for y in years.split(",")]
    result = backtest_source(source_slug, cohort_years, window_years=window, position=position)
    if result is None:
        console.print("[yellow]Insufficient data to backtest. Need ≥5 (rank, production) pairs.[/yellow]")
        raise typer.Exit(0)

    console.print_json(json.dumps(result, default=str))


@app.command("inspect")
def cli_inspect(name: str):
    """Show all rankings + score history for a player by name (substring match)."""
    with get_session() as session:
        players = session.execute(
            select(Player).where(Player.full_name.ilike(f"%{name}%")).limit(5)
        ).scalars().all()
        if not players:
            console.print(f"[yellow]No players found matching '{name}'[/yellow]")
            return
        for p in players:
            console.print(f"\n[bold cyan]{p.full_name}[/bold cyan] ({p.position}, {p.nfl_team})  sleeper_id={p.sleeper_id}")
            rankings = session.execute(
                select(Ranking, Source)
                .join(Source, Ranking.source_id == Source.id)
                .where(Ranking.player_id == p.id)
                .order_by(Ranking.captured_at.desc())
                .limit(10)
            ).all()
            for r, s in rankings:
                console.print(
                    f"  {r.captured_at:%Y-%m-%d}  {s.slug:>20}  "
                    f"rank={r.overall_rank}  val={r.market_value}  fmt={r.league_format}"
                )


@app.command("run-scheduler")
def cli_run_scheduler():
    """Run the daily/weekly scheduler in the foreground."""
    from .scheduler import build_scheduler
    sched = build_scheduler()
    console.print("[cyan]Scheduler starting. Configured jobs:[/cyan]")
    for j in sched.get_jobs():
        console.print(f"  {j.id}: {j.trigger}")
    sched.start()


if __name__ == "__main__":
    app()
