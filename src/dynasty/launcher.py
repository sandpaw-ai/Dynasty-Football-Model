"""All-in-one launcher.

This is the script your double-click scripts call. It:
  1. Initializes the database (idempotent — safe to re-run)
  2. Pulls the Sleeper player map (only if not already loaded)
  3. Syncs market values from FantasyCalc and DynastyProcess
  4. Computes composite scores
  5. Builds an HTML report
  6. Opens it in your default browser

Designed to be foolproof. Prints friendly status messages with color where
supported, falls back to plain text otherwise.
"""
from __future__ import annotations
import sys
import time
import webbrowser
from pathlib import Path

# --- Pretty printing -------------------------------------------------------

USE_COLOR = sys.stdout.isatty()

def _c(s, code):
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s

def step(n, total, msg):
    print(_c(f"\n[{n}/{total}] {msg}", "1;36"))

def ok(msg):
    print(_c(f"  ✓ {msg}", "32"))

def warn(msg):
    print(_c(f"  ⚠ {msg}", "33"))

def err(msg):
    print(_c(f"  ✗ {msg}", "31"))

def info(msg):
    print(f"  {msg}")


# --- Main pipeline ---------------------------------------------------------

def main():
    print(_c("\n" + "=" * 60, "1;36"))
    print(_c("  DYNASTY MODEL — running full refresh", "1;36"))
    print(_c("=" * 60, "1;36"))

    total_steps = 6

    # Step 1: init DB
    step(1, total_steps, "Setting up database…")
    try:
        from dynasty.db.session import init_db, get_session
        from dynasty.db.models import Player
        from sqlalchemy import select, func
        init_db()
        ok("Database ready.")
    except Exception as e:
        err(f"Could not initialize database: {e}")
        sys.exit(1)

    # Step 2: Sleeper players (only if empty — this download is ~5MB)
    step(2, total_steps, "Loading player metadata from Sleeper…")
    try:
        from dynasty.sync import sync_sleeper_players
        with get_session() as session:
            existing = session.execute(select(func.count(Player.id))).scalar_one()
        if existing < 100:
            info("(first run — this takes about 30 seconds)")
            n = sync_sleeper_players()
            ok(f"Loaded {n:,} players.")
        else:
            ok(f"Already have {existing:,} players. Skipping (re-run with --refresh-players to force).")
    except Exception as e:
        warn(f"Could not load Sleeper players: {e}")
        warn("Continuing — sources will still work but player matching may be weaker.")

    # Step 3: Sync market values + evaluator sources
    step(3, total_steps, "Syncing all sources…")
    from dynasty.sync import sync_source

    synced_any = False
    sources_to_sync = [
        ("fantasycalc", "FantasyCalc (market)"),
        ("dynastyprocess", "DynastyProcess (consensus)"),
        ("brainy_ballers", "Brainy Ballers SPS (model)"),
    ]
    for slug, label in sources_to_sync:
        try:
            n = sync_source(slug)
            ok(f"{label}: {n:,} ranking rows")
            if n > 0:
                synced_any = True
        except Exception as e:
            warn(f"{label} failed: {e}")
            warn(f"  (will continue without this source)")

    # Load starter-pack evaluator rankings (publicly-transcribed top-N)
    try:
        from dynasty.starter_pack import import_starter_pack
        n = import_starter_pack()
        ok(f"Evaluator starter pack: {n} ranking rows from public articles")
        if n > 0:
            synced_any = True
    except Exception as e:
        warn(f"Starter pack import failed: {e}")

    if not synced_any:
        err("No sources synced successfully. Cannot compute rankings.")
        err("Check your internet connection and try again.")
        sys.exit(1)

    # Step 4: Compute composite scores
    step(4, total_steps, "Computing composite scores…")
    try:
        from dynasty.scoring import compute_composite_scores
        for fmt in ["sf_ppr", "1qb_ppr"]:
            n = compute_composite_scores(league_format=fmt)
            ok(f"{fmt}: {n:,} players scored")
    except Exception as e:
        err(f"Scoring failed: {e}")
        sys.exit(1)

    # Step 5: Generate multi-page HTML site
    step(5, total_steps, "Building site…")
    try:
        from dynasty.report import generate_site
        out = generate_site(output_dir="dynasty_site", league_format="sf_ppr", limit=300)
        ok(f"Site written to {out}")
    except Exception as e:
        err(f"Site generation failed: {e}")
        sys.exit(1)

    # Step 6: Open in browser
    step(6, total_steps, "Opening in your browser…")
    try:
        # `out` is the absolute path to index.html
        url = Path(out).resolve().as_uri()
        webbrowser.open(url)
        ok("Opened.")
    except Exception as e:
        warn(f"Could not auto-open browser: {e}")
        info(f"Open this file manually: {out}")

    print(_c("\n" + "=" * 60, "1;32"))
    print(_c("  DONE. Re-run this any time you want fresh rankings.", "1;32"))
    print(_c("=" * 60 + "\n", "1;32"))


if __name__ == "__main__":
    main()
