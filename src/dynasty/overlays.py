"""Data-driven overlays — RAS and Brainy Ballers SRS as opt-in modifiers.

Per Phil's v0.14 directive: athleticism (RAS) and Brainy Ballers'
Star-Predictor Score (SRS) should NOT have a baked-in weight in the
composite. Instead, they should appear as overlays the user toggles,
with a *data-driven* default suggestion derived from each signal's
historical correlation to realized fantasy production at each position.

Workflow:
  1. ``scripts/correlation_audit.py`` runs once (or whenever the corpus
     is refreshed) and writes ``data/overlays/correlation_table.json``.
  2. ``apply_overlay()`` reads that file and applies the overlay to a
     list of composite results, returning a new ranking.

The site UI shows each correlation alongside its slider as the
"suggested overlay weight" so users can see why one knob has more
mechanical effect than another.
"""
from __future__ import annotations

import json
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
CORRELATION_TABLE_PATH = _REPO_ROOT / "data" / "overlays" / "correlation_table.json"


def load_correlation_table() -> dict:
    """Return the cached correlation table, or an empty stub if missing.

    Schema:
        {
          "ras":                {"QB": float, "RB": float, "WR": float, "TE": float},
          "brainy_ballers_srs": {"QB": float, "RB": float, "WR": float, "TE": float},
          "methodology":        str
        }
    """
    if not CORRELATION_TABLE_PATH.exists():
        return {
            "ras": {},
            "brainy_ballers_srs": {},
            "methodology": "not yet computed",
        }
    try:
        return json.loads(CORRELATION_TABLE_PATH.read_text())
    except json.JSONDecodeError:
        return {"ras": {}, "brainy_ballers_srs": {}, "methodology": "invalid json"}


def suggested_overlay_weight(overlay_slug: str, position: str) -> float:
    """Default slider value (= max(correlation, 0)) for a given overlay.

    Negative correlations are clamped to 0 so the suggested overlay
    never hurts the model out of the box.
    """
    table = load_correlation_table()
    return max(0.0, float(table.get(overlay_slug, {}).get(position.upper(), 0.0)))


def apply_overlay(
    rankings: list[dict],
    overlay_slug: str,
    weight_by_position: dict[str, float],
    signal_by_pid: dict,
) -> list[dict]:
    """Apply an overlay to a list of composite results.

    Args:
      rankings: ordered list of dicts with at least {player_id, position,
                composite_score}.
      overlay_slug: "ras" or "brainy_ballers_srs".
      weight_by_position: {"QB": float, ...} — the user's slider value
                          per position.
      signal_by_pid: {player_id: signal_value_0_100} for this overlay.

    Returns a new list with ``composite_score`` adjusted by:

        new = old + (correlation × signal_normalized × user_weight × 10)

    The 10× scale factor brings the overlay delta into the same order of
    magnitude as composite_score (which lives in 0..100). Players without
    a signal in ``signal_by_pid`` are unaffected.
    """
    table = load_correlation_table()
    corrs = table.get(overlay_slug, {})
    out: list[dict] = []
    for r in rankings:
        pos = (r.get("position") or "").upper()
        sig = signal_by_pid.get(r.get("player_id"))
        new = dict(r)
        if sig is None:
            out.append(new)
            continue
        corr = float(corrs.get(pos, 0.0))
        uw = float(weight_by_position.get(pos, 0.0))
        delta = corr * (float(sig) / 100.0) * uw * 10.0
        new["composite_score"] = float(r.get("composite_score", 0.0)) + delta
        new[f"overlay_{overlay_slug}_delta"] = round(delta, 2)
        out.append(new)
    out.sort(key=lambda x: x["composite_score"], reverse=True)
    return out
