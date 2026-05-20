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

## v0.5.0 — RAS (Relative Athletic Score) source (PR #4)

**Date:** 2026-05-20

**What changed**
- New ranking source: `ras` (slug). Reads Kent Lee Platte's RAS database
  from a local CSV at `data/ras/ras_database.csv` (overridable via
  `DYNASTY_RAS_CSV_PATH` env var). Adapter is forgiving on column casing
  and accepts the most common aliases for `name`, `pos`, `college`, `year`,
  `RAS`.
- Filters to skill positions (QB/RB/WR/TE; FB folded into RB). Computes
  *per-position-per-draft-class* ranks: within a draft year, the WR with
  the highest RAS gets rank 1, second-highest rank 2, etc.
- Only the last 6 draft classes emit ranking rows (older RAS rows still
  enrich the Player table). All emitted rows are flagged `is_rookie_only`
  because RAS is fundamentally a pre-draft / draft-class signal — vets'
  rankings should come from production, not their Combine 7 years ago.
- Stores the raw 0–10 score in `market_value` so the value-based
  normalization branch in `scoring.py` can use it directly (RAS 10 → 100
  composite contribution from this source; RAS 1 → 10).
- `default_weight = 0.8`, `category = model`. The position-aware weighting
  in PR #6 will boost this to ~1.5× for WR/TE/RB and zero it for QB.
- If the CSV file is missing the adapter yields nothing rather than
  raising, so `sync-all` keeps working out of the box.

**Why**
- Research doc §A2: RAS shows a small but positive correlation with NFL
  fantasy production at WR/TE/RB. More importantly, low RAS is a strong
  *bust filter* — prospects with RAS < 5 substantially underperform their
  draft capital. RAS is best deployed as a tail-risk dampener on the
  composite, not as a primary signal.
- ToS-clean: Kent Lee Platte explicitly encourages redistribution with
  attribution. We picked the local-CSV approach because Kent doesn't host
  a stable public download URL; the file goes in `data/ras/` (gitignored).

**Expected output shift**
- Once the CSV is dropped in: rookies with high RAS (≥9) get a small
  upward bump in the composite — not enough to override draft capital or
  market value, but enough to break ties.
- Rookies with RAS ≤ 4 in a position where athleticism matters (WR/TE)
  get a small downward drag.
- For QBs, the effect should be near-zero (RAS rank emits but is one of
  many signals); PR #6 will explicitly zero out the position weight for
  QBs anyway.
- Veterans are unaffected (emit-window cutoff + rookie-only flag).

**Validation**
- Backtest at WR specifically: `backtest_source("ras", years=[2020..2023],
  window_years=3)` filtered to WR should show a small positive Spearman
  correlation (~0.10–0.20 in published studies). If it's flat or
  negative, the position-by-position weighting in PR #6 will adjust.
- Bust-filter validation: for the rookie cohort, look at the bottom RAS
  quartile and confirm their realized Year-1 PPR points are below their
  draft-capital expectation.

**Files touched**
- `src/dynasty/sources/ras.py` (new)
- `src/dynasty/sources/__init__.py` — registry entry
- `data/ras/README.md` (new) — how to drop the CSV in
- `.gitignore` — ignores `data/ras/*.csv|xlsx|tsv` so the data file isn't
  accidentally committed
- `tests/test_ras.py` (new) — fixture-driven, no network

---

## v0.4.0 — FantasyFootballCalculator ADP source (PR #3)

**Date:** 2026-05-20

**What changed**
- New ranking source: `ffc_adp` (slug). Pulls FantasyFootballCalculator's free
  public ADP REST API across four formats: PPR redraft, 2QB (Superflex),
  Dynasty, and Rookie.
- `category = market`. This is now our second market signal alongside
  FantasyCalc.
- `default_weight = 0.7` (per research §A3, lower than FantasyCalc).
- Filters to QB/RB/WR/TE (K and DEF are dropped). Synthesizes a 0..300
  `market_value` from inverse-ADP so the value-normalization branch in
  `scoring.py` can use either rank- or value-based normalization.
- Graceful handling of pre-season 404s for niche formats: skip rather than
  fail the whole sync.

**Why**
- Research doc §A3: FFC's user base skews casual / redraft, which makes it a
  *noise-uncorrelated* second market signal to FantasyCalc (dynasty-leaning
  revealed-preference from real Sleeper trades). Two market sources with
  uncorrelated user populations average to a steadier consensus, and
  divergence between them is its own buy/sell signal, especially for rookies
  where FantasyCalc lags post-draft sentiment.
- ToS-clean: FFC explicitly invites third-party use of the REST API.

**Expected output shift**
- Rookie composite rankings get a fresher market read: FFC's rookie-only
  endpoint reacts within hours of draft night, while FantasyCalc rookie
  values can lag several days.
- Players who are *consensus market favorites* (high agreement between
  FantasyCalc and FFC) get reinforced in the composite — their consensus
  rank becomes more stable.
- Players FFC's casual users overrate but FantasyCalc's traders don't
  (typical case: a flashy WR/RB rookie with low draft capital but a hype
  cycle) will get a small upward bump in the composite. That's the *casual*
  signal coming through, deliberately. The position-specific weighting in
  PR #6 will let us cap this if needed.
- `rank_divergence` (model vs. market) becomes a richer signal because the
  consensus side now blends two independent crowds.

**Validation**
- After several syncs, `SELECT slug, COUNT(*) FROM sources s JOIN rankings r
  ON r.source_id = s.id WHERE r.league_format = 'sf_ppr' GROUP BY 1` should
  show `ffc_adp` accumulating rows daily.
- Backtest comparison: `backtest_source("ffc_adp", years=[2022,2023],
  window_years=3)` Spearman correlation should be *positive* but *lower
  than FantasyCalc's* (casual ADP is noisier than revealed-preference trade
  data). If it comes in higher, that's a surprising and worth-investigating
  result.

**Files touched**
- `src/dynasty/sources/ffc_adp.py` (new)
- `src/dynasty/sources/__init__.py` — registry entry
- `tests/test_ffc_adp.py` (new) — fixture-driven, no network
- `docs/CHANGELOG-model.md` (this entry)

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

<!-- Future entries below. Newest first. -->
