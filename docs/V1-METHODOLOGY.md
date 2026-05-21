# v1.0 Methodology — Dynasty Football Model

> This is the canonical methodology document for the Dynasty Football Model
> v1.0 engine. v0.x methodology docs (SIMILARITY-METHODOLOGY, VORP-METHODOLOGY,
> CUMULATIVE-ARC, ELITE-PROVEN-CALIBRATION, CORRELATION-METHODOLOGY,
> ROOKIE-SIMILARITY-FB) are preserved under [`archive/v0.X/`](archive/v0.X/)
> for historical reference.

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

## Retired-only corpus

The comp pool is built from `data/nflverse/player_stats_season.csv.gz`
filtered to:

- Skill positions only (QB, RB, WR, TE)
- Regular season only
- ≥4 games played in the season (filter cup-of-coffee seasons)
- Player's `last_season` is on or before **2022** (3+ years inactive)

Why retired-only? Phil's reasoning, verbatim:

> "When you were looking at historical comparisons for the players on pro
> football reference I would air on the side of comparing current players
> to historical players whose careers have already ended. You were going
> to get a bad analysis if you compare them to players who have not
> finished their careers yet."

This is enforced by construction: every active player's top-K comps are
guaranteed to have `last_season ≤ 2022`, and the test suite
(`test_no_active_in_comps`) checks that no active player leaks into any
other active player's comp list.

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

## Era-normalised z-scoring

For each (position, era, stat) cell, we compute the mean and standard
deviation of the per-game rate across qualifying seasons. A
player-season's z-score is `(per_game_rate - μ) / σ`, where μ and σ come
from the matching (position, era, stat) cell.

This means:

- A 2010 Peyton Manning at 285 passing yds/game is era-elite (top 5% of
  Era-3 QBs).
- A 2024 Justin Herbert at 285 passing yds/game is era-average (top 50%
  of Era-4 QBs).

The engine sees them as different shapes even though the raw numbers
match. That's the point.

## Cumulative-through-age vector

For each player at a given age, the vector is the **games-weighted
average** of their per-season era z-scores across their position's
feature set, taken across all qualifying seasons up to that age.

This is similar to PR #17's cumulative-arc vector with one critical
difference: **the cohort is restricted to retired players**. There is
no active-to-active comping in v1.

## Finding comps

For an active player at age A:

1. Build their cumulative-through-A vector.
2. Iterate every retired player at the same position.
3. Require the retired player to have at least one season in age window
   A±1 (so they were active at a comparable age — no comping a 22-year-old
   to a player whose only seasons were post-30).
4. Require the retired player to have at least one season **after** age A
   (so there's a remaining career to project).
5. Build the retired comp's cumulative-through-(A+1) vector.
6. Score by cosine similarity.
7. Keep the top-20.

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

## Known limitations

- **Corpus depth.** Stats start in 1999. Players who retired before then
  (Jim Brown, OJ Simpson, prime Steve Young, John Elway pre-1999) are
  not in the comp pool.
- **Mobile-QB comps are scarce.** Allen / Hurts / Lamar / Jackson pull
  Culpepper / Cam / McNair / RGIII / Vick — none of whom played past age
  37 in good health. The engine projects shorter post-age careers for
  the modern dual-threat cohort. Phil's call whether this is right or
  needs a calibration overlay.
- **Birth dates missing for ~2% of retired players.** Age falls back to
  `rookie_season + 22`.
- **No market signals.** The engine produces a pure-production ranking
  with no Sleeper / FantasyCalc / consensus blend. This is intentional
  per the rewrite brief — adding market signal back in is a v1.1
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
