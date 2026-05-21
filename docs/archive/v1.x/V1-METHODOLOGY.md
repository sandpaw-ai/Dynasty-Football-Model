# v1.0 / v1.1 / v1.2 Methodology — Dynasty Football Model

> This is the canonical methodology document for the Dynasty Football Model
> v1.x engine. v0.x methodology docs (SIMILARITY-METHODOLOGY,
> VORP-METHODOLOGY, CUMULATIVE-ARC, ELITE-PROVEN-CALIBRATION,
> CORRELATION-METHODOLOGY, ROOKIE-SIMILARITY-FB) are preserved under
> [`archive/v0.X/`](archive/v0.X/) for historical reference.
>
> **v1.2.0** — fantasy-point-weighted vectorization + style-conditioned
> KNN. The KNN vector is now in fantasy-points-per-stat-per-game space
> (not raw counting-stat space), and the comp pool for each target is
> restricted to its style cohort. See [Fantasy-point-weighted
> vectorization (v1.2)](#fantasy-point-weighted-vectorization-v12) and
> [Style-conditioned KNN cohort (v1.2)](#style-conditioned-knn-cohort-v12),
> plus [STYLE-COHORTS.md](STYLE-COHORTS.md) for the per-position
> calibration notes.
>
> **v1.1.0** — dual-threat QB calibration. Adds a long-arc corpus and a
> career-length era lift for mobile / dual-threat QBs. See the
> [Long-arc corpus](#long-arc-corpus-v11) and
> [QB style classification + career-length era adjustment](#qb-style-classification--career-length-era-adjustment-v11)
> sections below, plus [CAREER-LENGTH-CALIBRATION.md](CAREER-LENGTH-CALIBRATION.md)
> for the full technical writeup.

## The one-sentence summary

Every active NFL player is ranked by the similarity-weighted projected
fantasy points of the top-20 most similar **retired** NFL players at the
same career age, with every retired comp's stats rescaled forward to the
current era via empirically-calibrated era-pace multipliers.

That's it. One engine, one source of truth.

## Why a rewrite

v0.x had 10+ ranking sources fed into a composite with hand-tuned weights
and overlay correlations. Each PR added a layer. The model accumulated
complexity faster than insight. Phil:

> "It seems like you are having a very difficult time with this."

He was right. Every fix introduced a new abstraction. The way out is to
strip down to one engine and let production data do the work — because
the basketball model (DARKO-driven) is cleaner and the football model
should look the same.

## Long-arc corpus (v1.1)

The comp pool is built from `data/nflverse/player_stats_season.csv.gz`
filtered to:

- Skill positions only (QB, RB, WR, TE)
- Regular season only
- ≥4 games played in the season (filter cup-of-coffee seasons)
- The player satisfies ANY of:
  1. `last_season ≤ 2022` (retired — the v1.0 rule), OR
  2. `career_seasons ≥ 8` (long-arc veteran with an established arc), OR
  3. `age ≥ 33` AND `career_seasons ≥ 6` (late-career veteran).

For a player satisfying rule 2 or 3 who is still active, **only their
completed seasons** (≤ `current_season`) are included as comp data. The
in-progress season can never leak into the historical pool.

Why the broader corpus? Phil's v1.0 reasoning still holds for the most
part:

> "When you were looking at historical comparisons for the players on pro
> football reference I would air on the side of comparing current players
> to historical players whose careers have already ended."

But v1.0's strict retired-only filter created a structural bias against
modern dual-threat QBs: their style-matched retired comps (Cam Newton,
Vick, McNair, RGIII, Culpepper) all had careers cut short by injury or
the pre-modern rules environment, while pocket-passer comps were
Brady/Manning/Brees/Favre 18-21 season arcs. The v1.1 long-arc corpus
adds Aaron Rodgers (16 seasons), Stafford (16), Russell Wilson (13),
Derek Carr (11), Tannehill (11), Kirk Cousins (12), and others — their
completed seasons surface as longevity comps for the current dual-threat
class.

### Corpus size: v1.0 → v1.1

| Version | Corpus rule | Pool size |
| --- | --- | --- |
| v1.0 | retired (last_season ≤ 2022) | ~1,431 |
| v1.1 | long-arc (above rules) | ~1,514 |

The brief estimated the long-arc pool at ~1,800+ assuming many more 10+
season veterans would qualify; in practice the nflverse 1999-2024 window
only contains ~35 active 10+ season careers, so the bar was lowered to 8
seasons + a veteran-age fallback to materially expand the pool while
preserving the "established arc" spirit.

## Retired-only corpus (v1.0 — deprecated)

The v1.0 retired-only filter (`last_season ≤ 2022`, no veteran fallback)
is retained as one of the three inclusion rules above. Test suites that
relied on the strict v1.0 invariant ("no active player in any comp
list") have been updated for v1.1 to a relaxed form ("no
short-career-active player in any comp list").

## Era buckets

Every season is bucketed into one of four eras:

| Era | Years        | Note                                                |
| --- | ------------ | --------------------------------------------------- |
| 1   | 1980 - 2004  | Pre-modern. Corpus only has 1999-2004 in practice.  |
| 2   | 2005 - 2014  | Mid-modern.                                         |
| 3   | 2015 - 2019  | Post-pass-inflation, pre-Mahomes saturation.        |
| 4   | 2020 - now   | Current.                                            |

The brief specified Era 1 = 1980-1994 and Era 2 = 1995-2004; nflverse
only ships per-season stats back to 1999, so Era 1 in our corpus
effectively covers 1999-2004. The conceptual structure is identical:
monotonically inflating passing volume + rising QB rushing usage from
Era 1 → 4.

## Per-position feature sets

Similarity vectors live in era-normalised z-score space. The feature set
varies by position:

- **QB**: passing_yards, passing_tds, interceptions, rushing_yards, rushing_tds
- **RB**: rushing_yards, rushing_tds, receptions, receiving_yards, receiving_tds
- **WR**: receptions, receiving_yards, receiving_tds, rushing_yards, rushing_tds
- **TE**: receptions, receiving_yards, receiving_tds

All stats are **per-game rates**, not season totals. This decouples
similarity from games played (injuries shouldn't make a player look
fundamentally different in shape).

## Fantasy-point-weighted vectorization (v1.2)

v1.0 / v1.1 used a per-stat z-score vector where each stat (passing
yards, passing TDs, rushing TDs, ...) was treated as one feature
dimension with equal weight. v1.2 RE-EXPRESSES each component as the
per-game fantasy points contributed by that stat under the active league
format:

  vec_component(stat) = (raw_stat_per_game * scoring_coef[stat])

under ``scoring_rules.LEAGUE_SCORING[league_format]``. Each component is
then era-z-scored per position per format (Era-Z-Norm keyed by
``(position, era, stat, format)``).

**Why fantasy-weighted matters.** Under sf_ppr 1 passing yard = 0.04 fp
and 1 rushing TD = 6 fp — a ~150x scoring spread. v1.1's equal-weighted
raw-stat z-score vector buried that. v1.2's fantasy-point vector means
cosine similarity now matches players on the *shape of their fantasy
production*, not the *shape of which counting-stat columns they fill*.
Josh Allen (high rushing-TD volume) now correctly compares to dual-
threat-style retired QBs whose fantasy production was rushing-heavy,
not to pocket passers who happened to share his cumulative-shape signature.

**Why sub-features and not coarse categories.** A 2-component vector
(passing total, rushing total) for QBs collapses to near-degenerate
cosine — every pocket passer sits on the same ray. v1.2 keeps the
stat-level granularity so the KNN can distinguish (e.g.) high-TD pocket
from high-volume pocket.

**Format awareness.** The Era-Z-Norm is computed per format, so the
same player has a different vector under ``sf_ppr`` (receptions = 1.0
fp/catch) vs ``std`` (receptions = 0 fp/catch). For ppr-equivalent
formats z-score invariance under positive linear scaling means the
receptions component is identical numerically; under ``std`` the
receptions component collapses to zero (after era-z-norm), materially
shifting the KNN match space for reception-heavy RBs and WRs.

## Era-normalised z-scoring

For each (position, era, stat, format) cell, we compute the mean and
standard deviation of the per-game fantasy-point contribution across
qualifying seasons. A player-season's z-score is `(per_game_fp - μ) /
σ`, where μ and σ come from the matching cell.

This means:

- A 2010 Peyton Manning at 285 passing yds/game (= 11.4 fp/game from
  passing yards alone) is era-elite (top 5% of Era-3 QBs).
- A 2024 Justin Herbert at 285 passing yds/game (= 11.4 fp/game) is
  era-average (top 50% of Era-4 QBs).

The engine sees them as different shapes even though the raw numbers
match. That's the point.

## Style-conditioned KNN cohort (v1.2)

Every player (active and retired) is classified into a per-position
style cohort and the KNN pool is restricted to comps in the same cohort,
with adjacent-bucket fallback when the strict cohort yields fewer than
20 qualified comps after age-window filtering. The fallback chain is
capped at 2 styles (primary + 1 adjacent).

Full threshold definitions, fallback chains, and calibration notes are
in [STYLE-COHORTS.md](STYLE-COHORTS.md). At a glance:

- **QB**: pocket / mobile / dual-threat by rushing_fp_share (0.15 / 0.30).
- **RB**: workhorse / committee / receiving-back by touches/game +
  rec_fp_share.
- **WR**: alpha / secondary / deep-threat by targets/game + ypr.
- **TE**: receiving / hybrid / blocking by rec_fp_share (effectively
  single-cohort because nflverse only tracks receiving stats for TEs).

The two style classifications (v1.1's `style_for_career` in
`career_length_era` and v1.2's `cohort_for` in `style_cohort`) are
INTENTIONALLY INDEPENDENT:

  * v1.1 `style_for_career` (rypg-based) → which career-length lift
    multiplier the player gets.
  * v1.2 `cohort_for` (fp_share-based) → which comp pool the KNN draws
    from.

Mahomes (rypg ~20 → mobile under v1.1, gets 1.3x lift; fp_share 0.127 →
pocket under v1.2, pulls Brady/Brees comps) is the canonical hybrid case
that the two-classification approach handles cleanly.

## Cumulative-through-age vector

For each player at a given age, the vector is the **games-weighted
average** of their per-season era z-scores across their position's
feature set (fantasy-points-per-stat-per-game under the active format,
see [Fantasy-point-weighted vectorization](#fantasy-point-weighted-
vectorization-v12) above), taken across all qualifying seasons up to
that age.

This is similar to PR #17's cumulative-arc vector with one critical
difference: **the cohort is restricted to retired and long-arc players**.
There is no active-to-active comping in v1.x.

## Finding comps

For an active player at age A:

1. Build their cumulative-through-A vector (v1.2: fantasy-point-weighted,
   format-aware — see [Fantasy-point-weighted vectorization](#fantasy-
   point-weighted-vectorization-v12)).
2. Determine the player's style cohort (v1.2) and walk the cohort's
   fallback chain to assemble the candidate pool.
3. Iterate the candidate pool (long-arc retired members of the same
   position).
4. Require the candidate to have at least one season in age window A±1
   (so they were active at a comparable age — no comping a 22-year-old
   to a player whose only seasons were post-30).
5. Require the candidate to have at least one season **after** age A
   (so there's a remaining career to project).
6. Build the candidate comp's cumulative-through-(A+1) vector under the
   same format.
7. Score by cosine similarity.
8. Keep the top-20.

The v1.2 cohort restriction (step 2) is the structural change from v1.0/
v1.1's whole-position iteration. Cohort fallback widens from the strict
bucket to 1 adjacent style only when the strict cohort yields fewer than
20 qualified comps after age-window filtering.

## Era-pace projection

To project a retired comp's post-A career into the modern era, every
season's stats are multiplied by a per-position, per-stat, per-source-era
multiplier targeting **era 4** (current).

Multipliers are calibrated empirically from the corpus: for each
(position, stat, era_from), take the median per-game rate; the
multiplier is `median_era_4 / median_era_from`, clamped to [0.6, 2.0]
to suppress one-off outlier seasons.

When a cell is empty (e.g. pre-1999 QB rushing is absent), the engine
falls back to a documented table that captures the brief's expected
ratios (QB passing 1.25× from Era 1, QB rushing 1.40× from Era 1
because modern QBs run more, etc.).

The live era-pace table is rendered into `methodology.html` on every
build.

## Production score

For each retired comp:

```
post_age_pts = Σ_seasons_after_A (
    Σ_stats raw_value × era_pace_mult(pos, stat, era_from) × scoring[stat]
    × (1 - 0.05)^year_offset
)
```

The player's `production_score` is the similarity-weighted average across
their top-20 comps. Each comp contributes `sim_i / Σ sim` of their
post-age pts.

## Format overlay (`/league.html`)

The base `/rankings.html` page uses Superflex PPR scoring by default. The
overlay page re-runs the projection pass under different scoring + roster
presets:

- **SF PPR**: 1 QB + 2 RB + 3 WR + 1 TE + 1 FLEX + 1 SF (QBs play SF 85%)
- **1QB PPR**: 1 QB + 2 RB + 3 WR + 1 TE + 1 FLEX (no SF)
- **2QB PPR**: 2 QB + 2 RB + 3 WR + 1 TE + 1 FLEX (no SF, but two real QBs)
- **SF TE-Premium PPR**: same as SF but +0.5 PPR on TE receptions

For each format, the engine recomputes positional VORP from its own
projections: replacement baseline = (effective starters per position) +
6-slot waiver buffer.

2QB QB premium is slightly higher than SF QB premium because 2QB cannot
flex a non-QB into the second QB slot.

The overlay does NOT change which retired comps each active player has —
just how their comps' careers are scored.

## Prospects page

`/prospects.html` is a deliberately decoupled placeholder in v1.0. NFL
veterans and rookies don't share an engine: the similarity model needs
NFL production data, and prospects don't have any yet. The v0.16 college
→ NFL chain was tied to the v0.x composite; a clean prospects engine
that mirrors the basketball model's rookie page is v1.1 work.

## QB style classification + career-length era adjustment (v1.1)

v1.0 produced structurally short projections for modern dual-threat QBs
because the retired comp pool's dual-threat cohort (Culpepper, Cam,
Vick, McNair, RGIII) had careers shortened by:

- Injury under pre-modern roughing-the-passer enforcement.
- Style-of-play tax (designed-runs / scrambles taking direct hits).
- Pre-RPO offenses that didn't pre-empt contact with read options.
- Pre-modern medical care + recovery protocols.

The modern dual-threat cohort (Allen, Lamar, Hurts, Daniels) plays in
a strictly safer environment on all four axes. v1.0's KNN-only model
ignored that structural change and projected their careers to mirror
their short-career style comps.

v1.1.0 corrects this with a one-way **career-length era lift** applied
to each active QB's projected_remaining_years AND
projected_fantasy_points.

### Style classification

Each QB is classified by career rushing yards per game:

| Style | Threshold | Era-4 lift |
| --- | --- | --- |
| Pocket | < 15 ru/g | 1.00× |
| Mobile | 15-30 ru/g | 1.30× |
| Dual-Threat | ≥ 30 ru/g | 1.50× |

### How the lift is computed

For each (style, era) bucket in the long-arc QB corpus, the engine
computes the median career length (seasons played). The lift for a
(style, era) cell is:

```
lift[style][era] = pocket_median[era] / style_median[era]
```

clamped to `[1.00, 1.50]`. Pocket passers always have lift = 1.00.

Era 3 (2015-2019) and era 4 (2020+) are merged into a single "modern"
bucket for the calibration because the current dual-threat cohort
hasn't produced any retired members yet (Cam Newton's career midpoint
lands in era 3). The lift is then applied at era 4 for all current
players.

### Why a one-way lift

The lift only raises projections — it never lowers them. This is by
design:

- Pocket-passer projections are already well-calibrated in v1.0
  (Brady/Brees/Manning are the comp pool, and their 18-21 season careers
  are the right reference).
- Mobile / dual-threat projections in v1.0 were structurally biased
  LOW. The fix is to raise them.
- We never want to invent extra career length — the 1.5× cap is the
  ceiling we're willing to assert based on observable evidence.

### Applied to which positions

**QB-only.** RB careers genuinely DO cliff hard — the historical record
(Tomlinson / Faulk / Peterson 8-11 season arcs) IS the right reference
for Bijan / Gibbs / CMC, so we don't lift them. WRs and TEs are already
well-calibrated against retired greats (Megatron, Moss, Fitz, Gronk).

### Visibility

Every QB player page surfaces:

- Their style classification (Pocket / Mobile / Dual-Threat) as a badge.
- The career-length lift applied (e.g. `1.50× — era 4 modern medicine +
  RPO scheme adjustment`) as a callout.

The rankings.json sidecar carries `qb_style`, `qb_career_rypg`, and
`career_length_lift` for every active player.

## Known limitations

- **Corpus depth.** Stats start in 1999. Players who retired before then
  (Jim Brown, OJ Simpson, prime Steve Young, John Elway pre-1999) are
  not in the comp pool.
- **Dual-threat era-4 corpus is empty.** No retired QB has fully
  developed a career in era 4 (2020+) yet — Cam Newton's career midpoint
  is era 3. The lift table merges eras 3 and 4 to compensate; once a few
  current dual-threat QBs retire, the corpus-derived lift will reflect
  real era-4 evidence.
- **Allen / Lamar still rank below pocket veterans.** The lift closes
  most of the gap from v1.0 (Allen moves from SF #133 → ~#55) but the
  KNN-weighted base projection still favours high-volume passers. See
  [CAREER-LENGTH-CALIBRATION.md](CAREER-LENGTH-CALIBRATION.md) for
  Phil-facing context.
- **Birth dates missing for ~2% of retired players.** Age falls back to
  `rookie_season + 22`.
- **No market signals.** The engine produces a pure-production ranking
  with no Sleeper / FantasyCalc / consensus blend. This is intentional
  per the rewrite brief — adding market signal back in is a v1.2
  decision.

## File map

- [`src/dynasty/engine/similarity_v1.py`](../src/dynasty/engine/similarity_v1.py)
  — engine entry point. `run_engine()` builds everything.
- [`src/dynasty/engine/era_pace.py`](../src/dynasty/engine/era_pace.py)
  — era buckets, fallback multipliers, `EraPaceTable`.
- [`src/dynasty/engine/format_overlay.py`](../src/dynasty/engine/format_overlay.py)
  — league-format presets, VORP recompute.
- [`src/dynasty/report.py`](../src/dynasty/report.py) — static-site
  generator. Mirrors the basketball model's `report.py` structure
  (`_shared_css`, `_site_header`, `_build_rankings`, player pages).
- [`tests/test_engine_v1.py`](../tests/test_engine_v1.py) — 18 contract
  tests pinning retired-only corpus, era-pace ranges, comp-list shape,
  format-overlay behaviour, and UI parity with the basketball model.
