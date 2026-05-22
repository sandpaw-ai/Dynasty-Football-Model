# Consensus vs Model

The **Dynasty Rankings** tab (`dynasty_site/league.html`) pairs the
engine's similarity-score output against a community-consensus snapshot.
The point of the view is to surface where the dynasty community's
prognostication isn't backed by the production data the engine uses.

## Source: KeepTradeCut

[KeepTradeCut][ktc] (KTC) publishes its dynasty rankings as a single
server-rendered HTML page that embeds the full `playersArray` JSON
inline. One polite GET (~1.3 MB) returns the top 500 players with both
Superflex and 1QB ranks/values/tiers/ADP plus a stable `mflid`
crosswalk.

We chose KTC because:

- Community consensus is what we actually want to diff against — it's
  derived from millions of community trades, not one analyst's opinion.
- The data is publicly displayed on a single page; no auth, no
  rate-limited API.
- Both league formats we care about (Superflex / 1QB) are published.

[FantasyPros][fp] is the planned fallback. The adapter is currently
stubbed (see `src/dynasty/sources/fantasypros.py`); we'll only wire it
if KTC ever blocks us.

[ktc]: https://keeptradecut.com/dynasty-rankings
[fp]: https://www.fantasypros.com/nfl/rankings/dynasty-superflex.php

## How it works

```
                +---------------------------+
                |  KTC dynasty-rankings.html|
                |  (one polite GET per day) |
                +-------------+-------------+
                              |
                  scripts/refresh_ktc_consensus.py
                              |
                              v
                +---------------------------+
                | data/consensus/           |
                |   ktc_YYYY-MM-DD.json     |
                |   ktc_latest.json         |
                +-------------+-------------+
                              |
                              |   dynastyprocess
                              |   db_playerids.csv
                              |   (ktc_id -> gsis_id)
                              v
   +---------------+   +---------------------+   +---------------------+
   | engine v2.2   |-->| dynasty.consensus   |<--| crosswalk loader    |
   |  rankings     |   | compare_to_consensus|   | (Crosswalk)         |
   +---------------+   +----------+----------+   +---------------------+
                                  |
                                  v
                +---------------------------+
                | league.html (per-format)  |
                |  Superflex PPR / 1QB PPR  |
                +---------------------------+
```

## Player matching

KTC publishes `playerID` (their internal id) and `mflid` (MFL
crosswalk). The free dynastyprocess
[`db_playerids.csv`](https://github.com/dynastyprocess/data/blob/master/files/db_playerids.csv)
maps `ktc_id`, `mfl_id`, and `gsis_id` in one row, so we can resolve
KTC → model gsis_id in a single hop.

Resolution order (cheapest hit wins):

1. `ktc_id → gsis_id` (direct, no false positives)
2. `mfl_id → gsis_id` (covers KTC rows where `ktc_id` isn't in the
   crosswalk — usually fresh rookies still propagating)
3. Normalized `(name, position)` fallback (suffix-stripped, punctuation
   removed, diacritics folded)

Any KTC row that fails all three is **counted** under
`n_unmatched_consensus` rather than silently dropped, so we'll notice
mapping decay over time.

## The delta column

The Dynasty Rankings table shows `Δ = model_rank − consensus_rank`.

| Δ                  | Interpretation                                |
| ------------------ | --------------------------------------------- |
| **Negative** (`–`) | Model ranks the player **higher** than the crowd → model is more **bullish** |
| **Positive** (`+`) | Crowd ranks them higher than the data justifies → **community narrative** running ahead |
| **Zero**           | Model and consensus agree                     |

Examples from the 2026-05-22 snapshot:

- **Anthony Richardson**: model #35, KTC #234, Δ −199. The model still
  comps him to early Cam Newton / Daunte Culpepper / RG3 production
  arcs; the community has fully bailed on him.
- **Bo Nix**: model #3, KTC #32, Δ −29. The v2.2 late-breakout penalty
  pulled him down from #2 but his comp pool is still strong; the
  community heavily discounts his rookie-year-age-24 breakout.
- **Tetairoa McMillan**: model #21, KTC #26, Δ −5. The model is mildly
  more bullish than the consensus — minor disagreement.
- **Ashton Jeanty**: model #22, KTC #16, Δ +6 (SF) / +12 (1QB). The
  crowd loves his draft capital + college profile; the model has no
  NFL production to credit yet. This is the engine's structural
  blind spot for 0-NFL-season rookies.
- **Sam Howell / Joe Flacco / Marcus Mariota / Deshaun Watson**:
  large negative deltas (model bullish, crowd bearish). The model
  accumulates their career fantasy points; the community treats them
  as backups. These are interesting only if a starting opportunity
  opens up.

## Refresh cadence

KTC is scraped **once per day** via
`scripts/refresh_ktc_consensus.py`. The headless launcher
(`dynasty.launcher_headless`) calls it between the engine step and the
site build, so every fresh site build sees same-day consensus.

If the refresh fails (network, KTC layout change, Cloudflare block),
the site falls back to the previously cached `ktc_latest.json`. If no
snapshot has ever been cached, the Dynasty Rankings tab degrades to the
legacy Superflex-vs-2QB overlay (see `_build_league_overlay_legacy`).

## What this is NOT

- **Not** an input to the model ranking composite. KTC has zero weight
  in `production_score`. The consensus is a *comparison*, not a signal.
- **Not** a verdict on who's right. The engine is data-only; the
  community prices in context (coaching, target competition, injury
  history) that the model deliberately ignores. The point of the diff
  is to start an argument, not settle one.
- **Not** league-customized. The diff uses KTC's standard Superflex
  and 1QB tiers; format quirks (TE premium, custom scoring) are
  handled in the model's own per-league overlay (which the engine
  still computes and stamps into `engine.overlays`).

## Files

| File                                            | Purpose                              |
| ----------------------------------------------- | ------------------------------------ |
| `src/dynasty/sources/keeptradecut.py`           | KTC adapter + snapshot parsing       |
| `src/dynasty/consensus.py`                      | Diff engine + crosswalk loader       |
| `scripts/refresh_ktc_consensus.py`              | Daily scrape + cache                 |
| `data/consensus/ktc_latest.json`                | Canonical "latest" snapshot          |
| `data/consensus/ktc_YYYY-MM-DD.json`            | Dated history (for diff-the-diff)    |
| `data/consensus/dp_playerids.csv`               | dynastyprocess KTC→GSIS crosswalk    |
| `tests/test_consensus.py`                       | 9-case test suite over fixture HTML  |
| `tests/fixtures/ktc_sample.html`                | Captured KTC page for offline tests  |
