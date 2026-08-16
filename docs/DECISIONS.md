# Decision Log

Architectural decisions for the PADER report engine, with the reasoning behind each.
Format: what was decided, what the alternatives were, why this one won.

Kept as a running record so any decision can be defended or revisited.

---

## D-001 — Build a report engine, not a PADER generator

**Decided:** The system is a generic pipeline (`reporting task -> gather evidence -> reason -> controlled traceable report`). PADER is the first report type it loads from configuration.

**Alternative:** Write a script that produces a PADER directly.

**Why:** The challenge brief states this explicitly as the lens for the whole assignment. A direct PADER script scores on Execution and nothing else. The engine framing is what makes the Generalization criterion answerable.

**Consequence:** Every piece of PADER-specific knowledge — section names, required analyses, tone rules, prompt text — must live in YAML or Jinja, never in Python.

---

## D-002 — LLM never computes a number

**Decided:** All arithmetic, aggregation, grouping and counting happens in pandas. The LLM receives finished figures and writes prose around them.

**Alternative:** Pass the CSV or a sample to the model and let it analyse.

**Why:** The brief calls handing the model the raw CSV "the thing we explicitly don't want to see; it's fast to build and impossible to trust." Deterministic analysis is exact, reproducible, and unit-testable. LLM arithmetic is none of those.

**Consequence:** The interesting engineering problem moves from prompting to *context assembly* — deciding exactly which computed facts each section is allowed to see.

---

## D-003 — Python 3.11+, pandas, pydantic v2, PyYAML, Jinja2, pytest

**Decided:** Standard library plus five small, boring dependencies.

**Rejected and why:**

| Rejected | Reason |
|---|---|
| LangChain / LangGraph | Adds an abstraction layer over a 7-step linear pipeline with no branching. Cost without benefit. |
| Vector database / RAG | The corpus is 1,068 rows of structured tabular data. `groupby` *is* the retrieval mechanism. A vector index would be strictly worse at the actual task. |
| Agent framework | No step requires dynamic tool selection. The pipeline order is fixed and known at design time. |

**Why it matters:** The brief says "more agents, more frameworks, or RAG where a lookup would do doesn't score points." Naming these rejections explicitly is worth more than silently not using them.

---

## D-004 — pydantic v2 for evidence contracts

**Decided:** Every analysis returns a typed pydantic object, not a dict.

**Why:** Three benefits at once — runtime validation, free JSON serialisation into the evidence packet, and a visible, enforced contract between the deterministic layer and the LLM layer. That contract is the thing being graded under "Grounding."

**Consequence:** Adding an analysis means adding a model, which means the packet schema is self-documenting.

---

## D-005 — Groq, model `openai/gpt-oss-120b`

**Decided:** Groq Cloud via the official `groq` Python SDK. Primary model `openai/gpt-oss-120b`, `temperature=0`, fixed seed.

**Why this model specifically:** Groq's strict structured-output mode uses constrained decoding to guarantee schema conformance, but strict mode is only supported on `openai/gpt-oss-20b` and `openai/gpt-oss-120b`. Other Groq models silently ignore `strict=True` and fall back to best-effort. Since the grounding validator depends on parsing a structured response, guaranteed conformance outranks any quality difference. 120B over 20B for output quality on regulatory prose.

**Constraint this imposes:** Strict mode requires every field to be required and every object to set `additionalProperties: false`. So no `Optional[...]` fields in LLM output schemas — use empty lists and empty strings instead of nulls.

**Secondary model:** `llama-3.3-70b-versatile`, used only in the evaluation layer for cross-checking. Never in a production run.

---

## D-006 — Streamlit for the human review layer

**Decided:** A Streamlit app showing each generated section beside the exact evidence packet that produced it, with approve/flag controls. Approval state persists to `review.json`. The report renders as final only when all sections are approved.

**Alternative:** CLI plus a hand-edited `review.json`, with the UI described in the README (explicitly permitted by the brief).

**Why:** The side-by-side layout demonstrates two separate grading criteria — Human control and Grounding — with one artifact. Seeing prose next to its evidence is more convincing than any written description of traceability.

---

## D-007 — Markdown canonical, HTML and DOCX as pluggable renderers

**Decided:** Sections are structured objects. Markdown, HTML and DOCX are three implementations of a `Renderer` protocol, selected via `output_formats: [...]` in the report config.

**Why:** The supplied reference artifact is a Word document with structured tables, so DOCX shows understanding of the real end product. Making renderers pluggable rather than hardcoding three export functions applies the same "behaviour is configuration" principle used everywhere else in the system.

---

## D-008 — Version 1 is a second report type config

**Decided:** `configs/psur_lite.yaml` — different sections, different required analyses, different tone rules, same engine, same dataset, zero Python changes.

**Rejected alternatives:**

- *Section dependency graph* — already core V0 behaviour (sections declare `requires:`). Claiming it as V1 would be padding.
- *Evidence tracing UI* — duplicates coverage already provided by the grounding validator and the Streamlit review panel.
- *Run manifest / versioning* — valuable but small (~40 lines), so folded into V0 instead.

**Why this one:** It is the only option that converts the generalisation *claim* into generalisation *evidence*. A `git diff --stat` showing a complete second report type that touched no `.py` files is falsifiable in a way that prose is not.

**Stated limitation:** This is not real PSUR data. It demonstrates that the engine is report-type-agnostic, not that the output is a valid PSUR. Documented as such.

---

## D-009 — Evaluation has three tiers with asymmetric authority

**Decided:**

> Deterministic checks are hard gates. LLM judgments are soft flags. An LLM never approves anything.

| Tier | Mechanism | Catches | Authority |
|---|---|---|---|
| 1 | Number extraction and set-membership against the evidence packet; schema conformance; section completeness; forbidden-phrase scan | Fabricated figures | **Blocks** render |
| 2 | Same packet through `gpt-oss-120b` and `llama-3.3-70b`, diffed | Prompt instability, ambiguity | **Flags** |
| 3 | LLM-as-judge against a rubric (over-claiming, causal language, interpretation beyond evidence) | Domain-specific regulatory risk | **Flags** for human review |

**Why asymmetric:** Tier 1 catches the failure mode that actually matters and costs nothing. Tier 3 catches the only thing regex cannot — the line between observation and interpretation, which the Starter Guide is explicit about. But an LLM that can approve is an LLM that can hallucinate approval, so tier 3 only ever routes upward to a human.

**Honesty note for the README:** Tier 2 detects *disagreement*, not *correctness*. Two models can agree and both be wrong. This limitation is stated rather than glossed.

---

## D-010 — Forbidden-phrase scan as an explicit gate

**Decided:** A hardcoded, config-extensible denylist checked against every generated section. Initial entries include: "no safety concerns were identified", "confirmed signal", "causally related", "proven safe", "demonstrates that".

**Why:** The challenge brief names this failure mode directly — the report "can't say 'no safety concerns were identified' unless something in your system actually establishes that." Catching it deterministically is a five-line function that addresses a stated worry head-on.

---

## D-011 — Reaction fields are comma-joined multi-value strings and must be split

**Decided:** `patient_reaction_reactionmeddrapt` and `patient_reaction_reactionoutcome` contain multiple values per cell, comma-separated. They are split and exploded before any reaction-level analysis.

**How it was discovered:** Profiling showed 882 unique values in a MedDRA PT column across 1,068 rows, and outcome values such as `recovered/resolved,recovered/resolved`. A vocabulary of ~6 outcomes cannot have 251 uniques.

**Validation:** Splitting on comma yields **exactly 3,648 reaction events**, matching the total in the supplied reference PADER. Top PTs after split (Acute kidney injury 81, Drug ineffective 60, Hypotension 48) approximate the reference (80, 53, 46). This confirms the reference pipeline splits the same way.

**Why it matters:** Without splitting, every reaction figure in the report is wrong while still looking plausible. The worked example in the challenge brief (`Acute kidney injury: 22`) is the unsplit figure, and that document describes itself as a miniature illustration.

**Consequence:** Two distinct units of count exist — case-level (1,024) and reaction-event-level (3,648). Every analysis declares which one it uses.

---

## D-012 — Multi-row cases are report versions; dedup keeps the highest version

**Decided:** Dedup by `safetyreportid`, keeping the row with the maximum `safetyreportversion`.

**Evidence:** Of 41 multi-row cases, all 41 differ by `safetyreportversion`; none differ by `report_date`. A sampled case shows identical reaction strings across versions 2 and 3. Only 15 differ in reaction PT, consistent with version updates revising the reaction list.

**Alternative rejected:** Treating extra rows as additional reactions. That would double-count, and is contradicted by the identical reaction strings across versions.

**Consequence:** Reaction counts computed post-dedup will fall slightly below the raw-split figures. The residual gap against the reference is recorded rather than tuned away.

---

## D-013 — `receivedate` is an integer YYYYMMDD; use the pre-parsed `report_date`

**Decided:** Reporting period derives from `report_date`, which is already `datetime64` and verified equal to a correct parse of `receivedate`.

**Period confirmed:** 2024-12-27 to 2025-12-26 — matches the reference PADER's stated interval exactly.

**Trap recorded:** `pd.to_datetime` on the raw int64 silently interprets it as nanoseconds since epoch and returns 1970 dates without erroring. Parsing must go via `.astype(str)` with `format="%Y%m%d"`.

---

## D-014 — Age normalised to years via `patient_patientonsetageunit`

**Decided:** Convert to years using the unit column before any bucketing. Rows with the corrupt unit value `800` (3 rows) are quarantined, not converted.

**Evidence:** Unit distribution is year 975, month 5, day 3, week 1, `800` 3, null 81. A raw minimum of 1.0 is one *month*, not one year.

**Why:** The Starter Guide suggests bucketing the numeric column directly. Doing so would classify infants as adults. Deviating from the guide here is deliberate and documented.

**Bands:** <18, 18-44, 45-64, 65-74, 75-84, 85+, unknown.

---

## D-015 — Country field is `primarysourcecountry`

**Decided:** Use `primarysourcecountry` for all geographic analysis.

**Why:** Zero nulls, versus 0.7% for `occurcountry`. It is the regulatory source of record. Three country columns exist, not the two the guide mentions; they disagree on only 8 rows, and that figure is stated in the report.

---

## D-016 — `duplicate` flag is surfaced, not acted on

**Decided:** 218 rows spanning 204 cases carry `duplicate = 1`. These are counted and reported; they are **not** removed.

**Why:** Roughly 20% of cases is too large to drop silently, and nothing in the supplied material defines what the flag means or whether the duplicate is this case or another. Removing them would be an unevidenced analytical decision. Surfacing the count lets a qualified reviewer decide.

---

## D-017 — Version-dedup is correct; 3,648 is not the comparable figure (revised)

**Superseded an earlier version of this decision.** The original reasoning defended dedup on principle while treating the reference PADER's 3,648 as evidence the reference did not dedupe. Phase 2 showed that reading was wrong.

**What the evidence actually says.** Comparing per-PT counts against the reference PADER's Case Presentation section:

| Preferred Term | Reference | Ours, pre-dedup | Ours, post-dedup |
|---|---|---|---|
| Acute kidney injury | 80 | 81 | **80** |
| Hypotension | 46 | 48 | **46** |
| Drug interaction | 43 | 45 | **43** |
| Fatigue | 33 | 35 | **33** |
| Drug ineffective | 53 | 60 | 54 |

Four of five reproduce exactly after dedup and none match before it. Version-dedup is therefore **confirmed correct against an independent artifact**, not merely defensible.

**Revised reading of 3,648.** That total is a different quantity from the per-PT figures, computed over a different population or a different unit in the reference pipeline. We report 3,429 reaction events post-dedup and state the difference; we no longer claim the reference "does not dedupe," because its own per-PT numbers show it does.

**Remaining discrepancy:** Drug ineffective, 54 against 53. Open — see E-011.

---

## D-018 — Case-level and event-level reaction counts are separate analyses

**Decided:** `top_reactions` (unit `reaction_event`) and `top_reactions_by_case` (unit `case`) both exist. A sentence of the form "N cases of X were reported" must read the case-level one.

**Why, when they currently return identical numbers:** On this dataset no case reports the same PT twice, so the two coincide for every PT. That is a property of *this data*, not a rule. The reference PADER's wording is explicitly case-level ("80 cases of acute kidney injury"), so matching its number with an event count would be correct by coincidence.

**How the coincidence is held:** a test asserts the two agree on every shared PT. If a future dataset breaks it, the test fails and names the diverging term, rather than a report quietly attaching the word "cases" to an event count.

**Cost:** one extra analysis. **Benefit:** the unit boundary is enforced rather than assumed, which is the same principle as D-011 applied one level up.

---

## Open questions

*(all Phase 0 open questions resolved by profiling — see D-011 through D-016)*

- Reaction and outcome lists align positionally in 99.4% of rows. The 0.6% that do not need an explicit handling rule rather than a silent `zip()` truncation. To be decided in Phase 2.
