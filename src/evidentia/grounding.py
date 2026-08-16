"""Phase 5 — grounding.

The gate. Every number in a generated section is extracted and checked against
the set of figures its packet made available. A number outside that set was
invented, and the section does not pass.

Authority is asymmetric by design (D-009):

    blocking   deterministic, mechanical, cannot be argued with. The report
               will not render. Ungrounded numbers, forbidden phrases,
               evidence the section never declared.
    review     needs a human. Model-raised flags, length overruns, sections
               that quote no evidence at all.

Nothing here calls an LLM. A check that can hallucinate is not a gate.

The hard part is knowing what counts as a claim. Dates, age-band labels like
"65-74" and ordinals are not numeric claims, and treating them as such produces
false positives that train a reviewer to ignore the gate — which is worse than
having no gate. Those are masked before extraction, and every mask is narrow
and explicit.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from evidentia.assembler import SectionPacket
from evidentia.generate import GeneratedSection

Severity = Literal["blocking", "review"]

# ISO dates and long-form dates are period statements, not counts.
_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{4}-\d{2}\b"),
]

# 1,024 | 99.9 | 80 — longest alternative first so thousands separators win.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+")

# Regulatory boilerplate that is not a data claim.
_CITATION = re.compile(r"\b21\s*CFR\s*[\d.]+(?:\([a-z0-9]+\))*", re.IGNORECASE)
_ORDINAL = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE)

# Domain terminology of the form "15-day Alert", "30-day follow-up". The number
# names a regulatory interval, not a count of anything in the dataset.
_INTERVAL_TERM = re.compile(
    r"\b\d+[-\s](?:day|days|hour|hours|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)

# Models emit typographic dashes: en dash, em dash, non-breaking hyphen, minus.
# An age band stored as "45-64" then appears as "45\u201364" and a literal
# string comparison misses it, so 45 and 64 surface as fabrications. This was a
# real false positive on live output (E-014).
_DASHES = dict.fromkeys(
    [0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212], "-"
)


def normalise_dashes(text: str) -> str:
    return text.translate(_DASHES)


# Markers that flip a forbidden phrase from an assertion into a denial.
# "no safety concerns were identified" must block; "reporting rates cannot be
# calculated" must not, because the config explicitly instructs that sentence.
# A naive substring match cannot tell them apart (E-016).
#
# Two hard-won constraints on this list:
#
#   1. The matched phrase is excluded from the window. "no safety concerns"
#      begins with "no", so including it let the most important forbidden
#      phrase in the system excuse itself.
#   2. Bare "no", "none" and "never" are absent. They are too common in
#      regulatory prose to distinguish a denial of *this* claim from a denial
#      of something nearby. Only explicit constructions count.
_NEGATION = (
    "cannot",
    "can not",
    "could not",
    "couldn't",
    "was not",
    "were not",
    "is not",
    "are not",
    "not be",
    "unable",
    "without",
    "absence",
    "absent",
    "unavailable",
    "not available",
    "not supplied",
    "not provided",
    "not possible",
    "not performed",
    "not established",
    "not assessed",
    "not calculated",
    "insufficient",
    "precludes",
    "prevents",
    "lack of",
    "would require",
)

_BEFORE_WINDOW = 45
_AFTER_WINDOW = 70


def _is_negated(text: str, start: int, end: int) -> bool:
    """Whether a forbidden phrase sits inside a denial.

    The matched phrase itself is excluded, and the windows are deliberately
    tight. A wide window would let a genuine claim escape because an unrelated
    negation appeared elsewhere in the paragraph.
    """
    before = text[max(0, start - _BEFORE_WINDOW) : start]
    after = text[end : end + _AFTER_WINDOW]
    return any(m in before or m in after for m in _NEGATION)


class GroundingIssue(BaseModel):
    code: str
    severity: Severity
    detail: str

    def __str__(self) -> str:
        return f"[{self.severity:8s}] {self.code:22s} {self.detail}"


class GroundingResult(BaseModel):
    """Verdict on one generated section."""

    section_id: str
    issues: list[GroundingIssue] = Field(default_factory=list)
    numbers_found: list[float] = Field(default_factory=list)
    numbers_ungrounded: list[float] = Field(default_factory=list)
    word_count: int = 0

    @property
    def blocking(self) -> list[GroundingIssue]:
        return [i for i in self.issues if i.severity == "blocking"]

    @property
    def passed(self) -> bool:
        """No blocking issue. Review issues do not block; they escalate."""
        return not self.blocking

    @property
    def needs_review(self) -> bool:
        return bool(self.issues)

    @property
    def grounded_count(self) -> int:
        return len(self.numbers_found) - len(self.numbers_ungrounded)

    def summary(self) -> str:
        head = (
            f"{self.section_id}: {'PASS' if self.passed else 'BLOCKED'} "
            f"({self.grounded_count}/{len(self.numbers_found)} numbers grounded, "
            f"{self.word_count} words)"
        )
        return "\n".join([head, *(f"  {i}" for i in self.issues)])


def _mask(text: str, packet: SectionPacket) -> str:
    """Remove spans that look numeric but are not numeric claims.

    Every mask is narrow. A broad mask hides real fabrications, so anything not
    listed here stays in and must be grounded.
    """
    text = normalise_dashes(text)

    for pattern in (*_DATE_PATTERNS, _CITATION, _ORDINAL, _INTERVAL_TERM):
        text = pattern.sub(" ", text)

    # Category labels from this packet's own evidence — "65-74", "85+", "<18".
    # These are quoted from the packet, so they are grounded by construction.
    labels: list[str] = []
    for item in packet.evidence.values():
        for bucket in item.get("buckets", []) or []:
            label = normalise_dashes(str(bucket.get("label", "")))
            if label and any(ch.isdigit() for ch in label):
                labels.append(label)
    for label in sorted(labels, key=len, reverse=True):
        text = text.replace(label, " ")

    return text


def extract_numbers(text: str, packet: SectionPacket) -> list[float]:
    """Every numeric claim the prose makes."""
    out: list[float] = []
    for match in _NUMBER.finditer(_mask(text, packet)):
        try:
            out.append(float(match.group().replace(",", "")))
        except ValueError:  # pragma: no cover - regex guarantees parseability
            continue
    return out


class GroundingValidator:
    """Checks generated sections against their packets."""

    def __init__(self, *, tolerance: float = 0.051) -> None:
        # Percentages are rounded to one decimal upstream, so a model quoting
        # 99.9 against a stored 99.90 must not be flagged. Tolerance covers
        # rounding only — it is far too tight to admit a fabricated figure.
        self.tolerance = tolerance

    def _is_grounded(self, value: float, allowed: list[float]) -> bool:
        return any(abs(value - a) <= self.tolerance for a in allowed)

    def validate(
        self, section: GeneratedSection, packet: SectionPacket
    ) -> GroundingResult:
        issues: list[GroundingIssue] = []
        prose = section.prose

        found = extract_numbers(prose, packet)
        ungrounded = [
            v for v in found if not self._is_grounded(v, packet.allowed_numbers)
        ]
        for value in sorted(set(ungrounded)):
            issues.append(
                GroundingIssue(
                    code="ungrounded_number",
                    severity="blocking",
                    detail=(
                        f"{value:g} does not appear in this section's evidence "
                        f"packet ({', '.join(packet.evidence_keys)})"
                    ),
                )
            )

        lowered = prose.lower()
        for phrase in packet.forbidden_phrases:
            needle = phrase.lower()
            start = lowered.find(needle)
            while start != -1:
                end = start + len(needle)
                if _is_negated(lowered, start, end):
                    # Downgraded, never silently permitted. The section may have
                    # been instructed to deny this exact thing, but a reviewer
                    # still sees that the phrase appeared.
                    issues.append(
                        GroundingIssue(
                            code="forbidden_phrase_negated",
                            severity="review",
                            detail=(
                                f"{phrase!r} appears in a negated context: "
                                f"...{prose[max(0, start - 40) : end + 40].strip()}..."
                            ),
                        )
                    )
                else:
                    issues.append(
                        GroundingIssue(
                            code="forbidden_phrase",
                            severity="blocking",
                            detail=f"prose contains {phrase!r}",
                        )
                    )
                    break
                start = lowered.find(needle, end)

        undeclared = [
            key for key in section.evidence_used if key not in packet.evidence_keys
        ]
        for key in undeclared:
            issues.append(
                GroundingIssue(
                    code="undeclared_evidence",
                    severity="blocking",
                    detail=(
                        f"claims to use {key!r}, which this section never declared"
                    ),
                )
            )

        if not section.evidence_used:
            issues.append(
                GroundingIssue(
                    code="no_evidence_cited",
                    severity="review",
                    detail="section cites no evidence keys",
                )
            )

        for flag in section.flags:
            issues.append(
                GroundingIssue(
                    code="model_flag",
                    severity="review",
                    detail=f"model reported a gap: {flag}",
                )
            )

        words = section.word_count
        if words > packet.max_words:
            issues.append(
                GroundingIssue(
                    code="over_length",
                    severity="review",
                    detail=f"{words} words against a limit of {packet.max_words}",
                )
            )

        if section.output_mode != "strict":
            issues.append(
                GroundingIssue(
                    code="degraded_output_mode",
                    severity="review",
                    detail=(
                        f"generated under {section.output_mode!r} rather than "
                        "strict constrained decoding"
                    ),
                )
            )

        return GroundingResult(
            section_id=section.section_id,
            issues=issues,
            numbers_found=found,
            numbers_ungrounded=ungrounded,
            word_count=words,
        )

    def validate_all(
        self,
        sections: list[GeneratedSection],
        packets: dict[str, SectionPacket],
    ) -> dict[str, GroundingResult]:
        return {
            s.section_id: self.validate(s, packets[s.section_id]) for s in sections
        }


def report_is_renderable(results: dict[str, GroundingResult]) -> bool:
    """A report renders only if no section is blocked.

    One fabricated number invalidates the document, not just its paragraph.
    """
    return all(r.passed for r in results.values())
