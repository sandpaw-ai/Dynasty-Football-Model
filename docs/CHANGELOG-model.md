# Model changelog

A running log of *what the dynasty composite score actually changes* with each
PR. The point is to be able to read this top-to-bottom and understand how the
model has evolved — what was added, what shifted in the outputs, and where the
biggest player-level movements happen.

Format for each entry:

- **What changed** — the mechanical change.
- **Why** — citation back to `docs/RESEARCH-sources.md` and/or external evidence.
- **Expected output shift** — qualitative and (where possible) quantitative
  predictions about which player cohorts move and in which direction.
- **Validation** — how we'll know in backtesting whether the change helped.

---

## v0.3.0 — NFL Draft Capital source + research doc commit (PR #2)

**Date:** 2026-05-20

**What changed**
- New ranking source: `nfl_draft_capital` (slug). Pulls every NFL draft pick
  since 1980 from the public nflverse CSV release (free, no scraping, no auth).
- Filters to fantasy skill positions only: QB / RB / WR / TE (FB folded into
  RB). 4,314 historical skill-position picks; the most recent 6 draft classes
  (~80 picks each, ~557 total) are *emitted as rankings* and flow into the
  composite scorer. Older picks still enrich the Player table but don't
  pollute current rankings.
- Player schema gained `gsis_id` (canonical nflverse/NFL id) and the sync layer
  now also populates `pfr_id`. Player resolution order extended:
  `sleeper_id → gsis_id → mfl_id → (full_name, position)`.
- Player enrichment now writes `draft_round`, `draft_pick_overall`,
  `draft_team`, `college` whenever they're currently NULL.
- Source `default_weight = 1.5` — highest of any source in the registry.
  Rationale: in published studies, NFL draft pick is the single strongest
  predictor of rookie fantasy production (r ≈ 0.4–0.6 vs. 3-year fantasy
  points; see `docs/RESEARCH-sources.md` §A1).

**Why**
- Research doc §A1: across PFF, Hayden Winks, Sharp Football, Brainy Ballers,
  and academic-style replications, NFL draft capital consistently emerges as
  the single most predictive *public* variable for rookie fantasy outcomes.
- It costs nothing to add (free data, ToS-clean) and has the largest expected
  marginal value of anything in the research recommendations.

**Expected output shift (qualitative)**
- Rookies and 2nd-year players drafted in the top of the NFL draft will *rise*
  in the composite, especially in early-season rankings where FantasyCalc and
  ECR are still slow to react to draft-night surprises.
- Players the NFL itself passed on — late-round or UDFA — will *fall* in the
  composite relative to where market sources have them. The 2025–2026 classes
  in particular will see the strongest delta, because draft capital is the
  freshest signal we have on them.
- For Year 3+ veterans, the effect is minimal: the emit-years-back window (6
  years) does include them, but their draft pick is increasingly diluted by
  FantasyCalc, ECR, and (eventually) Production data. PR #5's years-pro decay
  will sharpen this further.

**Expected output shift (where to look)**
Run `python -m dynasty.cli top --n 50 --league-format sf_ppr` before and after
syncing this source. The largest absolute rank moves will be:
- Rookie WR/RBs taken in the first round in the last 2 classes — these jump
  the most in a vacuum, because previously the model had no first-principles
  draft-pedigree signal for them.
- Day-3 picks at WR (5th–7th rounds in 2024–2026 classes) — these get a *low*
  ranking contribution from this source, pulling them down in the composite
  unless other sources strongly disagree.
- Divergence metric (`rank_divergence`) gets more interesting: this source is
  classified `category="model"`, NOT `category="market"`, so it counts as an
  *evaluator* opinion vs. consensus — exactly the kind of "buy/sell vs. market"
  signal Phil's model is designed to surface.

**Validation**
- `backtest_source("nfl_draft_capital", cohort_years=[2020,2021,2022,2023],
  window_years=3)` should report a Spearman correlation that beats both
  FantasyCalc and DynastyProcess on the rookie cohort, especially for
  Year-1-only outcomes. If it doesn't, the weight is wrong or the cutoff
  window is too aggressive.
- Track-record multiplier (in `scoring.py::_track_record_multipliers`) will
  then automatically scale this source's effective weight on subsequent
  `score` runs.

**Files touched**
- `src/dynasty/sources/nfl_draft_capital.py` (new)
- `src/dynasty/sources/__init__.py` — registry entry
- `src/dynasty/sources/base.py` — `RankingRecord` gained `gsis_id`, `pfr_id`,
  `college`, `draft_round`, `draft_pick_overall`, `draft_team`
- `src/dynasty/db/models.py` — `Player.gsis_id` column
- `src/dynasty/sync.py` — resolution + enrichment for new fields
- `tests/test_nfl_draft_capital.py` (new) — unit tests with fixture CSV
- `docs/RESEARCH-sources.md` (new, 440 lines) — the full source research doc
- `docs/CHANGELOG-model.md` (this file, new)

**Migration note**
- The new `Player.gsis_id` column is a schema change. Existing dev databases
  need to drop and re-`init-db`, or hand-add the column:
  `ALTER TABLE players ADD COLUMN gsis_id VARCHAR(32); CREATE INDEX
  ix_players_gsis_id ON players(gsis_id);`
- Production databases get an Alembic migration when we add Alembic (not in
  this PR — kept scope tight).

---

<!-- Future entries below. Newest first. -->
