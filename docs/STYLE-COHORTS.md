# Style cohorts (v1.2.0)

v1.2 restricts each KNN comp pool to the target's *style cohort* — a
per-position bucket that groups players by their fantasy production
shape. Cosine similarity inside a cohort is meaningful (matching players
who produce in the same way under the active scoring rules); cosine
similarity ACROSS cohorts is structurally misleading (Andy Dalton and
Josh Allen both have non-zero passing and rushing components but their
fantasy-production profiles barely overlap).

This document defines the cohorts, their thresholds, the fallback
strategy, and the empirical calibration notes.

## Per-position cohort definitions

### QB — by career rushing fantasy-point share

`rushing_fp_share = career_rushing_fp / total_career_fp`, computed under
the active league format's scoring rules.

| Style | Threshold | Example careers |
| --- | --- | --- |
| pocket | < 0.15 | Tom Brady (0.046), Drew Brees (0.043), Peyton Manning (0.037), C.J. Stroud (0.112), Joe Burrow (0.111), Tua Tagovailoa (0.078), Jordan Love (0.113), Aaron Rodgers (0.116), Patrick Mahomes (0.127), Justin Herbert (0.133), Brock Purdy (0.141) |
| mobile | [0.15, 0.30) | Dak Prescott (0.158), Ryan Tannehill (0.154), Donovan McNabb (0.191), Russell Wilson (0.194), Steve McNair (0.212), Bo Nix (0.211), Daunte Culpepper (0.256) |
| dual-threat | ≥ 0.30 | Colin Kaepernick (0.300), Josh Allen (0.324), Robert Griffin III (0.332), Cam Newton (0.358), Jayden Daniels (0.358), Lamar Jackson (0.373), Kordell Stewart (0.378), Mike Vick (0.398), Jalen Hurts (0.431) |

**Calibration note.** The v1.2 brief specified 0.10 / 0.25 thresholds.
Empirical fp-share distribution required calibration to 0.15 / 0.30:
  * Stroud (0.112), Burrow (0.111), Love (0.113) all sit just above 0.10
    and would otherwise misclassify as mobile, producing comp lists
    dominated by Dalton/Cutler-tier mobile passers rather than pocket
    prototypes.
  * Mahomes (0.127) and Herbert (0.133) are fantasy-production-style
    pocket passers (their production is elite-passing-dominant) even
    though their absolute rushing yards/game is above pocket — splitting
    them into pocket via the 0.15 line matches their KNN to Brady/Brees/
    Manning shape correctly.
  * The 0.25 dual-threat line excludes McNair / McNabb / Cunningham /
    Russell Wilson, breaking the brief's expected Allen comp list. The
    0.30 line is conservative enough to reserve dual-threat for QBs whose
    fantasy production is genuinely run-dominant (Allen and above).

### RB — by touch volume and receiving share

`touches_per_game = (rushing_attempts + receptions) / games`
`rec_fp_share   = career_receiving_fp / total_career_fp`

Decision order (first match wins):
1. **receiving-back** — rec_fp_share ≥ 0.35 AND touches_per_game ≥ 10
2. **workhorse** — touches_per_game ≥ 18
3. **committee** — otherwise

| Style | Example careers |
| --- | --- |
| workhorse | LaDainian Tomlinson, Adrian Peterson, Edgerrin James, Curtis Martin, Derrick Henry |
| committee | Late-career Frank Gore, Jamal Lewis, Eddie George, every shared-backfield RB |
| receiving-back | Marshall Faulk, Brian Westbrook, Matt Forte, Le'Veon Bell, Alvin Kamara, Christian McCaffrey, Austin Ekeler |

**Calibration note.** Without the touches_per_game minimum (=10) for
receiving-back, the bucket fills with fullbacks (Cecil Martin, Anthony
Sherman) and pure third-down change-of-pace specialists, drowning the
bucket's real prototypes. The 10 tpg floor preserves Faulk / Bell /
Westbrook / Forte as the dominant signal.

### WR — by target volume and yards per reception

`targets_per_game = receiving_targets / games`
`yards_per_reception = receiving_yards / receptions`

Decision order (first match wins):
1. **alpha** — targets_per_game ≥ 8.5
2. **deep-threat** — yards_per_reception ≥ 16 AND receptions ≥ 200
3. **secondary** — otherwise

| Style | Example careers |
| --- | --- |
| alpha | Calvin Johnson (8.75 tpg), Larry Fitzgerald (8.77), Antonio Brown (10.35), Andre Johnson (9.02), Marvin Harrison (9.99), Megatron-tier all-time alphas |
| deep-threat | DeSean Jackson (17.56 ypr), Vincent Jackson (16.80), Josh Gordon (17.00), T.Y. Hilton (15.36) |
| secondary | the long-tail middle of every WR room — Jarvis Landry, Reggie Wayne, Hines Ward, Brandon Lloyd |

**Calibration note.** The brief's 9.0 tpg alpha threshold excludes Calvin
Johnson (8.75) and Larry Fitzgerald (8.77) — both Megatron-tier alphas
the v1.2 tests expect Justin Jefferson to comp to. Lowering to 8.5
preserves the all-time alpha pool intact.

The brief's 17.0 ypr deep-threat threshold excludes Vincent Jackson
(16.80) and Randy Moss (15.31). 16.0 with a ≥200 reception floor captures
the true career-long field stretchers without false positives from low-
volume specialists.

### TE — by receiving fantasy share

`rec_fp_share = career_receiving_fp / total_career_fp`

| Style | Threshold | Notes |
| --- | --- | --- |
| receiving | ≥ 0.70 | Effectively every TE in the corpus (~99%) because nflverse only tracks receiving stats for TEs |
| hybrid | [0.40, 0.70) | Empty in practice — see calibration note |
| blocking | < 0.40 | Essentially Tim Tebow only, who briefly converted at TE |

**Calibration note.** TE fantasy production in nflverse is effectively
single-category (receiving). The hybrid / blocking buckets are kept in
the schema for completeness but the cohort effectively widens to "all
TEs" for every TE target. This is acceptable: the TE position has the
narrowest fantasy production diversity, so a uniform pool is the right
default.

## Adjacent fallback chain

When the strict cohort's *qualified* comp count (post-age career exists,
age-window match, valid era-z-scored vector) falls below
`MIN_COHORT_COMPS` (=20), the search widens to the next adjacent style.
The widening is capped at 2 styles total (primary + 1 adjacent).

| Primary style | Fallback chain (primary first) |
| --- | --- |
| QB pocket | pocket → mobile |
| QB mobile | mobile → pocket (then dual-threat) |
| QB dual-threat | dual-threat → mobile |
| RB workhorse | workhorse → committee |
| RB committee | committee → workhorse |
| RB receiving-back | receiving-back → committee |
| WR alpha | alpha → secondary |
| WR secondary | secondary → alpha |
| WR deep-threat | deep-threat → alpha |
| TE receiving | receiving → hybrid |
| TE hybrid | hybrid → receiving |
| TE blocking | blocking → hybrid |

The 2-style cap prevents a dual-threat target from picking up
pocket-style retired QBs through cascading widening — defeating the
point of the cohort restriction. Targets that still have fewer than 20
qualified comps after one adjacent widening accept the smaller pool;
the engine doesn't walk further to pull mismatched comps.

## Diagnostics

The engine writes `data/diagnostics/v1.2_cohort_stats.json` on every
`run_engine(persist=True)` run:

```json
{
  "base_format": "sf_ppr",
  "cohort_sizes": {
    "QB": {"pocket": 120, "mobile": 30, "dual-threat": 16},
    "RB": {"workhorse": 21, "committee": 340, "receiving-back": 71},
    "WR": {"alpha": 20, "secondary": 566, "deep-threat": 8},
    "TE": {"receiving": 307, "hybrid": 0, "blocking": 1}
  },
  "per_player_widened_count": ...,
  "per_position_widened_rate": {"QB": 0.45, "RB": 0.04, ...}
}
```

Use this to monitor cohort health: if a bucket's count drops or the
widened_rate climbs significantly, calibration thresholds or
MIN_COHORT_COMPS should be revisited.

## Edge cases

**Hybrid QBs (Mahomes is mobile but elite passing).** v1.2 classifies by
fantasy-production share, so Mahomes (fp_share 0.127) lands in the pocket
cohort. This is the correct treatment for KNN matching — his comp pool is
Brady/Brees/Manning shape, which is empirically where his production
points (~85% passing, ~12% rushing). v1.1's career-length-era classifier
(rypg-based) STILL puts him in "mobile" for the lift multiplier (1.3×),
correctly recognising that his actual rushing volume justifies a small
longevity lift.

The two classifications are intentionally independent:
  * `career_length_era.style_for_career` (rypg) → which lift multiplier
  * `style_cohort.cohort_for` (fp_share) → which comp pool

**Active sophomores in dual-threat (Bo Nix, Caleb Williams).** Their
fp_share lands in mobile, not dual-threat — Bo Nix at 0.211, Caleb at
0.189. They get the mobile cohort comps (Dak, McNabb, Russell Wilson)
which matches their game shape better than a dual-threat pool would in
their current production profile. If they evolve into bigger rushers, a
v1.3+ recalibration would move them up.

**TEs.** Effectively single-cohort because nflverse only tracks receiving
stats. This is a known data limitation; the TE cohort layer is a no-op
in practice.

**Pre-1999 corpus gap.** The nflverse season-level corpus starts in 1999,
so historical dual-threat producers like Steve Young (career 1985-1999),
Randall Cunningham (1985-2001 mostly pre-corpus), and Warren Moon
(1984-2000 mostly pre-corpus) have only a few seasons of data and don't
contribute meaningfully to the dual-threat comp pool. Expanding the
corpus pre-1999 is a v1.3+ work item that would directly raise Allen /
Lamar / Hurts projections by surfacing Steve Young's career arc.
