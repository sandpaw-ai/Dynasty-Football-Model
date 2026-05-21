# Correlation Methodology (overlays)

> v0.14.0 — Phil's directive: "RAS and SRS should be overlays where you
> show the historical statistical correlation by position for those
> scores and then allow the user to overlay that into the model."

## What's an overlay?

A user-toggleable modifier on top of the composite, with a *data-driven*
default suggestion. The suggested slider value is the historical
Pearson correlation between the overlay signal and a player's realized
NFL fantasy production.

If RAS correlates 0.23 with RB first-3-year PPR but 0.14 with WR, then
the RB slider's suggested default is 0.23 and the WR slider's is 0.14.
The mechanical effect is "real" — driven by actual outcome data — and
the UI labels it as such so users see why one knob has more reshuffling
power than another.

## Why "first 3 NFL seasons" as the production window

We compute the correlation between the overlay signal (RAS / SRS — both
*pre-NFL* prospect signals) and the sum of `fantasy_points_ppr` over
the player's first 3 NFL seasons. Reasoning:

1. RAS and SRS are pre-NFL signals. The strongest test of whether they
   predict on-field translation is the early-career window where they're
   most relevant.
2. Career totals are dominated by longevity. The similarity engine
   already projects longevity from realized on-field production — that's
   not what the overlays are measuring.
3. 3 seasons matches the conventional dynasty rookie-pick valuation
   horizon. "Will this prospect produce for me in years 1-3?" is the
   question RAS / SRS are asked to answer.

We include players who washed out (zero or near-zero first-3-year PPR).
That means the correlation also captures *survival* — guys with high RAS
who never saw the field hurt the correlation, which is the right
behavior because the overlay claim is "RAS is meaningfully predictive
of on-field production."

## Computed values (v0.14.0)

Run: `python3 scripts/correlation_audit.py`

| Signal × Position | r | n |
|-------------------|----|---|
| RAS × QB first-3yr PPR | +0.172 | 245 |
| RAS × RB first-3yr PPR | **+0.228** | 527 |
| RAS × WR first-3yr PPR | +0.142 | 719 |
| RAS × TE first-3yr PPR | +0.177 | 354 |

**Interpretation:**
- RAS is strongest for RBs (r=0.23) — agility / size correlate with
  early-career rushing translation more than they do for the other
  positions.
- For QBs and WRs, RAS is in the +0.14–0.17 range — meaningful but
  modest. Enabling RAS at suggested weight will reshuffle the rankings
  perceptibly but won't dominate.
- The point of the overlay is *not* "RAS is gospel" — it's "RAS is
  worth ~r of the composite's leverage at this position."

## Brainy Ballers SRS

We do NOT yet have a historical archive of Brainy Ballers' rankings —
the source only publishes current top-500. Until that archive exists we
use a conservative low-confidence prior:

| Signal × Position | r (prior) |
|-------------------|-----------|
| SRS × QB | +0.15 |
| SRS × RB | +0.20 |
| SRS × WR | +0.30 |
| SRS × TE | +0.25 |

The correlation table marks these as `"brainy_ballers_srs_confidence": "low"`
so the UI can render them with the appropriate caveat. When/if a
historical archive becomes available the audit re-runs and replaces these
priors with computed values.

## How the overlay is applied

```
new_composite_score = old_composite_score
                    + (correlation × normalized_signal × user_weight × 10)
```

Where:
- `correlation` is the position-specific value from the correlation table
- `normalized_signal` is the overlay's value for this player, in [0, 1]
  (RAS rescaled from its 0-10 native scale to 0-1; SRS as fractional)
- `user_weight` is the slider's current value (default = max(corr, 0))
- `× 10` is a scale factor that brings the overlay delta into the same
  order of magnitude as the 0..100 composite_score

So at the suggested default `user_weight = correlation`, the maximum
overlay delta a player can receive is `correlation² × 10`. For RB+RAS
that's ~0.5 points of composite score — meaningful for tier-line cases,
not enough to flip a top-30 player to bottom-100.

## When to re-run the audit

- After the PFR / nflverse cache is refreshed (annually or post-season)
- After RAS database is updated (Kent Lee Platte posts annually)
- After a Brainy Ballers historical archive becomes available

Run: `python3 scripts/correlation_audit.py`. Outputs
`data/overlays/correlation_table.json`. Commit that file.

## Limitations

- The audit doesn't currently bucket by *draft year* — a 2010 RB with
  RAS=9.5 sits in the same bucket as a 2020 RB with RAS=9.5. League
  scoring and offensive scheme have shifted enough that this introduces
  some heteroskedasticity. A future enhancement: era-weighted
  correlations (e.g. emphasize 2015+ seasons since modern usage is more
  relevant).
- The 3-year window favors prospects who got opportunity early. A
  player who took 2 years to break out has 1 productive year in the
  window, which depresses the correlation. Acceptable for dynasty
  rookie pick valuation; not ideal for long-run prospect evaluation.
- Pearson correlation assumes linearity. RAS specifically is known to
  have threshold effects (RAS < 5 = bust risk; RAS > 8 = high ceiling).
  Switching to Spearman or a rank-correlation would be more robust but
  the Pearson value already lines up with prior published research.
