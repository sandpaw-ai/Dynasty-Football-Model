# Dynasty Fantasy Model — Prospect Source Research

**Author:** Subagent (Ada-stack research) — for Phil Stiehl's `Dynasty-Football-Model`
**Date:** 2026-05-20
**Scope:** Find statistically credible sources to add to the composite ranker, especially for rookies and 2nd-year players. Address KTC question. Suggest weighting.

---

## 1. Executive Summary

The current source mix (FantasyCalc + DynastyProcess/FantasyPros ECR + Brainy Ballers SPS) is actually a *reasonable* skeleton — one market signal, one expert consensus, one production-based model. The biggest gaps are:

1. **No athleticism signal** (RAS / Combine).
2. **No college production / age signal** (Breakout Age, Phenom Index, Dominator).
3. **No NFL Draft capital signal** (single best public predictor of rookie fantasy outcomes; nflverse has it free).
4. **No 1-for-1 substitute for PFF College** in the free tier — it's still the gold-standard hand-graded model and worth a manual CSV slot.

**Top 3 to add first (free + scrapable / API, Tier A):**
1. **NFL Draft capital** via `nflverse/nflreadr` `load_draft_picks()` — single most predictive variable for rookie fantasy production. Free CSV/parquet.
2. **Relative Athletic Score (RAS)** via `ras.football` CSVs (Kent Lee Platte, public) — best free athleticism composite. Modest but real signal, especially for WR/TE/RB.
3. **FantasyFootballCalculator rookie ADP** REST API — second market signal, complements FantasyCalc; tilts toward casual/redraft sentiment which catches things FantasyCalc misses.

**Top 3 to skip / deprioritize:**
1. **KeepTradeCut (KTC)** — explicitly forbids scraping in ToS *and* FAQ. No public API. Adds little marginal value over FantasyCalc for a model. **Do not add.**
2. **FantasyPros direct API (paid)** — DynastyProcess already gives you FantasyPros ECR for free. Don't pay twice.
3. **Player-only "vibes" aggregators** like Dynasty League Football, Dynasty Nerds — paywalled, no public accuracy ledger, duplicative with ECR.

**Biggest insight:** For *prospect* evaluation specifically (rookies + 2nd-year), the literature converges on a small set of variables that matter: NFL Draft capital, age-adjusted college production, athleticism (RAS/Speed Score), and target/touch share (Dominator). Almost every "good" public rookie model is some weighted combination of those four. Crowdsourced market values (FantasyCalc, KTC) are *trailing* indicators for rookies — they reflect post-draft consensus, not independent signal. **Weight market sources lower for rookies than for established players.**

---

## 2. Per-Source Deep Dives

### TIER A — Highly accurate + accessible (add first)

---

#### A1. NFL Draft Capital (via nflverse)
- **URL:** https://nflreadr.nflverse.com/ — `load_draft_picks()` returns full historical draft data; Python equivalent: `nfl_data_py.import_draft_picks()`.
- **Category:** Model input / objective signal (not a "source" in the ranking sense, but the single best variable to encode).
- **Measures:** NFL teams' aggregate evaluation as expressed by where a player was drafted (round + pick). Implicitly includes scouting, athleticism, medicals, character.
- **Track-record evidence:**
  - Multiple PFF studies (and the Brainy Ballers methodology itself) cite NFL Draft pick as the **single strongest predictor of NFL fantasy production**, particularly through Year 3. Brainy Ballers' SPS uses it as a core feature.
  - Pearson correlations of ~0.4–0.6 between draft pick (inverted) and 3-year fantasy points across positions in publicly-replicated studies (see https://apexfantasyleagues.com/2020/05/how-to-evaluate-fantasy-football-players).
  - For RBs specifically, opportunity (which flows from draft capital) explains the majority of fantasy variance — Hayden Winks, PFF, and Sharp Football all report this consistently.
- **Access:** Free, CSV/parquet on GitHub releases.
- **ToS:** MIT/CC0-style nflverse data — scraping/redistribution fine.
- **Update frequency:** Updated post-NFL-Draft (April), then static. Pre-draft, use mock-draft consensus as a proxy.
- **Recommended weight:** 1.5 (highest tier — better than any single ranking source for *rookies* in Year 1). For Year 2+ players, decay to 0.5 as actual NFL production replaces it.
- **Adapter approach:** `nfl_data_py` is already a thin wrapper. Pull `import_draft_picks()`, join to your canonical player table on `gsis_id` or name+pos+college+year. Transform pick → score with something like `score = max(0, 1 - (pick-1)/256)` or use the classic Jimmy Johnson trade-value chart (publicly available).

---

#### A2. Relative Athletic Score (RAS)
- **URL:** https://ras.football/ — Kent Lee Platte's public RAS database; he releases CSVs and posts every prospect's score on X/Twitter (@MathBomb). Historical CSV: https://github.com/mrcaseb/ffopportunity-data or Kent's own downloads.
- **Category:** Model (athleticism composite).
- **Measures:** Position-adjusted z-score of Combine/Pro Day testing (40, vertical, broad, shuttle, 3-cone, bench, height, weight) on a 0–10 scale.
- **Track-record evidence:**
  - Brainy Ballers' multi-position studies (WR, TE, LB): WR RAS has a *weak* but positive correlation with NFL fantasy production (~0.10–0.15 Pearson); TE and LB show similar small positive signal. (https://brainyballers.com/nfl-wide-receivers-does-ras-matter-a-comprehensive-analysis/)
  - For *busts*, low RAS (<5) is a much stronger negative filter than high RAS is a positive — useful as a tail-risk flag.
  - Sharp Football Analysis: "the most useful single-number summary of Combine performance for fantasy purposes." (https://www.sharpfootballanalysis.com/fantasy/what-nfl-combine-results-mean-for-fantasy/)
- **Access:** Free, downloadable CSVs. Twitter announces per-player scores live during Combine week.
- **ToS:** Kent Lee Platte explicitly encourages use and redistribution with attribution.
- **Update frequency:** Annually around Combine (late Feb/early March) + Pro Days.
- **Recommended weight:** 0.8 (modest signal, but free and complements other sources well). Position-specific — see §4.
- **Adapter approach:** Pull CSV from Kent's site or mirror it on GitHub. Join on name + draft year. Use RAS directly as a feature (0–10) or convert to a 0–1 score. Treat missing RAS (no Combine/Pro Day) as median (5.0) with a flag.

---

#### A3. FantasyFootballCalculator ADP
- **URL:** https://fantasyfootballcalculator.com/api — public REST API, free. Rookie ADP: https://fantasyfootballcalculator.com/adp/rookie
- **Category:** Market (redraft + dynasty rookie ADP from real drafts).
- **Measures:** Real ADP from drafts run on their platform. Skews casual but very current.
- **Track-record evidence:** FFC publishes accuracy comparisons; they claim "draft rankings outperform 91% of experts." (https://fantasycalc.com/ — wait, that's FantasyCalc, not FFC. FFC's own accuracy claims are less rigorously published. Treat as a *second* market data point, not an oracle.)
- **Access:** Free public REST API, JSON. `GET https://fantasyfootballcalculator.com/api/v1/adp/standard?teams=12&year=2026`.
- **ToS:** Explicitly invites third-party use ("Use this ADP data for free in your website or application with our REST API").
- **Update frequency:** Continuous (live as drafts happen).
- **Recommended weight:** 0.7 (lower than FantasyCalc because the user base skews casual/redraft, but useful as a noise-uncorrelated second market signal).
- **Adapter approach:** `requests.get(...)` against the JSON endpoint. Match on name+pos. Especially valuable for **rookie ADP** as a sanity check on FantasyCalc which can lag rookie movement.

---

#### A4. Hayden Winks / Underdog Rookie Model (free portion)
- **URL:** https://underdognetwork.com/ (Hayden Winks pieces are free on Underdog Network); X: @HaydenWinks
- **Category:** Expert / model (production + draft capital + opportunity-based)
- **Measures:** Production-weighted prospect model. Especially strong on RB opportunity / WR target-share projections.
- **Track-record evidence:** Hayden is one of the most consistently top-ranked analysts in the FantasyPros accuracy contest year over year (no precise number citable, but he's regularly in the top 5 for rookie/dynasty content). His Underdog "Best Ball Mania" content has produced documented edges on rookie picks.
- **Access:** Free articles + paid Underdog content. Rankings often dropped on X as plain text — easy to manually CSV.
- **ToS:** Manual import only — do not scrape Underdog's gated pages.
- **Update frequency:** Continuous through draft season; monthly in-season.
- **Recommended weight:** 1.2 (strong analyst, but no clean machine-readable feed → manual CSV).
- **Adapter approach:** Manual CSV import into the existing analyst-ranking pipeline. No code needed beyond what's already there.

---

#### A5. Sharp Football Analysis — Rich Hribar
- **URL:** https://www.sharpfootballanalysis.com/ (free pre-draft content) + Sharp Football Stats (paid).
- **Category:** Expert / analyst, opportunity-and-volume framework.
- **Measures:** Rookie rankings grounded in projected NFL opportunity (target share, route participation, snap share).
- **Track-record evidence:** Hribar's annual rookie rankings publish openly; he has a reputation for being early on opportunity-rich landing spots. No published Spearman, but FantasyPros accuracy history is solid (consistent top-25 ranker).
- **Access:** Free articles → manual CSV.
- **ToS:** Articles freely readable; full data is paid. Manual import OK.
- **Update frequency:** Multiple times a year, big drop post-NFL-Draft.
- **Recommended weight:** 1.1.
- **Adapter approach:** Manual CSV. Optionally scrape the public free articles if ToS allows (check robots.txt — appears permissive).

---

### TIER B — Highly accurate but paid / manual-only

---

#### B1. PFF College Big Board + Fantasy Rankings
- **URL:** https://www.pff.com/draft (paid: PFF+ ~$10/mo for fantasy, PFF Elite for full draft data)
- **Category:** Model + grader composite (hand grading + production + athleticism).
- **Measures:** Per-play grades aggregated to season grade, plus a "Fantasy Big Board" ranking that explicitly weighs fantasy upside.
- **Track-record evidence:**
  - PFF college grades correlate with NFL grades at **r ≈ 0.56** for OL pass-blocking (https://www.pff.com/news/draft-importance-in-pff-grades-for-ol-production-ncaa-to-nfl).
  - For WRs/TEs, PFF receiving grades have shown ~0.25–0.30 Pearson correlation with future NFL fantasy production — meaningfully stronger than draft capital alone for some sub-positions (https://brainyballers.com/tight-ends-can-pffs-receiving-grades-help-predict-nfl-success/).
  - Pat Kerrane's PFF fantasy model has been one of the more credible analyst-driven prospect models the past 3 years.
- **Access:** Paid (PFF+ tier sufficient). No public API for fantasy rankings; manual CSV via "Export" buttons in their UI for subscribers.
- **ToS:** Subscriber-only. Manual export allowed for personal use; redistribution forbidden.
- **Update frequency:** Weekly during season, multiple times during draft cycle.
- **Recommended weight:** 1.3 (already in the repo as a stub at 1.3 — keep it; it's strongest at top-of-class WR/TE).
- **Adapter approach:** Manual CSV import. The existing PFF stub is the right shape — just feed real CSV when subscribed.

---

#### B2. Matt Harmon — Reception Perception (WRs only)
- **URL:** https://receptionperception.com/ (paid Substack)
- **Category:** Expert / hand-charted model (route-running success rates).
- **Measures:** Per-route success rates (separation %) vs. man, zone, press. Position-leading granularity for WR route ability.
- **Track-record evidence:**
  - Recognized at MIT Sloan Sports Analytics Conference (https://www.sloansportsconference.com/event/reception-perception-using-data-to-grade-nfl-receivers-and-predict-breakouts).
  - Reddit/DynastyFF discussion is consistent: RP is strong on *predicting* WR translation (think Puka, Justin Jefferson) but **misses WRs from limited college route trees** (e.g., BTJ types) — known systematic blind spot.
  - No published Spearman, but predictive of WR season-1/2 success in independently-cited cases.
- **Access:** Paid Substack (~$10/mo). Rankings publicly posted as articles (https://receptionperception.com/matt-harmons-nfl-draft-prospect-wr-rankings-2021-2025-stacked/) — those are free to read.
- **ToS:** Personal use of free articles OK; data scraping not permitted.
- **Update frequency:** Per-class (annually), then in-season WR follow-ups.
- **Recommended weight:** 1.3 for WRs only; ignore for other positions. **Position-specific weight is critical here.**
- **Adapter approach:** Manual CSV ingest of the published WR rankings.

---

#### B3. Matt Waldman — Rookie Scouting Portfolio (RSP)
- **URL:** https://mattwaldmanrsp.com/
- **Category:** Expert / hand-graded film study (qualitative + tiering).
- **Measures:** Per-prospect narrative + skill profile scoring. QB/RB/WR/TE coverage, 100+ deep profiles.
- **Track-record evidence:** Reddit consensus: "better than average Joe, but not perfect" — found Puka Nacua early; also had some misses (Trey Sermon, Isaiah Spiller). No published Spearman; RSP doesn't publish a clean retrospective.
- **Access:** Paid PDF (~$25 annually).
- **ToS:** Personal use only.
- **Update frequency:** Annual (pre-draft) + dynasty updates for subscribers.
- **Recommended weight:** 1.0 — strong qualitative signal but no quantitative track record. Helpful as a *tiebreaker* / qualitative override, not as a heavy quantitative weight.
- **Adapter approach:** Manual CSV import. Could be used as a binary "RSP top-50" flag rather than as a granular ranking, given its qualitative nature.

---

#### B4. Dane Brugler — "The Beast" (The Athletic)
- **URL:** https://www.nytimes.com/athletic/the-beast/2026/
- **Category:** Expert / hand-scout NFL-style big board (not fantasy-specific).
- **Measures:** Pure draft prospect rankings, scouting reports, NFL-verified testing data.
- **Track-record evidence:** Widely considered the gold-standard *NFL* big board; less explicitly fantasy-tuned. Strong NFL-Draft-position predictor → useful as a *pre-draft* proxy for draft capital.
- **Access:** Paid (Athletic subscription, ~$12/mo or $1/mo intro).
- **ToS:** Subscription content; manual reading only.
- **Update frequency:** Annual, monster ~500-page pre-draft drop.
- **Recommended weight:** 0.8 as a fantasy ranking (he doesn't optimize for fantasy), but **1.5 as a pre-draft draft-capital proxy** before NFL Draft happens.
- **Adapter approach:** Manual CSV pre-draft only; replace with actual draft capital after the NFL Draft.

---

#### B5. Campus2Canton (Devy/Dynasty)
- **URL:** https://www.campus2canton.com/
- **Category:** Expert + community model (devy/CFB-focused).
- **Measures:** Multi-analyst rankings with quantitative inputs (age, BMI, target share).
- **Track-record evidence:** Limited published accuracy data — primarily a devy community. Useful pre-NFL-Draft for *out-year* prospects.
- **Access:** Paid (~$10/mo).
- **ToS:** Subscription, manual.
- **Update frequency:** Continuous, focused around college season.
- **Recommended weight:** 0.9 (lower confidence in track record but useful for 2nd-year / devy crossover).
- **Adapter approach:** Manual CSV.

---

### TIER C — Niche / unverified (skip or defer)

---

#### C1. PlayerProfiler (Breakout Age, College Dominator, Athleticism Score)
- **URL:** https://www.playerprofiler.com/
- **Comment:** Their *metrics* are excellent (Breakout Age has r ≈ 0.43 with NFL production per multiple replications — among the strongest single signals for WRs). But their *rankings* are decent-not-great, and the underlying metrics are now well-publicized — you can recompute Breakout Age yourself from `cfbfastR` / `cfbd_api` data.
- **Recommendation:** Don't add PlayerProfiler as a *source* — instead, **add Breakout Age and College Dominator as engineered features** from raw CFB data. Tier A-equivalent if implemented as features.
- **Adapter approach:** Use `cfbd-api-py` or `cfbfastR` data to compute Breakout Age (year a player first hit ≥20% dominator) and Dominator Rating (player's share of team rec-yards + rec-TDs). Encode as features alongside other source scores.

---

#### C2. RotoViz (Phenom Index, Box Score Scout)
- **URL:** https://www.rotoviz.com/
- **Comment:** Phenom Index (age-adjusted college production) was the precursor to modern Breakout Age work — solid signal, but it's paywalled and the methodology is public enough to replicate. Same situation as PlayerProfiler.
- **Recommendation:** Don't subscribe just for the model. **Replicate Phenom Index as a feature** (`(college dominator) - (age at midpoint of season) * k`).

---

#### C3. FTN Fantasy (formerly Football Outsiders metrics, including Playmaker Score / Speed Score)
- **URL:** https://ftnfantasy.com/
- **Comment:** Playmaker Score (Aaron Schatz, WR projection model) and Speed Score (RB projection model) are classic models with documented track records — Speed Score (`weight × 200 / 40_time^4`) is a known proxy for RB success. Football Outsiders folded into FTN.
- **Recommendation:** Skip the paid FTN data; **compute Speed Score and BMI-adjusted athleticism scores yourself** from the same Combine data RAS uses.

---

#### C4. Pat Kerrane PFF Prospect Model
- Covered under PFF (B1). His model is bundled in PFF's fantasy product.

---

#### C5. The Athletic — Jake Ciely
- Solid in-season rankings; for *rookies*, less differentiated. Skip until you exhaust higher-signal sources.

---

#### C6. FTN Rookie Super Model (Fantasy Life)
- **URL:** https://www.fantasylife.com/ — referenced in 2026 rookie content (https://www.fantasylife.com/articles/fantasy/rating-the-2026-nfl-running-back-prospects-rookie-super-model).
- **Comment:** Self-described "strong track record vs NFL Draft pick alone" but no published Spearman. Could be a free signal if rankings are public. Worth a follow-up if Phil wants another free model voice.
- **Recommendation:** Tier C now; revisit if their methodology paper drops.

---

### TIER D — Do not add

---

#### D1. **KeepTradeCut (KTC)** — Explicitly forbidden + low marginal value
- **URL:** https://keeptradecut.com/dynasty/power-rankings
- **API:** None publicly. KTC FAQ:
  > *"This is something we've discussed adding at some point down the line, however, so stay tuned. That said, please note that **scraping player values and other data from the site is expressly forbidden by our Terms and Conditions**."*
  — https://keeptradecut.com/frequently-asked-questions
- **Terms and Conditions:**
  > *"Prohibited Activities… Any form of automated data collection…"*
  — https://keeptradecut.com/terms-and-conditions
- **No data-sharing program** as of 2026-05. They've said "stay tuned" for years without delivering one.
- **Track record vs FantasyCalc:** No published head-to-head Spearman comparison exists. Reddit /r/DynastyFF consensus (multiple threads cited below) is split:
  - **FantasyCalc** = based on *real Sleeper trades*, more "objective." More accurate per most quant-leaning users. (https://www.reddit.com/r/DynastyFFTradeAdvice/comments/1l7zy80/how_accurate_is_keep_trade_cut/)
  - **KTC** = crowdsourced ELO from triplet rankings, more "vibes" — overreacts to hype. ("KTC is just vibes" — top reply on https://www.reddit.com/r/DynastyFF/comments/18hj165/fantasycalc_or_keeptradecut/)
  - **Use case difference:** KTC reflects *what people think players are worth*, FantasyCalc reflects *what people actually trade*. For a *predictive* model of fantasy production, neither is strong — but FantasyCalc is closer to ground-truth.
- **Recommendation:**
  - **Do NOT scrape KTC.** Their ToS is explicit; ignoring it creates legal/ethical risk for a public GitHub repo and could get the repo flagged/DMCA'd.
  - **Do NOT pay for KTC** (it's free, but they don't sell API access).
  - **Keep FantasyCalc as the single market signal at weight 1.0**, optionally add FantasyFootballCalculator (A3) as a second market signal at 0.7.
  - **If Phil personally wants KTC's view**, recommend he manually export via the KTC league-import workflow for his own leagues and treat it as a private CSV override — but don't bake it into the public model.

---

#### D2. FantasyPros Premium API (paid)
- Already covered for free via DynastyProcess. **Don't pay twice.**

---

#### D3. Dynasty League Football / Dynasty Nerds
- Paywalled, no public accuracy ledger, duplicative with ECR. Skip.

---

#### D4. ESPN / Yahoo dynasty rankings
- Generally lag the analyst consensus. Skip.

---

## 3. KTC vs FantasyCalc — Direct Analysis

| Dimension | FantasyCalc | KeepTradeCut |
|---|---|---|
| Methodology | Optimization algorithm over **real Sleeper trades** (logged transactions) | ELO-style algorithm over **user-submitted Keep/Trade/Cut triplets** |
| Signal type | Revealed preference (what people actually trade) | Stated preference (what people say they prefer) |
| Bias | Slightly trails sharp market; some illiquid players have wide error bars | Vibes-heavy; spikes on Twitter narratives faster than real-trade markets |
| Public API | **Yes, free, undocumented but stable:** `https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=1&numTeams=12&ppr=1` | **No.** Scraping ToS-prohibited. |
| ToS for automation | Permissive | **Forbidden** |
| Published accuracy | "Outperform 91% of experts" claim on landing page — not independently audited but plausible for ADP/redraft | None published |
| Best use | Default market signal for a quant model | Manual gut-check only |

**Verdict:** Use FantasyCalc. KTC adds no measurable predictive value to a quant model and creates legal/ethical risk to ingest. If Phil wants a *second* market signal, add FantasyFootballCalculator (A3) — it's casual-skewed but ToS-friendly and uncorrelated noise with FantasyCalc.

---

## 4. Weighting Recommendations

### Current weights (from repo, normalized to FantasyCalc = 1.0):
- FantasyCalc: 1.0
- DynastyProcess (ECR): unspecified, likely 1.0
- Brainy Ballers SPS: 1.3
- PFF (stub): 1.3

### Recommended weights (post-additions):

**Established players (Year 2+):**
| Source | Weight | Notes |
|---|---:|---|
| FantasyCalc | 1.0 | Baseline market |
| DynastyProcess ECR | 1.0 | Expert consensus |
| Brainy Ballers SPS | 1.2 | Slightly lower than current 1.3 — it's stronger for rookies than for vets |
| PFF (manual) | 1.3 | Paid premium, strong signal |
| Hayden Winks (manual) | 1.0 | Strong analyst |
| Sharp/Hribar (manual) | 0.9 | Strong analyst, narrower lens |
| RAS | 0.2 | Mostly useless for established players; combine fades fast |
| Draft capital | 0.3 | Decays fast; included for years 1–3 |

**Rookies (Year 1) — different weighting:**
| Source | Weight | Notes |
|---|---:|---|
| **NFL Draft capital** | **1.8** | Strongest single predictor |
| Brainy Ballers SPS | 1.5 | Production-based, well-calibrated for rookies |
| PFF (manual) | 1.5 | Best paid signal |
| FantasyCalc | 0.6 | Trailing indicator post-draft; weight DOWN for rookies |
| DynastyProcess ECR | 1.0 | Expert consensus |
| Hayden Winks | 1.3 | His rookie work is his strength |
| Matt Harmon RP (WR only) | 1.5 | WR position only |
| Waldman RSP | 0.8 | Qualitative signal, tiering only |
| Dane Brugler (pre-draft) | 1.2 | Best pre-draft NFL big board → proxy for draft capital before April |
| RAS | 0.6 | More useful pre-draft as a filter; downweight low-RAS prospects |
| Engineered: Breakout Age | 0.8 | Compute from cfbd; r ≈ 0.43 for WR |
| Engineered: College Dominator | 0.6 | Compute from cfbd |
| Engineered: Speed Score (RB) | 0.5 | Compute from Combine |

### Track-record multiplier (currently 0.6/1.0/1.2/1.5 based on Spearman |ρ|):
- **Keep the tiering structure**, but tune the cutoffs and make them position-specific.
- Suggested Spearman thresholds: `|ρ| < 0.15` → 0.5x; `0.15–0.25` → 1.0x; `0.25–0.35` → 1.3x; `>0.35` → 1.6x.
- **Position-specific** is essential: Reception Perception is great at WR but useless at RB. Suggested implementation:

```python
position_modifier = {
    ("reception_perception", "WR"): 1.5,
    ("reception_perception", "RB"): 0.0,
    ("reception_perception", "TE"): 0.7,
    ("ras", "WR"): 0.8,
    ("ras", "RB"): 0.6,
    ("ras", "TE"): 0.9,
    ("ras", "QB"): 0.2,
    ("speed_score", "RB"): 1.0,
    ("speed_score", "WR"): 0.0,
    ("breakout_age", "WR"): 1.0,
    ("breakout_age", "TE"): 0.7,
    ("breakout_age", "RB"): 0.3,  # production matters less for RBs vs opportunity
    # default 1.0 otherwise
}
```

### Year-of-experience decay:
- Rookies: heavy weight on draft capital, college production, athleticism.
- Year 2: blend rookie weights with actual NFL production data (target share, snap %, expected fantasy points from `nflverse`).
- Year 3+: full pivot to NFL production-based features; college/athleticism near zero weight.

```python
def years_pro_weight(metric_category, years_pro):
    if metric_category == "college_production":
        return max(0.0, 1.0 - 0.4 * years_pro)
    if metric_category == "athleticism":
        return max(0.1, 1.0 - 0.3 * years_pro)
    if metric_category == "nfl_production":
        return min(1.0, 0.2 * years_pro)
    return 1.0  # market and ECR are evergreen
```

---

## 5. Implementation Priority

1. **(1–2 hr)** Add **NFL Draft capital** via `nfl_data_py.import_draft_picks()`. Direct, free, highest-impact. (A1)
2. **(1 hr)** Add **FantasyFootballCalculator ADP** as a second market signal. Free REST API. (A3)
3. **(2–3 hr)** Add **RAS** by downloading Kent Lee Platte's CSV → join on name+year. Position-specific weighting. (A2)
4. **(3–4 hr)** Compute **Breakout Age** and **College Dominator Rating** from `cfbd-api-py` data as engineered features. (C1 + C2 → DIY)
5. **(2 hr)** Add **manual CSV slots** for Hayden Winks, Matt Harmon RP, Dane Brugler, Matt Waldman, PFF — all flow through the existing analyst-CSV adapter. (A4, A5, B-tier)
6. **(2 hr)** Refactor the weighting layer to support **position-specific** modifiers and **years-pro decay**.
7. **(deferred)** Compute **Speed Score** (RBs) from Combine data as an additional engineered feature.
8. **(NEVER)** Do not add KTC.

Total implementation effort estimate: **~12–14 hours** to ship A1–A4 + position-specific weighting. Tier B is "as needed" / manual-only and shouldn't gate.

---

## 6. References

### KTC / FantasyCalc
- KTC FAQ (scraping forbidden): https://keeptradecut.com/frequently-asked-questions
- KTC Terms & Conditions (automated collection prohibited): https://keeptradecut.com/terms-and-conditions
- FantasyCalc API endpoint: https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=1&numTeams=12&ppr=1
- FantasyCalc API Python intro: https://www.fantasydatapros.com/fantasyfootball/blog/fantasycalc/1
- /r/DynastyFF comparison: https://www.reddit.com/r/DynastyFF/comments/18hj165/fantasycalc_or_keeptradecut/
- /r/DynastyFFTradeAdvice: https://www.reddit.com/r/DynastyFFTradeAdvice/comments/1l7zy80/how_accurate_is_keep_trade_cut/
- /r/DynastyFF accuracy thread: https://www.reddit.com/r/DynastyFF/comments/1onh4ud/is_ktc_or_fantasycalc_a_better_gauge/

### NFL Draft capital / nflverse
- nflreadr: https://nflreadr.nflverse.com/
- nfl_data_py (Python wrapper): https://github.com/cooperdff/nfl_data_py
- Apex / breakout-age + draft-capital correlations: https://apexfantasyleagues.com/2020/05/how-to-evaluate-fantasy-football-players

### RAS
- ras.football: https://ras.football/
- Sharp Football RAS commentary: https://www.sharpfootballanalysis.com/fantasy/what-nfl-combine-results-mean-for-fantasy/
- Brainy Ballers RAS WR study: https://brainyballers.com/nfl-wide-receivers-does-ras-matter-a-comprehensive-analysis/
- Brainy Ballers RAS TE study: https://brainyballers.com/tight-end-ras-does-it-matter-a-comprehensive-analysis/

### PFF
- PFF OL grade correlation study: https://www.pff.com/news/draft-importance-in-pff-grades-for-ol-production-ncaa-to-nfl
- PFF pass-rusher grade study: https://www.pff.com/news/draft-pff-college-grades-and-nfl-correlation-for-pass-rushers
- Brainy Ballers PFF TE grade study: https://brainyballers.com/tight-ends-can-pffs-receiving-grades-help-predict-nfl-success/

### Reception Perception
- Home: https://receptionperception.com/
- MIT Sloan recognition: https://www.sloansportsconference.com/event/reception-perception-using-data-to-grade-nfl-receivers-and-predict-breakouts
- Stacked WR rankings (free): https://receptionperception.com/matt-harmons-nfl-draft-prospect-wr-rankings-2021-2025-stacked/
- Reddit critique on limits: https://www.reddit.com/r/DynastyFF/comments/1m61i47/is_matt_harmons_reception_perception_truly/

### PlayerProfiler / RotoViz
- Breakout Age article: https://www.playerprofiler.com/article/stefon-diggs-breakout-age-advanced-stats-analytics-metrics/
- College Dominator article: https://www.playerprofiler.com/article/college-dominator-rating-wide-receiver-nfl-draft-advanced-stats-metrics-analytics-profiles/
- RotoViz Phenom Index 2026: https://www.rotoviz.com/2026/03/carnell-tate-bryce-lance-and-the-complexities-of-prospect-age-the-2026-phenom-index-for-rookie-wide-receivers/
- RotoViz Phenom Index 2017 (original methodology): https://www.rotoviz.com/2017/02/the-2017-phenom-index-for-rookie-wide-receivers/

### Other
- Dane Brugler "The Beast" 2026: https://www.nytimes.com/athletic/interactive/the-beast-2026/
- Sharp Football 2026 rookie rankings (Hribar): https://www.sharpfootballanalysis.com/fantasy/2026-rookie-rankings-dynasty-fantasy-football/
- Matt Waldman RSP: https://mattwaldmanrsp.com/
- Establish The Run 2026 rookie rankings: https://establishtherun.com/rookie-dynasty-rankings-2/
- Hayden Winks at FantasyPros: https://www.fantasypros.com/experts/hayden-winks.php
- FantasyFootballCalculator rookie ADP + REST API: https://fantasyfootballcalculator.com/adp/rookie
- College Football Data API (cfbd): https://collegefootballdata.com/

---

## 7. Brutally Honest Take

- **Marketing vs. signal:** KTC has the best brand, but for a *predictive model* (not a trade UI), it's the weakest input of the major names. Skip it without hesitation.
- **The single highest-ROI addition is NFL draft capital from nflverse.** It's free, it's the strongest predictor, and it's already in the Python ecosystem via `nfl_data_py`. If Phil only adds one thing, add this.
- **Most "models" in this space are 70% draft capital + 20% age-adjusted production + 10% athleticism, dressed up with proprietary names.** Build those three ingredients yourself and you've replicated 80% of what the paid models do. Use Brainy Ballers + PFF as the "secret sauce" / hand-graded overlay.
- **Reception Perception is genuinely valuable for WRs** — but only WRs, and only as a position-specific source. Don't use it cross-position.
- **PlayerProfiler-style metrics are a buy-the-feature, not the source.** Compute Breakout Age and Dominator yourself from `cfbd`.
- **The current track-record multiplier (0.6/1.0/1.2/1.5) is the right structure but needs position-specific overrides** — a uniform Spearman across positions hides the fact that the same source can be great at one position and noise at another.
- **Don't overthink rookie weighting.** The literature is consistent: draft capital + age-adjusted production + athleticism + opportunity (landing spot) is ~80% of what matters. Get those four right before chasing the next analyst.

---

## Addendum (v0.14.0, PR #14)

### Pro Football Reference / nflverse (Tier S, free)

- **What:** Player-season aggregated stats 1999–2024 from
  pro-football-reference.com, republished by the nflverse project as
  MIT-licensed CSV releases on GitHub.
  - https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats_season.csv
  - https://github.com/nflverse/nflverse-data/releases/download/players/players.csv
- **Why:** The corpus is the substrate for the v0.14 similarity engine
  AND the DARKO-style current-skill signal. Scraping PFR directly at
  3s/page over 45 years is fragile and CI-hostile; nflverse is the
  standard mirror.
- **Cached in repo:** `data/nflverse/player_stats_season.csv.gz` (~2.8MB),
  `data/nflverse/players.csv.gz` (~2.4MB). CI never hits the network;
  live refresh gated behind `DYNASTY_FB_PFR_LIVE=1`.
- **Coverage:** 33,636 player-seasons; 24,966 player bios with `pfr_id`
  ↔ `gsis_id` crosswalk + birth_date + draft info.

### Similarity Career Arc (Tier S, model, weight 1.8)

- **What:** KNN comparable search over the PFR/nflverse corpus. For
  each active NFL player, find the top 20 historical seasons at the
  same position and age, then aggregate their realized future careers
  (time-discounted 5%/yr) into a projected dynasty value.
- **Why:** Phil's directive — "make similarity scores the heart of the
  model." Encodes both current skill (via vectorization features) and
  longevity (via comp careers). Replaces hand-coded source weights as
  the dominant signal.
- **Detail:** See `docs/SIMILARITY-METHODOLOGY.md`.

### NFL Impact / DARKO-style current-skill (Tier A, model, weight 0.8)

- **What:** Per-position skill formulas computed off the same PFR
  corpus: ANY/A + TD%-INT% + sack rate (QB); yards-per-touch + TD rate
  + target share (RB); YPRR proxy + aDOT + TD rate (WR/TE). Normalized
  0–100 within position.
- **Why:** Paired with the similarity engine. Similarity owns
  longevity; NFL Impact owns "how good are they right now."

### Overlays (RAS, Brainy Ballers SRS)

- Both moved OUT of the composite (`default_weight=0.0`) and INTO a
  user-toggle overlay system. Each has a position-specific suggested
  default weight derived from the historical Pearson correlation
  between the signal and a player's first 3 NFL seasons of fantasy
  PPR. See `docs/CORRELATION-METHODOLOGY.md`.

DONE
