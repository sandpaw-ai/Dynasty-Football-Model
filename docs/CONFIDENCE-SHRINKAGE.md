# v2.2 — Sample-Size Confidence Shrinkage

> "A lot of these issues have a common theme. The model is just taking
> their fantasy points per game and extrapolating. There is not much
> depth of starts or in other words … so sample size of data. The model
> should punish players for that." — Phil

## Motivation

A 5-game-started QB's per-game fp is an unreliable predictor of his
10-year fp trajectory. The v2.0 engine treats Shedeur Sanders' rookie
fp/G the same as a 5-year-veteran's career-average fp/G — both are
extrapolated forward at full weight. v2.2 introduces a Bayesian-style
shrinkage that pulls projections toward a position-tier baseline when
the NFL sample is small.

## Definitions

- **Career NFL starts proxy** = `games_played` for QBs;
  `0.6 × games_played` for RB / WR / TE (backups still play meaningful
  snaps without "starting"). We use a proxy because consistent
  `games_started` data isn't available across the historical corpus.
- **Confidence** = `min(career_starts / 32, 1.0)`.
  32 starts ≈ 2 full modern NFL seasons of starts.
- **QB rule**: QBs with fewer than 16 career starts (< 1 full season)
  additionally cap confidence at 0.5.
- **Position tier baseline** = median `production_score` of the top-50
  players at that position (computed PRE-penalty).

## Formula

```
if (raw × survival) > position_tier_baseline:
    shrunk = (raw × survival) × confidence
           + position_tier_baseline × (1 - confidence)
else:
    shrunk = (raw × survival) × confidence       # straight discount
```

The two-branch formulation is the key v2.2 design choice. The brief's
textbook Bayesian formula `raw × conf + baseline × (1 - conf)` works
correctly only when `raw > baseline`. For a below-baseline projection
the formula would INFLATE the projection toward the median — exactly the
wrong direction. Phil's directive ("punish players for [small sample]")
requires small-sample BUSTS to drop, not get lifted.

Asymmetric shrinkage preserves the Bayesian intuition above the median
while honoring Phil's directive below it.

## Case Studies (sf_ppr)

### Shedeur Sanders — small sample, low projection

- career_starts ≈ 8 → confidence = `min(8/32, 1.0)` = 0.25
- raw projection ≈ 1064 (rookie engine, comp-weighted)
- after survival (0.91): 968
- 968 < QB baseline (~1500) → straight multiply: `968 × 0.25 = 242`
- final ≈ 242 (after lb 1.0 — no late-breakout for rookies)

Rank: ~ #240+ (deep, per Phil's expectation).

### Anthony Richardson — small sample, optimistic projection

- career_starts ≈ 15 → confidence = `min(15/32, 1.0)` = 0.47
  QB rule (< 16 starts) caps at 0.50 — actual confidence = min(0.47, 0.50) = 0.47.
- raw projection ≈ 1806 (high — his comp-weighted projection includes
  some elite-tier comps)
- after survival (0.83): 1499
- 1499 ≈ QB baseline (~1500) → Bayesian pull is mild
  `1499 × 0.47 + 1500 × 0.53 ≈ 1499`
- final ≈ 1490 (after lb 1.0)

Rank: ~ #30 (dropped from v2.1's #23 — meaningful but not punishing).

### Josh Allen — full confidence

- career_starts = 126 → confidence = 1.0
- raw 2224 → after survival 2224 → after confidence 2224 → final 2224

Allen passes through with no shrinkage. Same for Mahomes, Burrow,
Hurts, Lamar.

### Jayden Daniels — partial confidence

- career_starts = 24 → confidence = `24/32 = 0.75`
- raw 2752 → after survival (0.97) 2670
- 2670 > QB baseline (1500) → Bayesian pull:
  `2670 × 0.75 + 1500 × 0.25 = 2378`
- lb (0.88, conf-weighted to 0.91 effective): 2378 × 0.91 ≈ 2164
- final ≈ 2100

Rank: top 5. Confidence shrinkage protects the elite invariant.

## Diagnostics

Per-player confidence diagnostics:
`data/diagnostics/v2.2_confidence.json`:

```json
{
  "00-0038122": {
    "name": "Anthony Richardson",
    "position": "QB",
    "career_nfl_starts": 15,
    "confidence": 0.469,
    "position_tier_baseline": 1502.4
  }
}
```

## Implementation note: rookie engine

For 1-NFL-season rookies routed through the v2.1 rookie engine (Jaxson
Dart, Ashton Jeanty, Cam Ward, Tetairoa McMillan, Travis Hunter):

- The rookie engine ALREADY applies its own games-played confidence
  factor (`FULL_CONFIDENCE_GAMES = 8`).
- For RB / WR / TE rookies we set v2.2 effective_confidence = 1.0 so we
  don't double-penalize them. This preserves the v2.1 invariants
  (Jeanty top 25, Tetairoa top 30).
- For QB rookies (Sanders, Ward, Dart) we still apply v2.2 confidence
  shrinkage because their projection horizon is much longer (10+ year
  QB career) and the rookie engine's games-based confidence doesn't
  capture starts-based career-projection uncertainty. The combined
  shrinkage is what drops Sanders to #240+ as Phil asked.

## Tunable knobs

- `FULL_CONFIDENCE_STARTS` (32) — raise to demand more NFL evidence.
- `QB_MIN_FULL_CONF_STARTS` (16) and `QB_MIN_FULL_CONF_CAP` (0.5) —
  the explicit QB tier-1 cap.
- Position-baseline `top_n` (currently 50) — lower for stricter
  league-average-starter definition.
