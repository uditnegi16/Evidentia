"""Phase 2 contracts — the evidence layer.

An EvidenceItem is a single computed fact plus everything needed to defend it:
what it measures, how it was derived, which source columns it read, and exactly
which cases contributed.

The central design decision (D-018) lives here. Every item carries full case-ID
provenance, and `to_prompt_dict()` is a *projection* that strips it. The audit
record and the model's payload are deliberately different objects sharing one
source of truth:

    EvidenceItem  ──► to_prompt_dict()  ──►  LLM        (lean, no IDs)
                  └─► model_dump()      ──►  audit,     (complete)
                                             validator,
                                             review UI

Provenance can only be captured at compute time. Once an analysis returns
"81 cases" without recording which 81, that information no longer exists.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

CountUnit = Literal["case", "reaction_event"]
EvidenceKind = Literal["scalar", "distribution", "timeseries", "table", "statement"]

_LABEL_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in(label: str) -> set[float]:
    """Numbers embedded in a category label.

    "65-74" yields 65 and 74; "2025-03" yields 2025 and 3. These are shown to
    the model as part of the packet, so quoting them is grounding, not
    fabrication.
    """
    return {float(m.group()) for m in _LABEL_NUMBER.finditer(label or "")}


class Bucket(BaseModel):
    """One category within a distribution or timeseries.

    Carries its own case IDs so a reviewer can trace a single bar of a chart,
    not just the total.
    """

    label: str
    count: int
    pct: float | None = None
    case_ids: list[int] = Field(default_factory=list)

    def to_prompt_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"label": self.label, "count": self.count}
        if self.pct is not None:
            d["pct"] = self.pct
        return d


class Provenance(BaseModel):
    """How an item was derived. Answers 'why should I believe this number?'"""

    unit: CountUnit
    method: str
    source_columns: list[str]
    n_contributing: int
    denominator: int | None = None
    case_ids: list[int] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def to_prompt_dict(self) -> dict[str, Any]:
        """Unit and denominator go to the model; IDs never do.

        The unit matters: without it a model can compare a case-level figure to
        a reaction-level one and produce a sentence that is arithmetically
        incoherent while reading fluently.
        """
        d: dict[str, Any] = {"unit": self.unit}
        # How many records this figure was computed from. Withholding it is
        # what pushed a model to reconstruct it as denominator minus unknown,
        # which the gate then correctly blocked as arithmetic (E-015).
        d["n_contributing"] = self.n_contributing
        if self.denominator is not None:
            d["denominator"] = self.denominator
        if self.notes:
            d["notes"] = self.notes
        return d


class EvidenceItem(BaseModel):
    """One computed fact. The atomic unit of everything the report may assert."""

    key: str
    label: str
    kind: EvidenceKind
    value: float | int | str | None = None
    buckets: list[Bucket] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    provenance: Provenance
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )

    def to_prompt_dict(self, max_buckets: int | None = None) -> dict[str, Any]:
        """The projection sent to the model.

        Strips case IDs, timestamps, and — for tables — the row payload, which
        would swamp the context for no benefit. A table's *shape* is all the
        model needs; the table itself is rendered deterministically.
        """
        d: dict[str, Any] = {"label": self.label, "kind": self.kind}

        if self.value is not None:
            d["value"] = self.value

        if self.buckets:
            shown = (
                self.buckets if max_buckets is None else self.buckets[:max_buckets]
            )
            d["buckets"] = [b.to_prompt_dict() for b in shown]
            if len(shown) < len(self.buckets):
                d["buckets_omitted"] = len(self.buckets) - len(shown)

        if self.kind == "table":
            d["row_count"] = len(self.rows)
            d["columns"] = self.columns
            d["note"] = "rendered deterministically; not summarised by the model"

        d["provenance"] = self.provenance.to_prompt_dict()
        return d

    def numeric_claims(self) -> set[float]:
        """Every number a section may legitimately state, given this item.

        Phase 5's grounding gate checks generated prose against the union of
        these across the section's packet. A number outside that set was
        invented.

        Labels count. A bucket called "65-74" puts 65 and 74 in front of the
        model, so a sentence saying "patients aged 65 and over" is quoting the
        packet, not calculating. Harvesting labels is what keeps the gate
        pointed at fabrication rather than at vocabulary.
        """
        out: set[float] = set()
        if isinstance(self.value, (int, float)):
            out.add(float(self.value))
        for b in self.buckets:
            out.add(float(b.count))
            if b.pct is not None:
                out.add(round(float(b.pct), 1))
            out |= _numbers_in(b.label)
        if self.provenance.denominator is not None:
            out.add(float(self.provenance.denominator))
        out.add(float(self.provenance.n_contributing))
        if self.kind == "table":
            out.add(float(len(self.rows)))
        return out


class EvidenceStore(BaseModel):
    """All evidence computed for one run, keyed by analysis name."""

    items: dict[str, EvidenceItem] = Field(default_factory=dict)
    period_start: date | None = None
    period_end: date | None = None
    source_sha256: str | None = None

    def add(self, item: EvidenceItem) -> None:
        if item.key in self.items:
            raise ValueError(f"duplicate evidence key: {item.key}")
        self.items[item.key] = item

    def get(self, key: str) -> EvidenceItem:
        if key not in self.items:
            raise KeyError(
                f"evidence '{key}' not computed. available: "
                f"{sorted(self.items)}"
            )
        return self.items[key]

    def subset(self, keys: list[str]) -> dict[str, EvidenceItem]:
        """The scoping operation Phase 3 depends on.

        A section receives exactly the evidence it declared and nothing else.
        Missing keys raise rather than silently yielding a thinner packet — a
        section quietly generated without its evidence is the failure this
        whole architecture exists to prevent.
        """
        return {k: self.get(k) for k in keys}

    def __len__(self) -> int:
        return len(self.items)

    def __contains__(self, key: object) -> bool:
        return key in self.items
