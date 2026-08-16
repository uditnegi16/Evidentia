"""Frozen contracts emitted by Phase 1 (ingest).

Everything downstream consumes CaseFrame and never touches the source file.
This module deliberately contains no report-type knowledge — it describes the
shape of *safety data*, not the shape of a PADER.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "error"]


class ValidationIssue(BaseModel):
    """A single data-quality finding. Surfaced, never silently swallowed."""

    code: str
    severity: Severity
    message: str
    count: int = 0

    def __str__(self) -> str:
        return f"[{self.severity.upper():7s}] {self.code:28s} n={self.count:<5d} {self.message}"


class ValidationReport(BaseModel):
    """Everything Phase 1 learned about the data, including what it could not fix.

    This object is part of the evidence chain: figures quoted in the report about
    data completeness trace back to here, not to the LLM.
    """

    source_file: str
    source_sha256: str
    raw_rows: int
    unique_cases: int
    rows_dropped_as_superseded: int
    period_start: date
    period_end: date
    reaction_events_raw: int
    reaction_events_deduped: int
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        lines = [
            f"source            {self.source_file}",
            f"sha256            {self.source_sha256[:16]}...",
            f"raw rows          {self.raw_rows}",
            f"unique cases      {self.unique_cases}",
            f"superseded rows   {self.rows_dropped_as_superseded}",
            f"period            {self.period_start} -> {self.period_end}",
            f"reaction events   {self.reaction_events_raw} raw / "
            f"{self.reaction_events_deduped} deduped",
            "",
            "issues:",
        ]
        lines += [f"  {i}" for i in self.issues] or ["  (none)"]
        return "\n".join(lines)


@dataclass(frozen=True)
class CaseFrame:
    """The Phase 1 -> Phase 2 boundary.

    cases      one row per case, deduped to the latest report version (1,024 rows)
    reactions  one row per reaction *event*, comma-split and exploded (~3,600 rows)
    validation what was found, fixed, and quarantined along the way

    Two units of count exist and are never interchangeable. Every analysis in
    Phase 2 must declare which frame it reads.
    """

    cases: pd.DataFrame
    reactions: pd.DataFrame
    validation: ValidationReport

    @property
    def n_cases(self) -> int:
        return len(self.cases)

    @property
    def n_reaction_events(self) -> int:
        return len(self.reactions)
