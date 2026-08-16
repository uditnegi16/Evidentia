"""Phase 8 tests — the evaluation tiers.

The property under test is authority, not accuracy: tier 2 and tier 3 must be
able to raise concerns and must never be able to approve anything.
"""

from __future__ import annotations

import json
from typing import Any

from evidentia.assembler import SectionPacket
from evidentia.config import ModelConfig
from evidentia.evaluate import JUDGE_SCHEMA, Evaluator, _parse_judge
from evidentia.generate import GeneratedSection, Generator, LLMResponse

PROSE_A = "A total of 1024 cases were received, of which 1023 were serious."
PROSE_B = "During the interval 1024 reports were received; 1023 were serious."
PROSE_C = "A total of 1024 cases were received across 12 countries."


class ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages: list[dict[str, str]], **kw: Any) -> LLMResponse:
        self.calls.append({"messages": messages, **kw})
        return LLMResponse(content=self.responses.pop(0), model=kw["model"])


def wrap(prose: str) -> str:
    return json.dumps({"prose": prose, "evidence_used": [], "flags": []})


def packet() -> SectionPacket:
    return SectionPacket(
        section_id="narrative_summary",
        title="Narrative Summary",
        mode="generated",
        report_type="PADER",
        product_name="Bisoprolol",
        evidence={"total_cases": {"label": "Total cases", "value": 1024}},
        instructions="Summarise.",
        rules=["state observations, not conclusions"],
        forbidden_phrases=["no safety concerns"],
        max_words=320,
        allowed_numbers=[1023.0, 1024.0],
        evidence_keys=["total_cases"],
    )


def section(prose: str = PROSE_A) -> GeneratedSection:
    return GeneratedSection(
        section_id="narrative_summary",
        title="Narrative Summary",
        prose=prose,
        evidence_used=["total_cases"],
        model="openai/gpt-oss-120b",
        output_mode="strict",
    )


def evaluator(responses: list[str]) -> tuple[Evaluator, ScriptedClient]:
    client = ScriptedClient(responses)
    return Evaluator(Generator(ModelConfig(), client=client), client), client


# --------------------------------------------------------------------------
# Tier 2 — cross-model
# --------------------------------------------------------------------------


def test_agreement_when_both_models_state_the_same_figures():
    ev, _ = evaluator([wrap(PROSE_B)])
    r = ev.cross_check(packet(), section(PROSE_A), "llama-3.3-70b-versatile")
    assert r.agrees
    assert r.jaccard == 1.0


def test_divergence_is_reported_with_the_offending_figures():
    ev, _ = evaluator([wrap(PROSE_C)])
    r = ev.cross_check(packet(), section(PROSE_A), "llama-3.3-70b-versatile")
    assert not r.agrees
    assert 1023.0 in r.only_in_a
    assert 12.0 in r.only_in_b
    assert "DIVERGE" in r.summary()


def test_cross_check_uses_the_second_model():
    ev, client = evaluator([wrap(PROSE_B)])
    ev.cross_check(packet(), section(), "llama-3.3-70b-versatile")
    assert client.calls[0]["model"] == "llama-3.3-70b-versatile"


def test_cross_check_reports_when_the_second_model_is_ungrounded():
    ev, _ = evaluator([wrap("A total of 7777 cases were received.")])
    r = ev.cross_check(packet(), section(), "llama-3.3-70b-versatile")
    assert not r.b_grounded
    assert "NOT grounded" in r.summary()


def test_cross_check_cannot_block():
    """Tier 2 has no blocking verdict at all — by construction."""
    ev, _ = evaluator([wrap(PROSE_C)])
    r = ev.cross_check(packet(), section(), "llama-3.3-70b-versatile")
    assert not hasattr(r, "passed")
    assert not hasattr(r, "blocking")


# --------------------------------------------------------------------------
# Tier 3 — judge
# --------------------------------------------------------------------------


CLEAN = json.dumps(
    {
        "overreach": [],
        "interpretation": [],
        "unit_errors": [],
        "missing": [],
        "notes": "Section reports figures without interpretation.",
    }
)


def test_clean_judgement_raises_nothing():
    ev, _ = evaluator([CLEAN])
    r = ev.judge(packet(), section())
    assert r.clean
    assert r.concerns == 0
    assert "no concerns" in r.summary()


def test_judge_findings_are_counted_and_summarised():
    resp = json.dumps(
        {
            "overreach": ["A total of 1024 cases were received, of which 1023"],
            "interpretation": [],
            "unit_errors": [],
            "missing": ["outcome distribution"],
            "notes": "Overstates.",
        }
    )
    ev, _ = evaluator([resp])
    r = ev.judge(packet(), section())
    assert r.concerns == 2
    assert "review required" in r.summary()


def test_judge_result_has_no_approval_verdict():
    """The judge can raise concerns and cannot bless anything."""
    ev, _ = evaluator([CLEAN])
    r = ev.judge(packet(), section())
    for forbidden in ("passed", "approved", "acceptable", "ok", "score"):
        assert not hasattr(r, forbidden)


def test_confabulated_quotes_are_detected():
    """A judge quoting sentences the section never contained is unreliable."""
    resp = json.dumps(
        {
            "overreach": ["The drug was proven safe in all populations studied"],
            "interpretation": [],
            "unit_errors": [],
            "missing": [],
            "notes": "",
        }
    )
    ev, _ = evaluator([resp])
    r = ev.judge(packet(), section())
    assert not r.quotes_verified
    assert "confabulating" in r.summary()


def test_genuine_quotes_verify():
    resp = json.dumps(
        {
            "overreach": ["A total of 1024 cases were received"],
            "interpretation": [],
            "unit_errors": [],
            "missing": [],
            "notes": "",
        }
    )
    ev, _ = evaluator([resp])
    assert ev.judge(packet(), section()).quotes_verified


def test_unparseable_judge_response_does_not_read_as_clean():
    """Absence of evaluation must not be mistaken for a pass."""
    out = _parse_judge("the model rambled instead of returning json")
    assert out["overreach"] == []
    assert "no evaluation performed" in out["notes"]


def test_judge_is_told_not_to_check_arithmetic():
    ev, client = evaluator([CLEAN])
    ev.judge(packet(), section())
    system = client.calls[0]["messages"][0]["content"]
    assert "NOT checking arithmetic" in system
    assert "cannot approve" in system


def test_judge_receives_evidence_and_rules():
    ev, client = evaluator([CLEAN])
    ev.judge(packet(), section())
    user = client.calls[0]["messages"][1]["content"]
    assert "total_cases" in user
    assert "state observations" in user
    assert PROSE_A in user


def test_judge_schema_is_strict_mode_compatible():
    assert JUDGE_SCHEMA["additionalProperties"] is False
    assert set(JUDGE_SCHEMA["required"]) == set(JUDGE_SCHEMA["properties"])


def test_judge_uses_zero_temperature():
    ev, client = evaluator([CLEAN])
    ev.judge(packet(), section())
    assert client.calls[0]["temperature"] == 0.0
