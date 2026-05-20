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


@app.command("league")
def cli_league(
    platform: str = typer.Argument(..., help="sleeper | mfl"),
    league_id: str = typer.Argument(..., help="Sleeper league_id or MFL league_id"),
    league_format: str = typer.Option("sf_ppr", "--league-format", "-f"),
    year: int = typer.Option(None, "--year", help="MFL year (defaults to current)"),
    as_json: bool = typer.Option(False, "--json", help="Emit full JSON report"),
):
    """Pull a league from Sleeper or MFL and rate every team against the model.

    Examples:
        python -m dynasty.cli league sleeper 968712712272838656
        python -m dynasty.cli league mfl 12345 --year 2026 --json

    Requires the latest composite scores to have been computed for the
    given league_format (run `score -f sf_ppr` first).
    """
    from .league import evaluate_sleeper_league, evaluate_mfl_league

    if platform.lower() == "sleeper":
        report = evaluate_sleeper_league(league_id, league_format=league_format)
    elif platform.lower() == "mfl":
        report = evaluate_mfl_league(league_id, year=year, league_format=league_format)
    else:
        console.print(f"[red]Unknown platform:[/red] {platform!r}. Use 'sleeper' or 'mfl'.")
        raise typer.Exit(1)

    if as_json:
        console.print_json(json.dumps(report.to_dict(), default=str))
        return

    console.print(f"\n[bold cyan]{report.name}[/bold cyan]  ({report.platform} {report.league_id}, {report.league_format})")
    console.print(f"League avg roster value: [bold]{report.league_avg_score:.1f}[/bold]\n")

    rank_table = Table(title="Power rankings (by total roster value)")
    rank_table.add_column("#", justify="right")
    rank_table.add_column("Team")
    rank_table.add_column("Total", justify="right")
    rank_table.add_column("vs Avg", justify="right")
    for row in report.power_rankings:
        diff = row["vs_league_avg"]
        diff_str = f"+{diff}" if diff >= 0 else f"{diff}"
        rank_table.add_row(
            str(row["rank"]), row["display_name"],
            f"{row['total_score']:.1f}", diff_str,
        )
    console.print(rank_table)

    for t in report.teams:
        console.print(f"\n[bold]{t.display_name}[/bold]  total={t.total_score:.1f}  avg={t.avg_score:.1f}  rated={t.players_evaluated}  unrated={t.players_unrated}")
        if t.top_assets:
            console.print("  top 5 assets:")
            for a in t.top_assets:
                console.print(f"    • {a['name']:<24} {a['position']:>3}  rank={a['rank']:>3}  tier=T{a['tier']}  score={a['score']:.1f}")
        if t.weaknesses:
            console.print("  [yellow]weaknesses:[/yellow]")
            for w in t.weaknesses:
                console.print(f"    - {w}")


@app.command("managers")
def cli_managers(
    platform: str = typer.Argument(..., help="sleeper | mfl"),
    league_id: str = typer.Argument(...),
    year: int = typer.Option(None, "--year", help="MFL year (defaults to current)"),
    as_json: bool = typer.Option(False, "--json", help="Emit full JSON report"),
):
    """Manager skill rankings from draft + trade history.

    Examples:
        python -m dynasty.cli managers sleeper 968712712272838656
        python -m dynasty.cli managers mfl 12345 --year 2026
    """
    from .manager import manager_report_sleeper, manager_report_mfl
    from datetime import datetime as _dt

    plat = platform.lower()
    if plat == "sleeper":
        report = manager_report_sleeper(league_id)
    elif plat == "mfl":
        report = manager_report_mfl(league_id, year=year or _dt.utcnow().year)
    else:
        console.print(f"[red]Unknown platform:[/red] {platform!r}. Use 'sleeper' or 'mfl'.")
        raise typer.Exit(1)

    if as_json:
        console.print_json(json.dumps(report, default=str))
        return

    console.print(
        f"\n[bold cyan]Manager rankings[/bold cyan]  ({plat} {league_id})  "
        f"picks={report['n_picks']}  trades={report['n_trades']}\n"
    )

    table = Table(title="Manager skill rankings")
    table.add_column("#", justify="right")
    table.add_column("Manager")
    table.add_column("Skill", justify="right")
    table.add_column("Picks", justify="right")
    table.add_column("Draft Δ (avg)", justify="right")
    table.add_column("z_draft", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("Trade Δ (total)", justify="right")
    table.add_column("z_trade", justify="right")
    table.add_column("Notes")
    for m in report["managers"]:
        table.add_row(
            str(m["skill_rank"]),
            m["display_name"],
            f"{m['skill_score']:+.2f}",
            str(m["n_picks"]),
            f"{m['draft_delta_avg']:+.1f}" if m["n_picks"] else "-",
            f"{m['z_draft']:+.2f}" if m["n_picks"] else "-",
            str(m["n_trades"]),
            f"{m['trade_delta_total']:+.1f}" if m["n_trades"] else "-",
            f"{m['z_trade']:+.2f}" if m["n_trades"] else "-",
            ", ".join(m["notes"]) or "",
        )
    console.print(table)


@app.command("prefetch-leagues")
def cli_prefetch_leagues():
    """Run the leagues.json pre-fetcher and write JSON into dynasty_site/leagues/."""
    from pathlib import Path as _P
    import sys as _sys
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent.parent / "scripts"))
    import prefetch_leagues
    summary = prefetch_leagues.prefetch_all()
    console.print_json(json.dumps(summary, default=str))


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
