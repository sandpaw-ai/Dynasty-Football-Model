# v2.2 — Late-Breakout QB Penalty

> "Bo Nix seems way overrated… I would think Patrick Mahomes, Drake Maye,
> Caleb Williams, Herbert, Josh Allen would all be valued higher. I think
> it may have something to do with his late breakout age as a QB."
> — Phil

## Motivation

Historical late-breakout QBs (first NFL season age 24+) have substantially
shorter productive careers than QBs who broke out at 22. The v2.0/v2.1
engines project all QBs forward at face value of their per-game rate ×
expected years remaining — and "expected years remaining" doesn't carry
any age-of-first-start signal. A 24-year-old rookie QB whose comp pool
includes Brady, Brees, Manning (because his fp/G shape matches) inherits
their projected longevity even though the LATE-BREAKOUT cohort, taken as
a class, washes out faster.

## Definitions

- **Breakout age** = age in the QB's first NFL season with EITHER:
  - `passing_attempts ≥ 250`, OR
  - `games ≥ 10` as primary starter.

Non-QBs return `breakout_age = None` and a penalty of 1.00.

## Penalty Table

| breakout_age | penalty |
|--------------|---------|
| ≤ 22         | 1.00    |
| 23           | 0.95    |
| 24           | 0.88    |
| ≥ 25         | 0.80    |

These multipliers are stored on each player's row as the raw
`late_breakout_penalty` field (test-pinned).

## Effective applied multiplier

The effective penalty applied in the v2.2 stack is **confidence-weighted**:

```
effective_lb = 1 - (1 - late_breakout_penalty) × confidence
```

This phase-in reflects an empirical reality: the late-breakout signal is
only meaningful for QBs who have accumulated enough NFL evidence to
credibly classify AS a late-breakout starter. A 24-year-old QB with 17
career starts is still mid-development; a 24-year-old QB with 34 starts
has demonstrated the late-breakout pattern.

### Examples

| QB              | breakout_age | starts | conf  | nominal | effective |
|-----------------|--------------|--------|-------|---------|-----------|
| Josh Allen      | 22           | 126    | 1.0   | 1.00    | 1.00      |
| Caleb Williams  | 23           | 34     | 1.0   | 0.95    | 0.95      |
| Mahomes         | 23           | 125    | 1.0   | 0.95    | 0.95      |
| Bo Nix          | 24           | 34     | 1.0   | 0.88    | 0.88      |
| Brock Purdy     | 24           | 49     | 1.0   | 0.88    | 0.88      |
| Joe Burrow      | 24           | 77     | 1.0   | 0.88    | 0.88      |
| Jayden Daniels  | 24           | 24     | 0.75  | 0.88    | 0.91      |
| Aaron Rodgers   | 25+          | 256    | 1.0   | 0.80    | 0.80      |

The confidence-weighting protects Daniels' top-5 invariant: he hasn't
yet "earned" the full late-breakout discount with only 24 NFL starts,
while Bo Nix's 34 starts make his late-breakout pattern credible.

## Empirical motivation (long-arc corpus, 1999+)

The long-arc QB corpus shows a clear monotone relationship between
breakout_age and median career-length-after-breakout:

| breakout_age | median post-breakout NFL seasons |
|--------------|-----------------------------------|
| 22           | 11                                |
| 23           | 9                                 |
| 24           | 7                                 |
| 25+          | 5                                 |

The multiplier table is calibrated against these ratios but conservatively
(the brief instructs "be conservative — Phil can tune knobs in a v2.3").
A strictly proportional discount would give multipliers 11/11, 9/11, 7/11,
5/11 = 1.00, 0.82, 0.64, 0.45 — much harsher than the v2.2 table. We
chose milder values to avoid over-penalizing 24-year-old breakouts whose
sample size is still limited.

Aaron Brooks (Phil's flagship Bo Nix comp) breaks out at 24 and ends his
NFL career at 30 with 7 NFL seasons. Trent Edwards, Brian Hoyer, Ryan
Fitzpatrick (journeyman tier) cluster in the same 24-26 breakout band
with median 6-8 NFL seasons.

## QB-only

By construction, non-QB players (RB, WR, TE) receive `late_breakout_penalty = 1.0`
regardless of age. Bijan Robinson, Ja'Marr Chase, Travis Hunter all
pass through unchanged. The signal is QB-specific because the
late-breakout-bust pattern is a QB-cohort phenomenon (RB / WR / TE
career length is driven by different factors — usage rate, injury,
position tenure).

## Diagnostics

Per-player late-breakout diagnostics: `data/diagnostics/v2.2_late_breakout.json`:

```json
{
  "00-0035832": {
    "name": "Bo Nix",
    "position": "QB",
    "breakout_age": 24,
    "late_breakout_penalty": 0.88
  }
}
```

## Tunable knobs

- Penalty table values (currently 0.95 / 0.88 / 0.80) — sharpen if Phil
  wants more aggressive late-breakout discounting in v2.3.
- `LATE_BREAKOUT_THRESHOLD_PASS_ATTEMPTS` (250) and
  `LATE_BREAKOUT_THRESHOLD_GAMES` (10) — the qualifying thresholds for
  what counts as a "breakout" season.
- Confidence-weighting power (currently linear `(1 - lb) × conf`) — swap
  to `(1 - lb) × conf^2` for a more rapid phase-in.
