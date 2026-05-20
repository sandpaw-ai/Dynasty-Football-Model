"""Headless launcher — used by GitHub Actions (no browser to open).

Difference from launcher.py:
  - No webbrowser.open() call (no display in CI)
  - No interactive prompts
  - Non-zero exit only on hard failures (no data could be synced at all)
  - All output is plain (no ANSI colors that look messy in CI logs)
"""
from __future__ import annotations
import sys
from pathlib import Path


def main():
    print("=" * 60)
    print("Dynasty Model — headless refresh (CI)")
    print("=" * 60)

    # Step 1: init DB
    print("\n[1/5] Initializing database...")
    try:
        from dynasty.db.session import init_db, get_session
        from dynasty.db.models import Player
        from sqlalchemy import select, func
        init_db()
        print("  OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    # Step 2: Sleeper players
    print("\n[2/5] Loading player metadata from Sleeper...")
    try:
        from dynasty.sync import sync_sleeper_players
        n = sync_sleeper_players()
        print(f"  OK ({n:,} players)")
    except Exception as e:
        print(f"  WARN: {e}")
        print("  Continuing without Sleeper player map.")

    # Step 3: Sync data sources
    print("\n[3/5] Syncing data sources...")
    from dynasty.sync import sync_source

    synced_any = False
    # Order matters slightly: the sources that *enrich* the canonical Player
    # table (draft capital, RAS, CFBD) are best run AFTER the market/aggregator
    # sources so that name-based player resolution finds the existing rows
    # rather than auto-creating duplicates. The Sleeper player upsert runs in
    # step [2/5] above, which is the most important canonicalization step.
    sources_to_sync = [
        # Market + consensus (core composite signal)
        ("fantasycalc", "FantasyCalc"),
        ("dynastyprocess", "DynastyProcess"),
        # ffc_adp removed v0.10: its top picks consistently disagreed with
        # dynasty-superflex consensus because FFC's user base skews casual /
        # redraft. Adapter file kept on disk for future re-enable.
        # Model + analytics overlays
        ("brainy_ballers", "Brainy Ballers"),
        ("nfl_draft_capital", "NFL Draft Capital"),
        # Local-CSV sources — will sync zero rows until the data file is
        # dropped into the corresponding data/ directory, but they register
        # cleanly either way.
        ("ras", "RAS (Relative Athletic Score)"),
        ("cfbd_breakouts", "CFBD Breakouts (Breakout Age + Dominator)"),
    ]
    for slug, label in sources_to_sync:
        try:
            n = sync_source(slug)
            print(f"  {label}: {n:,} rows")
            if n > 0:
                synced_any = True
        except Exception as e:
            print(f"  {label}: FAILED ({e})")

    # Always try to load the starter pack (offline data)
    try:
        from dynasty.starter_pack import import_starter_pack
        n = import_starter_pack()
        print(f"  Starter pack: {n} rows")
        if n > 0:
            synced_any = True
    except Exception as e:
        print(f"  Starter pack: FAILED ({e})")

    if not synced_any:
        print("\nERROR: No sources synced successfully. Cannot build site.")
        sys.exit(1)

    # Step 4: Score
    print("\n[4/5] Computing composite scores...")
    try:
        from dynasty.scoring import compute_composite_scores
        for fmt in ["sf_ppr", "1qb_ppr"]:
            n = compute_composite_scores(league_format=fmt)
            print(f"  {fmt}: {n:,} players scored")
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    # Step 5: Build site
    print("\n[5/5] Building site...")
    try:
        from dynasty.report import generate_site
        out = generate_site(output_dir="dynasty_site", league_format="sf_ppr", limit=300)
        print(f"  OK -> {out}")
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
