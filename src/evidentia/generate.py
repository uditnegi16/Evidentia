"""Phase 4 — section generation.

One LLM call per section. The model receives an assembled packet and returns
structured JSON: prose, the evidence keys it used, and anything it needed but
could not find.

Three design points worth defending:

**Strict structured output.** Groq guarantees schema conformance via constrained
decoding, but only on `openai/gpt-oss-*` models. That is why the model is chosen
in D-005. Strict mode forbids optional fields, so the schema below uses empty
lists rather than nulls.

**The `flags` field is not decoration.** A model asked for a figure it does not
have will otherwise supply a plausible one. Giving it a sanctioned way to say
"I needed X and it was not in the packet" converts a silent fabrication into a
reviewable signal. Any non-empty flags list routes the section to human review.

**The client is injected.** Generation is testable, and the whole pipeline below
this line runs with no API key. That is what lets CI verify everything except
the call itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from evidentia.assembler import SectionPacket
from evidentia.config import ModelConfig

# Strict mode requires every property listed in `required` and
# additionalProperties false. No Optional fields — absence is an empty list.
SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prose": {
            "type": "string",
            "description": "The section text as continuous regulatory prose.",
        },
        "evidence_used": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evidence keys from the packet that this prose draws on.",
        },
        "flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Figures or facts needed but absent from the packet. Report them "
                "here instead of supplying a value."
            ),
        },
    },
    "required": ["prose", "evidence_used", "flags"],
    "additionalProperties": False,
}


class GenerationError(RuntimeError):
    """Raised when a section cannot be generated after all retries."""


class LLMResponse(BaseModel):
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Which structured-output mode actually produced this. Degradation is
    # recorded rather than hidden, so the run manifest shows what guarantee
    # the output was actually written under.
    output_mode: str = "strict"


class LLMClient(Protocol):
    """Minimal surface the generator needs. Keeps Groq out of the type graph."""

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        seed: int | None,
        schema: dict[str, Any],
    ) -> LLMResponse: ...


class GroqClient:
    """Groq Cloud via the official SDK.

    Structured output degrades through three modes rather than failing the run:

        strict        constrained decoding, guaranteed schema conformance
        schema        same schema, best-effort validation
        json_object   valid JSON, shape checked by our own parser

    The ladder exists because strict mode is a provider feature that has been
    observed to fail on this model family. Recording which rung succeeded keeps
    the degradation visible instead of silent.
    """

    LADDER = ("strict", "schema", "json_object")

    def __init__(
        self,
        api_key: str | None = None,
        *,
        reasoning_effort: str | None = "low",
        start_mode: str = "strict",
        rate_limit_retries: int = 4,
        retry_base_delay: float = 20.0,
    ) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise GenerationError(
                "GROQ_API_KEY is not set. Put it in .env or the environment."
            )
        from groq import Groq

        self._client = Groq(api_key=key)
        self.reasoning_effort = reasoning_effort
        self.start_mode = start_mode
        self.rate_limit_retries = rate_limit_retries
        self.retry_base_delay = retry_base_delay

    def _request_kwargs(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        seed: int | None,
        schema: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            # Reasoning tokens count here too, so this must cover both the
            # chain of thought and the answer.
            "max_completion_tokens": max_tokens,
        }
        if seed is not None:
            kwargs["seed"] = seed
        if self.reasoning_effort and "gpt-oss" in model:
            kwargs["reasoning_effort"] = self.reasoning_effort

        if mode == "strict":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "report_section",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif mode == "schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "report_section", "schema": schema},
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

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
        start = self.LADDER.index(self.start_mode)
        errors: list[str] = []

        for mode in self.LADDER[start:]:
            kwargs = self._request_kwargs(
                messages, model, temperature, max_tokens, seed, schema, mode
            )
            try:
                resp = self._call_with_backoff(kwargs)
            except Exception as exc:  # noqa: BLE001 — provider errors vary
                if not _is_recoverable(exc):
                    raise
                errors.append(f"{mode}: {exc}")
                continue

            content = resp.choices[0].message.content or ""
            if not content.strip():
                errors.append(f"{mode}: empty completion")
                continue

            usage = getattr(resp, "usage", None)
            return LLMResponse(
                content=content,
                model=model,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                output_mode=mode,
            )

        raise GenerationError(
            "every structured-output mode failed:\n  " + "\n  ".join(errors)
        )

    def _call_with_backoff(self, kwargs: dict[str, Any]):
        """Retry on rate limits, honouring the provider's stated wait.

        Free-tier token-per-minute limits are hit routinely by a report of this
        size, so treating a 429 as a failure would make the pipeline unusable
        for exactly the reader most likely to run it. The provider tells us how
        long to wait; obeying that is cheaper and politer than guessing.
        """
        delay = self.retry_base_delay
        for attempt in range(self.rate_limit_retries + 1):
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                if not _is_rate_limit(exc) or attempt == self.rate_limit_retries:
                    raise
                wait = _retry_after(exc) or delay
                print(
                    f"    rate limited; waiting {wait:.0f}s "
                    f"(attempt {attempt + 1}/{self.rate_limit_retries})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                delay = min(delay * 2, 60.0)
        raise GenerationError("unreachable")  # pragma: no cover


_RETRY_AFTER = re.compile(r"try again in ([\d.]+)\s*s", re.IGNORECASE)


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "rate_limit" in text or "rate limit" in text or "429" in text


def _retry_after(exc: Exception) -> float | None:
    """The provider states how long to wait. Prefer that over a guess."""
    m = _RETRY_AFTER.search(str(exc))
    return float(m.group(1)) + 1.0 if m else None


def _is_recoverable(exc: Exception) -> bool:
    """Schema or JSON-validation failures are worth retrying at a lower rung.

    Auth failures, rate limits and unknown models are not — retrying those
    just burns quota and hides the real problem.
    """
    text = str(exc).lower()
    fatal = ("api key", "authentication", "unauthorized", "rate limit", "not found")
    if any(f in text for f in fatal):
        return False
    recoverable = (
        "json_validate_failed",
        "failed to validate json",
        "failed to generate json",
        "json_schema",
        "response_format",
        "400",
    )
    return any(r in text for r in recoverable)


class GeneratedSection(BaseModel):
    """The Phase 4 -> Phase 5 boundary.

    Carries enough to reproduce itself: which packet, which prompt, which model.
    Two runs producing different prose from the same hashes is a finding.
    """

    section_id: str
    title: str
    prose: str
    evidence_used: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    model: str = ""
    packet_sha256: str = ""
    prompt_sha256: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attempts: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    output_mode: str = "strict"

    @property
    def word_count(self) -> int:
        return len(self.prose.split())

    @property
    def needs_review(self) -> bool:
        """Flags raised by the model always escalate. It said it was missing
        something; a human decides what that means."""
        return bool(self.flags)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Generator:
    """Turns packets into generated sections."""

    def __init__(
        self,
        model_config: ModelConfig,
        client: LLMClient | None = None,
        *,
        max_retries: int = 2,
    ) -> None:
        self.model_config = model_config
        self.client = client
        self.max_retries = max_retries

    def _client(self) -> LLMClient:
        if self.client is None:
            self.client = GroqClient(
                reasoning_effort=self.model_config.reasoning_effort,
                start_mode=self.model_config.structured_output,
            )
        return self.client

    def generate(
        self, packet: SectionPacket, *, model: str | None = None
    ) -> GeneratedSection:
        if packet.mode != "generated":
            raise GenerationError(
                f"section '{packet.section_id}' is {packet.mode}; "
                "deterministic sections are rendered, not generated"
            )

        model_name = model or self.model_config.name
        messages = packet.to_messages()
        prompt_blob = json.dumps(messages, sort_keys=True)

        last_error = ""
        for attempt in range(1, self.max_retries + 2):
            resp = self._client().complete(
                messages,
                model=model_name,
                temperature=self.model_config.temperature,
                max_tokens=self.model_config.max_tokens,
                seed=self.model_config.seed,
                schema=SECTION_SCHEMA,
            )
            try:
                payload = self._parse(resp.content)
            except ValueError as exc:
                last_error = str(exc)
                messages = [
                    *packet.to_messages(),
                    {
                        "role": "user",
                        "content": (
                            f"Your previous response was rejected: {exc}. "
                            "Return only JSON with keys prose, evidence_used, flags."
                        ),
                    },
                ]
                continue

            return GeneratedSection(
                section_id=packet.section_id,
                title=packet.title,
                prose=payload["prose"].strip(),
                evidence_used=payload["evidence_used"],
                flags=payload["flags"],
                model=model_name,
                packet_sha256=_sha(packet.evidence_digest()),
                prompt_sha256=_sha(prompt_blob),
                attempts=attempt,
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                output_mode=resp.output_mode,
            )

        raise GenerationError(
            f"section '{packet.section_id}' failed after "
            f"{self.max_retries + 1} attempts: {last_error}"
        )

    def generate_all(
        self, packets: list[SectionPacket]
    ) -> list[GeneratedSection]:
        """Generate every generated-mode section, skipping deterministic ones."""
        return [self.generate(p) for p in packets if p.mode == "generated"]

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        """Parse and shape-check the response.

        Strict mode should make this unnecessary, but a guarantee you have not
        verified is an assumption. Parsing defensively costs microseconds and
        turns a provider-side regression into a clear error rather than a
        malformed report.
        """
        text = content.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"response was not valid JSON ({exc})") from exc

        if not isinstance(payload, dict):
            raise ValueError("response JSON was not an object")

        missing = {"prose", "evidence_used", "flags"} - set(payload)
        if missing:
            raise ValueError(f"response missing keys: {sorted(missing)}")
        if not isinstance(payload["prose"], str) or not payload["prose"].strip():
            raise ValueError("prose was empty")
        for key in ("evidence_used", "flags"):
            if not isinstance(payload[key], list):
                raise ValueError(f"{key} was not a list")
            payload[key] = [str(x) for x in payload[key]]

        return payload
