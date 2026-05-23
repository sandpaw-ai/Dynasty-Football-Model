# Rookie-NFL fp-arc engine (v2.1)

> **TL;DR.** A 1-NFL-season rookie's fantasy projection is built by
> finding their nearest 20 historical rookies (by rookie-year fp profile),
> then summing those comps' realised year-2+ careers — weighted by
> similarity, time-discounted 5%/yr, padded by a peak-anchored floor based
> on the rookie's own rookie-rate, and shrunk by a confidence factor when
> games-played is low.

## Motivation

After v2.0 shipped, the 2025 draft class (1 completed NFL season) was
running through the v2.0 cumulative-arc engine. The 10-dim v2.0 vector
includes career-avg fp/G, peak-3yr fp/G, peak-single-season fp/G, career
total, slope, durability, percentile — all derived from
multi-year arcs. For a 1-data-point rookie, most dimensions collapse to
the same single value (career-avg == peak == single-season fp/G), and
the slope is undefined. Cosine and weighted-Euclidean comparisons
against full-career 10+-season veteran vectors are noisy by
construction.

Symptom: Jaxson Dart's top comp came out as Vince Young (early bust);
Ashton Jeanty's top comp was Jordan Howard (limited NFL career). The
projections didn't match either Phil's qualitative expectations or
consensus dynasty rankings.

Phil's directive (verbatim):

> "the 2025 draft class should have a full season of stats under their
> belt... You should be able to identify players in pro-football
> reference who have only one year of experience as rookies and
> extrapolate their careers based on one season of stats compared to
> historically similar player profiles."

The fix: a SEPARATE engine that compares 1-season rookies to other
1-season rookies (i.e., the historical rookies' year-1 profiles), then
projects from those comps' realised year-2+ careers.

## Pipeline

```
1. Build the v2.0 fantasy-point-arc corpus (era-pace adjusted, scored
   under sf_ppr and other formats per season).
2. For every player, look up their actual rookie_season from
   players.csv.gz#rookie_season. Reject if rookie_season < 1999 (corpus
   floor: Marshall Faulk's actual 1994 rookie season isn't in nflverse).
3. Snapshot the 11-dim rookie-year profile vector for each historical
   player. Filter: position ∈ {QB,RB,WR,TE}, rookie games ≥ 4, at least
   one post-rookie season in the arc.
4. For each ACTIVE player with exactly 1 completed NFL season (and the
   season is current or current-1, OR rookie_season is recent):
   a. Build the target's 11-dim rookie profile vector.
   b. Find top-20 historical rookies (same position, age ±2) by
      weighted-Euclidean inverse-distance similarity, breakout-biased
      toward proven year-2+ careers (unless the target is limited-usage
      < 10 games, in which case breakout-bias is disabled).
   c. project_year_2_plus(comp): sum comp's realised post-rookie fp under
      league_format, 5%/yr discount.
   d. comp_weighted_fp = sum(sim_i × pts_i) / total_sim.
   e. peak_anchored_fp = rookie_fp/G × 17 × expected_career_seasons[pos] ×
                         peak_anchored_discount[pos].
   f. base = max(comp_weighted, peak_anchored).
   g. confidence = max(CONFIDENCE_FLOOR, min(games / FULL_CONFIDENCE_GAMES, 1.0)).
   h. projected = base × confidence.
5. Output rookie_dynasty_value in RAW fantasy points, same scale as
   v2.0 veteran production_score. Rookies appear DIRECTLY in the main
   sorted top-300.
```

## The 11-dim rookie profile vector

| Dim | Feature | Weight | Rationale |
|---:|---|---:|---|
| 0 | rookie_fp_per_game (modern-era equivalent, scored per league format) | 8.0 | DOMINANT tier separator. A 17 fp/G rookie should not comp with a 12 fp/G rookie at the same position. |
| 1 | rookie_games / 17 | 0.1 | Durability proxy. Low weight because games-played is already used by the confidence shrinkage. |
| 2 | passing_yards_per_game (raw, era-pace adjustment is implicit via v[0]'s fp/G) | 0.0005 | Tie-breaker within fp/G tier. Low weight because magnitudes (0-300) would otherwise dominate squared distance. |
| 3 | rushing_yards_per_game | 0.003 | Differentiates dual-threat from pocket / receiving-back from power-back. |
| 4 | receiving_yards_per_game | 0.003 | Differentiates receiving back / slot vs power / outside. |
| 5 | passing_TDs_per_game | 0.2 | |
| 6 | rushing_TDs_per_game | 0.3 | Each rushing TD is 6 fp — small absolute differences are fantasy-significant. |
| 7 | receiving_TDs_per_game | 0.3 | |
| 8 | rookie_completion_rate (QB only, else 0) | 0.1 | Currently set to 0 in practice — the simplified `PlayerSeason.stats` dict doesn't carry attempts. fp/G + passing_TDs/G capture most of the same signal. |
| 9 | age_at_rookie_year | **2.5** | **v2.3.5**: bumped from 0.2. A 21-yo and 25-yo rookie at the same fp/G profile have very different career horizons. At weight 2.5, a 2-year age gap contributes ~10 to squared distance — dominates a small fp/G match instead of being washed out by it (fix for Phil's Johnny-Wilson-vs-Steve-Smith-Sr. age-blind bug report). |
| 10 | position_encoded | 0.0 | Informational only; position-filter applies upstream. |

## Similarity function

Weighted-Euclidean inverse-distance, mirroring v2.0:

```
distance(a, b) = sqrt( sum_i  W_i × (a_i - b_i)² )
similarity(a, b) = 1 / (1 + distance / SIMILARITY_SCALE)
```

With `SIMILARITY_SCALE = 15`, same-tier rookies score ~0.65-0.85,
across-tier rookies score ~0.4-0.5.

After computing raw similarity, two re-ranking multipliers apply:

* **Breakout-bias** (1.0 → 1.3): `1 + 0.3 × log(1 + post_rookie_fp/1500) / log(2)`.
  Comps with proven year-2+ careers (Burrow 1333 post-rookie fp;
  McCaffrey 2140) outrank vector-equivalent busts (Tim Tebow 272).
  **DISABLED** when the TARGET rookie has games < 10 — limited-usage
  rookies should comp with limited-usage historical rookies per the
  brief's Hunter directive.
* **Recency-bias** (1.0 → 1.25): linearly ramps from 1.0 at rookie_season =
  2015 to 1.25 at rookie_season ≥ 2022. Modern (post-2015) rookies are
  more predictive of current rookies' outcomes even after era-pace
  normalization \— scheme, draft analytics, and athletic profiles in the
  modern era are tighter to today's rookies than to 2005-era rookies.

## Projection function

```
peak_anchored_fp = rookie_fp_per_game
                 × PROJECTION_GAMES_PER_SEASON          # 17
                 × EXPECTED_CAREER_SEASONS[position]    # QB 8, RB 8.5, WR 9.5, TE 9
                 × PEAK_ANCHORED_DISCOUNT[position]     # QB 0.72, RB/WR/TE 0.85

comp_weighted_fp = sum_k  (sim_k / total_sim) × project_year_2_plus(comp_k)

base = max(comp_weighted_fp, peak_anchored_fp)

confidence = max(CONFIDENCE_FLOOR, min(games / FULL_CONFIDENCE_GAMES, 1.0))
           # FULL_CONFIDENCE_GAMES = 10, CONFIDENCE_FLOOR = 0.35

projected_year_2_plus_fp = base × confidence
```

The `max(comp_weighted, peak_anchored)` is the same hybrid as v2.0:
elite-producer rookies get a peak-anchored floor (so a poor comp pool
doesn't drag them down), while sub-elite rookies fall back to the
comp-weighted projection (so a generous expected-career-length horizon
doesn't inflate them).

## Edge cases

### Limited-usage rookies (Travis Hunter: 7 G / 298 yds)

Hunter played only 7 games as a rookie before injury. His rookie fp/G
(9.1) is real but the sample is half a season.

* `confidence = min(7/10, 1.0) = 0.70` → projection cut by 30%.
* Breakout-bias DISABLED → his comp pool reflects limited-usage
  historical rookies (Kadarius Toney, Aaron Dobson, Josh Downs, Marlon
  Brown), not elite WR breakouts.
* Result: engine rank ~ #74 SF, which is **above** the typical dynasty
  consensus floor (~ top 50 for an elite WR draft pick with injury
  history) but conservative enough to reflect 7-game uncertainty.

### Partial-season rookies (4-6 G)

The corpus minimum is 4 games. A 4-game rookie:
* `confidence = max(0.35, 4/10) = 0.40`.
* Breakout-bias disabled (games < 10).

Jalen McMillan's 2024 rookie season (13 G) means he counts as a full
2024 rookie in the corpus AND routes through the v2.0 sophomore engine
(2 completed seasons) for ranking purposes. The 4-game 2025 season
becomes his SECOND completed year.

### Position mismatch (Travis Hunter is WR/DB)

The rookie's listed position in `players.csv.gz` is used (offensive snap
position only). Hunter is classified WR throughout the engine; his
defensive snaps don't enter the fantasy arc.

### 2024-rookies-now-sophomores (Jayden Daniels, etc.)

Daniels played 17 G in 2024 + 7 G in 2025 (injury-limited). Both
seasons count as completed → 2 completed seasons → v2.0 cumulative-arc
engine, NOT the rookie engine. Daniels' v2.0 arc surfaces him at #1 SF
as expected.

### Pre-1999 retired greats (Marshall Faulk, Curtis Martin)

These players' nflverse data starts in 1999 but their ACTUAL rookie
year predates the corpus (Faulk 1994, Martin 1995, Ricky Watters 1991).
Including their 1999 season as a "rookie comp" would be misleading
(it's actually their 5th-7th NFL year). They're EXCLUDED from the
historical rookie corpus via the
`rookie_season_by_pid[pid] < CORPUS_FIRST_SEASON` filter.

## Expected behavior on the 2025 draft class

| Player | Position | Rookie Stats | Engine Rank | Top 5 Comp Pool |
|---|---|---|---:|---|
| Jaxson Dart | QB | 14G, 241.6 PPR, 9 rush TDs | #30 SF | Kyler/Dak/Anthony Richardson/Burrow/Daniel Jones |
| Cam Skattebo | RB | 8G, high fp/G | #15 SF | Workhorse RB rookies |
| Omarion Hampton | RB | 9G | #21 SF | RB rookies |
| Ashton Jeanty | RB | 17G, 245 PPR | #25 SF | Bijan/Josh Jacobs/Swift/A.Gibson/Bucky Irving |
| Tetairoa McMillan | WR | 17G, 1014 yds, 7 TDs | #29 SF | A.J.Brown/G.Wilson/T.McLaurin/CeeDee/Z.Flowers |
| Cam Ward | QB | 17G, 186 PPR | #71 SF (QB #28) | Pocket-rookie QBs |
| Travis Hunter | WR | 7G, 298 yds (LIMITED USAGE) | #74 SF | Limited-usage rookie WRs |

## Calibration notes

The position-specific constants (`EXPECTED_CAREER_SEASONS`,
`PEAK_ANCHORED_DISCOUNT`) were tuned to satisfy the brief's pinned
ranking thresholds:

* Dart top 50 SF
* Jeanty top 25 SF
* Cam Ward QB top 40
* Tetairoa top 30
* Hunter top 80

Tightening these constants further would push elite rookies higher and
make all of Phil's pins comfortably comfortable, but at the cost of
ranking rookies above proven veterans (e.g. pushing Dart into the top 5
ahead of Lamar Jackson). The current calibration intentionally keeps
v2.0 invariants for 2+ season veterans (Allen top 5, Hurts top 10, etc.)
while putting the 2025 class in the realistic dynasty-consensus band.

## Code locations

* `src/dynasty/engine/rookie_nfl_fp_arc.py` — engine module
* `src/dynasty/engine/similarity_v1.py::run_engine` — cohort dispatcher
* `src/dynasty/engine/similarity_v1.py::_rookie_comp_records` — comp
  record serialization for format_overlay
* `tests/test_v2_1_rookie_nfl.py` — 19 invariant tests

## Future work (v2.2+)

* College → NFL chain for 2026 draft class (0 completed NFL seasons).
  Will produce a separate `/prospects.html` page so prospects don't
  pollute the main rankings.
* Per-format rookie-engine calibration (currently the peak-anchored
  discount uses sf_ppr-tuned values; std/half-ppr may need separate
  position discounts).
* Replace the placeholder QB completion_rate dimension with actual
  passing_attempts data once the season-stats loader carries it.
