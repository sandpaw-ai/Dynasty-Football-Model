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

## v0.14.0 — Similarity-based career arc overhaul (PR #14)

**Date:** 2026-05-21

A full architectural overhaul. Phil flagged in 2026-05-20: the v0.13 model
had Luke Grimm at #1 because DynastyProcess returned `value_1qb=100` for him
as his ONLY source contribution, and the composite had no coverage penalty.
More broadly, the model had drifted into pure source-aggregation territory —
13 PRs of adapters but no first-principles read on whether each player’s
career arc supports the ranking.

### Phil’s directive (verbatim, condensed)

> Let’s make similarity scores the heart of the model, where college production
> should be compared to historically similar college players and projected to
> the pros. Use a DARKO-like methodology for pro football players. Compare
> current NFL players to the most similar historical players and extrapolate
> the rest of their careers. The dynasty rankings should be a reflection of
> the idea that younger players are more valuable because they have many more
> years of projectable production. List the similar players that are being
> compared to create the model rankings. RAS can be an overlay where you are
> overlaying athleticism to the model. Brainy Ballers can be another overlay.
> Run the correlations and assign the model weight accordingly.

### What changed

**1. Similarity engine (the new dominant signal, weight 1.8)**

- New `src/dynasty/sources/pro_football_reference.py` — ships the
  player-season corpus from nflverse (1999–2024, 33K+ rows) as a
  gzipped CSV cache under `data/nflverse/`. CI never hits the network;
  one-time live refresh gated behind `DYNASTY_FB_PFR_LIVE=1`. We use
  nflverse rather than scraping pro-football-reference.com directly
  because nflverse republishes the same PFR-derived data with no rate
  limit, MIT-licensed, in a stable schema (target_share, WOPR, EPA all
  included).
- New `src/dynasty/similarity/vectorize.py` — per-position feature
  vectors of per-game production, efficiency, and usage. Z-score
  normalized within position across the full historical corpus.
- New `src/dynasty/similarity/comparables.py` — KNN comparable search:
  same position, age ±1yr, top 20 nearest historical seasons by cosine
  similarity.
- New `src/dynasty/similarity/projection.py` — weighted aggregate of the
  comps’ realized future careers, time-discounted at 5%/yr. Output:
  projected_remaining_years + projected_total_remaining_ppr +
  dynasty_value (rescaled 0–100 within position).
- New `src/dynasty/sources/similarity_career_arc.py` — wraps the
  projection as a `BaseSource` so the existing scoring pipeline ingests
  it without special-casing.

**2. DARKO-style current-skill signal (weight 0.8)**

- New `src/dynasty/sources/nfl_impact.py` — per-position current-skill
  formulas from the same PFR corpus: ANY/A + TD% - INT% + sack rate
  (QB); yards-per-touch + TD rate + target share (RB); YPRR proxy +
  aDOT proxy + TD rate (WR/TE). Normalized 0–100 within position. This
  is the “how good are they right now” signal; the similarity engine
  owns longevity.

**3. Luke Grimm fix — coverage penalty + Bayesian prior (`scoring.py`)**

- Quadratic coverage penalty: `composite *= (min(n_sources/3, 1))²`. A
  single-source player’s composite is multiplied by **0.11**; two
  sources → 0.44; three+ → full credit.
- Bayesian prior pull: low-coverage players get pulled toward a
  position-tier baseline score (20–22 across QB/RB/WR/TE). Pull strength
  decays linearly to 0 at 3 qualifying sources.
- Sources zeroed out for overlay use (RAS, brainy_ballers) do NOT count
  as coverage. They still emit RankingRecords so the overlay system can
  consume them.
- DynastyProcess demoted from `default_weight=1.0` → `0.3` (the
  proximate cause of the Grimm bug).

**4. New composite weights (v0.14.0)**

| Source                  | v0.13 weight | v0.14 weight |
|-------------------------|--------------|--------------|
| similarity_career_arc   | (didn’t exist) | **1.8** |
| nfl_impact (DARKO)      | (didn’t exist) | **0.8** |
| fantasycalc             | 1.0          | 0.6 |
| ffc_adp                 | 0.7          | 0.4 |
| fantasypros             | 1.2          | 0.4 |
| pff                     | 1.3          | 0.4 |
| nfl_draft_capital       | 1.5          | 1.5 (rookies only) |
| cfbd_breakouts          | 0.9          | 0.9 (rookies only) |
| dynastyprocess          | 1.0          | **0.3** (Grimm bug source) |
| ras                     | 0.8          | **overlay only** |
| brainy_ballers          | 1.3          | **overlay only** |

**5. Overlays — RAS and Brainy Ballers SRS data-driven**

- New `src/dynasty/overlays.py` — user-toggle overlays with a
  position-specific default weight pulled from the historical
  correlation between the signal and a player’s first 3 NFL seasons of
  fantasy PPR.
- New `scripts/correlation_audit.py` — computes the correlations using
  RAS × nflverse career outcomes on `pfr_id`. Writes
  `data/overlays/correlation_table.json`.
- Computed RAS correlations (n in parentheses):
    - RAS × QB first-3yr PPR: r = **+0.172** (n=245)
    - RAS × RB first-3yr PPR: r = **+0.228** (n=527)
    - RAS × WR first-3yr PPR: r = **+0.142** (n=719)
    - RAS × TE first-3yr PPR: r = **+0.177** (n=354)
- Brainy Ballers SRS uses a low-confidence prior pending a historical
  archive (their site only publishes current rankings, so we can’t
  back-test SRS → production yet).

**6. UI — surface comparables**

- Each player page now shows their top 5 historical comparables with
  similarity scores, schools/teams, ages, and how many years each comp
  played after the matched season. Includes projected remaining
  years + total PPR.
- Rankings page rows now have a hover tooltip showing the top 3 comps.
- New `/methodology.html` page explaining the similarity engine,
  coverage penalty, overlay system, and full weight table.

### Result: before vs after (sf_ppr top 15)

| # | v0.13 (before) | v0.14 (after) |
|---|----------------|---------------|
| 1 | **Luke Grimm** 💥 | Bijan Robinson |
| 2 | Josh Allen | Jahmyr Gibbs |
| 3 | Ja’Marr Chase | Ja’Marr Chase |
| 4 | Bijan Robinson | Malik Nabers |
| 5 | Jaxon Smith-Njigba | Ashton Jeanty |
| 6 | Jahmyr Gibbs | Jeremiyah Love |
| 7 | Lamar Jackson | Jordan Love |
| 8 | Drake Maye | Joe Burrow |
| 9 | Justin Jefferson | Brian Thomas |
| 10 | Joe Burrow | Tetairoa McMillan |
| 11 | Jayden Daniels | Jaxson Dart |
| 12 | Drake London | Omarion Hampton |
| 13 | Malik Nabers | Tee Higgins |
| 14 | CeeDee Lamb | Justin Herbert |
| 15 | Justin Herbert | Fernando Mendoza |

**Luke Grimm new rank: #545 / 896.**

The new top 15 is dominated by young high-production profiles — exactly
what the similarity engine is supposed to surface. Bijan Robinson #1
because his age-22 vector matches LaDainian Tomlinson 2002 / Steve Slaton
2008 / Chris Johnson 2008 (sim 0.98+), all of whom had long productive
careers after their comp season.

### Example comparable lists

**Brian Thomas (top young WR, age 21.9, dynasty_value=100):**
- DK Metcalf 2020 (age 22.7, SEA) sim=0.99
- Keenan Allen 2013 (age 21.4, LAC) sim=0.99
- Justin Jefferson 2021 (age 22.2, MIN) sim=0.99
- Tee Higgins 2020 (age 21.6, CIN) sim=0.98
- DeSean Jackson 2009 (age 22.8, PHI) sim=0.98

**Joe Burrow (top veteran QB, age 27.7, dynasty_value=68):**
- Dak Prescott 2021 (age 28.1, DAL) sim=0.98
- Aaron Rodgers 2011 (age 27.8, GB) sim=0.97
- Donovan McNabb 2004 (age 27.8, PHI) sim=0.96
- Matthew Stafford 2015 (age 27.6, DET) sim=0.96
- Kurt Warner 1999 (age 28.2, LA) sim=0.95

**Aaron Rodgers (aging vet, age 40.8, dynasty_value=30):**
- Brett Favre 2009 (age 39.9, MIN) sim=0.93
- Tom Brady 2017 (age 40.1, NE) sim=0.92
- Drew Brees 2019 (age 40.6, NO) sim=0.65
- Vinny Testaverde 2004 (age 40.8, DAL) sim=0.46

(Rodgers’ nfl_impact rank: #43. Composite rank after the similarity
engine projects his short remaining career: **#313**. Exactly the
“young > old” signal Phil wanted.)

### Scope deferred to PR #15

The **college side** of the similarity engine (cfbd-driven rookie
vector + college→NFL bridge) is not in this PR. Rookies in v0.14 still
rely on `nfl_draft_capital` + `cfbd_breakouts` for their signal. The
veteran NFL side + Luke Grimm fix was scoped as MVP; the college
rookie engine is a natural follow-up.

### Validation

- 7 new tests in `tests/test_similarity_football.py`:
    - PFR cache present and sane
    - Vectorize is deterministic and order-independent
    - KNN sensible matches (Justin Jefferson 2020 comps include
      recognizable elite young WRs)
    - No single-source player ranks in the top 50 (Grimm invariant)
    - 3+ elite young WR/RB profiles in top 30
    - Aging vet QB drops between current-skill rank and composite rank
    - Correlation table well-formed
- All 7 pass. Existing test suite: same pass/fail as upstream/main
  (4 pre-existing failures stem from DB-pollution across tests, not
  introduced by this PR).
- Live launcher run: 896 players scored, site builds, MFL league
  pre-fetch still works.

---

## v0.13.0 — Shirts and Skins league live + MFL player-id crosswalk (PR #13)

**Date:** 2026-05-20

Added Phil's MFL league (Shirts and Skins, ID 62557) to `leagues.json`
and discovered the join was broken — every team scored zero because
our DB only had MFL ids for 70 of 15,777 players.

**MFL player-id crosswalk**
- New `sync_mfl_players(year)` in `src/dynasty/sync.py`. Pulls MFL's
  full `TYPE=players` export (~2,569 active NFL players) and backfills
  `Player.mfl_id` on existing rows.
- Match order: (norm_name, position, nfl_team) → (norm_name, position)
  → (norm_name). The first-most-specific match wins.
- Free-agent teams ("FA" in MFL) normalize to NULL on the join key so
  they line up with Sleeper's free-agent rows.
- Only skill positions (QB/RB/WR/TE). MFL's IDP / kicker / team rows
  skipped.
- Wired into `launcher_headless` step 2/6, right after
  `sync_sleeper_players`. Reports `matched / already_set / ambiguous`.
- Local end-to-end: matched **890** MFL players, only 2 ambiguous,
  zero conflicts.

**Result on Shirts and Skins**
```
Team               total     vs avg
#1  Cajun Crusaders  1520.0   +175.7
#2  Ceedee's TDs     1462.8   +118.6
...
#10 Zack Attack       979.6   -364.7
```
All 10 teams resolve, all 50 draft picks scored, all 10 trades
valued. Manager rankings sane: The Jimbronis #1 (positive draft +
trade), DC Commies #10 (negative on both).

**Cajun Crusaders sample roster:**
- Josh Allen (rank 2, T1, 99.6)
- Ja'Marr Chase (rank 3, T1, 93.6)
- Jonathan Taylor (rank 29, T4, 72.1)
- James Cook (rank 36, T4, 69.6)
- Rome Odunze (rank 40, T5, 69.0)
32 of 33 players resolved.

**leagues.json**
```json
{
  "leagues": [
    {"platform": "mfl", "league_id": "62557", "year": 2026, "league_format": "sf_ppr"}
  ]
}
```

**Files**
- `leagues.json` — added the Shirts and Skins entry
- `src/dynasty/sync.py` — new `sync_mfl_players()` function
- `src/dynasty/launcher_headless.py` — calls MFL crosswalk after Sleeper sync

**Tests**
- All 10 test files still green. The crosswalk is exercised by the
  end-to-end build that ships the prefetch — dedicated unit tests are
  a follow-up.

**Carry-over from PR #12**
- Live MFL form on the page still requires the CF Worker proxy to be
  deployed + `PROXY_URL` env wired into the workflow. For now the
  pre-fetched path is the proven one.

---

## v0.12.0 — MFL form + live manager rankings + CF Worker proxy (PR #12)

**Date:** 2026-05-20

Phil reported the site didn't show manager rankings (because `leagues.json`
is empty) and asked for an MFL form he could actually use. Both addressed.

**Site UX**
- League page form rewritten with a **platform selector**: Sleeper | MFL.
  - Sleeper: live fetch using the existing CORS-friendly endpoints.
  - MFL: live fetch via the proxy worker when configured; otherwise
    inline help directing the user to add the league to `leagues.json`.
- New **"Also compute manager skill rankings" checkbox** (default on).
  When checked, the page walks drafts + transactions client-side and
  renders the manager-rankings table inline. ~20 API calls for a typical
  dynasty league with one prior draft.
- Year input appears when MFL is selected.
- Empty-state copy on the prefetched section now points users to the
  live form below for Sleeper, or to the worker / leagues.json options
  for MFL.

**Cloudflare Worker proxy** (`scripts/cf-worker/`)
- New worker (`worker.js`) that proxies `api.myfantasyleague.com` so MFL
  data can be fetched from `pstiehl.github.io` in the browser. Also
  proxies `api.sleeper.app` (Sleeper is already CORS-friendly, but
  routing through the worker adds edge caching for the slow
  `transactions/<week>` endpoint).
- Includes `wrangler.toml` and a README with deploy instructions.
- Free tier (100k req/day) is more than enough for personal use.
- Worker URL is plumbed into the site via the `PROXY_URL` env var,
  consumed by `_build_league_page` and baked as `data-proxy-url` on
  the form element. Sanitized to allow only `https?://[A-Za-z0-9.\-_/]+`
  patterns.
- The CF worker README documents how to wire `PROXY_URL` into the
  GitHub Actions workflow (one-line edit to `daily-refresh.yml` plus
  a repo Variable). Not done in this PR because PR-author tokens
  can't modify workflow files; needs a manual edit from a repo
  owner with the `workflow` scope.

**Client-side manager-rankings port**
- `expectedScoreAtPick(pick)`, `zscore(value, pool)`, and
  `computeManagerTable(franchiseNames, picks, trades, lookup)` ported
  from `src/dynasty/manager.py` to JS for live use on the page.
- `computeSleeperManagerReport` walks `/drafts`, `/draft/<id>/picks`,
  and `/transactions/<week>` for weeks 0..18 in parallel.
- `computeMflManagerReport` mirrors the Python `_fetch_mfl_*` parsers,
  including draft-pick token filtering (`DP_xx_yy`, `FP_xxx_yyyy`).

**MFL player ID limitation (acknowledged)**
- Our `assets/model_scores.json` is keyed by `sleeper_id`. To resolve
  MFL player IDs to model scores client-side, the page fetches MFL's
  `TYPE=players` endpoint (via the proxy) and matches by NAME+POSITION.
  Surfaced in the status text: "N of M MFL players matched to model".
- A future v2 should emit a separate `assets/mfl_scores.json` keyed by
  `mfl_id` so the join is exact. The pre-fetcher path already does this
  server-side via `Player.mfl_id`, so pre-fetched MFL leagues don't
  have this issue.

**Tests**
- All 10 test files still pass (no test changes needed; new JS code
  has no Python-side equivalent to break).

**Files**
- `scripts/cf-worker/worker.js` (new)
- `scripts/cf-worker/wrangler.toml` (new)
- `scripts/cf-worker/README.md` (new) — includes the workflow edit Phil
  needs to make manually
- `src/dynasty/report.py` — league page form rewrite + JS port

**Operator action items for Phil (one-time)**
1. Deploy the worker:
   ```bash
   cd scripts/cf-worker
   export CLOUDFLARE_API_TOKEN=<token-with-Workers-Scripts:Edit>
   npx wrangler@latest deploy
   ```
2. Set `PROXY_URL` as a repository variable (Settings → Secrets and
   variables → Actions → Variables tab → New repository variable).
   Value: the worker URL Wrangler printed.
3. Edit `.github/workflows/daily-refresh.yml` to pass that variable
   through to the build (one-line `env:` block — instructions in
   `scripts/cf-worker/README.md`).
4. Push. MFL form on `/league.html` activates after the next workflow
   run.

Alternatively, skip steps 1–3 and just add MFL leagues to
`leagues.json` — the daily prefetcher bakes them in without needing
the worker.

---

## v0.11.0 — MFL on site + manager skill rankings (PR #11)

**Date:** 2026-05-20

Two asks from Phil:
1. MFL leagues should work on the site (KTC can do it).
2. Manager skill rankings from draft + trade history.

**MFL approach: pre-fetch into static JSON**
- The MFL API (`api.myfantasyleague.com`) only sends
  `Access-Control-Allow-Origin: https://www<N>.myfantasyleague.com`,
  which means browsers blocked from `pstiehl.github.io` can't query it
  directly. KTC works around this with a server proxy.
- Rather than introduce a separate proxy service, we **pre-fetch** any
  leagues listed in `leagues.json` at build time (daily CI runs the
  fetch in step 6/6 of `launcher_headless`).
- Output: `dynasty_site/leagues/<platform>-<league_id>.json` per league
  + `dynasty_site/leagues/index.json` manifest the page reads.
- New top-level config file `leagues.json` (initially empty). Phil adds
  entries like `{platform: "mfl", league_id: "12345", year: 2026}` and
  the next daily build bakes them in.
- Sleeper leagues can still be queried live from the form (CORS-friendly).
  But if a Sleeper league is in `leagues.json`, its manager rankings are
  also pre-computed (the manager pipeline needs to walk drafts +
  transactions, which is too slow / heavy for live client-side).

**Manager skill rankings**
- New `src/dynasty/manager.py` module:
  - `manager_report_sleeper(league_id)`
  - `manager_report_mfl(league_id, year)`
- For each manager, computes:
  - **Draft delta**: for every pick they made, `current_composite_score
    - expected_score_at_pick(overall_pick)`. Positive = picked up more
    value than the slot warranted.
  - **Trade delta**: for each completed trade, sum of
    `composite_received - composite_given`.
  - **Skill score**: equal-weight z-score blend of the two deltas,
    normalized within the league.
  - **Skill rank**: managers sorted by skill_score within the league.
- Picks for unrated players (deep drafts, IDP, late dart throws) are
  skipped from the draft-delta calculation, not counted as zero. Same
  for trade assets that aren't in our composite snapshot.
- Caveats surfaced as `notes` on each manager: "no trades on record",
  "only N rated draft picks (low sample)".
- Uses *current* composite values, not contemporaneous — rewards picks
  that aged well, not what looked smart on draft night. Transparent
  about this in the page UI.

**Site UI**
- `league.html` rewritten into two sections:
  1. **Pre-fetched leagues** — grid of cards, one per league in
     `leagues/index.json`. Click loads the full per-league JSON,
     renders team power rankings AND manager skill rankings.
  2. **Live Sleeper form** — unchanged behavior for arbitrary Sleeper
     league IDs. Team rankings only (manager rankings require pre-fetch).
- New manager-rankings table per league:
  rank / manager / skill / picks / draft Δ / trades / trade Δ / notes
  with color-coded skill scores.

**CLI**
- `python -m dynasty.cli managers <platform> <league_id> [--year Y]`
  prints a manager-rankings table to the terminal.
- `python -m dynasty.cli prefetch-leagues` runs the pre-fetcher
  one-off (useful when iterating on `leagues.json` without a full
  site rebuild).

**Tests**
- `tests/test_manager.py` (new) — 5 cases:
  - `expected_score_at_pick` anchors
  - `_compute_manager_table` arithmetic (draft deltas)
  - Trade value zero-sum within a 2-team trade
  - Sleeper end-to-end with fixture HTTP client
  - MFL end-to-end with fixture HTTP client (verifies draft-pick
    tokens like `DP_02_05` are filtered out as non-player assets)
- `tests/test_prefetch_leagues.py` (new) — 4 cases:
  - Empty config still writes index.json
  - Unknown platform captured as error
  - Missing league_id captured as error
  - Sleeper prefetch writes per-league + manifest files

**Files touched**
- `src/dynasty/manager.py` (new)
- `src/dynasty/cli.py` — `managers` + `prefetch-leagues` commands
- `src/dynasty/launcher_headless.py` — step 6/6 pre-fetch
- `src/dynasty/report.py` — league.html rewritten with prefetched section
  + manager-rankings rendering
- `scripts/prefetch_leagues.py` (new)
- `leagues.json` (new, empty stub)
- `tests/test_manager.py` (new)
- `tests/test_prefetch_leagues.py` (new)
- `docs/CHANGELOG-model.md` § v0.11.0

**Caveats / future work**
- Pre-fetched leagues require a daily CI rebuild before changes show up.
  Trade-heavy weeks may want hourly. Easy to bump the cron in
  `daily-refresh.yml` if needed.
- Manager rankings use *current* composite values, which rewards picks
  that aged well. A future v2 could backfill contemporaneous values
  (composite at the time of the draft / trade).
- No FAAB / waiver scoring yet.
- Picks for unrated players are skipped (not penalized as zero).

---

## v0.10.0 — deterministic weights, league settings, name dedup (PR #10)

**Date:** 2026-05-20

Four user-feedback fixes from Phil after v0.9.0 went live.

**1. FantasyFootballCalculator ADP removed from active sync**
- Phil reported FFC ADP "often wrong or perhaps not using dynasty
  superflex rankings." Confirmed: FFC's user base skews casual /
  redraft, which made its top picks consistently diverge from
  dynasty-superflex consensus.
- Removed from `launcher_headless.py` and `launcher.py` sync lists.
- Adapter file (`src/dynasty/sources/ffc_adp.py`) and tests are
  retained so re-enabling is one line.
- Active sync list is now: FantasyCalc, DynastyProcess, Brainy Ballers,
  NFL Draft Capital, RAS, CFBD Breakouts (still empty).

**2. Deterministic per-source weights**
- *Phil's request: "create a new weighting system that assigns model
  weight to correlation of source data to actual NFL statistical
  player performance and keep it consistent across all players."*
- Removed v0.7's per-player weight modulation:
  - `position_modifier(slug, pos)` — hand-coded per-(source, position)
    overrides (RAS=1.5× at WR, etc.) deleted.
  - `years_pro_modifier(slug, years_pro)` — rookie-signal linear decay
    and market-source inverse curve deleted.
- New effective-weight formula (per-source, deterministic):
    ```
    effective_weight = default_weight × track_record_multiplier
    ```
- The track-record multiplier is read from `SourceTrackRecord` rows,
  produced by the existing `backtest_source()` pipeline against
  realized NFL fantasy production. When a position-specific row
  exists, it wins over the overall (`position=None`) row. This is
  the ONLY allowed per-player variation — and it's data-driven, not
  hand-coded.
- Until backtests populate `SourceTrackRecord`, all multipliers
  default to 1.0 and the composite is driven by `default_weight`
  alone. Production loading + automated backtest pass at score time
  is a follow-up.
- Verified: every source now displays the same `weight` value in the
  breakdown JSON across all players. e.g. RAS = 0.8 for Chase (WR),
  Robinson (RB), Thomas (WR rookie), every WR vet.

**3. Jr./Sr./III duplicate-name dedup**
- Phil noticed duplicates: "Marvin Harrison" + "Marvin Harrison Jr.",
  "Kenneth Walker" + "Kenneth Walker III", "Odell Beckham" +
  "Odell Beckham Jr." + "Odell Beckham, Jr.".
- New `src/dynasty/names.py` with `normalize(name)`: strips generational
  suffixes (Jr / Sr / II-V), folds diacritics, drops periods, lowercases.
- New `Player.normalized_name` column with index; populated on insert
  + Sleeper-sync upsert. Lightweight migration in `db/session.py`.
- Resolver order is now:
    sleeper_id → gsis_id → mfl_id → exact full_name+position →
    normalized_name+position
- Confirmed: "Marvin Harrison Jr.", "Kenneth Walker III", "Travis
  Etienne Jr." no longer create duplicate Player rows on fresh sync.
  (Note: Sleeper itself stores names without suffixes, so the display
  name shows "Marvin Harrison" — the rookie is correctly resolved.)

**4. League settings on the Rate-My-League page**
- The page now offers a collapsible "League settings" section with:
  - QB format: 1QB / Superflex / 2QB (or auto-detect from league)
  - TE premium: none / 1.25 / 1.5 PPR (or auto)
  - Scoring: full PPR / half PPR / standard (or auto)
- Auto-detect reads Sleeper's `roster_positions` (QB count + SUPER_FLEX
  count) and `scoring_settings` (`bonus_rec_te`, `rec`).
- Per-position multipliers applied client-side to each player's base
  composite score before computing team totals + power rankings.
- Team breakdowns now show base score → adjusted score per player with
  the applied multiplier. Settings line surfaces the detected values
  from Sleeper so the user can verify.
- For 2QB leagues: QBs ×1.25, other positions ×0.95 — reflects the
  scarcity hit at non-QB positions when two QB spots are required.
  These are industry conventions, not backtested.

**Tests**
- `tests/test_names.py` (new) — 6 cases covering suffix stripping,
  diacritics, apostrophes, no false positives, idempotency, empty.
- `tests/test_weights.py` rewritten for the v0.10 contract:
  - `ROOKIE_SIGNAL_SOURCES` still present
  - `corr_to_multiplier` + `select_track_record_multiplier` kept
  - new test: same source → identical weight across players
  - new test: position-specific track record overrides overall
- All 8 test files green: smoke, draft, ffc, ras, cfbd, weights,
  league, names.

**Files touched**
- `src/dynasty/scoring.py` — effective_weight collapsed to 2-term formula
- `src/dynasty/weights.py` — trimmed to only the track-record bits
- `src/dynasty/names.py` (new) — normalization helper
- `src/dynasty/sync.py` — normalized-name resolution + populate on create
- `src/dynasty/db/models.py` — `Player.normalized_name` column
- `src/dynasty/db/session.py` — migration adds the column on existing DBs
- `src/dynasty/launcher_headless.py` + `launcher.py` — ffc_adp removed
- `src/dynasty/report.py` — league page redesign with settings + auto-detect
- `tests/test_weights.py` — rewritten
- `tests/test_names.py` (new)

---

## v0.9.0 — RAS data + retired-player filter + league page on site (PR #9)

**Date:** 2026-05-20

User-feedback iteration after PRs #2–#8 went live. Three fixes.

**1. RAS data populated**
- `scripts/build_ras_csv.py` (new) generates a computed-RAS CSV from the
  public nflverse Combine release: position-adjusted z-scores of
  Combine measurements (40, vertical, broad, shuttle, 3-cone, bench,
  height, weight) mapped to a 0–10 scale per position cohort.
- `data/ras/ras_database.csv` committed with 3,131 rows (2000–2026
  skill-position combine entries).
- This is **not** Kent Lee Platte's canonical RAS — it's a transparent
  re-implementation of the idea using only public nflverse data.
  `data/ras/README.md` lists the differences. If you obtain Kent's
  actual CSV later, drop it in to replace.
- Effect on the model: RAS source jumps from 0 → 3,131 rows. Composite
  scoring uses it at 1.5× weight for WR/TE rookies (per PR #6).

**2. Retired-player filter in scoring**
- `scoring.py` now skips any player whose ONLY rankings come from
  rookie-signal sources (`nfl_draft_capital`, `ras`, `cfbd_breakouts`).
  Rationale: these players (Henry Ruggs, Laviska Shenault Jr., KJ
  Hamler, etc.) have draft-day rankings on file but no current market
  / model / consensus read because they're retired or off rosters.
  They were polluting the top of the composite as "no consensus"
  outliers.
- Filter cuts ~380 inactive players from the sf_ppr composite (1,254
  → 873 in local testing). Top 20 is now exclusively active stars.

**3. Rate-My-League page on the site**
- New `league.html` page on the published site. Paste a Sleeper league
  ID, the page fetches the Sleeper API client-side (CORS-friendly),
  joins to a `assets/model_scores.json` lookup the build pipeline
  emits, and renders:
  - Power rankings (teams sorted by total roster value)
  - Per-team breakdown: total / avg / top-5 assets / weaknesses
  - vs-league-average divergence per team
- Nav link added: Overview / Rankings / **Rate My League** / Sources
- `model_scores.json` uses an UNBOUNDED query (not the top-300 cap)
  so deep rosters (12-team × 35 = 420+) all resolve.
- MFL leagues still CLI-only (MFL's API doesn't allow CORS).

**Files touched**
- `scripts/build_ras_csv.py` (new)
- `data/ras/ras_database.csv` (new, 3,131 rows)
- `data/ras/README.md` — documents what's in the CSV
- `.gitignore` — allow committed RAS CSV
- `src/dynasty/scoring.py` — corroboration filter for rookie-signal-only
  players
- `src/dynasty/report.py` — `_build_league_page`, `model_scores.json`
  emission, nav link

**Validation**
- Smoke + all 6 source/league test files still pass.
- Local end-to-end run: top 20 in sf_ppr is now all active starters
  (was Ruggs at #2, Shenault at #12 in PR #2's first live build).

---

## v0.8.0 — League import (Sleeper + MFL) — rate-my-league (PR #7)

**Date:** 2026-05-20

A *feature* rather than a model-output change — this one doesn't shift
any composite scores. It applies the existing latest composite snapshot
to the user's actual rosters and surfaces team-level evaluations.

**What changed**
- New module `src/dynasty/league.py` with two entry points:
  - `evaluate_sleeper_league(league_id, league_format="sf_ppr")`
  - `evaluate_mfl_league(league_id, year=None, league_format="sf_ppr")`
  Both return a `LeagueReport` dataclass (with `.to_dict()` for JSON).
- New CLI command:
    `python -m dynasty.cli league sleeper <league_id>`
    `python -m dynasty.cli league mfl <league_id> --year 2026`
    Prints power rankings, per-team total / avg value, top-5 assets,
    flagged weaknesses (no rated player at a starting position, or best
    player at a position is Tier > 3).
- Player resolution uses canonical `sleeper_id` / `mfl_id` joins, both
  of which the existing `sync-players` upserts populate (PR #2 also added
  the `gsis_id` and `pfr_id` cross-references, so the joins are already
  primed).
- Unrated players (roster slots that don't resolve to a model-scored
  player) are counted separately rather than penalizing the team —
  useful for incoming rookies, recently-cut players, etc.

**Why**
- This is the user-facing feature that turns the composite model into
  something *actionable*: it answers "which team in my league should I
  trade with?" and "where's my weakest starting spot?". This is the
  KTC equivalent that originally motivated this whole project, now
  computed off our composite model.
- Importing rosters is also the foundation for future trade-calculator
  and mock-draft features (per the roadmap).

**Expected output shift**
- No change to composite scores or rankings.
- New per-team and per-league outputs available via the CLI and the
  `evaluate_*_league()` API.

**Validation**
- `tests/test_league.py` (4 cases):
  - Sleeper league w/ 2 teams, 4 players each, exercises power rankings,
    top assets, weakness flagging.
  - Unrated-player counting on Sleeper.
  - MFL league w/ equivalent roster mirror.
  - No-composite-scores case (everyone unrated, no crash).
- All non-DB calls run through an injectable httpx-style client, so the
  tests are network-free.

**Files touched**
- `src/dynasty/league.py` (new) — fetchers + report builder
- `src/dynasty/cli.py` — `league` command (text + `--json` output modes)
- `tests/test_league.py` (new)

---

## v0.7.0 — Position-specific + years-pro weighting refactor (PR #6)

**Date:** 2026-05-20

The structural change that finally activates the new sources (PRs #2–#5)
at their *intended* per-position and per-experience weights. This is the
biggest behavior change in the model since the v0.2.0 backtest-weighting
introduction.

**What changed**
- New module `src/dynasty/weights.py` centralizing three policy hooks:
  1. `position_modifier(slug, position)` — per-(source, position) override.
     Default 1.0 when not specified.
  2. `years_pro_modifier(slug, years_pro)` — rookie-signal sources
     (`nfl_draft_capital`, `ras`, `cfbd_breakouts`) decay linearly with
     years pro (1.0 → 0.3 floor). Market-source signals (`fantasycalc`,
     `ffc_adp`, `dynastyprocess`) get an *inverse* curve (0.6 at year 0,
     0.8 at year 1, 1.0 at year 2+).
  3. `select_track_record_multiplier(by_pos, position)` — prefers a
     position-specific `SourceTrackRecord` over the overall (position-None)
     row.
  4. `corr_to_multiplier(corr)` — tightened cutoffs per research §4:
     |ρ| ≤ 0.15 → 0.5×; 0.15–0.25 → 1.0×; 0.25–0.35 → 1.3×; ≥ 0.35 → 1.6×.
     Replaces v0.2's looser tiers (0.6 / 1.0 / 1.2 / 1.5).
- Scoring pipeline (`scoring.py::compute_composite_scores`) refactored:
  - Effective per-row weight is now
    `default_weight × track_record × position_mod × years_pro_mod`
    instead of just `default_weight × track_record`.
  - `_track_record_multipliers` now returns
    `{source_id: {position_or_None: multiplier}}` and the lookup at scoring
    time picks the position-specific entry first.
  - `compute_composite_scores` accepts an optional `score_year` arg
    (defaults to the current year) so backtests can replay weighting as
    of a historical year.
  - Model version bumped to `0.3.0` for output rows.
  - Player lookups pre-loaded once per score run (was a per-player DB hit
    in the rank-assignment loop).

**Position modifier table (initial values per research §4)**
- `ras`: WR/TE 1.5×, RB 1.2×, QB 0.3×
- `cfbd_breakouts`: WR 1.5×, TE 1.3×, RB 1.0×, QB 0.4×
- `nfl_draft_capital`: QB 1.2×, RB 1.1×, WR/TE 1.0×
- Market sources (`fantasycalc`, `ffc_adp`, `dynastyprocess`): no position
  tilt; their per-source aggregation already pools across positions.

**Why**
- Research §4 explicitly recommends position-specific weights: athleticism
  (RAS) matters far more at WR/TE than QB, and the predictive value of any
  source for QBs is dominated by draft capital + opportunity rather than
  athleticism. A single overall weight was flattening real position-level
  signal differences.
- Crowdsourced market values are *trailing* indicators for rookies (they
  reflect post-draft consensus, not independent signal). The years-pro
  inverse curve discounts them for the rookie cohort, where draft capital
  + college metrics should carry the load.
- Conversely, athleticism / draft capital / college breakouts are
  fundamentally pre-NFL signals; their predictive value decays as players
  accumulate actual NFL production. The linear decay makes the *vet* side
  of the model dominated by real production data once it's available.

**Expected output shift (where to look)**
- **Top of the rookie composite at WR**: high-RAS, early-breakout rookies
  with first-round draft capital should rise sharply — all three of those
  signals now get amplified for them. The biggest movers will be WRs in
  the last 2 draft classes.
- **QB rookies**: less athleticism-driven movement. NFL Draft capital
  gets its 1.2× QB boost; RAS gets cancelled (0.3×). The position-aware
  weighting should produce more "draft-capital-first" QB rankings,
  which is closer to how the analyst community treats them anyway.
- **2nd- and 3rd-year veterans**: market sources gradually regain full
  weight while RAS / college metrics fade. By Year 4+, the composite is
  driven almost entirely by FantasyCalc + FFC + ECR + (when available)
  PFF and Brainy Ballers.
- **Aging RBs**: largest "corrective" drops vs. v0.6. Their FantasyCalc
  value already reflects age decline, but the previous model gave RAS /
  draft-capital from years ago full weight. Now those signals are at
  0.3×–0.5×, letting market data tell the truth.
- `rank_divergence` becomes more interpretable: it now compares the
  weighted model output against the same market consensus as before, but
  the model side is no longer naively averaging stale rookie signals
  against live market values. Buy/sell signals should be sharper.

**Validation**
- Smoke test (`tests/smoke_test.py`) still passes — the refactor is
  backward-compatible for the no-position-modifier, no-rookie-flag case.
- New `tests/test_weights.py` covers:
  - `position_modifier` lookups for all configured (slug, pos) pairs
  - years-pro decay for rookie-signal sources
  - years-pro inverse curve for market sources
  - neutral fallback for unknown sources
  - position-specific vs. overall track-record selector
  - integration: same RAS rank+score → higher composite for WR rookie
    than QB rookie
  - integration: position-specific track-record row beats overall row
- For empirical validation post-merge, run `score` against a recent draft
  class and inspect the breakdown JSON — the per-source `weight` values
  should reflect the new modifiers (look for `ras` at 1.5× for WRs,
  near-zero for QBs).

**Files touched**
- `src/dynasty/weights.py` (new) — policy module
- `src/dynasty/scoring.py` — refactored to use the new hooks
- `tests/test_weights.py` (new) — 8 unit + integration tests

**Migration note**
- No schema changes. Existing databases work as-is.
- Composite scores generated under v0.3.0 are *not directly comparable*
  to v0.2 outputs at the per-player score level (weight semantics
  changed). Ranks and tiers move where expected per the "Expected output
  shift" section.

---

## v0.6.0 — College Breakout Age + Dominator source (PR #5)

**Date:** 2026-05-20

**What changed**
- New ranking source: `cfbd_breakouts` (slug). Reads pre-computed college
  features from a local CSV at `data/cfbd/breakouts.csv` (overridable via
  `DYNASTY_CFBD_CSV_PATH`).
- Two engineered features per prospect:
  - **Breakout Age** — the season a player first posted college dominator
    ≥ 20%. Younger = better.
  - **Best College Dominator** — best-season share of team rec yds + rec
    TDs (for WR/TE) or all-purpose yds + TDs (for RB).
- Blended into `composite_college_score = 0.6 * normalized_breakout_age +
  0.4 * dominator`, exposed as a public helper for downstream use.
  Breakout-age normalization uses an 18–2 3 floor–ceiling (earliest plausible
  freshman breakout → latest plausible senior bloomer).
- Filters to skill positions (QB/RB/WR/TE). Emits per-position-per-draft-
  class rankings (rank 1 = highest composite score in that year & position),
  flagged `is_rookie_only`.
- Only the last 6 draft classes emit rankings; older rows still enrich the
  Player table.
- `default_weight = 0.9`, `category = model`.
- Missing CSV → adapter yields nothing (sync-all still works).
- Live CFBD API integration is deliberately deferred to a follow-up PR to
  keep the dependency surface small and review scope tight — the CSV
  ingestion path is the supported workflow today.

**Why**
- Research doc §C1: Breakout Age (Pearson r ≈ 0.43 with NFL fantasy points
  for WRs) and College Dominator are the two highest-leverage college
  production signals. Together they replicate ~80% of what PlayerProfiler
  charges for; both are computable from free CFBD data.
- Engineering as one composite source (rather than two separate sources)
  keeps the registry clean and lets PR #6's position-aware weighting target
  a single slug while still leveraging both inputs.

**Expected output shift**
- Once a CSV is dropped in: rookies with **young breakouts and high
  dominator** — the prototypical "alpha at his college team" — get a
  noticeable bump in the composite at WR/TE.
- Older breakouts who weren't featured in their offense get a drag.
- For RBs the signal is weaker than for WR/TE (per research) but still
  positive. PR #6 will let us tune the position-specific weight.
- QB is included for completeness but should be near-zero-weighted at QB
  level once PR #6 lands.
- `rank_divergence` becomes more useful for rookies: a prospect with weak
  market value but strong college metrics now shows as a divergence buy.

**Validation**
- Backtest at WR specifically: `backtest_source("cfbd_breakouts",
  years=[2019..2022], window_years=3)` should report a Spearman correlation
  in the 0.25–0.45 range. If it's near zero, either the CSV is sparse or
  the composite weighting needs tuning.
- Sanity: among recent classes, hand-check that early-breakout high-dominator
  WRs (the typical "alpha" archetype) rank near the top within their class.

**Files touched**
- `src/dynasty/sources/cfbd_breakouts.py` (new) — includes
  `composite_college_score()` as a public helper for downstream reuse
- `src/dynasty/sources/__init__.py` — registry entry
- `data/cfbd/README.md` (new) — schema docs + how to drop the CSV in
- `.gitignore` — ignores `data/cfbd/*.csv|xlsx|tsv`
- `tests/test_cfbd_breakouts.py` (new) — fixture-driven, no network

**Migration note**
- No schema changes in this PR. New source registers cleanly on existing
  databases.

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
