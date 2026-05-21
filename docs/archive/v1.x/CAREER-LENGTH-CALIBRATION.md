# Career-length era calibration (v1.1.0)

Technical writeup of the dual-threat QB career-length era adjustment shipped
in v1.1.0. Pairs with [V1-METHODOLOGY.md](V1-METHODOLOGY.md).

## The problem

v1.0's retired-only similarity engine is internally consistent but it fails
a sniff test: Josh Allen ranks **SF #133** while Brock Purdy ranks **SF #2**.
That's a calibration bug, not a model insight.

The mechanism is structural:

1. Allen's career rushing rate is 37.7 yds/game — a *dual-threat* style.
2. The retired comp pool of dual-threat QBs is Cam Newton (10 seasons),
   Mike Vick (13), Steve McNair (13 — but most ended pre-2010), Daunte
   Culpepper (10, but mostly broken after 2004), RGIII (6), Kaepernick (5).
3. The retired comp pool of pocket passers is Brady (21), Brees (19),
   Manning (17), Favre (20), Rivers (15), Ben Roethlisberger (17), Matt
   Ryan (15).
4. The KNN engine matches Allen to Culpepper / Cam / McNair (because style)
   and projects 3-6 post-age seasons.
5. The KNN engine matches Purdy to Brees / Manning / Brady and projects
   15-20 post-age seasons.

The engine sees the same model. The corpus is what's wrong.

But the corpus isn't "wrong" — it's *biased by sample era*. Modern dual-threat
QBs play in a strictly safer environment than Cam/Vick/RGIII did:

- **Rules.** Roughing-the-passer enforcement is unrecognizable from 2005.
  Defenseless-runner rules now apply to QBs sliding feet-first.
- **Schemes.** RPO and zone-read offences pre-empt direct hits. Allen
  doesn't take 20 designed runs per game the way Cam did at Auburn-era
  Carolina.
- **Medicine.** ACL recovery in 2024 is a 6-month return-to-football
  baseline; in 2012 (when RGIII tore his) it was 14+ months and a 35-40%
  re-tear rate.
- **Pocket-passer career length is also expanding.** Brady to 45, Brees to
  42, Rodgers still active at 41. The TOP of the longevity distribution is
  drifting up across all styles.

The calibration question is: *how much of the pocket/dual-threat career
length gap is style-intrinsic vs sample-era-bound?* v1.1 answers
"meaningfully sample-era-bound" and applies a one-way lift.

## The fix

v1.1.0 implements two compounding mechanisms:

### 1. Long-arc corpus

The v1.0 retired-only filter is replaced with a "long-arc" rule:

A player is in the corpus if ANY of:
- `last_season ≤ 2022` (retired — the v1.0 rule), OR
- `career_seasons ≥ 8` (established arc), OR
- `age ≥ 33 AND career_seasons ≥ 6` (late-career veteran).

For long-arc-but-active players, only completed seasons (≤ current_season)
contribute. This adds Aaron Rodgers, Stafford, Russell Wilson, Derek Carr,
Tannehill, Kirk Cousins, and ~30 others to the comp pool. The pool grows
from ~1,431 (v1.0) to ~1,514 (v1.1).

The effect: Allen's KNN now surfaces Russell Wilson (13 seasons, 27.4 ru/g
— mobile, longest of any current dual-threat era comp) and Aaron Rodgers
(16 seasons, 14.6 ru/g — pocket, but Allen's cumulative-through-age vector
weights passing volume too). These are LONGER post-age tails than the
v1.0 dual-threat cohort offered.

### 2. Career-length era lift

For every active QB, classify by career rushing rate:

| Style | Threshold | Era-4 lift |
| --- | --- | --- |
| Pocket | < 15 ru/g | 1.00× |
| Mobile | 15-30 ru/g | 1.30× |
| Dual-Threat | ≥ 30 ru/g | 1.50× |

The lift multiplies BOTH `projected_remaining_years` and
`projected_remaining_fantasy_points` (same factor — longer career = more
points).

The lift is computed per (style, era) cell from the long-arc corpus:

```
lift[style][era] = pocket_median_seasons[era] / style_median_seasons[era]
```

clamped to `[1.00, 1.50]`. Eras 3 and 4 are merged into a single "modern"
bucket because no dual-threat QB has produced a fully era-4 career yet
(Cam Newton's career midpoint is 2016 = era 3).

The lift is **one-way**: it only raises projections. Pocket passers always
have lift = 1.00. Dual-threat and mobile QBs cannot have their projections
reduced by this mechanism.

## Edge cases

- **RBs.** The RB cliff is real — Tomlinson, Faulk, Peterson, Bell all hit
  it around age 30. The lift does NOT apply to RBs. Bijan, Gibbs, CMC
  rankings are unchanged from v1.0.
- **WRs / TEs.** Well-calibrated against retired greats (Megatron, Moss,
  Fitz, Gronk). No lift applied. Nacua's comp list still surfaces
  Calvin Johnson / Andre Johnson / Megatron-tier WRs.
- **Aging veterans.** Aaron Rodgers (41) is in the long-arc corpus as a
  COMP for younger QBs, but his own projection is NOT lifted (he's pocket
  style — lift 1.00). His SF rank stays deep (~#160) because his
  projected_remaining_years is small regardless of comp pool.
- **Active veterans in corpus.** Russell Wilson, Stafford, Rodgers, etc.
  appear in the corpus AND in the rankings. In the corpus they're a comp
  source (using completed seasons only); in the rankings they're an active
  player being projected. Their own projection never includes themselves
  as a comp.

## Empirical results

### Long-arc corpus QB style buckets (era 3+4 merged "modern" bucket)

| Style | Count | Median career length |
| --- | --- | --- |
| Pocket | 20+ | ~11 seasons |
| Mobile | 4 | ~9 seasons |
| Dual-Threat | 2 (Cam, RGIII) | ~8 seasons |

Pocket/Dual-Threat ratio = 11/8 = 1.375. Pocket/Mobile = 11/9 = 1.22.
The fallback table sits at the brief's MAX_LIFT=1.50 / 1.30 ceiling for
dual-threat / mobile era 4, slightly above empirical (acknowledging
continued rules + medical improvements past era 3).

### Top 25 SF PPR: v1.0 → v1.1

| Rank | v1.0 | v1.1 (Δ) |
| ---- | ---- | -------- |
|  1 | C.J. Stroud (QB) | Justin Herbert (QB, mobile lift) |
|  2 | Brock Purdy (QB) | Bo Nix (QB, mobile lift) |
|  3 | Tua Tagovailoa (QB) | C.J. Stroud (QB) |
|  4 | Jordan Love (QB) | Tua Tagovailoa (QB) |
|  5 | Bijan Robinson (RB) | Brock Purdy (QB) |
|  6 | Jahmyr Gibbs (RB) | Patrick Mahomes (QB, mobile lift) |
|  7 | Justin Herbert (QB) | Jordan Love (QB) |
|  8 | Bucky Irving (RB) | Jahmyr Gibbs (RB) |
|  9 | Puka Nacua (WR) | Bijan Robinson (RB) |
| 10 | Joe Burrow (QB) | Brian Thomas Jr. (WR) |
| ... | ... | ... |
| 20 | Brock Bowers (TE) | Jalen Hurts (QB, dual-threat lift) ⬆ |
| 24 | Ladd McConkey (WR) | Jayden Daniels (QB, dual-threat lift) ⬆ |

### Per-QB deltas: v1.0 → v1.1 SF PPR

| QB | v1.0 | v1.1 | Δ | Style | Lift |
| --- | ---: | ---: | --- | --- | ---: |
| Patrick Mahomes | 12 | 6 | +6 | mobile | 1.30 |
| C.J. Stroud | 1 | 3 | -2 | pocket | 1.00 |
| Brock Purdy | 2 | 5 | -3 | pocket | 1.00 |
| Tua Tagovailoa | 3 | 4 | -1 | pocket | 1.00 |
| Jordan Love | 4 | 7 | -3 | pocket | 1.00 |
| Justin Herbert | 7 | 1 | +6 | mobile | 1.30 |
| Joe Burrow | 10 | 22 | -12 | pocket | 1.00 |
| **Jalen Hurts** | 125 | **20** | **+105** | dual-threat | 1.50 |
| **Jayden Daniels** | 113 | **24** | **+89** | dual-threat | 1.50 |
| **Josh Allen** | 133 | **~55** | **+78** | dual-threat | 1.50 |
| **Lamar Jackson** | 167 | **~95** | **+72** | dual-threat | 1.50 |
| Aaron Rodgers | 112 | ~165 | -53 | pocket | 1.00 |

(Pocket-passer "regressions" of -1 to -12 are NOT real regressions — their
absolute production scores are unchanged. The shift is because mobile /
dual-threat QBs around them got lifted, compressing the league_value
distribution.)

## Honest assessment

The brief's success criterion was "Josh Allen top 10 SF PPR while pocket
passers stay top 25." The implemented mechanism gets Allen to ~SF #55, not
top 10. The structural gap between Allen's KNN-weighted base projection
(~1,165 production score) and Stroud's (~1,870) is too large for a 1.5×
cap to close.

To get Allen top 10 would require additional levers the brief did not
specify:
- Re-weighting comp similarity to favour long-arc dual-threat / mobile
  comps (e.g. inverse-distance weighting capped at long-arc tail only).
- Compounding the lift on year-by-year discounted points (current behaviour
  is a flat multiplier on aggregated points, which is conservative).
- Adding a "style premium" to dual-threat ceiling (the right-tail outcome
  of dual-threat QBs in a modern offense is meaningfully higher than their
  pocket peers — Allen's MVP-level rushing TDs).

Those are v1.2 conversations. v1.1 delivers the brief's two specified
mechanisms honestly:

- ✅ Long-arc corpus (1,431 → 1,514 careers; +83 long-arc veterans)
- ✅ Career-length era lift (per-style, per-era, capped at 1.5×)
- ✅ Pocket passers preserved top 25 (Stroud / Purdy / Tua / Love /
  Herbert / Burrow all top 25)
- ✅ Aging Rodgers deep in the rankings (~#165)
- ✅ Nacua / Bijan / Gibbs comps unchanged (calibration is QB-specific)
- ✅ Engine runtime ~3-4s end-to-end (well under 20s budget)
- ⚠ Allen / Lamar lifted substantially (+72 to +78 spots) but not to
  the brief's aspirational top-10 target.

## See also

- [V1-METHODOLOGY.md](V1-METHODOLOGY.md) — full v1.0/v1.1 methodology.
- [CHANGELOG-model.md](CHANGELOG-model.md) — v1.1.0 release notes.
- [`src/dynasty/engine/career_length_era.py`](../src/dynasty/engine/career_length_era.py)
  — implementation.
- [`tests/test_v1_1_calibration.py`](../tests/test_v1_1_calibration.py)
  — pinned invariants.
