# Positional VORP + Format-Aware Scoring

*Companion to* [CHANGELOG-model.md](CHANGELOG-model.md) *v0.15.0 (PR #15)*

## Why this exists

Dynasty fantasy football is a positional cap-space problem, not a raw
fantasy-points contest. The 12-team dynasty SF league asks you to
field 2 QBs, 3 RBs, 4 WRs, 1 TE every week. That asymmetry — there
are 24 startable QBs vs 48 startable WRs — is the entire reason
Mahomes is worth a #1 dynasty pick in SF and a #25 pick in 1QB. A
ranking model that mixes positions without controlling for it will
rank a journeyman WR ahead of an elite QB whenever the WR's raw
projection is higher.

The v0.14 model had this bug. The v0.15 fix uses Value Over
Replacement Player (VORP), which has been the standard valuation
primitive in baseball analytics for 20+ years, adapted for the
dynasty fantasy roster shape.

## Replacement baselines

For each (league format, position) we compute the *replacement-level
baseline* as the Nth-best projected lifetime fantasy points across
the current active-player pool, where N is dictated by league
construction:

| Format          | QB | RB | WR | TE |
|-----------------|---:|---:|---:|---:|
| sf_ppr          | 24 | 36 | 48 | 12 |
| 1qb_ppr         | 12 | 36 | 48 | 12 |
| sf_te_premium   | 24 | 36 | 36 | 24 |
| sf_ppr_redraft  | 24 | 36 | 48 | 12 |

The intuition: in SF you start 2 QBs/team × 12 teams = 24 QBs total,
so the worst startable QB is whatever player is 24th-best on
projection. In 1QB you start 1 × 12 = 12, so replacement is the
12th-best QB and the cliff is much shallower.

We sort the player pool by `projected_discounted_ppr` per position,
take the Nth entry, and that's the baseline. A player's raw VORP
is `projected_discounted_ppr − baseline`, so sub-replacement players
have negative VORP (and rank near the bottom of the format) while
top-tier players have large positive VORP.

We deliberately do NOT floor VORP at 0 in the rank-order calculation
— that would compress the bottom of the pool unhelpfully. We DO
shift the final `dynasty_value` so the minimum lands at 0 and the
maximum at 100 (cross-position).

## Scarcity-cliff multiplier

A second knob captures how steep the tier dropoff is between the
last starter and the next 6 players. Steep cliff → multiplier > 1,
amplifying the VORP gap for that position.

```
top_avg = average projected_discounted_ppr of top N starters
cliff_avg = average projected_discounted_ppr of the next 6 (post-replacement)
steepness = (top_avg − cliff_avg) / max(cliff_avg, 1)
multiplier = clamp(1.0 + 0.3 × steepness, 1.0, 1.5)
```

Typical computed values in the May 2026 player pool:

| Position | sf_ppr | 1qb_ppr |
|----------|-------:|--------:|
| QB       |   1.20 |    1.08 |
| RB       |   1.22 |    1.22 |
| WR       |   1.19 |    1.19 |
| TE       |   1.06 |    1.06 |

The QB cliff is dramatically steeper in SF than in 1QB, exactly as
expected: in SF the top 24 QBs each consume valuable roster space,
so the gap between the elite 6 and the rest amplifies; in 1QB you
only need 12 starting QBs total and the next 12 are cheap streamers
who can fill in.

## Format-aware comp re-scoring

The KNN comparable engine pulls the top-20 historical comp seasons
at the same position and age. Each comp has a realized future career
in the corpus (seasons after the comp season). The v0.14 model
projected each comp's future by summing their stored
`fantasy_points_ppr` field — which is whatever scoring was applied
at the time the data was compiled.

That's wrong for two reasons:

1. The corpus spans 1999–2024. Scoring conventions have drifted over
   that window (PPR adoption, pass-TD value changes, TE-premium).
2. We project under a SPECIFIC league_format — the same comp should
   project differently for sf_ppr vs sf_te_premium because their
   per-stat coefficients differ.

The fix lives in `src/dynasty/scoring_rules.py`: a `LEAGUE_SCORING`
dict maps each format to per-stat coefficients (passing_yards,
passing_tds, interceptions, rushing/receiving stats, fumbles, 2pt
conversions, special-teams TDs). The `score_season(raw, format,
position)` function re-scores any historical row under any format's
rules.

In the projection layer, `_rescored_remaining_after(comp_id,
comp_season, by_pid, league_format)` walks every future season of
the comp and re-scores it under the active format. The aggregate is
what feeds into VORP.

For sf_ppr vs 1qb_ppr the per-stat coefficients happen to be
identical (both dynasty-default 4pt-pass-TD PPR), so the comp
re-scoring produces identical numbers for those two formats. The
format difference comes from VORP only (different replacement
baselines + scarcity multipliers).

For sf_te_premium the re-scoring DOES differ: TE receptions earn
1.5 points instead of 1.0, which lifts TE comp projections
materially.

## Self-projection floor

The KNN engine has a structural weakness: same-age elite comps are
rare. Josh Allen at age 28 searches the 1999-2024 corpus for
high-volume mobile QBs at age 28, and the corpus contains roughly:
peak-Brady, peak-Manning, Cam Newton in his late-career decline,
Jake Plummer three years from retirement, Joshua Dobbs as a
journeyman. The KNN draws comps from across that distribution and
the projection regresses Allen to a mediocre future career.

This is a corpus-skew problem, not a method bug. We mitigate it
with a self-projection floor: take the player's own recent 2-3
seasons, re-score them under the active format, then project
forward with a position-specific decay curve:

| Position | Decay/year | Expected remaining years | Floor blend weight |
|----------|-----------:|-------------------------:|-------------------:|
| QB       |       0.94 |           max(1, 37 − age) |               0.55 |
| RB       |       0.85 |           max(1, 30 − age) |               0.35 |
| WR       |       0.92 |           max(1, 34 − age) |               0.40 |
| TE       |       0.92 |           max(1, 34 − age) |               0.40 |

Final projection:

```
projected_total_remaining_ppr = (1 − w) × KNN_projection + w × self_projection
```

The blend weight w is tuned per position. QBs get the heaviest
self-projection lean (0.55) because the KNN miss is largest for
them. RBs get the lightest (0.35) because their realized future
careers are short and KNN's mortality signal is genuinely valuable.

## Format-aware composite weights

On top of VORP, the composite scorer multiplies each source's
`default_weight` by a per-(format, position, source) factor. Lives
in `src/dynasty/composite_weights.py`.

The full active override table:

| Format          | Position | Source               | Multiplier |
|-----------------|----------|----------------------|-----------:|
| sf_ppr          | QB       | similarity_career_arc| × 1.333    |
| sf_ppr          | QB       | nfl_impact           | × 2.500    |
| sf_ppr          | QB       | fantasycalc          | × 3.000    |
| sf_ppr          | QB       | dynastyprocess       | × 4.000    |
| sf_ppr          | QB       | brainy_ballers       | × 2.000    |
| sf_ppr          | QB       | nfl_draft_capital    | × 2.000    |
| 1qb_ppr         | QB       | similarity_career_arc| × 0.778    |
| 1qb_ppr         | QB       | nfl_impact           | × 1.000    |
| 1qb_ppr         | QB       | fantasycalc          | × 0.750    |
| 1qb_ppr         | QB       | dynastyprocess       | × 0.750    |
| 1qb_ppr         | QB       | brainy_ballers       | × 0.750    |
| sf_te_premium   | QB       | similarity_career_arc| × 1.333    |
| sf_te_premium   | QB       | nfl_impact           | × 2.500    |
| sf_te_premium   | QB       | fantasycalc          | × 2.000    |
| sf_te_premium   | TE       | similarity_career_arc| × 1.111    |
| sf_te_premium   | TE       | nfl_impact           | × 1.125    |

The SF QB block is aggressive intentionally. The dynasty SF QB
premium is large enough that even with VORP doing the heavy lifting
on the projection side, the composite mixer benefits from an
explicit QB lift on the sources that have proven the most accurate
for SF dynasty QB pricing (fantasycalc, dynastyprocess, fantasypros
SF tier).

## Pitfalls to watch

1. **Cross-format projections will sometimes show identical per-stat
   numbers** (sf_ppr and 1qb_ppr share scoring coefficients). The
   format difference shows up via VORP + composite weight overrides,
   not via re-scored comp seasons. This is expected. Don't add
   redundant per-stat overrides for 1QB.

2. **Replacement baselines drift with the player pool.** A weak QB
   draft class can drop the SF QB24 baseline, making mid-tier QBs
   look better via VORP. This is correct behavior, but worth
   surfacing in the methodology page so users understand why the
   numbers shift year-to-year.

3. **The self-projection floor is a corpus-skew patch, not a true
   model improvement.** The right long-term fix is a richer KNN
   vector (3-year rolling profile + tier indicator) that finds
   elite-tier comps more reliably. Folded into a future PR.

4. **Coverage penalty + Bayesian prior interact with VORP rescale.**
   A player with low coverage gets their pre-shift score pulled
   toward the position baseline; if that pull lands them BELOW
   replacement, they don't even appear at the top of their position.
   The Luke Grimm regression test confirms this still works.

5. **The format toggle's URL is by suffix, not query parameter.**
   `rankings.html` (sf_ppr default) vs `rankings_1qb_ppr.html`.
   Bookmarks survive page refreshes; the `<select>` swaps with
   `window.location.href` so the format change is a real navigation,
   not a JS hash change.
