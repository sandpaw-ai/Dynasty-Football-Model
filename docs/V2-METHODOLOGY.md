# Dynasty Football Model — v2.0 Methodology

> **TL;DR.** v1.x ranked players by per-stat z-score shape. v2.0 ranks
> players by the fantasy points they actually produce under modern scoring,
> comping them to historical players whose fp/g curves match. Elite-fp QBs
> (Allen, Hurts, Lamar, Daniels, Mahomes) cluster at the top regardless
> of style.

---

## Why v1.x failed

v1.0's similarity engine compared players by per-stat z-scores: passing
yards, passing TDs, INTs, rushing yards, rushing TDs (for QBs). Cosine
similarity matched players with similar STAT SHAPE — but z-scoring is
*scale-invariant within era*. A player producing 28 fp/G under sf_ppr
has the same z-score-shape as a player producing 17 fp/G if their stat
proportions match.

Josh Allen, the canonical example, produces 24-26 fp/G at peak under
sf_ppr — most of that from rushing TDs (6 pts each). v1.0's vector had
"rushing TDs" as one of five components, equally weighted with passing
yards (which scored 0.04 pts each — 150× lower per unit). The cosine
match for Allen pulled in pocket passers with similar passing volume,
ignoring the massive fantasy-point gap.

v1.1 added a career-length era lift (capped at 1.5× for dual-threats),
but it multiplied only `projected_remaining_years`. The BASE projection
was still the comp-pool average of stat-shape-similar players. v1.2
moved to per-category fp z-scoring, but z-scoring is *still*
scale-invariant.

**The diagnosis (Phil verbatim):**

> "I think we need a different methodology entirely. We should still
> compare to historical players at the position, but we should do a
> translation to fantasy point production before doing so."

## The v2.0 fix: fantasy-point arcs

v2.0 builds a per-player, per-format **fantasy-point arc**: a list of
season-by-season fp/g values, with each season's raw stats era-pace-
adjusted to era 4 (current) BEFORE scoring. Every value in the corpus
is in modern-fp-equivalent units. Players are then compared by their
fp/g curves directly, in raw fantasy-point space — no z-scoring.

### Pipeline

1. **Load corpus.** nflverse season-level stats (1999-present),
   restricted to skill positions (QB / RB / WR / TE), filtered to
   seasons with ≥ 4 games played.
2. **Build era-pace table.** Per-position, per-stat, per-era-from
   multipliers derived empirically from corpus median per-game rates,
   clamped to [0.6, 2.0]. Cell-level fallback to a documented table
   for thin buckets.
3. **Long-arc corpus.** Players are eligible as comps if any of:
   - retired (last_season ≤ 2022)
   - 8+ NFL seasons
   - age ≥ 33 with 6+ seasons.
   Long-arc-active veterans (Rodgers, Stafford, Wilson) contribute
   ONLY their completed seasons.
4. **Build fantasy-point arcs.** For each player and each scoring
   format (`sf_ppr`, `1qb_ppr`, `2qb_ppr`, `half_ppr`, `std`,
   `sf_te_premium`):
   - Era-pace-adjust each season's raw stats to era 4.
   - Apply the format's scoring coefficients → season fp total and
     fp/game.
   - Pre-compute career totals + peak-3yr + peak-single-season +
     career-avg per format.
5. **Build career-stage percentile table.** For each (position,
   career-season-index), collect the long-arc-corpus career-total
   fp through that stage, sorted ascending. Used for the v[9]
   vector dimension.

### The similarity vector (10-dim, in fantasy-point units)

For a target at age A, snapshotted at career-season-index S:

| v[i] | Component                                            | Weight  |
| ----:| ---------------------------------------------------- | -------:|
| 0    | fp/g at age A (recent-season weight 1.0)             |     2.0 |
| 1    | fp/g at age A-1 (weight 0.7)                         |     1.5 |
| 2    | fp/g at age A-2 (weight 0.5)                         |     1.0 |
| 3    | career-avg fp/g through age A                        |     3.0 |
| 4    | peak-3yr-avg fp/g through age A                      |     4.0 |
| 5    | peak-single-season fp/g through age A                |     3.0 |
| 6    | career-total fp through age A (/100 scale)           |     0.8 |
| 7    | trajectory slope (fp/g per career-season)            |     0.2 |
| 8    | durability × 10 (career games / possible games)      |     0.3 |
| 9    | career-stage percentile × 30                         |     0.5 |

All values era-pace-adjusted at corpus build → raw fantasy-point units.

**Distance metric: weighted Euclidean** (not cosine). We need magnitude
to matter: Allen (peak 25) is NOT similar to Daniel Jones (peak 16)
even if their proportions match. The peak / career-avg / current-fp
dimensions are weighted highest because they're the magnitude anchors;
slope and durability are weighted low because they're noisy on small
samples.

Similarity = `1 / (1 + d / 20.0)` so identical vectors → 1.0; a
different-tier comp scores ~0.2–0.4.

### Projection

For each comp returned by KNN:

- Sum the comp's realised post-snapshot fantasy points under the
  target format (already in modern-fp units → no additional era-pace
  multiplier needed).
- Time-discount 5%/year out.
- Similarity-weight, sum → `comp_weighted_fp`.
- Carry `comp_weighted_seasons` (similarity-weighted post-age
  seasons) as the expected remaining-career-length signal.

**Peak-anchored projection** (the second projection path):

- `projection_rate = max(recent_3yr × 1.10, peak_3yr × 0.90)`.
  - 1.10×recent: small upward bias on current form.
  - 0.90×peak: soft floor — a player who sustained an elite peak
    retains most of that ceiling.
  - `max()` of the two captures both stars-still-in-form (recent
    dominates) and stars-in-a-slump (peak floor catches them).
- `peak_anchored_fp = projection_rate × 17 × comp_weighted_seasons ×
  mid-life discount`.

### Final score

The dynasty production score per player is:

```
if target_peak_3yr >= ELITE_THRESHOLD[position]:
    production_score = max(comp_weighted_fp, peak_anchored_fp)
elif target_peak_3yr >= ELITE_THRESHOLD[position] - 5.0:
    # Linear blend in the soft band.
    production_score = blend(comp_weighted_fp, peak_anchored_fp)
else:
    production_score = comp_weighted_fp
```

ELITE thresholds (peak 3yr fp/g under sf_ppr):

- QB: 18.0
- RB: 15.0
- WR: 16.0
- TE: 12.0

Below the threshold the projection falls back to comp-weighted-only so
sub-elite players whose comp pool happens to include a few elite
long-career retired comps don't get inflated.

### Mobile / dual-threat lift

For QBs only, v1.1's per-style, per-era career-length lift is preserved:

- pocket: 1.00× fp, 1.00× years (no lift)
- mobile: 1.05× fp, 1.30× years (display)
- dual-threat: 1.10× fp, 1.50× years (display)

The fp lift is milder than v1.1's 1.5× because v2.0's fantasy-arc
methodology already surfaces long-career comps for any high-fp
dual-threat target via the projection-rate path — we no longer need
the brute-force lift to overcome a v1.x sample-bias bug. The display
lift on `projected_remaining_years` keeps the full v1.1 value so the
UI accurately reflects "modern medicine + rule changes continue to
extend mobile careers".

## Why this is dynasty-appropriate

Dynasty value is the projected lifetime fantasy points a player will
score for your roster. v1.x's stat-shape matching answered a different
question — "what shape of NFL career does this player project to
have?" — which correlated imperfectly with fantasy production. v2.0
measures the thing we actually care about: **fantasy points produced
under modern scoring**.

## Format overlay

Per-format projections (sf_ppr, 1qb_ppr, 2qb_ppr, sf_te_premium,
half_ppr, std) are produced by reading per-format fp totals directly
from the pre-computed arc corpus and recomputing positional VORP
baselines under the target roster rules. No re-scoring needed.

## Validation

See `tests/test_v2_fantasy_arc.py` (25 tests, all passing) for the
pinned methodology invariants:

- **Top-of-board**: Allen / Hurts / Lamar / Daniels in elite QB cluster.
- **Comp-list quality**: top-10 comps for Allen / Mahomes / Lamar /
  Hurts each include ≥3 elite-fp historical QBs regardless of style.
- **Sub-tiers**: pocket QBs (Stroud, Tua, Love, Purdy) NOT top 5 but
  still rosterable (top 75).
- **Aging-veteran haircut**: Rodgers at 41 ranks ≥ #100.
- **Non-QB invariants preserved**: Nacua → retired all-time WRs,
  Bijan → retired RBs, Bowers → retired TEs.
- **Format overlay**: Allen SF rank ≥ 1QB rank by ≥ 7 spots; 2QB QB
  premium ≥ SF QB premium.

## Known limitations

- Corpus starts in 1999 (nflverse). Pre-1999 retired greats (Jim
  Brown, Steve Young peak, Barry Sanders, Jerry Rice prime) are not
  fully represented. Era 1 → 4 multipliers fall back to a documented
  table for thin pre-1999 cells.
- Birth dates missing for ~2% of retired players; we fall back to
  `rookie_season + 22` for age estimation.
- Sample-of-1 comp pools (e.g., Rodgers at 41 with only Brady as a
  same-age comp) fall back to comp-weighted only — the peak-anchored
  projection requires ≥ 3 comps. This intentionally damps the
  inflation that would otherwise occur for outlier-comp aging stars.

## v1.x → v2.0 migration notes

- `style_cohort.py` is deleted. Per the brief: "fantasy arc
  methodology allows Allen → Brady if their fp curves match."
- `similarity_v1.py` is now a thin wrapper around the fantasy-arc
  engine. Module name retained for back-compat with all existing
  callers (`format_overlay`, `report`, sources, tests). The
  per-stat z-score machinery is gone.
- `career_length_era.py` is kept and applied as a final fp / years
  multiplier for mobile / dual-threat QBs.
- `era_pace.py` is kept and applied EARLIER in the pipeline (to raw
  stats before scoring, not to scored fp).
- Tests:
  - `tests/test_engine_v1.py` — all v1.0 invariants still pass
    (corpus, comps, formats).
  - `tests/test_v1_1_calibration.py` — two obsolete tests skipped
    with pointers to v2.0 replacements (`test_mahomes_top_10`,
    `test_pocket_passers_unchanged`).
  - `tests/test_v1_2_fantasy_weighted_knn.py` — module-level skip;
    methodology entirely replaced.
  - `tests/test_v2_fantasy_arc.py` — new, 25 tests, all passing.
