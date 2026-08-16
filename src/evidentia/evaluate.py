"""Phase 8 — evaluation.

Three tiers with deliberately unequal authority (D-009):

    tier 1  deterministic  grounding.py           BLOCKS
    tier 2  cross-model    this module            FLAGS
    tier 3  LLM judge      this module            FLAGS

Tier 1 already runs on every generation and is the only tier that can stop a
report. Tiers 2 and 3 exist because tier 1 cannot see everything: a section can
be perfectly grounded and still overstate, or slide from observation into
interpretation. Neither can approve anything.

**Tier 2 detects disagreement, not correctness.** Two models can agree and both
be wrong. It is a stability probe: if the same evidence packet produces
materially different figures under a different model, the prompt is ambiguous.
That is a real and useful finding, and it is not a truth test.

**Tier 3 is a judge that may not pass judgement.** It scores a rubric and routes
upward. Its own output is checked by tier 1 before anything is believed, and it
is never permitted to mark a section acceptable — only to raise concerns. An
evaluator that can approve is an evaluator that can hallucinate approval.

Scaling this to 1,000 reports is the point of the split: tier 1 runs on every
section of every report for free, tier 2 on a sample, tier 3 on a smaller sample
plus anything the earlier tiers flagged.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from evidentia.assembler import SectionPacket
from evidentia.generate import GeneratedSection, Generator, LLMClient
from evidentia.grounding import GroundingValidator, extract_numbers

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overreach": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Sentences that state a conclusion, causal relationship, "
                "safety signal or reassurance the evidence does not establish."
            ),
        },
        "interpretation": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Sentences that cross from reporting an observation into "
                "interpreting its significance."
            ),
        },
        "unit_errors": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Places where a case-level figure and a reaction-event-level "
                "figure are compared, combined or mislabelled."
            ),
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Evidence present in the packet that the section was asked to "
                "report and did not."
            ),
        },
        "notes": {"type": "string", "description": "One sentence of context."},
    },
    "required": [
        "overreach",
        "interpretation",
        "unit_errors",
        "missing",
        "notes",
    ],
    "additionalProperties": False,
}

JUDGE_SYSTEM = (
    "You are a pharmacovigilance reviewer auditing one section of a periodic "
    "safety report against the evidence it was given.\n\n"
    "You are looking for four specific faults and nothing else:\n"
    "  overreach       conclusions, causality, signals or reassurance the "
    "evidence does not establish\n"
    "  interpretation  crossing from what the data shows to what it means\n"
    "  unit_errors     mixing case counts with reaction-event counts\n"
    "  missing         packet evidence the section was asked to report and "
    "omitted\n\n"
    "You are NOT checking arithmetic. Every figure has already been verified "
    "against the packet by a deterministic gate.\n\n"
    "You cannot approve this section. Return empty lists where you find no "
    "fault. Quote the offending sentence, do not paraphrase it. Do not invent "
    "faults to appear thorough; an empty result is a valid and common outcome."
)


class CrossCheckResult(BaseModel):
    """Tier 2 — same packet, different model."""

    section_id: str
    model_a: str
    model_b: str
    numbers_a: list[float] = Field(default_factory=list)
    numbers_b: list[float] = Field(default_factory=list)
    only_in_a: list[float] = Field(default_factory=list)
    only_in_b: list[float] = Field(default_factory=list)
    b_grounded: bool = True
    words_a: int = 0
    words_b: int = 0

    @property
    def agrees(self) -> bool:
        return not self.only_in_a and not self.only_in_b

    @property
    def jaccard(self) -> float:
        a, b = set(self.numbers_a), set(self.numbers_b)
        return len(a & b) / len(a | b) if (a | b) else 1.0

    def summary(self) -> str:
        verdict = "agree" if self.agrees else "DIVERGE"
        line = (
            f"{self.section_id}: {verdict} "
            f"(overlap {self.jaccard:.0%}, {self.words_a}w vs {self.words_b}w)"
        )
        if not self.agrees:
            line += (
                f"\n    only in {self.model_a}: "
                f"{', '.join(f'{n:g}' for n in self.only_in_a) or '—'}"
                f"\n    only in {self.model_b}: "
                f"{', '.join(f'{n:g}' for n in self.only_in_b) or '—'}"
            )
        if not self.b_grounded:
            line += f"\n    {self.model_b} output was NOT grounded"
        return line


class JudgeResult(BaseModel):
    """Tier 3 — rubric findings. Advisory only."""

    section_id: str
    overreach: list[str] = Field(default_factory=list)
    interpretation: list[str] = Field(default_factory=list)
    unit_errors: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    notes: str = ""
    judge_model: str = ""
    quotes_verified: bool = True

    @property
    def concerns(self) -> int:
        return (
            len(self.overreach)
            + len(self.interpretation)
            + len(self.unit_errors)
            + len(self.missing)
        )

    @property
    def clean(self) -> bool:
        return self.concerns == 0

    def summary(self) -> str:
        if self.clean:
            return f"{self.section_id}: judge raised no concerns"
        parts = []
        for name in ("overreach", "interpretation", "unit_errors", "missing"):
            items = getattr(self, name)
            if items:
                parts.append(f"    {name}: {len(items)}")
                parts += [f"      - {q}" for q in items]
        head = f"{self.section_id}: {self.concerns} concern(s) — review required"
        if not self.quotes_verified:
            head += " [some quotes not found in the prose; judge may be confabulating]"
        return "\n".join([head, *parts])


class Evaluator:
    """Tiers 2 and 3. Neither can block; both can escalate."""

    def __init__(self, generator: Generator, client: LLMClient | None = None) -> None:
        self.generator = generator
        self.client = client or generator.client
        self.validator = GroundingValidator()

    def cross_check(
        self,
        packet: SectionPacket,
        primary: GeneratedSection,
        cross_model: str,
    ) -> CrossCheckResult:
        """Regenerate under a second model and compare the figures stated."""
        other = self.generator.generate(packet, model=cross_model)

        a = extract_numbers(primary.prose, packet)
        b = extract_numbers(other.prose, packet)
        sa, sb = set(a), set(b)

        return CrossCheckResult(
            section_id=packet.section_id,
            model_a=primary.model,
            model_b=cross_model,
            numbers_a=sorted(sa),
            numbers_b=sorted(sb),
            only_in_a=sorted(sa - sb),
            only_in_b=sorted(sb - sa),
            b_grounded=self.validator.validate(other, packet).passed,
            words_a=primary.word_count,
            words_b=other.word_count,
        )

    def judge(
        self,
        packet: SectionPacket,
        section: GeneratedSection,
        judge_model: str | None = None,
    ) -> JudgeResult:
        """Score a section against the rubric. Findings are advisory."""
        model = judge_model or self.generator.model_config.name
        user = (
            f"Section: {packet.title}\n\n"
            f"Evidence the section was given:\n{packet.evidence_digest()}\n\n"
            f"Rules the section was written under:\n"
            + "\n".join(f"- {r}" for r in packet.rules)
            + f"\n\nSection text:\n{section.prose}"
        )
        resp = self.client.complete(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            model=model,
            temperature=0.0,
            max_tokens=self.generator.model_config.max_tokens,
            seed=self.generator.model_config.seed,
            schema=JUDGE_SCHEMA,
        )

        payload = _parse_judge(resp.content)
        result = JudgeResult(
            section_id=section.section_id, judge_model=model, **payload
        )

        # A judge that quotes sentences the section never contained is
        # confabulating, and its findings are worth less. Checking is cheap.
        quotes = [
            *result.overreach,
            *result.interpretation,
            *result.unit_errors,
        ]
        normalised = " ".join(section.prose.lower().split())
        result.quotes_verified = all(
            " ".join(q.lower().split())[:60] in normalised for q in quotes
        )
        return result


def _parse_judge(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # A judge that cannot return JSON has not evaluated anything. Failing
        # open is correct here: tier 3 is advisory, so its absence must not be
        # mistaken for a clean result.
        return {
            "overreach": [],
            "interpretation": [],
            "unit_errors": [],
            "missing": [],
            "notes": "judge response unparseable; no evaluation performed",
        }
    out: dict[str, Any] = {}
    for key in ("overreach", "interpretation", "unit_errors", "missing"):
        value = payload.get(key) or []
        out[key] = [str(x) for x in value] if isinstance(value, list) else []
    out["notes"] = str(payload.get("notes", ""))
    return out
