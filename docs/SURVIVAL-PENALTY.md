# v2.2 — Survival (Bust-Rate) Penalty

> "If [Anthony Richardson] is comp'd to a lot of QBs that were out of the
> league pretty quickly because they were not good even if they put up
> good fantasy stats, the model should reflect that." — Phil

## Motivation

The v2.0/v2.1 engines project a player forward by similarity-weighting
their top-20 comps' realised post-snapshot fantasy points. If the comp
pool is dominated by short-career bust QBs, the projection still treats
those comps' realised fp as the player's expected future production —
but those comps' realised fp WAS the small-sample-then-out-of-the-league
trajectory. We need to discount accordingly.

## Definitions

For each comp in the top-20 pool:

- `career_length` = number of qualifying NFL seasons (games ≥ 4).
- `final_age` = age in comp's last qualifying season.
- **Bust**: `final_age ≤ 30 AND career_length < 8`.
  (We widened the brief's literal "< 30 AND < 6" to capture the Aaron
  Brooks tier of journeyman QB that Phil specifically called out.)
- **Short career**: `career_length ≤ 5`.
- **Durable**: `career_length ≥ 6`.

Still-active comps are not classified as busts (career not over yet)
but DO count their career-length-to-date in the durability mix.

## Formula

```
survival_multiplier = (1 - bust_rate)       × 0.20
                    + (1 - short_career_rate) × 0.10
                    + 0.70                          # base
```

Clamped to [0.65, 1.00]. The base of 0.70 + the conservative penalty
weights are intentionally mild per the brief's directive
("be CONSERVATIVE on penalty magnitudes").

## Case Studies (sf_ppr)

### Anthony Richardson — bust-heavy pool

Top comps include Mitchell Trubisky, Teddy Bridgewater (post-injury),
RG3 (post-rookie), Tyrod Taylor — most ended by age 30 with < 8 NFL
seasons in our corpus. Computed values (v2.2):

- bust_rate ≈ 0.45
- short_career_rate ≈ 0.35
- **survival_multiplier ≈ 0.78–0.92** (depending on exact comp pool snapshot)

His raw projection 1806 → after survival 1410 → further penalized by
confidence 0.47 → final ~1486. Rank drops from #23 in v2.1 to #30-38 in
v2.2.

### Josh Allen — clean pool

Top comps include Brady, Brees, Manning, Wilson, McNair, Rodgers — all
played 10+ NFL seasons.

- bust_rate ≈ 0.0
- short_career_rate ≈ 0.0
- **survival_multiplier = 1.00**

Allen's raw projection passes through unchanged: 2224 → 2224 final.
Confidence and late-breakout also 1.0 for Allen. Rank #1.

### Bo Nix — surprisingly clean pool

Top comps include Brady, Brees, Manning, Wilson, McNabb, Tannehill, Dak
Prescott — strong veteran careers. His comp pool isn't busty (the model
finds matches by fp/g shape, not by draft position or breakout age).

- bust_rate ≈ 0.05
- **survival_multiplier ≈ 0.96**

The survival penalty alone is small for Bo Nix. The late-breakout penalty
does most of the work (see `LATE-BREAKOUT-QBs.md`).

## Diagnostics

Per-player survival diagnostics are written to
`data/diagnostics/v2.2_survival.json`:

```json
{
  "00-0038122": {
    "name": "Anthony Richardson",
    "position": "QB",
    "bust_rate": 0.42,
    "short_career_rate": 0.31,
    "weighted_career_length": 6.8,
    "durable_career_rate": 0.52,
    "survival_multiplier": 0.83
  }
}
```

Users can inspect WHY any player got a particular survival multiplier
by looking up their player_id in the JSON.

## Tunable knobs (for v2.3+)

- `SURVIVAL_BUST_AGE` (currently 30) — raise to 32 if Phil wants
  to flag more journeymen as busts.
- `SURVIVAL_BUST_MAX_SEASONS` (currently 8) — lower to 6 for stricter
  bust definition; raise to 10 for a more aggressive net.
- Formula weights (currently 0.20 bust + 0.10 short + 0.70 base) — shift
  weight onto the bust signal vs the base if Phil wants the penalty to
  hit busty pools harder.
