"""Phase 4 tests.

Every test here runs with no API key and no network. The client is a Protocol,
so a fake satisfies it, and the retry path, the parser and the contract are all
exercised offline. The only untested surface is the HTTP call itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from evidentia.assembler import SectionPacket
from evidentia.config import ModelConfig
from evidentia.generate import (
    SECTION_SCHEMA,
    GeneratedSection,
    GenerationError,
    Generator,
    LLMResponse,
)

GOOD = (
    '{"prose": "During the reporting period 1,024 cases were received, of which '
    '1,023 were classified as serious.", '
    '"evidence_used": ["total_cases", "serious_split"], "flags": []}'
)


class FakeClient:
    """Returns queued responses and records what it was asked."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        seed: int | None,
        schema: dict[str, Any],
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "seed": seed,
                "schema": schema,
            }
        )
        if not self.responses:
            raise AssertionError("FakeClient ran out of queued responses")
        return LLMResponse(
            content=self.responses.pop(0),
            model=model,
            prompt_tokens=100,
            completion_tokens=50,
        )


def packet(mode: str = "generated") -> SectionPacket:
    return SectionPacket(
        section_id="narrative_summary",
        title="Narrative Summary and Analysis",
        mode=mode,
        report_type="PADER",
        product_name="Bisoprolol",
        evidence={"total_cases": {"label": "Total cases", "value": 1024}},
        instructions="Summarise the interval.",
        rules=["state observations, not conclusions"],
        forbidden_phrases=["no safety concerns"],
        max_words=320,
        allowed_numbers=[1023.0, 1024.0],
        evidence_keys=["total_cases", "serious_split"],
    )


def gen(responses: list[str], **kw) -> tuple[Generator, FakeClient]:
    client = FakeClient(responses)
    return Generator(ModelConfig(), client=client, **kw), client


# --------------------------------------------------------------------------
# Happy path and provenance
# --------------------------------------------------------------------------


def test_generates_a_section():
    g, _ = gen([GOOD])
    out = g.generate(packet())
    assert isinstance(out, GeneratedSection)
    assert "1,024 cases" in out.prose
    assert out.evidence_used == ["total_cases", "serious_split"]
    assert out.flags == []
    assert out.attempts == 1


def test_records_reproducibility_hashes():
    g, _ = gen([GOOD])
    out = g.generate(packet())
    assert len(out.packet_sha256) == 64
    assert len(out.prompt_sha256) == 64
    assert out.model == "openai/gpt-oss-120b"


def test_identical_packets_hash_identically():
    g1, _ = gen([GOOD])
    g2, _ = gen([GOOD])
    assert (
        g1.generate(packet()).packet_sha256
        == g2.generate(packet()).packet_sha256
    )


def test_token_usage_is_captured():
    g, _ = gen([GOOD])
    out = g.generate(packet())
    assert out.prompt_tokens == 100
    assert out.completion_tokens == 50


def test_word_count_is_available_for_the_length_gate():
    g, _ = gen([GOOD])
    assert g.generate(packet()).word_count > 5


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------


def test_strict_schema_is_sent():
    g, client = gen([GOOD])
    g.generate(packet())
    schema = client.calls[0]["schema"]
    assert schema is SECTION_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"prose", "evidence_used", "flags"}


def test_no_optional_fields_in_schema():
    """Groq strict mode rejects them; absence must be an empty list."""
    assert set(SECTION_SCHEMA["properties"]) == set(SECTION_SCHEMA["required"])


def test_temperature_zero_and_seed_are_sent():
    g, client = gen([GOOD])
    g.generate(packet())
    assert client.calls[0]["temperature"] == 0.0
    assert client.calls[0]["seed"] == 7


def test_system_and_user_are_separated():
    g, client = gen([GOOD])
    g.generate(packet())
    msgs = client.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "Bisoprolol" in msgs[0]["content"]
    assert "Summarise the interval" in msgs[1]["content"]


def test_cross_check_model_can_be_overridden_per_call():
    g, client = gen([GOOD, GOOD])
    g.generate(packet())
    g.generate(packet(), model="llama-3.3-70b-versatile")
    assert client.calls[0]["model"] == "openai/gpt-oss-120b"
    assert client.calls[1]["model"] == "llama-3.3-70b-versatile"


# --------------------------------------------------------------------------
# Flags escalate
# --------------------------------------------------------------------------


def test_flags_route_the_section_to_review():
    resp = (
        '{"prose": "Cases were received during the interval.", '
        '"evidence_used": ["total_cases"], '
        '"flags": ["needed the non-serious count; not in packet"]}'
    )
    g, _ = gen([resp])
    out = g.generate(packet())
    assert out.needs_review
    assert "non-serious" in out.flags[0]


def test_clean_section_does_not_need_review():
    g, _ = gen([GOOD])
    assert not g.generate(packet()).needs_review


# --------------------------------------------------------------------------
# Parsing and retry
# --------------------------------------------------------------------------


def test_retries_on_invalid_json_then_succeeds():
    g, client = gen(["not json at all", GOOD])
    out = g.generate(packet())
    assert out.attempts == 2
    assert len(client.calls) == 2


def test_retry_tells_the_model_what_was_wrong():
    g, client = gen(["{oops", GOOD])
    g.generate(packet())
    correction = client.calls[1]["messages"][-1]["content"]
    assert "rejected" in correction
    assert "prose" in correction


def test_gives_up_after_max_retries():
    g, _ = gen(["bad", "worse", "worst"], max_retries=2)
    with pytest.raises(GenerationError, match="failed after 3 attempts"):
        g.generate(packet())


def test_strips_markdown_fences():
    g, _ = gen(["```json\n" + GOOD + "\n```"])
    assert "1,024 cases" in g.generate(packet()).prose


def test_rejects_missing_keys():
    g, _ = gen(['{"prose": "text"}', GOOD])
    assert g.generate(packet()).attempts == 2


def test_rejects_empty_prose():
    g, _ = gen(['{"prose": "  ", "evidence_used": [], "flags": []}', GOOD])
    assert g.generate(packet()).attempts == 2


def test_rejects_non_object_json():
    g, _ = gen(["[1, 2, 3]", GOOD])
    assert g.generate(packet()).attempts == 2


def test_rejects_non_list_flags():
    g, _ = gen(
        ['{"prose": "t", "evidence_used": [], "flags": "none"}', GOOD]
    )
    assert g.generate(packet()).attempts == 2


def test_coerces_list_items_to_strings():
    g, _ = gen(['{"prose": "t", "evidence_used": [1, 2], "flags": []}'])
    assert g.generate(packet()).evidence_used == ["1", "2"]


# --------------------------------------------------------------------------
# Mode boundary
# --------------------------------------------------------------------------


def test_deterministic_sections_are_refused():
    g, client = gen([GOOD])
    with pytest.raises(GenerationError, match="deterministic sections are rendered"):
        g.generate(packet(mode="deterministic"))
    assert client.calls == [], "no API call should be made"


def test_generate_all_skips_deterministic_sections():
    g, client = gen([GOOD])
    out = g.generate_all([packet("deterministic"), packet("generated")])
    assert len(out) == 1
    assert len(client.calls) == 1


# --------------------------------------------------------------------------
# Client construction
# --------------------------------------------------------------------------


def test_missing_api_key_fails_clearly(monkeypatch):
    from evidentia.generate import GroqClient

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(GenerationError, match="GROQ_API_KEY is not set"):
        GroqClient()


# --------------------------------------------------------------------------
# Structured-output fallback ladder (E-013)
# --------------------------------------------------------------------------


class FakeGroq:
    """Stands in for the Groq SDK client so the ladder can be tested offline."""

    def __init__(self, fail_modes: set[str], content: str = GOOD) -> None:
        self.fail_modes = fail_modes
        self.content = content
        self.attempted: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        rf = kwargs.get("response_format", {})
        if rf.get("type") == "json_object":
            mode = "json_object"
        elif rf.get("json_schema", {}).get("strict"):
            mode = "strict"
        else:
            mode = "schema"
        self.attempted.append(mode)
        self.kwargs.append(kwargs)
        if mode in self.fail_modes:
            raise RuntimeError(
                "Error code: 400 - json_validate_failed, failed_generation: ''"
            )

        class _M:
            content = self.content

        class _C:
            message = _M()

        class _R:
            choices = [_C()]
            usage = None

        return _R()


def _client(fail_modes: set[str], **kw):
    from evidentia.generate import GroqClient

    c = GroqClient.__new__(GroqClient)
    c._client = FakeGroq(fail_modes)
    c.reasoning_effort = kw.get("reasoning_effort", "low")
    c.start_mode = kw.get("start_mode", "strict")
    c.rate_limit_retries = 0
    c.retry_base_delay = 0.0
    return c


def _call(c, model="openai/gpt-oss-120b"):
    return c.complete(
        [{"role": "user", "content": "hi"}],
        model=model,
        temperature=0.0,
        max_tokens=4000,
        seed=7,
        schema=SECTION_SCHEMA,
    )


def test_strict_mode_used_when_it_works():
    c = _client(set())
    assert _call(c).output_mode == "strict"
    assert c._client.attempted == ["strict"]


def test_falls_back_to_non_strict_schema():
    c = _client({"strict"})
    assert _call(c).output_mode == "schema"
    assert c._client.attempted == ["strict", "schema"]


def test_falls_back_to_json_object_mode():
    c = _client({"strict", "schema"})
    assert _call(c).output_mode == "json_object"
    assert c._client.attempted == ["strict", "schema", "json_object"]


def test_raises_when_every_mode_fails():
    c = _client({"strict", "schema", "json_object"})
    with pytest.raises(GenerationError, match="every structured-output mode failed"):
        _call(c)


def test_reasoning_effort_sent_only_to_gpt_oss():
    c = _client(set())
    _call(c, model="openai/gpt-oss-120b")
    assert c._client.kwargs[0]["reasoning_effort"] == "low"

    c2 = _client(set())
    _call(c2, model="llama-3.3-70b-versatile")
    assert "reasoning_effort" not in c2._client.kwargs[0]


def test_uses_max_completion_tokens_not_max_tokens():
    """Reasoning tokens count against the completion budget."""
    c = _client(set())
    _call(c)
    assert c._client.kwargs[0]["max_completion_tokens"] == 4000
    assert "max_tokens" not in c._client.kwargs[0]


def test_auth_errors_are_not_retried_down_the_ladder():
    from evidentia.generate import _is_recoverable

    assert not _is_recoverable(RuntimeError("401 invalid api key"))
    assert not _is_recoverable(RuntimeError("rate limit exceeded"))
    assert _is_recoverable(RuntimeError("400 json_validate_failed"))


def test_empty_completion_falls_through():
    c = _client(set())
    c._client.content = "   "
    with pytest.raises(GenerationError, match="empty completion"):
        _call(c)


def test_start_mode_can_skip_strict():
    c = _client(set(), start_mode="json_object")
    assert _call(c).output_mode == "json_object"
    assert c._client.attempted == ["json_object"]


# --------------------------------------------------------------------------
# Rate limit backoff (E-017)
# --------------------------------------------------------------------------


def test_rate_limit_is_detected_and_wait_is_parsed():
    from evidentia.generate import _is_rate_limit, _retry_after

    exc = RuntimeError(
        "Error code: 429 - rate_limit_exceeded ... Please try again in 26.64s."
    )
    assert _is_rate_limit(exc)
    assert _retry_after(exc) == pytest.approx(27.64)
    assert not _is_rate_limit(RuntimeError("400 json_validate_failed"))
    assert _retry_after(RuntimeError("429 slow down")) is None


def test_backoff_retries_then_succeeds(monkeypatch):
    from evidentia.generate import GroqClient

    slept: list[float] = []
    monkeypatch.setattr("evidentia.generate.time.sleep", slept.append)

    calls = {"n": 0}

    class Flaky:
        def create(self, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("429 rate_limit_exceeded try again in 2.0s")

            class _M:
                content = GOOD

            class _C:
                message = _M()

            class _R:
                choices = [_C()]
                usage = None

            return _R()

    c = GroqClient.__new__(GroqClient)
    c._client = type("X", (), {"chat": type("Y", (), {"completions": Flaky()})()})()
    c.reasoning_effort = "low"
    c.start_mode = "strict"
    c.rate_limit_retries = 4
    c.retry_base_delay = 20.0

    resp = c.complete(
        [{"role": "user", "content": "hi"}],
        model="openai/gpt-oss-120b",
        temperature=0.0,
        max_tokens=4000,
        seed=7,
        schema=SECTION_SCHEMA,
    )
    assert "1,024 cases" in resp.content
    assert calls["n"] == 3
    assert slept == [3.0, 3.0], "should honour the provider's stated wait"
