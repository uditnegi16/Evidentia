# Error Log

Problems encountered during the build, what caused them, and how they were resolved.
Kept because "what went wrong and how I fixed it" is one of the most likely live-interview
questions, and because several of these become Known Limitations in the README.

**Entry format**

```
## E-00N — Short title
Phase:      which build phase
Symptom:    what was observed
Cause:      what was actually wrong
Fix:        what changed
Carry-over: does this become a README limitation, a test, or a config option?
```

---

## Anticipated issues (pre-registered before they happen)

Recording these up front so it is clear which were predicted and which were surprises.

### A-1 — Case vs reaction double counting
1,068 rows map to 1,024 unique `safetyreportid` values. Any analysis that counts rows
rather than cases will silently overstate. Every analysis must declare its unit of
count in the evidence packet.
**Mitigation:** unit-of-count is a required field on the evidence contract, not a comment.

### A-2 — Seriousness flags are not mutually exclusive
The six `seriousness*` columns are independent yes/no flags. Summing them across a case
produces a number larger than the case count. Any breakdown by seriousness type must be
presented as overlapping categories.
**Mitigation:** a test asserting the sum of seriousness flags exceeds total serious cases.

### A-3 — Strict structured output rejects optional fields
Groq strict mode requires all fields present and `additionalProperties: false`. A pydantic
model using `Optional[...]` will produce a 400.
**Mitigation:** LLM-facing schemas use empty list / empty string, never null.

### A-4 — Near-degenerate serious/non-serious split
1,023 of 1,024 cases are serious. A model asked to "compare serious and non-serious cases"
will be tempted to invent contrast where the data has none.
**Mitigation:** the section prompt states the distribution is near-degenerate and instructs
the model to report it as such rather than analyse it as a comparison.

### A-5 — No SOC field
Only MedDRA Preferred Term is available. The reference sample document groups by System
Organ Class, which cannot be reproduced from this dataset.
**Mitigation:** report at PT level and state explicitly that SOC grouping is unavailable.
Under no circumstances infer SOC from PT.

### A-6 — Two country columns
`occurcountry` and `primarysource_reportercountry` usually agree. One must be chosen and
the choice surfaced in the report, with the disagreement rate computed and noted.

### A-7 — No history of actions, no product label
Neither is supplied. The report must state that none were provided rather than omitting
the section or inventing content. Expectedness / labelledness is out of scope.

---

## Actual issues

## E-001 — Multi-value reaction fields nearly went undetected
**Phase:** 0 (profiling)
**Symptom:** `patient_reaction_reactionmeddrapt` showed 882 uniques in 1,068 rows; `patient_reaction_reactionoutcome` showed 251 uniques for a ~6-term vocabulary. Values like `recovered/resolved,recovered/resolved` appeared in the head of the value counts.
**Cause:** Multiple reactions are packed comma-separated into a single cell. Neither the challenge brief nor the Starter Guide mentions this.
**Fix:** Split on comma and explode before reaction-level analysis. Verified: yields exactly 3,648 events, matching the reference PADER.
**Carry-over:** Becomes a pytest assertion (`test_reaction_event_count == 3648`) and a README paragraph. This was the single highest-impact finding of the build — without it every reaction figure would have been silently wrong.

## E-002 — `pd.to_datetime` silently produced 1970 dates
**Phase:** 0 (profiling)
**Symptom:** Date range printed as `1970-01-01 00:00:00.020241227 -> 1970-01-01 00:00:00.020251226`.
**Cause:** `receivedate` is `int64` in YYYYMMDD form. `to_datetime` on an integer series interprets it as nanoseconds since epoch. No error, no warning.
**Fix:** `pd.to_datetime(df["receivedate"].astype(str), format="%Y%m%d")`. Correct range is 2024-12-27 to 2025-12-26, matching the reference.
**Carry-over:** Validation test asserts the period bounds. Also a good example of why silent-success bugs are the dangerous ones in this domain.

## E-003 — Corrupt value in the age unit column
**Phase:** 0 (profiling)
**Symptom:** `patient_patientonsetageunit` value counts include the integer `800` (3 rows) alongside year/month/week/day.
**Cause:** Data quality defect in the source.
**Fix:** Quarantine those 3 rows from age analysis, count them in the validation report, do not convert.
**Carry-over:** Known limitation. Demonstrates that the ingest layer detects rather than assumes.

## E-004 — Three country columns, not the two documented
**Phase:** 0 (profiling)
**Symptom:** `primarysourcecountry`, `occurcountry`, and `primarysource_reportercountry` all present. The Starter Guide mentions two.
**Cause:** Guide is a simplification.
**Fix:** Use `primarysourcecountry` (zero nulls). Report the 8-row disagreement with `occurcountry`.
**Carry-over:** Noted in the report as a stated methodological choice.

## E-005 — `duplicate` flag set on ~20% of cases with no definition
**Phase:** 0 (profiling)
**Symptom:** 218 rows / 204 cases carry `duplicate = 1`. No accompanying documentation.
**Cause:** Field is present in the E2B/FAERS-style schema but undefined for this exercise.
**Fix:** Count and surface; do not remove.
**Carry-over:** Known limitation, and an example of the system declining to make an unevidenced analytical decision.

## E-006 — egg-info and xlsx nearly entered git history
**Phase:** 0 (scaffold)
**Symptom:** `.gitignore` specified `*.csv` but the supplied dataset is `.xlsx`. Separately, `pip install -e .` generated `src/evidentia.egg-info/`, which was committed.
**Cause:** Assumed the file format stated in the brief.
**Fix:** Added `*.xlsx`, `*.xls`, `data/`, `*.egg-info/`, `build/`, `dist/`. Untracked egg-info with `git rm -r --cached`.
**Carry-over:** Loader now dispatches on file extension and accepts both CSV and Excel, since the brief and the delivered artifact disagreed.

## E-007 — Misread the reference's 3,648 as evidence it did not deduplicate (corrected)
**Phase:** 1, corrected in Phase 2
**Symptom:** Post-dedup reaction events came to 3,429 against the reference's stated 3,648. 3,648 was exactly our pre-dedup figure, so I concluded the reference pipeline counted superseded versions.
**Cause:** Comparing one aggregate total against another without checking a second, independent quantity. A single matching number is weak evidence; I treated it as strong.
**Fix:** Compared per-PT counts instead. Post-dedup reproduces the reference on 4 of 5 terms (80/46/43/33); pre-dedup matches none. The reference *does* dedupe, and 3,648 is a different quantity. D-017 rewritten.
**Carry-over:** The generalisable lesson is that one coincidence is not corroboration. Cheap to check a second quantity, expensive to build on a wrong model of the source pipeline.

## E-008 — Branch already existed on re-run
**Phase:** 1 (setup)
**Symptom:** `git checkout -b phase-1-ingest` failed with "a branch named ... already exists".
**Cause:** Branch created in an earlier session.
**Fix:** `git checkout phase-1-ingest`. Untracked extracted files are unaffected by branch switching.
**Carry-over:** None.

## E-009 — Scripted refactor deleted constants by position
**Phase:** 2 (setup)
**Symptom:** `NameError: UNIT_TO_YEARS is not defined`, cascading into 33 test errors.
**Cause:** Moving `SCHEMA` from `ingest.py` to `contracts.py` used a text range from a comment marker to `UNKNOWN = "unknown"`. `UNIT_TO_YEARS` and `AGE_BANDS` sat between those markers and were swept up.
**Fix:** Restored both to `ingest.py`, where they belong — they are transformation *policy*, not source schema, so changing them changes every downstream number.
**Carry-over:** Constants are now split on a stated principle: source description in `contracts.py`, normalisation policy in `ingest.py`.

## E-010 — Regex-based code edit produced invalid syntax
**Phase:** 2 (lint)
**Symptom:** After scripting a fix for 15 ISC004 warnings, `ruff` reported `invalid-syntax: Positional argument cannot follow keyword argument`.
**Cause:** The text heuristic matched any line ending in a quote followed by a string line, which caught keyword arguments such as `code="country_disagreement"` as well as genuine list elements.
**Fix:** Rewrote the transform to locate implicit concatenations via `ast.walk`, filtering to elements of `List`/`Tuple`/`Set` nodes, and to apply edits bottom-up so earlier line numbers stay valid. Restored the corrupted file from the previous zip and reapplied.
**Carry-over:** Structural code edits need a parser, not a regex. Also a practical argument for shipping each phase as a zip: the previous good state was one command away.

## E-011 — Drug ineffective count differs from the reference by one
**Phase:** 2 (analyses) — **open**
**Symptom:** Our post-dedup count is 54 distinct cases; the reference PADER states 53. All four other compared terms match exactly.
**Cause:** Unknown. Candidates: the reference excludes one duplicate-flagged case, applies a different tie-break on equal `safetyreportversion`, or filters on a field we treat as out of scope.
**Fix:** None. The figure is asserted at 54 with a comment pointing here, so it cannot drift silently.
**Carry-over:** README limitation. Stating a one-case unexplained gap is more defensible than tuning the pipeline until it matches, which would be fitting to an artifact rather than to the data.


## E-012 — Import failure that was actually a branch-state failure
**Phase:** 3
**Symptom:** `ModuleNotFoundError: No module named 'evidentia.evidence'` on a module verified passing two phases earlier.
**Cause:** Phase 2 was pushed but its PR never merged, so `main` still held Phase 1. Branching Phase 3 off `main` produced a tree without `evidence.py`.
**Fix:** Merged Phase 2, rebased. Dropped the PR ceremony entirely afterwards — solo repo, no reviewer, so branch-per-phase bought nothing but a failure mode.
**Carry-over:** If a previously-green import breaks, suspect the tree before the code.

## E-013 — Empty generation reported as failed JSON validation
**Phase:** 4
**Symptom:** `400 json_validate_failed` with `failed_generation: ''`.
**Cause:** Two compounding causes. `gpt-oss-120b` is a reasoning model and reasoning tokens are charged to the completion budget; at `max_tokens=1200` the chain of thought consumed everything and the answer was empty. Separately, strict structured output on this model family is reported by other users to fail outright.
**Fix:** `max_tokens` → `max_completion_tokens` at 4000, `reasoning_effort: low`, and a three-rung fallback ladder recording which rung produced each section.
**Carry-over:** The provider's error named the wrong cause. Truncation was reported as a schema failure.

## E-014 — Six false blocking issues from typographic dashes
**Phase:** 6
**Symptom:** First full run blocked `summary_analysis_of_cases` on 45, 64, 65, 74, 75, 84.
**Cause:** Age bands stored as `45-64` arrive from the model as `45\u201364`. Label masking used literal string comparison, so the band never matched and both boundaries surfaced as fabrications.
**Fix:** Normalise seven Unicode dash variants before masking. Also masked interval terminology (`15-day Alert`), which was blocking on 15.
**Carry-over:** A gate producing false positives trains reviewers to ignore it, which is worse than no gate. Masks were kept narrow and a test confirms fabrications adjacent to masked terms still block.

## E-015 — Model performed arithmetic because the figure was withheld
**Phase:** 6
**Symptom:** 938 and 1068 blocked as ungrounded.
**Cause:** Both are real figures the system computed and never sent. 938 is `age_bands.provenance.n_contributing`; 1068 is the raw row count. The model reconstructed 938 as 1024 − 86.
**Fix:** `numeric_claims()` now harvests numbers from bucket labels (so `65-74` grounds 65 and 74, and `2025-03` grounds 2025), `n_contributing` is sent to the model and claimable, and ingest totals were added to `data_quality`.
**Carry-over:** Instructing a model not to calculate while hiding the figure it needs is an unfair instruction. Every fix put real figures into the packet; the tolerance was never loosened.

## E-016 — The most important forbidden phrase excused itself
**Phase:** 7 — **near miss**
**Symptom:** After adding negation awareness, `test_the_phrase_named_in_the_brief_is_blocked` failed. "no safety concerns were identified" was passing.
**Cause:** The negation window included the matched phrase, and "no" was a negation marker. The phrase named explicitly in the challenge brief was excusing itself through its own first word — silently, while every other test passed.
**Fix:** Exclude the matched phrase from the window; drop "no", "none", "never" as markers; tighten windows to 45 characters before and 70 after.
**Carry-over:** The most dangerous bugs are the ones that disable a safety check while leaving the surrounding tests green. Six tests now pin this, including one burying a real claim twelve sentences after an unrelated denial.

## E-017 — Free-tier rate limit halted a run mid-report
**Phase:** 7
**Symptom:** `429 ... tokens per minute (TPM): Limit 8000` after two PSUR sections.
**Cause:** Seven sections at roughly 2,000 prompt tokens each exceeds the free tier within a minute.
**Fix:** Retry with backoff, parsing the provider's stated wait.
**Carry-over:** Anyone running this from the README is on the free tier. Robustness here is not polish.

## E-018 — Internal issue codes leaked into regulatory prose
**Phase:** 8
**Symptom:** The Data Limitations section read "the duplicate_flag_present warning was raised for 197 cases".
**Cause:** `data_quality` buckets were labelled with raw validator codes, which was the only vocabulary the model had.
**Fix:** Human-readable labels, with a test asserting no underscore or severity marker survives into a packet label.
**Carry-over:** The model can only be as readable as its packet.

## E-019 — Repeated overwriting of the user's local fixes
**Phase:** 1-2, process
**Symptom:** The same test failure reappeared three times across turns.
**Cause:** Zips were built from the assistant's container copy, which lacked fixes already applied locally.
**Fix:** Container copy became the single source of truth; every fix applied there first, and every zip verified (lint plus tests) before delivery.
**Carry-over:** With two copies of a codebase and no sync, one of them is always wrong. Naming which is authoritative is not optional.
