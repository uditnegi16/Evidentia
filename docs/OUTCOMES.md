# Outcomes

What was built, what it produced, and which grading criterion each piece answers.
Doubles as a revision sheet: each phase below is a thing to be able to explain and
modify live.

---

## Grading criteria and where each is answered

| Criterion | Answered by | Status |
|---|---|---|
| AI fundamentals | D-002 split: LLM writes, pandas computes | planned |
| Context engineering | `assembler.py` — per-section scoped packet | planned |
| Prompt design | `prompts/` Jinja templates, one per section | planned |
| Architecture | 8 modules, single responsibility each | planned |
| Agent/tool judgment | D-003 explicit rejection of agents/RAG | planned |
| Grounding | `grounding.py` number-membership gate | planned |
| Evaluation | D-009 three-tier asymmetric evaluation | planned |
| Generalization | D-008 second report type, zero code change | planned |
| Execution | One command regenerates the report | planned |

---

## Phase tracker

| # | Phase | Output | Status |
|---|---|---|---|
| 0 | Scaffold + data profiling | Repo, venv, package skeleton, `scripts/profile_data.py` | **done** |
| 1 | Ingest, validate, dedupe | `ingest.py`, validation report | in progress |
| 2 | Analysis registry | `analyses/`, evidence contracts, pytest suite | not started |
| 3 | Config schema and assembler | `configs/pader_fda.yaml`, `assembler.py` | not started |
| 4 | Section generation | `generate.py`, `prompts/*.jinja` | not started |
| 5 | Grounding validator | `grounding.py` | not started |
| 6 | Human review | `review_app.py`, `review.json` | not started |
| 7 | Renderers | `render.py` — markdown, html, docx | not started |
| 8 | Evaluation layer | `eval/`, tier 1-3 checks | not started |
| 9 | V1 second report type | `configs/psur_lite.yaml` | not started |
| 10 | README, diagram, packaging | `README.md`, Mermaid diagram, zip | not started |

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

## Run results

*(to be filled after first successful end-to-end run — token counts, wall time, tier 1-3
pass rates, sections flagged)*

---

## Known limitations

*(to be filled — this section becomes the README's limitations section verbatim)*
