"""Interactive launcher — v1.0 rewrite.

Identical pipeline to launcher_headless.py, with friendly status prints and
a browser auto-open at the end.
"""
from __future__ import annotations
import sys
import webbrowser
from pathlib import Path

USE_COLOR = sys.stdout.isatty()


def _c(s, code):
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def step(n, total, msg):
    print(_c(f"\n[{n}/{total}] {msg}", "1;36"))


def ok(msg):
    print(f"  {_c('✓', '32')} {msg}")


def warn(msg):
    print(f"  {_c('!', '33')} {msg}")


def err(msg):
    print(f"  {_c('✗', '31')} {msg}")


def info(msg):
    print(f"  {msg}")


def main():
    print(_c("\n" + "=" * 60, "1;36"))
    print(_c("  DYNASTY FOOTBALL MODEL v1.0 — full refresh", "1;36"))
    print(_c("=" * 60, "1;36"))

    total = 5

    step(1, total, "Setting up database…")
    try:
        from dynasty.db.session import init_db
        init_db()
        ok("Database ready.")
    except Exception as e:
        err(f"Could not initialize database: {e}")
        sys.exit(1)

    step(2, total, "Syncing Sleeper + MFL metadata…")
    try:
        from dynasty.sync import sync_sleeper_players
        n = sync_sleeper_players()
        ok(f"Sleeper: {n:,} players")
    except Exception as e:
        warn(f"Sleeper sync failed: {e}")
    try:
        from dynasty.sync import sync_mfl_players
        mfl = sync_mfl_players()
        ok(
            f"MFL crosswalk: matched={mfl['matched']:,} "
            f"ambiguous={mfl['ambiguous']:,}"
        )
    except Exception as e:
        warn(f"MFL crosswalk failed: {e}")

    step(3, total, "Running v1 similarity engine…")
    try:
        from dynasty.engine.similarity_v1 import run_engine
        engine_result = run_engine(persist=True)
        ok(
            f"active={len(engine_result.active_players):,} "
            f"retired={len(engine_result.retired_corpus):,} "
            f"ranked={len(engine_result.rankings):,}"
        )
    except Exception as e:
        err(f"Engine failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    step(4, total, "Building site…")
    try:
        from dynasty.report import generate_site
        out = generate_site(
            output_dir="dynasty_site",
            league_format="sf_ppr",
            limit=300,
            engine=engine_result,
        )
        ok(f"Site written to {out}")
    except Exception as e:
        err(f"Site generation failed: {e}")
        sys.exit(1)

    step(5, total, "Opening in your browser…")
    try:
        url = (Path(out) / "rankings.html").resolve().as_uri()
        webbrowser.open(url)
        ok("Opened.")
    except Exception as e:
        warn(f"Could not auto-open browser: {e}")
        info(f"Open this file manually: {out}/rankings.html")

    print(_c("\n" + "=" * 60, "1;32"))
    print(_c("  DONE. Re-run any time for a fresh ranking.", "1;32"))
    print(_c("=" * 60 + "\n", "1;32"))


if __name__ == "__main__":
    main()
