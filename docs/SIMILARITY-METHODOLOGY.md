# Similarity Methodology

> v0.14.0 — Phil asked for "similarity scores at the heart of the model,
> where college production should be compared to historically similar
> college players and projected to the pros, and current NFL players
> compared to similar historical players to extrapolate the rest of
> their careers." This doc explains how that works.

## The big idea

A dynasty ranking is fundamentally a projection: how many years of
productive fantasy output does this player have left, and how good will
those years be? Source aggregation (FantasyCalc, ECR, etc.) is a useful
sanity check on the market's read, but it doesn't *explain* anything.
The similarity engine does:

> "Brian Thomas at age 21 looks like DK Metcalf 2020, Keenan Allen 2013,
> and Justin Jefferson 2021. Those three averaged X productive seasons
> after their comp year and Y total fantasy points. That's the basis
> for projecting Brian Thomas's remaining dynasty value."

## Pipeline

```
nflverse player-season corpus (1999-2024, ~33K rows)
       │
       ▼
build_nfl_corpus()         ← filter to QB/RB/WR/TE, min_games=4
       │
       ▼
compute_zscore_stats()     ← per-position (mean, stdev) for each feature
       │
       ▼
For each active NFL player (most recent season >= 2023, games >= 8):
       │
       ▼
find_comparables()         ← top-20 cosine-nearest historical seasons
                              at same position and age ±1yr
       │
       ▼
project_player()           ← weighted aggregate of comp future careers
                              time-discounted 5%/yr
       │
       ▼
rescale_dynasty_values()   ← rescale projected_discounted_ppr 0..100
                              per position
       │
       ▼
SimilarityCareerArc adapter ← emits RankingRecord(market_value=dynasty_value)
                              composite weight: 1.8 (DOMINANT)
```

## Feature vectors (per position)

| Position | Features |
|----------|----------|
| QB | pass_yds/G · pass_TD/G · INT/G · rush_yds/G · rush_TD/G · sack_rate · fantasy_ppr/G · games |
| RB | rush_att/G · rush_yds/G · rush_TD/G · rec/G · rec_yds/G · rec_TD/G · yds_per_touch · fppr/G · games |
| WR | targets/G · rec/G · rec_yds/G · rec_TD/G · target_share · WOPR · yds_per_target · fppr/G · games |
| TE | same shape as WR |

All features are z-score normalized within position across the full
historical corpus. Cosine similarity is computed in the normalized space
so the "scale" of different features doesn't dominate the comparison.

## Age windowing

Comparables MUST be at the same position and within ±1 year of the
query player's age. Why:

- Comparing a 21yo's vector to a 32yo's vector is structurally
  meaningless even if the per-game production looks similar. The 32yo
  has a clipped future career; the 21yo's career is the whole
  projection.
- The age filter is what makes the projection valid: the comp's *future*
  career after their comp season is what gets aggregated, and that
  future has to be apples-to-apples with the query player's remaining
  career.

## Time discount

Comp future careers are summed weighted by similarity. We then convert
the weighted total into a present-value projection at 5%/yr discount:

```
projected_remaining_years = weighted median of comp careers
proj_ppr_per_year = proj_ppr_total / proj_years
discounted_ppr = sum over years of (proj_ppr_per_year / 1.05^year)
```

5%/yr matches the conventional dynasty time-preference (a fantasy point
this year is meaningfully more valuable than one in 5 years, but not
dramatically more).

## Rescaling

`projected_discounted_ppr` is rescaled to 0..100 per position so:

- A top WR and a top RB can both be ~100 (positions normalize separately)
- A 36yo QB with 1-2 comp-projected years isn't crushed by a 24yo QB
  with 8-10 — they end up on the same scale within QB

The final `dynasty_value` in [0, 100] is what the composite consumes
under weight 1.8.

## How the comparables surface to the user

Each player page renders the top 5 comparables, deduplicated by comp
player (only the highest-similarity season per comp player makes the
list). Rankings page rows have a hover tooltip showing the top 3.

The cache is written to `data/similarity_comps_cache.json` keyed by
gsis_id. The site renderer reads it once per build.

## Why nflverse instead of scraping pro-football-reference.com

The task brief originally specified scraping PFR at 3s/page over 45
years. That's a fragile 2-3 minute single-threaded crawl that would
break under CI and add a brittle external dependency. **nflverse**
(https://github.com/nflverse/nflverse-data) is the de-facto open-source
PFR mirror — same underlying data, no rate limit, MIT-licensed, stable
CSV schema published as GitHub releases. We pin two cache files:

- `data/nflverse/player_stats_season.csv.gz` (~2.8MB, 33K player-seasons)
- `data/nflverse/players.csv.gz` (~2.4MB, 24K player bios)

Both are committed. The launcher reads them directly; CI never hits the
network. `refresh_cache()` re-pulls live but is gated behind the
`DYNASTY_FB_PFR_LIVE=1` env var.

## What's deferred to PR #15

- **College similarity engine.** Right now rookies still rely on
  `nfl_draft_capital` + `cfbd_breakouts` for their signal. The college
  side requires (a) a sports-reference college or CollegeFootballData
  corpus, (b) a college→NFL bridge that joins college player-seasons to
  pfr_id via draft year, (c) a parallel vectorization for college
  features, (d) a chain projection: college comps → realized NFL
  careers via the bridge → aggregated dynasty value for the rookie.
- Phil agreed in the task brief that the MVP is "veteran similarity
  engine + Luke Grimm fix" and rookie college engine is a natural
  follow-up.

## Test invariants enforced

See `tests/test_similarity_football.py`:

1. Vectorize is deterministic and order-independent
2. Justin Jefferson 2020's top-10 KNN match includes 3+ recognizable
   high-target young WRs (e.g. DeAndre Hopkins, Mike Evans, etc.)
3. No single-source player ranks in the top 50 (Luke Grimm invariant)
4. 3+ elite young WR/RB profiles rank in the top 30
5. Aaron Rodgers' composite rank > his nfl_impact rank (similarity
   engine penalizes his age) AND his similarity dynasty_value < 50
6. PFR cache exists and covers 1999-2024
