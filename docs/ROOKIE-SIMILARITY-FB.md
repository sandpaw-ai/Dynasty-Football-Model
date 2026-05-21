# Rookie similarity chain (football) — technical writeup

Shipped in PR #16 (`ada/rookie-college-nfl-chain`) as the football
counterpart to the basketball model's `ROOKIE-SIMILARITY.md`. Closes the
rookie / incoming-draft gap explicitly deferred by PR #14's MVP.

## Why the chain

PR #14 made the NFL similarity engine the dominant composite signal,
but it could only project veterans — its corpus is NFL player-seasons
from nflverse, and rookies (by definition) have no NFL seasons to
vectorize. Until PR #16, rookies fell back on:

- `nfl_draft_capital` — strong single-feature signal (r ≈ 0.4–0.6 vs
  3-year fantasy PPR per RESEARCH §A1), but one-dimensional.
- `cfbd_breakouts` — Breakout Age + College Dominator, useful but a
  thin two-feature summary.

Both are real signal but neither captures _which historical college
profile this rookie most resembles, and what NFL career that profile
produced_. The chain fixes that by:

1. Vectorizing the rookie's most recent college season.
2. K-Nearest-Neighbor against the cached NCAA corpus.
3. Resolving each comp through the `ncaa_to_nfl.json` bridge to its
   realized NFL career.
4. Aggregating those NFL careers (weighted by college similarity,
   time-discounted at 5%/yr) into a 0..100 `rookie_dynasty_value`.

## Pipeline (Mermaid)

```mermaid
flowchart LR
    cfbfastR[cfbfastR-data PBP CSVs<br/>2014-2025]
    rosterCSV[cfbfastR rosters<br/>position + class year]
    pfr[nflverse PFR seasons<br/>1999-2024]
    pfrPlayers[nflverse players table<br/>gsis_id, college, rookie_season]

    cfbfastR --> agg[Stream-aggregate per<br/>player-per-season]
    rosterCSV --> agg
    agg --> ncaa[data/historical_ncaa_football/<br/>season_YYYY.json]

    ncaa --> vec[vectorize_college_football_season<br/>z-score per position]
    vec --> knn[KNN at same position<br/>same class year]
    knn --> comps[Top-K college comps]

    ncaa --> bridge[Bridge builder<br/>(name, college, rookie_season)]
    pfrPlayers --> bridge
    bridge --> bridgejson[data/bridge/ncaa_to_nfl.json]

    bridgejson --> resolve[Resolve each comp<br/>-> NFL gsis_id]
    pfr --> nflcareer[NFL career index<br/>per gsis_id]
    nflcareer --> resolve
    comps --> resolve

    resolve --> proj[Aggregate weighted<br/>time-discounted 5%/yr<br/>career_seasons, lifetime_ppr]
    proj --> rookieVal[rookie_dynasty_value<br/>0..100 per position]

    rookieVal --> blend{NFL seasons played?}
    blend -->|0| pure[Emit raw rookie value]
    blend -->|1| mix[Blend 0.5 / 0.5 with<br/>similarity_career_arc value]
    blend -->|>=2| skip[Don't emit -<br/>PR #14 owns this player]

    pure --> composite[Composite scorer]
    mix --> composite
```

## Data sources

### NCAA corpus (`src/dynasty/sources/historical_ncaa_football.py`)

| Field | Description |
|-------|-------------|
| `cfb_player_id` | ESPN athlete_id from cfbfastR |
| `season` | Calendar year of the college season |
| `name`, `team`, `conference` | From cfbfastR PBP |
| `conference_tier` | P5 / G5_top / G5 / FCS (see below) |
| `class_year` | FR / SO / JR / SR (from roster) |
| `position` | QB / RB / WR / TE (roster, with PBP fallback) |
| `games` | Distinct game_ids the player appeared in |
| `pass_*`, `rush_*`, `rec_*` | Stat-line counts |
| `scrimmage_yds`, `scrimmage_td` | Derived |

Conference tier multipliers (applied to per-game features so a 1000
rec-yd SEC season isn't comparable to the same line at FCS):

| Tier | Multiplier | Examples |
|------|-----------|----------|
| P5 | 1.00 | ACC, Big 12, Big Ten, Pac-12, SEC, FBS Indep |
| G5 top | 0.85 | American Athletic, Mountain West, Sun Belt |
| G5 | 0.75 | Conference USA, Mid-American |
| FCS | 0.65 | Everything else |

Aggregation logic (PBP → season totals):

- **Passing:** completion / incompletion / interception_thrown /
  sack_taken events → per-play accumulators on the `completion_player_id`
  / `incompletion_player_id` / etc.
- **Rushing:** `rush_player_id` → carries + yards.
- **Receiving:** `reception_player_id` → catches + yards. `target_player_id`
  → targets (we add a target even on incompletions where the receiver
  was the target).
- **TDs:** keyed off `touchdown_stat == 1` (cfbfastR's
  `touchdown_player_id` is inconsistent on passing TDs). Receiver gets
  `rec_td`, completer gets `pass_td`. Rusher gets `rush_td`.

Per-season cap: top 4000 by a unified value score
(`scrimmage_yds + 0.5*pass_yds + 20*(pass_td + scrimmage_td)`) so QBs
and skill positions both qualify.

### College→NFL bridge (`src/dynasty/similarity/bridge.py`)

The bridge maps each `cfb_player_id` → `nfl_pfr_player_id` (`gsis_id`)
via three matching strategies, in priority order:

1. **`(name, college, rookie_season ± 1yr)`** — strongest. Normalizes
   names (lowercased, jr/sr/iii stripped) and college names through a
   small alias table (USC ↔ Southern California, UCF ↔ Central
   Florida, etc.). The plausible rookie window is
   `{last_college_season, +1, +2}` to allow for redshirts.
2. **`(name, rookie_season ± 1yr)`** — fallback when school strings
   disagree. Conservative: single-candidate matches only.
3. **`(last_name + first_initial, college, rookie_season ± 1yr)`** —
   catches first-name shorthand mismatches (Mitch ↔ Mitchell, Bobby ↔
   Robert).

Unmatched college seasons get `nfl_pfr_player_id = null`. They still
contribute to the projection — as "out-of-NFL-after-college" comps,
they pull the `nfl_hit_rate` down and contribute zero to lifetime PPR.

### NFL career index (`src/dynasty/similarity/rookie_projection.py`)

For each NFL player in the PFR corpus, aggregates:

- `seasons` — total NFL seasons in the cache
- `career_ppr` — lifetime sum of `fantasy_points_ppr`
- `career_standard` — lifetime sum of `fantasy_points`
- `last_season`, `first_season`, `position` (modal across seasons)

**Active-career extrapolation.** A 4-year vet still in the league
would otherwise be under-counted vs a retired vet with the same talent.
We project still-active comps (last_season ≥ max_known_season − 1) up
to position-typical full career length, discounted by 0.75 to reflect
that they might wash out:

| Position | Typical full career length |
|----------|--------------------------|
| QB | 12 seasons |
| RB | 6 seasons |
| WR | 10 seasons |
| TE | 9 seasons |

## Vectorization

`vectorize_college_football_season()` in
`src/dynasty/similarity/vectorize.py`. Per-position features (all
conference-multiplied where they're production rates):

- **QB**: pass_yds/G, pass_td/G, int/G, completion%, YPA, ANY/A proxy
  (`(pass_yds + 20*pass_td − 45*int) / pass_att`), rush_yds/G,
  rush_td/G, class_ord (1-4 for FR/SO/JR/SR), conf_mult.
- **RB**: rush_att/G, rush_yds/G, YPC, rush_td/G, rec/G, rec_yds/G,
  scrimmage_td/G, class_ord, conf_mult.
- **WR/TE**: rec/G, rec_yds/G, rec_td/G, YPC, target_share_proxy
  (`min(1, targets/G / 12)`), dominator_proxy
  (`min(1, (rec_yds + 20*rec_td) / 1600) * conf_mult`), class_ord,
  conf_mult.

Features are z-score normalized within position across the NCAA corpus.

## Similarity search

Cosine similarity (same kernel as PR #14's NFL engine), restricted to:

- Same position.
- Same class year (or one-step neighbor at 0.7× weight). A FR can comp
  against another FR at full weight, or against a SO at 0.7×.
- Strictly historical seasons (`comp.season < query.season`).
- Top-K (default 20).

## Projection

- `proj_career_seasons = weighted_avg(comp_realized_seasons)`
- `proj_lifetime_ppr = weighted_avg(comp_realized_career_ppr)`
- `nfl_hit_rate = weighted_avg(1.0 if comp_has_nfl else 0.0)`
- `proj_discounted_ppr = sum_{y=1..N}(per_year_ppr / (1.05)^y)`
- `rookie_dynasty_value` = per-position rescale of
  `proj_discounted_ppr` to [0, 100].

## Composite integration

`rookie_similarity_chain` source emits based on realized NFL seasons:

| NFL seasons | Emission |
|-------------|----------|
| 0 | Pure `rookie_dynasty_value` |
| 1 | Blend `0.5 × rookie_value + 0.5 × nfl_value` |
| ≥ 2 | Skip — PR #14's `similarity_career_arc` owns this player |

The NFL value comes from PR #14's
`data/similarity_comps_cache.json` (per-player NFL similarity output).

Default weight 1.6 (just under PR #14's 1.8 since the bridge adds one
layer of indirection).

## Site UX

- Per-player page: new card "Top 5 college comparables with realized
  NFL careers" alongside the existing NFL-comp card. Each comp shows
  similarity score and either the bridged NFL career (`Trevor Lawrence
  (10 seasons, 2257 career PPR)`) or "did not reach NFL."
- `/rankings.html`: new "Rookies / prospects only" filter checkbox.

## Tests

See `tests/test_rookie_similarity_football.py`:

| Test | What it gates |
|------|---------------|
| `test_ncaa_corpus_size_and_shape` | Corpus ≥10K rows, year range, schema |
| `test_bridge_coverage_minimum` | Bridge ≥75% of FBS post-2017 rookies |
| `test_college_vectorization_deterministic` | Vector stability + order-independence |
| `test_top_qb_prospect_projects_significant_nfl_career` | Caleb Williams 2022 ≥5 seasons, ≥60% hit rate |
| `test_udfa_or_late_round_player_projects_short_career` | UDFA WR ≤3 seasons |
| `test_rookie_value_higher_than_udfa` | Elite > UDFA after per-pos rescale |
| `test_top_qb_real_comps_known_nfl_qbs` | Caleb Williams comps include ≥2 known NFL QBs |
| `test_blend_logic_pure_rookie_and_one_nfl_season` | Blend math + cache lookup |
| `test_pr14_luke_grimm_coverage_penalty_intact` | Single-source player can't break top 50 |

## Known limitations

- **NCAA corpus only goes back to 2014.** cfbfastR-data limit. The
  CollegeFootballData.com integration (longer history, more granular
  per-season stats) is filed as PR #17.
- **Bridge nickname misses.** ~5% of FBS post-2017 rookies fail the
  bridge due to first-name shorthand mismatches that the last-initial
  fallback doesn't catch (e.g. Tutu Atwell vs Chatarius Atwell). A
  curated nickname table is filed as a follow-up.
- **PR #15 (VORP / SF-aware) not yet merged into upstream/main.** When
  it lands, the rookie engine will compose at the scoring layer with
  no engine changes — the NFL similarity values it blends already
  inherit PR #15's format-awareness.
