"""Pre-fetch leagues listed in leagues.json into static JSON for the site.

For each entry in `leagues.json`:
  - Fetch league + rosters via `dynasty.league.evaluate_*_league`.
  - Fetch drafts + trades + compute manager rankings via `dynasty.manager`.
  - Write `dynasty_site/leagues/<platform>-<league_id>.json` containing both.

Also writes `dynasty_site/leagues/index.json` — a manifest the site uses to
populate the league selector.

This is how MFL leagues reach the published site: the live API has no CORS,
so we bake them in at build time. Sleeper leagues can also be pre-fetched
here (manager rankings live in the JSON), but the league.html page will
prefer live fetches for them when the user enters an arbitrary ID.

Usage:
    python scripts/prefetch_leagues.py
    # or via the headless launcher (called automatically)
"""
from __future__ import annotations
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LEAGUES_CONFIG = REPO_ROOT / "leagues.json"
OUTPUT_DIR = REPO_ROOT / "dynasty_site" / "leagues"


def _load_config() -> list[dict]:
    if not LEAGUES_CONFIG.exists():
        return []
    with open(LEAGUES_CONFIG) as f:
        data = json.load(f) or {}
    return data.get("leagues", []) or []


def _prefetch_sleeper(entry: dict) -> dict:
    from dynasty.league import evaluate_sleeper_league
    from dynasty.manager import manager_report_sleeper

    league_id = str(entry["league_id"])
    league_format = entry.get("league_format", "sf_ppr")

    report = evaluate_sleeper_league(league_id, league_format=league_format).to_dict()
    managers = manager_report_sleeper(league_id)
    return {
        "platform": "sleeper",
        "league_id": league_id,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "team_report": report,
        "manager_report": managers,
    }


def _prefetch_mfl(entry: dict) -> dict:
    from dynasty.league import evaluate_mfl_league
    from dynasty.manager import manager_report_mfl

    league_id = str(entry["league_id"])
    year = int(entry.get("year") or datetime.utcnow().year)
    league_format = entry.get("league_format", "sf_ppr")

    report = evaluate_mfl_league(
        league_id, year=year, league_format=league_format
    ).to_dict()
    managers = manager_report_mfl(league_id, year=year)
    return {
        "platform": "mfl",
        "league_id": league_id,
        "year": year,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "team_report": report,
        "manager_report": managers,
    }


def prefetch_all(output_dir: Path = OUTPUT_DIR) -> dict:
    """Pre-fetch all leagues in leagues.json. Returns summary dict.

    Writes one file per league plus an index.json manifest.
    """
    entries = _load_config()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries: list[dict] = []
    errors: list[dict] = []

    for entry in entries:
        platform = (entry.get("platform") or "").lower()
        league_id = entry.get("league_id")
        if not league_id:
            errors.append({"entry": entry, "error": "missing league_id"})
            continue

        try:
            if platform == "sleeper":
                payload = _prefetch_sleeper(entry)
            elif platform == "mfl":
                payload = _prefetch_mfl(entry)
            else:
                errors.append({"entry": entry, "error": f"unknown platform: {platform}"})
                continue
        except Exception as e:  # noqa: BLE001 — capture-everything is intentional here
            errors.append({
                "entry": entry,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            })
            continue

        # Write per-league file.
        slug = f"{platform}-{league_id}"
        out_path = output_dir / f"{slug}.json"
        with open(out_path, "w") as f:
            json.dump(payload, f, separators=(",", ":"))

        manifest_entries.append({
            "slug": slug,
            "platform": platform,
            "league_id": str(league_id),
            "year": payload.get("year"),
            "name": (payload.get("team_report") or {}).get("name") or slug,
            "n_teams": len(((payload.get("team_report") or {}).get("teams") or [])),
            "n_managers": len(((payload.get("manager_report") or {}).get("managers") or [])),
            "fetched_at": payload["fetched_at"],
        })

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "leagues": manifest_entries,
        "errors": errors,
    }
    with open(output_dir / "index.json", "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest


def main():
    summary = prefetch_all()
    print(f"Pre-fetched {len(summary['leagues'])} leagues, {len(summary['errors'])} errors.")
    for L in summary["leagues"]:
        print(f"  {L['slug']:>40}  teams={L['n_teams']:>2}  managers={L['n_managers']:>2}  ({L['name']})")
    for err in summary["errors"]:
        print(f"  [error] {err['entry']}: {err['error']}")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT / "src"))
    main()
