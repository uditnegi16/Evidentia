# Outcomes

What was built, what it produced, and which grading criterion each piece answers.
Doubles as a revision sheet: each phase below is a thing to be able to explain and
modify live.

---

## Grading criteria and where each is answered

| Criterion | Answered by | Status |
|---|---|---|
| AI fundamentals | D-002 split: LLM writes, pandas computes; 0 numbers from the model | **done** |
| Context engineering | 20 analyses exist, no section sees more than 7; D-019 projection | **done** |
| Prompt design | 12 Jinja templates; every packet written to disk per run | **done** |
| Architecture | 9 modules, frozen contracts between stages | **done** |
| Agent/tool judgment | D-003 explicit rejection of agents/RAG, stated in README | **done** |
| Grounding | 253 claims checked, 0 ungrounded; 4 live catches documented | **done** |
| Evaluation | 3 tiers, structurally unequal authority (D-026) | **done** |
| Generalization | PSUR-lite: 0 shared sections, 18/18 analyses reused, 0 Python | **done** |
| Execution | `python -m evidentia.run` → md, html, docx in ~40s | **done** |

---

## Phase tracker

| # | Phase | Output | Status |
|---|---|---|---|
| 0 | Scaffold + data profiling | Repo, venv, package skeleton, `scripts/profile_data.py` | **done** |
| 1 | Ingest, validate, dedupe | `ingest.py`, `contracts.py`, 28 tests | **done** |
| 2 | Analysis registry | `analyses/` (20), `evidence.py` | **done** |
| 3 | Config schema and assembler | `config.py`, `assembler.py`, `configs/pader_fda.yaml` | **done** |
| 4 | Section generation | `generate.py`, 7 Jinja prompts, Groq fallback ladder | **done** |
| 5 | Grounding validator | `grounding.py` | **done** |
| 6 | Runner and renderers | `run.py`, `render.py`, md/html/docx | **done** |
| 7 | Human review | `review_app.py`, `review.json`, approval gate | **done** |
| 8 | Evaluation layer | `evaluate.py`, tiers 2 and 3 | **done** |
| 9 | V1 second report type | `configs/psur_lite.yaml` + 5 prompts, zero Python | **done** |
| 10 | README, diagram, packaging | `README.md` with Mermaid diagram | **done** |

---

## Verified dataset facts (Phase 0)

These are the ground truth the ingest layer asserts against. Every one was derived from
the data, not assumed from the guides.

| Fact | Value | Note |
|---|---|---|
| Raw rows | 1,068 | |
| Unique `safetyreportid` | 1,024 | 983 single-row, 38 two-row, 3 three-row |
| Multi-row cause | report versions | 41/41 differ by `safetyreportversion`, 0 by date |
| Reporting period | 2024-12-27 to 2025-12-26 | matches reference PADER exactly |
| Reaction events (comma-split, pre-dedup) | **3,648** | matches reference PADER exactly |
| Serious rows | 1,067 of 1,068 | |
| Expedite criteria met | 1,067 of 1,068 | |
| Reaction/outcome positional alignment | 99.4% | 0.6% needs explicit handling |
| Cases flagged `duplicate` | 204 | surfaced, not removed |
| Age unit distribution | year 975, month 5, day 3, week 1, corrupt 3, null 81 | |
| Country column disagreement | 8 rows | |

**Seriousness flags** (independent, overlapping): death 69, life-threatening 110,
hospitalisation 504, disabling 46, congenital anomaly 8, other 945. Sum 1,682 against
1,067 serious rows — confirms non-exclusivity.

**Reporter qualification:** physician 518, pharmacist 259, other HCP 171, consumer 120.

**Outcome vocabulary after split:** recovered/resolved 1,347, unknown 1,135,
not recovered/ongoing 569, recovering/resolving 420, fatal 137, recovered with sequelae 34.

---

## Phase 1 verified outputs

Produced by `python -m evidentia.ingest`, all pinned by tests.

| Output | Value |
|---|---|
| Source SHA-256 | `369476426a406d30...` |
| Raw rows | 1,068 |
| Cases after version-dedup | 1,024 |
| Superseded rows dropped | 44 |
| Reporting period | 2024-12-27 to 2025-12-26 |
| Reaction events, raw split | 3,648 (matches reference PADER) |
| Reaction events, post-dedup | **3,429** (reported figure) |
| Events removed by dedup | 219 (6.0%) |

**Quality issues surfaced (post-dedup, case-level):** superseded rows dropped 44,
corrupt age unit 3, age unavailable 86, duplicate flag 197, country disagreement 8,
reaction/outcome misalignment 6.

No `outcome_broadcast` issue fired — no row pairs a single outcome with multiple
reactions, so the broadcast branch is unit-tested but unused on this dataset.

Test suite: 28 passing. 13 ground-truth assertions (skip automatically without the
dataset, so CI runs green with no data), 15 unit tests requiring no data.

---

## Phase 2 verified outputs

20 registered analyses. Reference-matched figures below are the strongest
validation in the build: they reproduce an independently produced artifact.

**Against the reference PADER's Case Presentation** (distinct cases per PT):

| Preferred Term | Reference | Ours |
|---|---|---|
| Acute kidney injury | 80 | 80 |
| Hypotension | 46 | 46 |
| Drug interaction | 43 | 43 |
| Fatigue | 33 | 33 |
| Drug ineffective | 53 | 54 (open, E-011) |

Pre-dedup these were 81 / 48 / 45 / 35 / 60 — none correct. Version-dedup is
validated, not merely argued.

**Reaction outcomes** (3,429 events, exhaustive partition): recovered/resolved
1,257; unknown 1,086; not recovered/ongoing 512; recovering/resolving 406;
fatal 134; recovered with sequelae 34.

**15-day Alert cases:** 1,023.

**Data property held by test:** event-level and case-level PT counts are equal
for every term, because no case reports the same PT twice. Asserted rather than
assumed (D-018).

Test suite: 60 passing.

---

## Analyses implemented

| Name | Unit of count | Source fields | Tests |
|---|---|---|---|
| *(to be filled as built)* | | | |

---

## Report sections and their evidence dependencies

| Section | Requires | Notes |
|---|---|---|
| Reporting Period | `period_bounds`, `product_meta` | |
| Narrative Summary and Analysis | `total_cases`, `serious_split`, `top_reactions` | |
| Summary Analysis of Cases | `age_bands`, `sex_dist`, `country_dist`, `serious_split` | |
| Reaction / Adverse Event Analysis | `top_reactions`, `top_serious_reactions`, `outcomes` | PT level only, no SOC |
| Serious Cases / 15-Day Alerts | `alert_cases`, `serious_flag_breakdown` | |
| Trends and Important Observations | `monthly_volume`, `monthly_top_reactions` | Observation only, no signal claims |
| History of Actions | `actions_available` | Must state none supplied |
| Case Index / Listing | `case_index` | Traceability anchor |

---


## Final run — PADER

`python -m evidentia.run`

| | |
|---|---|
| Sections generated | 7 |
| Sections rendered deterministically | 2 |
| Numeric claims checked | **253** |
| Ungrounded | **0** |
| Sections needing review | 1 (`reaction_analysis`, over-length by 22 words) |
| Output mode | strict on every section |
| Outputs | `report.md`, `report.html`, `report.docx` |
| Status | DRAFT — not human approved |

Per-section: narrative_summary 20/20 · summary_analysis_of_cases 59/59 ·
reaction_analysis 102/102 · serious_cases_alerts 18/18 · trends_observations 44/44 ·
history_of_actions 0/0 · data_limitations 10/10.

## Final run — PSUR-lite

`python -m evidentia.run --config configs/psur_lite.yaml --out outputs/psur`

6 generated, 2 deterministic, 118 claims checked, 0 ungrounded. One review flag:
`exposure` used "reporting rate" inside an instructed denial, correctly downgraded
rather than blocked (D-023).

## Generalisation, measured

| | PADER | PSUR-lite |
|---|---|---|
| Sections | 9 | 8 |
| Shared section IDs | — | **0** |
| Analyses required | 20 | 18 |
| Analyses reused | — | **18** |
| New Python | — | **0** |

## Test suite

203 passing. Roughly 130 run with no dataset and no API key, because the deterministic
layer holds no LLM calls and the client is injected behind a Protocol.

| File | Covers |
|---|---|
| `test_ingest.py` | ground-truth figures, dedup, age normalisation, reaction explosion |
| `test_analyses.py` | 20 analyses, the prompt projection boundary, packet claims |
| `test_config.py` | scoping, load-time validation, the real PADER config |
| `test_generate.py` | schema shape, retry, fallback ladder, rate-limit backoff |
| `test_grounding.py` | real model output as fixture, fabrication detection, negation |
| `test_run.py` | end-to-end, audit trail, approval gate, partial runs |
| `test_evaluate.py` | tier 2 and 3 authority boundaries |
| `test_generalization.py` | the zero-new-code claim, made falsifiable |

## Known limitations

Maintained in the README under "Known limitations" and kept in sync with this file.
Headline items: the unexplained Drug ineffective 54-vs-53 discrepancy (E-011, left open);
no SOC field so no SOC grouping; expectedness out of scope with no product label;
197 duplicate-flagged cases retained rather than dropped; grounding is numeric only.
