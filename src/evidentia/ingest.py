"""Phase 1 — ingest, validate, dedupe.

Turns a raw ICSR line listing into a CaseFrame. Every transformation here is
deterministic and every data-quality decision is recorded as a ValidationIssue
rather than applied silently.

Design notes (see docs/DECISIONS.md):
  D-011  reaction fields are comma-joined multi-value strings; split and explode
  D-012  multi-row cases are report versions; keep the highest version
  D-013  receivedate is int YYYYMMDD; report_date is the pre-parsed equivalent
  D-014  age must be normalised to years via the unit column
  D-015  primarysourcecountry is the geographic field of record
  D-016  the duplicate flag is surfaced, not acted on
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from evidentia.contracts import CaseFrame, ValidationIssue, ValidationReport

# --------------------------------------------------------------------------
# Dataset schema.
#
# These are column names of an E2B/FAERS-style line listing, not PADER concepts.
# They live here rather than in report config because they describe the *source*,
# and a second report type over the same source reuses them unchanged.
# --------------------------------------------------------------------------

SCHEMA = {
    "case_id": "safetyreportid",
    "version": "safetyreportversion",
    "date": "report_date",
    "date_raw": "receivedate",
    "country": "primarysourcecountry",
    "country_alt": "occurcountry",
    "sex": "patient_patientsex",
    "age": "patient_patientonsetage",
    "age_unit": "patient_patientonsetageunit",
    "reaction_pt": "patient_reaction_reactionmeddrapt",
    "reaction_outcome": "patient_reaction_reactionoutcome",
    "serious": "serious",
    "expedite": "fulfillexpeditecriteria",
    "reporter": "primarysource_qualification",
    "duplicate": "duplicate",
    "indication": "patient_drug_drugindication",
}

SERIOUSNESS_FLAGS = [
    "seriousnessdeath",
    "seriousnesslifethreatening",
    "seriousnesshospitalization",
    "seriousnessdisabling",
    "seriousnesscongenitalanomali",
    "seriousnessother",
]

# Age unit -> multiplier to years. Anything not listed is quarantined, not guessed.
UNIT_TO_YEARS = {
    "year": 1.0,
    "years": 1.0,
    "month": 1.0 / 12.0,
    "months": 1.0 / 12.0,
    "week": 1.0 / 52.1775,
    "weeks": 1.0 / 52.1775,
    "day": 1.0 / 365.25,
    "days": 1.0 / 365.25,
    "hour": 1.0 / 8766.0,
    "hours": 1.0 / 8766.0,
}

AGE_BANDS = [
    (0.0, 18.0, "<18"),
    (18.0, 45.0, "18-44"),
    (45.0, 65.0, "45-64"),
    (65.0, 75.0, "65-74"),
    (75.0, 85.0, "75-84"),
    (85.0, 200.0, "85+"),
]

UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_raw(path: Path) -> pd.DataFrame:
    """Dispatch on extension. The brief described a CSV; an XLSX was supplied."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path)
    if suffix == ".tsv":
        return pd.read_csv(path, sep="\t")
    raise ValueError(f"Unsupported input format: {suffix}")


def _require_columns(df: pd.DataFrame, issues: list[ValidationIssue]) -> None:
    required = set(SCHEMA.values()) | set(SERIOUSNESS_FLAGS)
    missing = sorted(required - set(df.columns))
    if missing:
        issues.append(
            ValidationIssue(
                code="missing_columns",
                severity="error",
                message=f"expected columns absent: {', '.join(missing)}",
                count=len(missing),
            )
        )


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def parse_report_date(df: pd.DataFrame) -> pd.Series:
    """Parse the report date, preferring the pre-parsed column.

    Trap (E-002): receivedate is int64 YYYYMMDD. pd.to_datetime on an integer
    series interprets it as nanoseconds since epoch and returns 1970 dates with
    no error. It must be cast to string with an explicit format.
    """
    if SCHEMA["date"] in df.columns:
        parsed = pd.to_datetime(df[SCHEMA["date"]], errors="coerce")
        if parsed.notna().any():
            return parsed.dt.normalize()
    raw = df[SCHEMA["date_raw"]].astype("Int64").astype(str)
    return pd.to_datetime(raw, format="%Y%m%d", errors="coerce")


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------


def _dedupe_versions(
    df: pd.DataFrame, issues: list[ValidationIssue]
) -> tuple[pd.DataFrame, int]:
    """Keep the highest safetyreportversion per case (D-012).

    Profiling showed all 41 multi-row cases differ by version and none by date,
    so extra rows are superseded revisions, not additional reactions.
    """
    cid, ver = SCHEMA["case_id"], SCHEMA["version"]

    multi = df[df.duplicated(cid, keep=False)]
    if not multi.empty:
        by_version = multi.groupby(cid)[ver].nunique().gt(1).sum()
        n_multi = multi[cid].nunique()
        if by_version < n_multi:
            issues.append(
                ValidationIssue(
                    code="multirow_not_version",
                    severity="warning",
                    message=(
                        f"{n_multi - by_version} multi-row cases do not differ by "
                        "version; version-dedup assumption may not hold for them"
                    ),
                    count=int(n_multi - by_version),
                )
            )

    ordered = df.sort_values([cid, ver], ascending=[True, True], kind="mergesort")
    deduped = ordered.drop_duplicates(subset=cid, keep="last").reset_index(drop=True)

    dropped = len(df) - len(deduped)
    if dropped:
        issues.append(
            ValidationIssue(
                code="superseded_versions_dropped",
                severity="info",
                message="rows dropped as superseded report versions",
                count=dropped,
            )
        )
    return deduped, dropped


# --------------------------------------------------------------------------
# Age
# --------------------------------------------------------------------------


def _normalise_age(
    df: pd.DataFrame, issues: list[ValidationIssue]
) -> tuple[pd.Series, pd.Series]:
    """Convert onset age to years using the unit column, then band it (D-014).

    The Starter Guide suggests bucketing the numeric column directly. Profiling
    found units of month, week and day plus a corrupt value, so a raw minimum of
    1.0 is one month, not one year. Deviating from the guide is deliberate.
    """
    raw = pd.to_numeric(df[SCHEMA["age"]], errors="coerce")
    unit = df[SCHEMA["age_unit"]].astype("string").str.strip().str.lower()

    known = unit.isin(UNIT_TO_YEARS)
    bad_unit = unit.notna() & ~known
    if bad_unit.any():
        offenders = sorted(unit[bad_unit].dropna().unique())[:5]
        issues.append(
            ValidationIssue(
                code="age_unit_unrecognised",
                severity="warning",
                message=f"age quarantined, unit not interpretable: {offenders}",
                count=int(bad_unit.sum()),
            )
        )

    multiplier = unit.map(UNIT_TO_YEARS).astype("float64")
    years = (raw * multiplier).where(known & raw.notna())

    missing_unit = raw.notna() & unit.isna()
    if missing_unit.any():
        issues.append(
            ValidationIssue(
                code="age_unit_missing",
                severity="warning",
                message="age present but unit absent; not converted",
                count=int(missing_unit.sum()),
            )
        )

    implausible = years.notna() & ((years < 0) | (years > 120))
    if implausible.any():
        issues.append(
            ValidationIssue(
                code="age_implausible",
                severity="warning",
                message="age outside 0-120 years after conversion; quarantined",
                count=int(implausible.sum()),
            )
        )
        years = years.where(~implausible)

    missing = int(years.isna().sum())
    if missing:
        issues.append(
            ValidationIssue(
                code="age_unavailable",
                severity="info",
                message="cases with no usable age; reported as unknown",
                count=missing,
            )
        )

    return years, years.map(band_age)


def band_age(years: float | None) -> str:
    if years is None or pd.isna(years):
        return UNKNOWN
    for lo, hi, label in AGE_BANDS:
        if lo <= years < hi:
            return label
    return UNKNOWN


# --------------------------------------------------------------------------
# Reactions
# --------------------------------------------------------------------------


def _split(cell: object) -> list[str]:
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    return [p.strip() for p in str(cell).split(",") if p.strip()]


def _explode_reactions(
    df: pd.DataFrame, issues: list[ValidationIssue]
) -> pd.DataFrame:
    """One row per reaction event, with outcome aligned positionally (D-011).

    Reaction and outcome lists align in 99.4% of rows. Alignment rules:
      equal length      zip positionally
      one outcome       broadcast to every reaction
      otherwise         outcome recorded as unknown, row flagged
    """
    cid = SCHEMA["case_id"]
    records: list[dict] = []
    n_broadcast = n_misaligned = 0

    carry = [
        SCHEMA["country"],
        SCHEMA["sex"],
        SCHEMA["serious"],
        SCHEMA["expedite"],
        SCHEMA["reporter"],
        "report_date",
        "age_years",
        "age_band",
    ]

    for row in df.itertuples(index=False):
        d = row._asdict()
        pts = _split(d.get(SCHEMA["reaction_pt"]))
        outs = _split(d.get(SCHEMA["reaction_outcome"]))

        if not pts:
            continue

        if len(outs) == len(pts):
            aligned = outs
        elif len(outs) == 1:
            aligned = outs * len(pts)
            n_broadcast += 1
        else:
            aligned = [UNKNOWN] * len(pts)
            n_misaligned += 1

        base = {k: d.get(k) for k in carry if k in d}
        base[cid] = d[cid]
        for pos, (pt, outcome) in enumerate(zip(pts, aligned)):
            records.append(
                {
                    **base,
                    "reaction_pt": pt,
                    "reaction_outcome": outcome or UNKNOWN,
                    "reaction_index": pos,
                    "outcome_aligned": len(outs) == len(pts),
                }
            )

    if n_broadcast:
        issues.append(
            ValidationIssue(
                code="outcome_broadcast",
                severity="info",
                message="single outcome broadcast across multiple reactions",
                count=n_broadcast,
            )
        )
    if n_misaligned:
        issues.append(
            ValidationIssue(
                code="outcome_misaligned",
                severity="warning",
                message="reaction and outcome counts differ; outcome set to unknown",
                count=n_misaligned,
            )
        )

    return pd.DataFrame.from_records(records)


# --------------------------------------------------------------------------
# Other quality checks
# --------------------------------------------------------------------------


def _quality_checks(df: pd.DataFrame, issues: list[ValidationIssue]) -> None:
    dup_col = SCHEMA["duplicate"]
    if dup_col in df.columns:
        flagged = int(df[dup_col].notna().sum())
        if flagged:
            issues.append(
                ValidationIssue(
                    code="duplicate_flag_present",
                    severity="warning",
                    message=(
                        "cases carry the duplicate flag; surfaced but NOT removed, "
                        "as the flag is undefined for this exercise (D-016)"
                    ),
                    count=flagged,
                )
            )

    a, b = SCHEMA["country"], SCHEMA["country_alt"]
    if a in df.columns and b in df.columns:
        disagree = int((df[a].fillna("") != df[b].fillna("")).sum())
        if disagree:
            issues.append(
                ValidationIssue(
                    code="country_disagreement",
                    severity="info",
                    message=f"{a} differs from {b}; {a} used per D-015",
                    count=disagree,
                )
            )

    sex_missing = int(df[SCHEMA["sex"]].isna().sum())
    if sex_missing:
        issues.append(
            ValidationIssue(
                code="sex_unavailable",
                severity="info",
                message="cases with no recorded sex; reported as unknown",
                count=sex_missing,
            )
        )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def load_cases(path: str | Path) -> CaseFrame:
    """Load a raw ICSR line listing into the frozen Phase 1 contract."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")

    issues: list[ValidationIssue] = []
    raw = _load_raw(path)
    _require_columns(raw, issues)
    if any(i.severity == "error" for i in issues):
        raise ValueError("\n".join(str(i) for i in issues))

    raw = raw.copy()
    raw["report_date"] = parse_report_date(raw)

    unparsed = int(raw["report_date"].isna().sum())
    if unparsed:
        issues.append(
            ValidationIssue(
                code="date_unparsed",
                severity="error",
                message="rows with an unparseable report date",
                count=unparsed,
            )
        )

    reaction_events_raw = int(
        raw[SCHEMA["reaction_pt"]].map(lambda c: len(_split(c))).sum()
    )

    cases, dropped = _dedupe_versions(raw, issues)
    cases["age_years"], cases["age_band"] = _normalise_age(cases, issues)
    cases[SCHEMA["sex"]] = cases[SCHEMA["sex"]].fillna(UNKNOWN)
    cases[SCHEMA["country"]] = cases[SCHEMA["country"]].fillna(UNKNOWN)

    _quality_checks(cases, issues)
    reactions = _explode_reactions(cases, issues)

    report = ValidationReport(
        source_file=path.name,
        source_sha256=_sha256(path),
        raw_rows=len(raw),
        unique_cases=len(cases),
        rows_dropped_as_superseded=dropped,
        period_start=cases["report_date"].min().date(),
        period_end=cases["report_date"].max().date(),
        reaction_events_raw=reaction_events_raw,
        reaction_events_deduped=len(reactions),
        issues=issues,
    )

    if not report.ok:
        raise ValueError("ingest failed:\n" + report.summary())

    return CaseFrame(cases=cases, reactions=reactions, validation=report)


if __name__ == "__main__":
    import sys

    frame = load_cases(sys.argv[1])
    print(frame.validation.summary())
