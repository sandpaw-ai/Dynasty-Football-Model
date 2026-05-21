# Elite-proven veteran calibration (v0.18.0)

> **TL;DR.** Proven-elite veterans whose recent 2-3 seasons are
> statistically down (Mahomes 2023-24, Lamar 2022-23 missed games,
> Burrow's 2023 wrist) were being suppressed by the PR #15 / PR #17
> projection layer. PR #18 adds a narrow elite-proven classifier and
> blends in a peak-tilted self-projection plus a career-pace floor,
> so the model respects 5+ seasons of elite production rather than
> overweighting the last two.

## The problem

PR #15 introduced a self-projection floor (blend = `0.55 \u00d7 recent_3yr
avg + 0.45 \u00d7 KNN projection`) that fixed the KNN engine's
under-projection of veteran starters whose same-age comps in the
1999-2024 corpus retired early.

PR #17 introduced cohort-filtered + percentile-tiered KNN that
correctly comp'd Mahomes at csn=7 to other 7-season-deep elite QBs.

Both fixes were architecturally correct, but the resulting projection
still landed Patrick Mahomes at **sf_ppr rank #35** \u2014 because:

1. The self-projection floor used Mahomes' RECENT 3 seasons (2022,
   2023, 2024). His 2023-24 PPR (~280) is a real down stretch by his
   own standards, dragging the floor DOWN, not lifting it.
2. The cumulative-arc cohort vector did not weight track-record
   duration enough \u2014 a 5+ season elite-tier QB should get a
   "proven elite" prior that's resistant to 1-2 down seasons.

Phil's directive (2026-05-21):

> "Mahomes is still consensus-top-5 in superflex because his FLOOR is
> enormous (24-pt rushing-floor + elite passing + KC offense) and his
> elite-tier proven track record is being suppressed by recent stat
> decline. The model should respect 5+ seasons of elite production
> more than it currently does."

## Detection: who counts as ELITE_PROVEN?

A player at their query season is flagged ELITE_PROVEN if and only if
ALL of the following hold:

1. **`career_season_number >= csn_threshold`** (default 5)
2. **Cumulative-career re-scored fantasy points `>=` the CSN-cohort
   p85** \u2014 i.e. against historical players at the same position who
   reached at least `csn` seasons, measured by their
   cumulative-through-`csn`-N fantasy total.
3. **Peak single-season fantasy points `>=` the historical
   position-pool p90** \u2014 the full historical distribution of peak
   seasons at that position.
4. **Position is enabled.** `position_peak_weight[position] is not None`.

CSN-cohort normalization is the key design choice. A raw "top 15% of
all QB careers" threshold demands a long career to clear; that's the
wrong bar for a 5-7 season QB. By comparing Mahomes at csn=7 only
against historical QBs who reached at least csn=7, we ask the right
question: "is his career-to-this-point in the top 15% of all
'7-year-deep QB careers' on record?"

### Why all three criteria?

| Filter | Defends against |
|---|---|
| csn >= 5 | Young breakout (Jordan Love csn=4 \u2014 not yet proven) |
| cumulative >= p85 (csn-cohort) | Journeyman with one good year (Sam Howell) |
| peak >= p90 (pos) | Long-career low-ceiling QB (Tyrod Taylor, csn=8 but never an elite peak) |
| position enabled | RB cliff is real \u2014 RB recent decline IS predictive |

Position thresholds in our 1999-2024 corpus:

| Position | csn=5 cum p85 | Peak p90 |
|---|---:|---:|
| QB | ~1216 | ~309 |
| WR | ~1021 | ~257 |
| TE | ~ 528 | ~177 |
| RB | (disabled) | (disabled) |

### Who gets flagged today (sf_ppr)?

QBs flagged: Mahomes, Allen, Lamar, Burrow, Hurts, Herbert, Rodgers
(yes \u2014 see "Aging decline" below).

WRs flagged: Hill, Adams, Justin Jefferson, Brian Thomas (the new
elite-young cohort that already clears the bars).

TEs flagged: Kelce.

RBs flagged: none (position disabled).

Excluded (correctly): Tua Tagovailoa, Baker Mayfield, Jared Goff
(cum just below cohort p85), Jordan Love (csn=4), Tyrod Taylor (cum
+ peak both well below), Geno Smith (cum well below).

## Adaptive self-projection blend

For ELITE_PROVEN players, the self-projection's base-points changes
from:

```
base_pts = mean(recent_3_seasons_rescored)
```

to:

```
base_pts = recent_weight  \u00d7 mean(recent_3_seasons_rescored)
        + peak_weight    \u00d7 mean(top_3_seasons_rescored)

# Capped at career-best single season \u00d7 1.0 to prevent over-projection
base_pts = min(base_pts, career_peak_single)
```

The `top_3_seasons` is the player's OWN best 3 seasons by re-scored
fantasy points \u2014 NOT a recency window. Mahomes' top 3 are 2018, 2020,
2022; Lamar's are 2019, 2023, 2024; Burrow's are 2021, 2022, 2024.

### Position-specific peak weights

| Position | recent_weight | peak_weight | Rationale |
|---|---:|---:|---|
| QB | 0.30 | 0.70 | Long careers, high single-season variance (team / OL / injuries) |
| WR | 0.45 | 0.55 | Elite WRs sustain into late 30s but cliff is more real than QB |
| TE | 0.45 | 0.55 | Same TE cliff dynamic as WR |
| RB | (disabled) | (disabled) | RB careers cliff hard; recent decline IS signal |

The QB default of `0.70` reflects "trust the long career arc heavily."
The blend then feeds into the same position-aware
self-projection-vs-KNN floor blend introduced in PR #15:

```
proj_total = (1 - floor_weight) \u00d7 KNN_projection
           + floor_weight       \u00d7 self_projection(base_pts)
```

`floor_weight` per-position is unchanged from PR #15 (QB=0.55,
RB=0.35, WR/TE=0.40). The PR #18 change is to the BASE points used
inside `self_projection`, not the floor_weight itself.

## Track-record floor

In addition to the adaptive blend, ELITE_PROVEN players receive a
hard track-record floor on `projected_total_remaining_ppr`:

```
floor = (career_total_fantasy / career_seasons_played)
      \u00d7 projected_remaining_years
      \u00d7 floor_multiplier

projected_total_remaining_ppr = max(proj_total_after_blend, floor)
```

Reads as: "you've averaged X fantasy points per season for Y seasons
of elite track record. Assume at least `floor_multiplier` of that
pace for your projected remaining career."

`floor_multiplier` default: **0.78**.

> The original design value was 0.85. Tuning showed that 0.85
> inflated Mahomes / Allen / Lamar so aggressively that the elite RB
> Bijan Robinson slipped from #15 to #16 in the cross-position
> projection-only ranking, violating the PR #17 RB-top-15 invariant.
> 0.78 preserves the invariant while still moving Mahomes from #35
> (PR #15 baseline) into the top 5-7 range in the projection layer.
> The composite layer then adds market sources on top.

The floor never LOWERS the projection \u2014 it only raises it. If
post-blend `proj_total` already exceeds the floor (e.g. a healthy
recent year set), nothing changes.

## Aging decline still wins

A subtle but important property: `floor = career_pace \u00d7
projected_remaining_years \u00d7 floor_multiplier` is multiplicative in
`projected_remaining_years`. For Aaron Rodgers at age 41 (csn=16+),
the KNN cohort projects ~1 remaining year. Even though Rodgers IS
flagged ELITE_PROVEN (his historical career is unambiguously elite),
his floor collapses to ~`career_pace \u00d7 1 \u00d7 0.78` \u2014 small relative
to a young QB's `career_pace \u00d7 6 \u00d7 0.78`.

Net effect: Rodgers stays deep in the pool. The aging-decline signal
survives the elite_proven flag, because elite_proven only protects
against transient down years \u2014 not against approaching the end of a
career.

## Position-specific calibration summary

| Position | Detection | Blend | Floor |
|---|---|---|---|
| QB | csn>=5 + cum>=p85 + peak>=p90 | 0.30 recent / 0.70 peak | career_pace \u00d7 yrs \u00d7 0.78 |
| WR | csn>=5 + cum>=p85 + peak>=p90 | 0.45 recent / 0.55 peak | career_pace \u00d7 yrs \u00d7 0.78 |
| TE | csn>=5 + cum>=p85 + peak>=p90 | 0.45 recent / 0.55 peak | career_pace \u00d7 yrs \u00d7 0.78 |
| RB | DISABLED | PR #15 recent-only blend | (no elite_proven floor) |

## Configuration

All knobs live in `src/dynasty/composite_weights.py::ELITE_PROVEN_CONFIG`.
Future calibration is a config tweak, not a code change:

```python
ELITE_PROVEN_CONFIG = {
    "csn_threshold": 5,
    "cumulative_percentile_threshold": 0.85,
    "peak_percentile_threshold": 0.90,
    "recent_weight": 0.30,
    "peak_weight": 0.70,
    "floor_multiplier": 0.78,
    "position_peak_weight": {
        "QB": 0.70, "WR": 0.55, "TE": 0.55, "RB": None,
    },
}
```

## Expected output movement (sf_ppr projection-only)

| Player | PR #15 rank | PR #17 rank | PR #18 rank | Why |
|---|---:|---:|---:|---|
| Patrick Mahomes | #35 (#94 in v0.14) | ~#35 | top 5-7 | Elite_proven peak blend + floor |
| Josh Allen | #1 | #1 | #1 | Already #1, marginal lift |
| Lamar Jackson | top 10 | top 10 | top 6 | Elite_proven peak floor catches MVP years |
| Joe Burrow | top 15 | top 15 | top 8 | Floor catches injury-year suppression |
| Justin Herbert | top 15 | top 15 | top 5 | Genuine elite track record now respected |
| Jordan Love | #7 | #20 | ~#24 | NOT elite_proven (csn=4) \u2014 stays at PR #17 baseline |
| Aaron Rodgers | deep | deep | deep | Elite_proven flag set, but remaining_yrs near 0 |
| Bijan Robinson | top 15 | #15 | #15 | RB disabled \u2014 PR #17 baseline preserved |
| Christian McCaffrey | deep | deep | deep | RB disabled \u2014 recent decline still penalized |
| Tyrod Taylor | deep | deep | deep | Long csn but never elite cum/peak |
| Luke Grimm | #500+ | #500+ | #500+ | Coverage-penalty + Bayesian prior intact |

## Edge cases & gotchas

1. **The CSN-cohort threshold can be sparse at very high csn.** A QB
   at csn=22 (very rare) might have no historical comps. The
   implementation falls back to the highest-available `csn` bucket
   for that position.

2. **Peak 3-year average can be < recent 3-year average.** For a
   player who's improving (rare in the proven-elite cohort but
   possible), peak might equal recent and the blend doesn't add a
   lift. That's correct: the elite-proven mechanism only helps when
   the player has been better in the past than they are now.

3. **The cap at `career_peak_single`.** Without this, a player whose
   best 3 seasons all approached 400 PPR could project to a base of
   ~395; multiplied by remaining years that's >2400 lifetime PPR
   which exceeds any realistic ceiling. The cap floors `base_pts` at
   their actual peak season.

4. **RB elite-proven is deliberately disabled, not just weighted
   low.** Setting `peak_weight=0.20` would still inflate McCaffrey's
   projection. The clean signal here is: RB cliff arrives early and
   sharply; recent decline IS predictive. A separate RB-specific
   mechanism would need different criteria entirely (workload-share,
   pass-catching, age vs csn dynamics) \u2014 out of scope for PR #18.

5. **The composite vs the projection layer.** The composite scorer
   (`scoring.py`) consumes `dynasty_value` from the projection and
   re-weights it against market sources (FantasyCalc, DynastyProcess,
   nfl_impact). Projection-layer ranking != composite ranking. The
   PR #18 tests pin projection-layer behavior; the existing
   PR #15 `test_vorp_format_aware.py` tests pin composite-layer
   behavior, and both must stay green.

## Related docs

- `docs/SIMILARITY-METHODOLOGY.md` \u2014 the v0.14 similarity engine
- `docs/VORP-METHODOLOGY.md` \u2014 the v0.15 VORP + format-aware composite
- `docs/CUMULATIVE-ARC.md` \u2014 the v0.17 cumulative-career-arc cohort + tier
- `docs/CHANGELOG-model.md` \u2014 v0.18.0 entry summarizing the directional
  output movement
