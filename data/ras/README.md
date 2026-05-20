# RAS data directory

Drop **Kent Lee Platte's Relative Athletic Score CSV** here as
`ras_database.csv` and the `ras` source adapter will pick it up on the
next `python -m dynasty.cli sync ras`.

If the canonical filename ever changes, set `DYNASTY_RAS_CSV_PATH` to
point at the right file:

```bash
export DYNASTY_RAS_CSV_PATH=/path/to/my/ras_export.csv
python -m dynasty.cli sync ras
```

## Expected schema

The adapter is forgiving on column casing/spelling. It looks for any of
the following aliases per logical field (case- and space- insensitive):

| Logical field | Accepted column names                                           |
|---------------|------------------------------------------------------------------|
| name          | `Name`, `Player`, `full_name`                                    |
| position      | `Pos`, `Position`, `primary_position`                            |
| college       | `College`, `school`                                              |
| draft_year    | `Year`, `season`, `draft_year`, `class`                          |
| ras           | `RAS`, `RAS_Score`, `score`, `composite`, `RAS_Grade`            |

Rows missing a usable name, position, or RAS value are skipped.
K, DEF, and non-skill positions are filtered. Only the last 6 draft
classes emit ranking rows; older rows still enrich the Player table.

## Where to get the data

Kent Lee Platte (@MathBomb on social, https://ras.football/) publishes
RAS scores per prospect each Combine week and shares the database on
request. The site doesn't host a stable public download URL; the
typical workflow is to export from his shared spreadsheet or scrape
his per-player score pages (with attribution).

This directory is intentionally **empty in git** — drop the CSV
locally and don't commit it (it's in `.gitignore`).
