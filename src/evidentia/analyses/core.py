"""Core analyses.

Every number the report may state originates here, in pandas, deterministically.
The LLM never computes; it selects, orders and narrates (D-002).

Two units of count coexist and are never mixed silently:

    case            1,024 deduped cases        frame.cases
    reaction_event  3,429 exploded reactions   frame.reactions

Each item declares its unit in provenance, and that unit reaches the model. A
model told only "81" and "1,024" may divide them; told the first is a reaction
event count and the second a case count, it has what it needs not to.
"""

from __future__ import annotations

import pandas as pd

from evidentia.analyses.registry import analysis
from evidentia.contracts import SCHEMA, SERIOUSNESS_FLAGS, UNKNOWN, CaseFrame
from evidentia.evidence import Bucket, EvidenceItem, Provenance

CASE_ID = SCHEMA["case_id"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _buckets_from(
    df: pd.DataFrame,
    column: str,
    *,
    denominator: int,
    top_n: int | None = None,
    sort_by_index: bool = False,
    order: list[str] | None = None,
) -> list[Bucket]:
    """Group a frame by one column into buckets carrying their own case IDs."""
    series = df[column].fillna(UNKNOWN).astype(str)
    grouped = df.assign(_k=series).groupby("_k")

    rows: list[tuple[str, int, list[int]]] = [
        (str(k), int(len(g)), sorted({int(c) for c in g[CASE_ID]}))
        for k, g in grouped
    ]

    if order:
        rank = {v: i for i, v in enumerate(order)}
        rows.sort(key=lambda r: (rank.get(r[0], len(order)), r[0]))
    elif sort_by_index:
        rows.sort(key=lambda r: r[0])
    else:
        rows.sort(key=lambda r: (-r[1], r[0]))

    if top_n:
        rows = rows[:top_n]

    return [
        Bucket(label=lbl, count=n, pct=_pct(n, denominator), case_ids=ids)
        for lbl, n, ids in rows
    ]


def _all_ids(df: pd.DataFrame) -> list[int]:
    return sorted({int(c) for c in df[CASE_ID]})


# --------------------------------------------------------------------------
# reporting period and volume
# --------------------------------------------------------------------------


@analysis("period_bounds", "Reporting period", unit="case")
def period_bounds(frame: CaseFrame) -> EvidenceItem:
    """First and last report date in the dataset."""
    v = frame.validation
    days = (v.period_end - v.period_start).days + 1
    return EvidenceItem(
        key="period_bounds",
        label="Reporting period",
        kind="statement",
        value=f"{v.period_start} to {v.period_end}",
        provenance=Provenance(
            unit="case",
            method="min and max of report_date across deduped cases",
            source_columns=[SCHEMA["date"], SCHEMA["date_raw"]],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
            notes=[f"period spans {days} days"],
        ),
    )


@analysis("total_cases", "Total cases received", unit="case")
def total_cases(frame: CaseFrame) -> EvidenceItem:
    """Unique cases after collapsing superseded report versions."""
    v = frame.validation
    return EvidenceItem(
        key="total_cases",
        label="Total cases received",
        kind="scalar",
        value=frame.n_cases,
        provenance=Provenance(
            unit="case",
            method="unique safetyreportid, keeping highest safetyreportversion",
            source_columns=[CASE_ID, SCHEMA["version"]],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
            case_ids=_all_ids(frame.cases),
            notes=[
                f"{v.raw_rows} raw rows collapsed to {v.unique_cases} cases",
                f"{v.rows_dropped_as_superseded} rows dropped as superseded versions",
            ],
        ),
    )


@analysis("total_reaction_events", "Total reaction events", unit="reaction_event")
def total_reaction_events(frame: CaseFrame) -> EvidenceItem:
    """Reaction events after comma-splitting the MedDRA PT field."""
    v = frame.validation
    return EvidenceItem(
        key="total_reaction_events",
        label="Total reaction events",
        kind="scalar",
        value=frame.n_reaction_events,
        provenance=Provenance(
            unit="reaction_event",
            method="comma-split of reaction PT field, exploded, post version-dedup",
            source_columns=[SCHEMA["reaction_pt"]],
            n_contributing=frame.n_reaction_events,
            denominator=frame.n_reaction_events,
            notes=[
                f"{v.reaction_events_raw} events before version-dedup",
                "a case may report more than one reaction",
            ],
        ),
    )


# --------------------------------------------------------------------------
# seriousness
# --------------------------------------------------------------------------


@analysis("serious_split", "Serious vs non-serious cases", unit="case")
def serious_split(frame: CaseFrame) -> EvidenceItem:
    """Case-level seriousness classification."""
    return EvidenceItem(
        key="serious_split",
        label="Serious vs non-serious cases",
        kind="distribution",
        buckets=_buckets_from(
            frame.cases,
            SCHEMA["serious"],
            denominator=frame.n_cases,
            order=["serious", "not serious"],
        ),
        provenance=Provenance(
            unit="case",
            method="value counts of the serious flag over deduped cases",
            source_columns=[SCHEMA["serious"]],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
            notes=[
                (
                    "the distribution is near-degenerate; this is normal for "
                    "spontaneous ICSR data and is not a data defect"
                ),
            ],
        ),
    )


@analysis("seriousness_criteria", "Seriousness criteria met", unit="case")
def seriousness_criteria(frame: CaseFrame) -> EvidenceItem:
    """Breakdown by seriousness criterion. Criteria are NOT mutually exclusive."""
    cases = frame.cases
    buckets: list[Bucket] = []
    for col in SERIOUSNESS_FLAGS:
        hit = cases[cases[col].astype(str).str.lower() == "yes"]
        buckets.append(
            Bucket(
                label=col.replace("seriousness", "").replace(
                    "congenitalanomali", "congenital anomaly"
                ),
                count=len(hit),
                pct=_pct(len(hit), frame.n_cases),
                case_ids=_all_ids(hit),
            )
        )
    buckets.sort(key=lambda b: -b.count)
    total = sum(b.count for b in buckets)
    return EvidenceItem(
        key="seriousness_criteria",
        label="Seriousness criteria met",
        kind="distribution",
        buckets=buckets,
        provenance=Provenance(
            unit="case",
            method="independent yes/no flags counted separately",
            source_columns=SERIOUSNESS_FLAGS,
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
            notes=[
                "criteria are NOT mutually exclusive; one case may meet several",
                f"criterion hits total {total}, which exceeds the case count by design",
                "percentages therefore do not sum to 100",
            ],
        ),
    )


@analysis("alert_cases", "15-day Alert cases", unit="case")
def alert_cases(frame: CaseFrame) -> EvidenceItem:
    """Cases meeting expedited reporting criteria."""
    cases = frame.cases
    hit = cases[cases[SCHEMA["expedite"]].astype(str).str.lower() == "yes"]
    return EvidenceItem(
        key="alert_cases",
        label="15-day Alert cases",
        kind="scalar",
        value=len(hit),
        provenance=Provenance(
            unit="case",
            method="cases where fulfillexpeditecriteria is yes",
            source_columns=[SCHEMA["expedite"]],
            n_contributing=len(hit),
            denominator=frame.n_cases,
            case_ids=_all_ids(hit),
            notes=[
                (
                    "expedited and serious are near-identical populations in this "
                    "dataset; they are counted independently, not assumed equal"
                ),
                (
                    "expectedness (labelled/unlabelled) is not derivable: no product "
                    "label or CCDS was supplied"
                ),
            ],
        ),
    )


# --------------------------------------------------------------------------
# patient characteristics
# --------------------------------------------------------------------------


@analysis("age_bands", "Cases by age group", unit="case")
def age_bands(frame: CaseFrame) -> EvidenceItem:
    """Age distribution, normalised to years before banding."""
    order = ["<18", "18-44", "45-64", "65-74", "75-84", "85+", UNKNOWN]
    known = frame.cases["age_years"].notna().sum()
    return EvidenceItem(
        key="age_bands",
        label="Cases by age group",
        kind="distribution",
        buckets=_buckets_from(
            frame.cases, "age_band", denominator=frame.n_cases, order=order
        ),
        provenance=Provenance(
            unit="case",
            method="onset age converted to years via its unit column, then banded",
            source_columns=[SCHEMA["age"], SCHEMA["age_unit"]],
            n_contributing=int(known),
            denominator=frame.n_cases,
            notes=[
                "the coarse patient_patientagegroup column is >97% empty and unused",
                (
                    "ages recorded in months, weeks or days are converted, not assumed "
                    "to be years"
                ),
                f"{frame.n_cases - int(known)} cases have no usable age",
            ],
        ),
    )


@analysis("sex_distribution", "Cases by sex", unit="case")
def sex_distribution(frame: CaseFrame) -> EvidenceItem:
    """Sex distribution across cases."""
    return EvidenceItem(
        key="sex_distribution",
        label="Cases by sex",
        kind="distribution",
        buckets=_buckets_from(
            frame.cases, SCHEMA["sex"], denominator=frame.n_cases
        ),
        provenance=Provenance(
            unit="case",
            method="value counts of patient sex; blanks reported as unknown",
            source_columns=[SCHEMA["sex"]],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
        ),
    )


@analysis(
    "country_distribution",
    "Cases by country",
    unit="case",
    defaults={"top_n": 15},
)
def country_distribution(frame: CaseFrame, top_n: int = 15) -> EvidenceItem:
    """Geographic distribution using the primary source country."""
    return EvidenceItem(
        key="country_distribution",
        label="Cases by country",
        kind="distribution",
        buckets=_buckets_from(
            frame.cases,
            SCHEMA["country"],
            denominator=frame.n_cases,
            top_n=top_n,
        ),
        provenance=Provenance(
            unit="case",
            method=f"value counts of primarysourcecountry, top {top_n}",
            source_columns=[SCHEMA["country"]],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
            notes=[
                (
                    "three country columns exist; primarysourcecountry is used as it "
                    "has no missing values (D-015)"
                ),
                "'eu' appears as a reported region rather than a single country",
            ],
        ),
    )


@analysis("reporter_type", "Cases by reporter qualification", unit="case")
def reporter_type(frame: CaseFrame) -> EvidenceItem:
    """Who reported the case."""
    return EvidenceItem(
        key="reporter_type",
        label="Cases by reporter qualification",
        kind="distribution",
        buckets=_buckets_from(
            frame.cases, SCHEMA["reporter"], denominator=frame.n_cases
        ),
        provenance=Provenance(
            unit="case",
            method="value counts of primary source qualification",
            source_columns=[SCHEMA["reporter"]],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
        ),
    )


# --------------------------------------------------------------------------
# reactions
# --------------------------------------------------------------------------


@analysis(
    "top_reactions_by_case",
    "Most frequently reported reactions (distinct cases)",
    unit="case",
    defaults={"top_n": 20},
)
def top_reactions_by_case(frame: CaseFrame, top_n: int = 20) -> EvidenceItem:
    """Distinct cases reporting each PT, as opposed to reaction events.

    This exists because event-level and case-level counts happen to coincide on
    this dataset — no case lists the same PT twice — but nothing guarantees that
    for another dataset or another report type. Any sentence of the form
    "N cases of X were reported" must read this analysis, not top_reactions.
    """
    r = frame.reactions
    rows: list[tuple[str, list[int]]] = []
    for pt, group in r.groupby("reaction_pt")[CASE_ID]:
        ids = sorted({int(c) for c in group})
        rows.append((str(pt), ids))
    rows.sort(key=lambda x: (-len(x[1]), x[0]))

    buckets = [
        Bucket(
            label=pt,
            count=len(ids),
            pct=_pct(len(ids), frame.n_cases),
            case_ids=ids,
        )
        for pt, ids in rows[:top_n]
    ]
    return EvidenceItem(
        key="top_reactions_by_case",
        label="Most frequently reported reactions (distinct cases)",
        kind="distribution",
        buckets=buckets,
        provenance=Provenance(
            unit="case",
            method=(
                f"distinct safetyreportid per MedDRA PT, top {top_n}; "
                "a case reporting a PT more than once is counted once"
            ),
            source_columns=[SCHEMA["reaction_pt"], CASE_ID],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
            notes=[
                (
                    "counts are distinct cases; they do not sum to the case "
                    "total because one case may report several different PTs"
                ),
                (
                    "on this dataset these counts equal the event-level counts, "
                    "but that is a property of the data and is asserted by test "
                    "rather than assumed"
                ),
            ],
        ),
    )


@analysis(
    "top_reactions",
    "Most frequently reported reactions",
    unit="reaction_event",
    defaults={"top_n": 20},
)
def top_reactions(frame: CaseFrame, top_n: int = 20) -> EvidenceItem:
    """Most common MedDRA Preferred Terms across all reaction events."""
    r = frame.reactions
    return EvidenceItem(
        key="top_reactions",
        label="Most frequently reported reactions",
        kind="distribution",
        buckets=_buckets_from(
            r, "reaction_pt", denominator=len(r), top_n=top_n
        ),
        provenance=Provenance(
            unit="reaction_event",
            method=f"value counts of MedDRA PT over exploded events, top {top_n}",
            source_columns=[SCHEMA["reaction_pt"]],
            n_contributing=len(r),
            denominator=len(r),
            notes=[
                (
                    "counts are reaction events, not cases; one case may contribute "
                    "several"
                ),
                (
                    "no System Organ Class field exists in this dataset, so SOC-level "
                    "grouping is unavailable and is not inferred"
                ),
            ],
        ),
    )


@analysis(
    "top_serious_reactions",
    "Most frequently reported serious reactions",
    unit="reaction_event",
    defaults={"top_n": 20},
)
def top_serious_reactions(frame: CaseFrame, top_n: int = 20) -> EvidenceItem:
    """Most common PTs restricted to reaction events from serious cases."""
    r = frame.reactions
    serious = r[r[SCHEMA["serious"]].astype(str).str.lower() == "serious"]
    return EvidenceItem(
        key="top_serious_reactions",
        label="Most frequently reported serious reactions",
        kind="distribution",
        buckets=_buckets_from(
            serious, "reaction_pt", denominator=len(serious), top_n=top_n
        ),
        provenance=Provenance(
            unit="reaction_event",
            method=(
                f"reaction events from cases flagged serious, top {top_n} by PT"
            ),
            source_columns=[SCHEMA["reaction_pt"], SCHEMA["serious"]],
            n_contributing=len(serious),
            denominator=len(serious),
            notes=[
                (
                    "seriousness is a case-level attribute applied to every reaction "
                    "within that case"
                ),
                (
                    "near-identical to the overall ranking because almost all cases "
                    "are serious"
                ),
            ],
        ),
    )


@analysis("reaction_outcomes", "Reaction outcomes", unit="reaction_event")
def reaction_outcomes(frame: CaseFrame) -> EvidenceItem:
    """Outcome distribution across reaction events."""
    r = frame.reactions
    misaligned = int((~r["outcome_aligned"]).sum())
    return EvidenceItem(
        key="reaction_outcomes",
        label="Reaction outcomes",
        kind="distribution",
        buckets=_buckets_from(
            r, "reaction_outcome", denominator=len(r)
        ),
        provenance=Provenance(
            unit="reaction_event",
            method="outcome aligned positionally to its reaction, then counted",
            source_columns=[SCHEMA["reaction_outcome"]],
            n_contributing=len(r),
            denominator=len(r),
            notes=[
                (
                    f"{misaligned} events come from rows where reaction and outcome "
                    "counts differed; their outcome is recorded as unknown rather "
                    "than guessed"
                ),
            ],
        ),
    )


@analysis(
    "reactions_by_sex",
    "Reactions by sex",
    unit="reaction_event",
    defaults={"top_n": 10},
)
def reactions_by_sex(frame: CaseFrame, top_n: int = 10) -> EvidenceItem:
    """Top reactions split by patient sex."""
    r = frame.reactions
    buckets: list[Bucket] = []
    for sex, grp in r.groupby(r[SCHEMA["sex"]].fillna(UNKNOWN).astype(str)):
        for b in _buckets_from(
            grp, "reaction_pt", denominator=len(grp), top_n=top_n
        ):
            buckets.append(
                Bucket(
                    label=f"{sex} — {b.label}",
                    count=b.count,
                    pct=b.pct,
                    case_ids=b.case_ids,
                )
            )
    return EvidenceItem(
        key="reactions_by_sex",
        label="Reactions by sex",
        kind="distribution",
        buckets=buckets,
        provenance=Provenance(
            unit="reaction_event",
            method=f"top {top_n} PTs computed within each sex group",
            source_columns=[SCHEMA["reaction_pt"], SCHEMA["sex"]],
            n_contributing=len(r),
            denominator=len(r),
            notes=["percentages are within-group, not of the overall total"],
        ),
    )


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------


@analysis("monthly_case_volume", "Case volume by month", unit="case")
def monthly_case_volume(frame: CaseFrame) -> EvidenceItem:
    """Cases received per calendar month."""
    cases = frame.cases.copy()
    cases["_m"] = pd.to_datetime(cases["report_date"]).dt.to_period("M").astype(str)
    buckets = _buckets_from(
        cases, "_m", denominator=frame.n_cases, sort_by_index=True
    )
    counts = [b.count for b in buckets]
    notes = [
        (
            "the reporting period does not align to calendar month boundaries, so "
            "the first and last months are partial and not comparable to the rest"
        ),
        "a change in volume is an observation, not a safety signal",
    ]
    if len(counts) > 2:
        interior = counts[1:-1]
        notes.append(
            f"excluding partial first and last months, monthly volume ranges "
            f"from {min(interior)} to {max(interior)}"
        )
    return EvidenceItem(
        key="monthly_case_volume",
        label="Case volume by month",
        kind="timeseries",
        buckets=buckets,
        provenance=Provenance(
            unit="case",
            method="cases grouped by calendar month of report_date",
            source_columns=[SCHEMA["date"]],
            n_contributing=frame.n_cases,
            denominator=frame.n_cases,
            notes=notes,
        ),
    )


@analysis(
    "monthly_top_reactions",
    "Leading reaction by month",
    unit="reaction_event",
    defaults={"top_n": 3},
)
def monthly_top_reactions(frame: CaseFrame, top_n: int = 3) -> EvidenceItem:
    """Leading reaction terms per month, for observing shifts over time."""
    r = frame.reactions.copy()
    r["_m"] = pd.to_datetime(r["report_date"]).dt.to_period("M").astype(str)
    buckets: list[Bucket] = []
    for month in sorted(r["_m"].unique()):
        grp = r[r["_m"] == month]
        for b in _buckets_from(
            grp, "reaction_pt", denominator=len(grp), top_n=top_n
        ):
            buckets.append(
                Bucket(
                    label=f"{month} — {b.label}",
                    count=b.count,
                    pct=b.pct,
                    case_ids=b.case_ids,
                )
            )
    return EvidenceItem(
        key="monthly_top_reactions",
        label="Leading reaction by month",
        kind="timeseries",
        buckets=buckets,
        provenance=Provenance(
            unit="reaction_event",
            method=f"top {top_n} PTs within each calendar month",
            source_columns=[SCHEMA["reaction_pt"], SCHEMA["date"]],
            n_contributing=len(r),
            denominator=len(r),
            notes=[
                (
                    "monthly counts are small; ordering may shift on one or two "
                    "events and should not be read as a trend without review"
                ),
            ],
        ),
    )


# --------------------------------------------------------------------------
# regulatory content that must not be invented
# --------------------------------------------------------------------------


@analysis("safety_actions", "History of safety-related actions", unit="case")
def safety_actions(frame: CaseFrame) -> EvidenceItem:
    """Actions taken during the interval. None were supplied with this dataset.

    This analysis exists precisely because the answer is 'none'. A section with
    no evidence must still receive an explicit negative statement, otherwise the
    model is left with an empty packet and an instruction to write a section —
    the exact condition under which it invents content.
    """
    return EvidenceItem(
        key="safety_actions",
        label="History of safety-related actions",
        kind="statement",
        value="No safety-related action data was supplied with this dataset.",
        provenance=Provenance(
            unit="case",
            method="explicit negative: no action data source is configured",
            source_columns=[],
            n_contributing=0,
            denominator=0,
            notes=[
                (
                    "labelling changes, regulatory communications, safety studies and "
                    "risk-minimisation actions are all absent from the supplied data"
                ),
                (
                    "the absence must be stated in the report; actions must not be "
                    "inferred from case data"
                ),
            ],
        ),
    )


ISSUE_LABELS = {
    "superseded_versions_dropped": "rows removed as superseded report versions",
    "age_unit_unrecognised": "cases with an uninterpretable age unit",
    "age_unit_missing": "cases with an age but no unit",
    "age_implausible": "cases with an age outside 0-120 years",
    "age_unavailable": "cases with no usable age",
    "duplicate_flag_present": "cases carrying the duplicate flag, retained",
    "country_disagreement": "cases where the two country fields disagree",
    "outcome_misaligned": "reaction events with no aligned outcome",
    "outcome_broadcast": "rows with one outcome applied to several reactions",
    "sex_unavailable": "cases with no recorded sex",
    "date_unparsed": "rows with an unparseable report date",
    "multirow_not_version": "multi-row cases not explained by versioning",
}


@analysis("data_quality", "Data quality findings", unit="case")
def data_quality(frame: CaseFrame) -> EvidenceItem:
    """Validation findings from ingest, promoted to citable evidence.

    Labels are written for a reader, not for a log. An earlier version passed
    the raw issue codes through and the model dutifully wrote sentences like
    "the duplicate_flag_present warning was raised", because that was the only
    vocabulary it had. The model can only be as readable as its packet.
    """
    v = frame.validation
    buckets = [
        Bucket(
            label="rows in source file before deduplication",
            count=v.raw_rows,
            pct=None,
        ),
        Bucket(
            label="cases after collapsing superseded report versions",
            count=v.unique_cases,
            pct=None,
        ),
        Bucket(
            label="reaction events before deduplication",
            count=v.reaction_events_raw,
            pct=None,
        ),
        Bucket(
            label="reaction events after deduplication",
            count=v.reaction_events_deduped,
            pct=None,
        ),
    ]
    buckets += [
        Bucket(
            label=ISSUE_LABELS.get(i.code, i.code.replace("_", " ")),
            count=i.count,
            pct=_pct(i.count, frame.n_cases),
        )
        for i in v.issues
    ]
    return EvidenceItem(
        key="data_quality",
        label="Data quality findings",
        kind="distribution",
        buckets=buckets,
        provenance=Provenance(
            unit="case",
            method="issues recorded by the Phase 1 validator",
            source_columns=[],
            n_contributing=len(v.issues),
            denominator=frame.n_cases,
            notes=[
                f"source sha256 {v.source_sha256[:16]}",
                (
                    "cases carrying the duplicate flag are surfaced but not removed, "
                    "as the flag is undefined for this exercise"
                ),
                "expectedness is out of scope: no product label or CCDS supplied",
            ],
        ),
    )


# --------------------------------------------------------------------------
# case index
# --------------------------------------------------------------------------


@analysis("case_index", "Case index", unit="case", defaults={"limit": None})
def case_index(frame: CaseFrame, limit: int | None = None) -> EvidenceItem:
    """Per-case listing so aggregates can be traced to individual cases.

    Rendered deterministically. The model receives the table's shape, never its
    rows — summarising a listing is not a language task.
    """
    r = frame.reactions
    agg = (
        r.groupby(CASE_ID)
        .agg(
            reactions=("reaction_pt", lambda s: "; ".join(sorted(set(s)))),
            outcomes=("reaction_outcome", lambda s: "; ".join(sorted(set(s)))),
            n_reactions=("reaction_pt", "size"),
        )
        .reset_index()
    )
    meta = frame.cases[
        [
            CASE_ID,
            "report_date",
            SCHEMA["country"],
            SCHEMA["sex"],
            "age_band",
            SCHEMA["serious"],
            SCHEMA["expedite"],
        ]
    ]
    merged = meta.merge(agg, on=CASE_ID, how="left").sort_values("report_date")
    if limit:
        merged = merged.head(limit)

    rows = [
        {
            "case_id": int(rec[CASE_ID]),
            "report_date": str(pd.to_datetime(rec["report_date"]).date()),
            "country": rec[SCHEMA["country"]],
            "sex": rec[SCHEMA["sex"]],
            "age_band": rec["age_band"],
            "serious": rec[SCHEMA["serious"]],
            "expedited": rec[SCHEMA["expedite"]],
            "reactions": rec["reactions"] or "",
            "outcomes": rec["outcomes"] or "",
            "n_reactions": int(rec["n_reactions"]) if pd.notna(rec["n_reactions"]) else 0,
        }
        for rec in merged.to_dict("records")
    ]

    return EvidenceItem(
        key="case_index",
        label="Case index",
        kind="table",
        columns=[
            "case_id",
            "report_date",
            "country",
            "sex",
            "age_band",
            "serious",
            "expedited",
            "reactions",
            "outcomes",
            "n_reactions",
        ],
        rows=rows,
        provenance=Provenance(
            unit="case",
            method="deduped cases joined to their exploded reaction events",
            source_columns=[CASE_ID, SCHEMA["reaction_pt"], SCHEMA["date"]],
            n_contributing=len(rows),
            denominator=frame.n_cases,
            case_ids=[r_["case_id"] for r_ in rows],
            notes=["ordered by report date; every aggregate traces here"],
        ),
    )
