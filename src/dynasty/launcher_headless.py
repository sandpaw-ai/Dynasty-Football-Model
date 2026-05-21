"""Headless launcher — v1.0 rewrite.

Replaces the v0.x multi-source composite with a single-engine pipeline:
    1. Sync Sleeper player metadata (for current rosters / team display).
    2. Sync MFL player crosswalk (for MFL league imports).
    3. Build retired comp corpus + era-pace + similarity vectors.
    4. Run the v1 similarity engine, persist sidecars under data/engine_v1/.
    5. Build the static site (rankings.html, league.html, methodology.html,
       sources.html, prospects.html, per-player pages).
    6. Pre-fetch any leagues listed in leagues.json.

No more "compute composite scores" step. No more 10-source weighting. One
engine. One file of truth.
"""
from __future__ import annotations
import sys
from pathlib import Path


def main():
    print("=" * 60)
    print("Dynasty Football Model — v1.0 headless refresh (CI)")
    print("=" * 60)

    # Step 1: init DB (used for Sleeper metadata + league imports only).
    print("\n[1/5] Initializing database...")
    try:
        from dynasty.db.session import init_db
        init_db()
        print("  OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    # Step 2: Sleeper + MFL player metadata.
    print("\n[2/5] Syncing player metadata (Sleeper + MFL)...")
    try:
        from dynasty.sync import sync_sleeper_players
        n = sync_sleeper_players()
        print(f"  Sleeper: {n:,} players")
    except Exception as e:
        print(f"  Sleeper WARN: {e}")
    try:
        from dynasty.sync import sync_mfl_players
        mfl = sync_mfl_players()
        print(
            f"  MFL crosswalk: matched={mfl['matched']:,} "
            f"already_set={mfl['already_set']:,} "
            f"ambiguous={mfl['ambiguous']:,} "
            f"(of {mfl['total_mfl_players']:,})"
        )
    except Exception as e:
        print(f"  MFL crosswalk WARN: {e}")

    # Step 3: Run the v1 similarity engine.
    print("\n[3/5] Running v1 similarity engine...")
    try:
        from dynasty.engine.similarity_v1 import run_engine
        engine_result = run_engine(persist=True)
        print(
            f"  OK · active={len(engine_result.active_players):,} "
            f"retired_corpus={len(engine_result.retired_corpus):,} "
            f"ranked={len(engine_result.rankings):,}"
        )
        print(f"  Era-pace source: {engine_result.era_pace.source}")
        # Surface the top-5 for log breadcrumbs.
        for r in engine_result.rankings[:5]:
            print(
                f"    #{r['overall_rank']}. {r['name']:24s} "
                f"{r['position']:>2}  pts={r['production_score']:.0f}  "
                f"comp={r['top_comp']}"
            )
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Step 4: Build the static site.
    print("\n[4/5] Building site...")
    try:
        from dynasty.report import generate_site
        out = generate_site(
            output_dir="dynasty_site",
            league_format="sf_ppr",
            limit=300,
            engine=engine_result,
        )
        print(f"  OK -> {out}")
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Step 5: Pre-fetch leagues listed in leagues.json.
    print("\n[5/5] Pre-fetching listed leagues...")
    try:
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).resolve().parent.parent.parent / "scripts"))
        import prefetch_leagues  # type: ignore
        summary = prefetch_leagues.prefetch_all()
        ok_count = len(summary.get("leagues", []))
        err_count = len(summary.get("errors", []))
        print(f"  Pre-fetched {ok_count} leagues, {err_count} errors")
        for L in summary.get("leagues", []):
            print(f"    {L['slug']:>40}  teams={L['n_teams']:>2}  managers={L['n_managers']:>2}  ({L['name']})")
        for err in summary.get("errors", []):
            print(f"    [error] {err['entry']}: {err['error']}")
    except Exception as e:
        print(f"  WARN: pre-fetch step failed: {e}")
        print("  (Site still builds without pre-fetched leagues.)")

    print("\nDone.")


if __name__ == "__main__":
    main()
