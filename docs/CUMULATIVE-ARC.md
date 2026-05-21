# Cumulative Career Arc — Technical Writeup (v0.17.0, PR #17)

> Phil's directive (2026-05-21):
>
> *"The similarity scores need an adjustment. Look at Puka Nacua for
> example. There is no reason he should ever be compared to Jarrett
> Boykin. Nacua has 1715 yards and 10TDs in 2025 at age 24. The
> calculation should look like this for Nacua — Which historical
> players have had 4191 yards through 3 seasons in the NFL at his age.
> That type of analysis should be applied across every player. and
> thought about from a fantasy production lens"*

This doc explains the cumulative-career-arc engine that replaced
single-season-snapshot KNN as the primary similarity signal for
multi-NFL-season players.

## 1. The pathology PR #17 fixes

The v0.14 similarity engine vectorized one season at a time —
per-game rates plus YoY trajectory deltas — then KNN'd against the
historical corpus filtered by `position == query.position` and
`abs(age − query.age) ≤ 1`. Two players with categorically different
career production could end up matched on per-game shape:

| Player              | Age | NFL Yr | Career Rec Yds (through age) | This Yr Rec Yds | GP |
|---------------------|----:|-------:|-----------------------------:|----------------:|---:|
| Puka Nacua 2024     |  23 |   2    |                        2,476 |             990 | 11 |
| Jarrett Boykin 2013 |  24 |   2    |                           27 |             681 | 12 |

They both averaged ~80–90 receiving yards/game as starters at age
23–24 in their 2nd NFL season. But Nacua's career-to-date production
was 35× Boykin's. The v0.14 vector couldn't see this because it only
encoded the current season's per-game shape.

Phil's framing: "Which historical players have had 4,191 yards through
3 seasons in the NFL at his age?" That's a cumulative-through-age
question, not a per-game-shape question.

## 2. Cumulative vector construction

`vectorize_career_through_age(player_id, age, scoring_format)` returns
a `CareerArcVector` whose `raw_features` is a position-keyed dict.

Per-position feature schemas (order is stable):

**QB**: `career_pass_yds`, `career_pass_td`, `career_int`,
`career_rush_yds`, `career_rush_td`, `career_fantasy`,
`peak_fantasy`, `career_gs`, `career_durability`,
`fantasy_per_season`, `slope`, `peak_age_norm`,
`decayed_pass_yds`, `decayed_pass_td`, `decayed_rush_yds`,
`decayed_fantasy`.

**RB**: `career_rush_att`, `career_rush_yds`, `career_rush_td`,
`career_rec`, `career_rec_yds`, `career_scrimmage_yds`,
`career_scrimmage_td`, `career_fantasy`, `peak_fantasy`,
`career_gs`, `career_ypc`, `fantasy_per_season`, `slope`,
`peak_age_norm`, `decayed_rush_yds`, `decayed_rec_yds`,
`decayed_fantasy`.

**WR / TE**: `career_tgt`, `career_rec`, `career_rec_yds`,
`career_rec_td`, `career_tgt_per_season`, `career_fantasy`,
`peak_fantasy`, `career_gs`, `career_ypr`, `fantasy_per_season`,
`slope`, `peak_age_norm`, `decayed_rec_yds`, `decayed_rec`,
`decayed_fantasy`.

### Time-decay aggregation

The `decayed_*` features apply the following weight curve *inside*
the cumulative roll-up. Index = seasons-back-from-most-recent.

| Index | Weight |
|------:|-------:|
|     0 |  1.00  |
|     1 |  0.70  |
|     2 |  0.50  |
|    3+ |  0.35  |

So Nacua 2024 contributes 1.0× to the decayed aggregates and Nacua
2023 contributes 0.7×. The un-decayed `career_*` features stay as
absolute totals — the cumulative vector encodes BOTH the absolute
career floor and the recency-tilted trajectory simultaneously.

### Trajectory features

- `slope` — least-squares slope of per-season fantasy points across
  the career so far (intercept-free, mean-centered). Rising arcs are
  positive; declining arcs negative. Zero for 1-season players.
- `peak_age_norm` — `(peak_season_age − 25) / 10`. Normalized to roughly
  match the magnitude band of other features pre-z-score.
- `peak_fantasy` — the player's best single-season fantasy total in
  the cumulative window. Re-scored under the active format.

### Z-score normalization

`compute_cumulative_zscore_stats(arcs)` computes per-(position,
feature) (mean, stdev) across the *whole cumulative corpus* (every
through-age checkpoint for every player). Each query's
`vectorize_cumulative(arc, stats)` returns the z-score-normalized
tuple suitable for cosine similarity.

## 3. Cohort indexing

`build_cohort_index(corpus, league_format)` builds:

- A bucket map keyed by `(position, age_int, career_season_number)`
  whose values are lists of arc indices into the cumulative-corpus
  array.
- A per-bucket sorted list of `career_fantasy` values (for fast
  percentile lookup).
- The cumulative z-score stats.

`career_season_number` is the count of qualifying corpus seasons
(min_games=4) for the player through the given age. It IS NOT the
elapsed years since they were drafted — that distinction matters for
players with redshirt/IR years like Christian McCaffrey 2020.

`age_bucket(age)` truncates to int, so age 23.x → 23. Matches PFR
convention.

## 4. Cohort filter

For a query player at age A with N qualifying NFL seasons:

1. Look up buckets within `(position, A−1..A+1, N−1..N+1)`.
2. Exclude the query player themself.
3. Exclude seasons at or after `query.season` (historical comps only).
4. If fewer than `MIN_COHORT_COMPS=10` remain, widen age to ±2 then ±3.
5. If still under threshold, the engine falls back to snapshot-only
   KNN with no cohort or percentile filtering (rare; ~<2% of players).

## 5. Percentile-tier matching

Within the cohort, compute the query's percentile by `career_fantasy`
(re-scored under the active format). Restrict KNN to comps within a
band of the query:

| Query percentile | Band (±) |
|-----------------:|---------:|
|     ≥ 90 (elite) |   15 pp  |
|       40–90 (mid)|   20 pp  |
|       < 40 (low) |   25 pp  |

Elite tier gets the tightest band so a top-5% age-24 WR (Nacua-equivalent)
can only comp to WRs in p80–p100 of that exact cohort. Low-tier
players widen so they still surface 10+ comps.

## 6. Two-vector KNN blend

Final similarity per comp is:

```
sim = w_cum × cosine(query_cum, comp_cum)
    + (1 − w_cum) × cosine(query_snap, comp_snap)
```

Blend curve by `career_season_number`:

| Career season # | w_cum |
|---:|---:|
| 1  | 0.0 |
| 2  | 0.5 |
| 3+ | 0.7 |

Rationale: 1 NFL season is too few data points to compute a
meaningful trajectory or peak-fantasy feature — the cumulative vector
collapses to "one season's totals", which is exactly what the
snapshot vector already covers. So rookies use 100% snapshot. By the
3rd season the cumulative vector dominates because it now encodes a
real arc (career-to-date totals, slope, peak age, time-decay
aggregates).

## 7. Diagnostics

`find_comparables_cohort` returns a `(comps, diag)` tuple. The
`diag` dict exposes everything the report builder logs about the
filter stages:

```python
{
    "player_id": "00-0039075",
    "player_name": "Puka Nacua",
    "position": "WR",
    "query_age": 23.26,
    "career_season_number": 2,
    "used_blend_weight": 0.5,
    "cohort_size_raw": 1358,
    "cohort_size_after_percentile": 262,
    "query_percentile": 95.7,
    "percentile_band": 15.0,
    "widened_age_window": 1,
    "fallback_snapshot_only": False,
}
```

`project_all_active_players(collect_diagnostics=True)` collects every
player's diagnostic into the module-global
`_LAST_PROJECTION_DIAGNOSTICS`. The launcher prints aggregate stats
(fallback rate, mean cohort size) to CI logs.

## 8. Composition with PR #16 (rookie college→NFL chain)

PR #16 (pending merge) handles players with **zero NFL seasons** via a
college-similarity-to-NFL bridge. PR #17 handles players with 1+ NFL
seasons via cumulative-arc + cohort filter. The layers compose: a
rookie's 1st NFL season triggers the snapshot-only fallback in PR
#17, which is exactly what was happening pre-#17 anyway. PR #16's
output never enters the PR #17 pipeline (different code path entirely).

If PR #16 merges before PR #17, the rebase touches the same files
(vectorize / comparables / projection). The cumulative-arc additions
are additive — they don't remove or modify the snapshot vector or
the legacy `find_comparables` function. The rebase is mostly
conflict-free; the only risk is at the import / fixture-list level
in the test files.

## 9. Performance

The cohort index build runs once per format per projection (~10k
arcs, ~560 buckets, ~1.5s on the dev box). Per-query KNN over the
filtered cohort (~200–1500 candidates) is ~5–10ms. The full
`project_all_active_players` run is ~5s for 490+ players across both
formats. Acceptable for the daily CI build.

## 10. References

- `src/dynasty/similarity/vectorize.py` — cumulative vector + corpus build
- `src/dynasty/similarity/comparables.py` — cohort index + filter + blend KNN
- `src/dynasty/similarity/projection.py` — projection pipeline wiring
- `tests/test_cumulative_career_arc.py` — pinned tests
- `docs/CHANGELOG-model.md` — v0.17.0 entry + BEFORE/AFTER comp lists
