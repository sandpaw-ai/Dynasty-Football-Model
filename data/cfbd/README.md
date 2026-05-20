# CFBD breakouts data directory

Drop a pre-computed CSV of college Breakout Age + Dominator features here as
`breakouts.csv` and the `cfbd_breakouts` source adapter will pick it up on
the next `python -m dynasty.cli sync cfbd_breakouts`.

Override the location via env var if you prefer a different path:

```bash
export DYNASTY_CFBD_CSV_PATH=/path/to/my/breakouts.csv
python -m dynasty.cli sync cfbd_breakouts
```

## Expected schema

Adapter is forgiving on column casing/spelling. Accepted aliases per field:

| Logical field   | Accepted column names                                                |
|-----------------|----------------------------------------------------------------------|
| name            | `name`, `player`, `full_name`                                        |
| position        | `pos`, `position`, `primary_position`                                |
| college         | `college`, `school`, `team`                                          |
| draft_year      | `year`, `season`, `draft_year`, `class`, `nfl_draft_year`            |
| breakout_age    | `breakout_age`, `breakout`, `breakout_year`, `ba`                    |
| best_dominator  | `best_dominator`, `dominator`, `college_dominator`, `dr`, `best_dr`  |

* **breakout_age**: numeric, in years (e.g. `19.5` for a player who first
  posted ≥20% college dominator at age 19.5). Earlier = better.
* **best_dominator**: 0..1 share of team rec yards + rec TDs (for WR/TE) or
  all-purpose yards + TDs (for RB) in their *best* college season.

Rows missing both `breakout_age` and `best_dominator` are skipped, as are
K, DEF, and other non-skill positions. Only the last 6 draft classes emit
ranking rows.

## How to generate the CSV

Two recommended approaches:

1. **CFBD API directly** (https://api.collegefootballdata.com). Free
   tier-limited keys at https://collegefootballdata.com/key. For each
   prospect, pull season-level receiving + rushing stats, compute their
   team-share, and identify the earliest year share ≥ 0.20.

2. **`nflverse` college data** has a partial overlap and may be enough
   for the top of recent classes.

Live API integration is a planned follow-up. For now, dropping a CSV in
this directory is the supported path.

This directory is intentionally **empty in git** — drop the CSV locally
and don't commit it (it's in `.gitignore`).
