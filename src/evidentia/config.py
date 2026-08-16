"""Phase 3 — report configuration.

A report type is a YAML file. It names its sections, each section declares which
analyses it needs, and nothing here knows what a PADER is. Adding PSUR means
adding a file (D-001, D-008).

The critical property is **fail at load, not at render**. A config naming an
analysis that does not exist is a configuration error, and discovering it after
six LLM calls have already been paid for is strictly worse than discovering it
in the first millisecond.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

SectionMode = Literal["generated", "deterministic"]


class ModelConfig(BaseModel):
    """Which model produced a report. Part of the run manifest, not a constant."""

    provider: str = "groq"
    name: str = "openai/gpt-oss-120b"
    temperature: float = 0.0
    max_tokens: int = 4000
    seed: int | None = 7
    cross_check_model: str | None = None

    # gpt-oss models emit reasoning tokens that count against the completion
    # budget. At medium effort over a large evidence packet, reasoning can
    # consume the whole allowance and leave nothing for the JSON, which the
    # provider then rejects as an empty generation (E-013).
    reasoning_effort: str | None = "low"

    # Strict constrained decoding is the strongest guarantee available, but it
    # is provider-dependent and has been reported to fail on this model family.
    # The client degrades through non-strict schema to plain JSON mode rather
    # than failing the run.
    structured_output: str = "strict"


class ProductConfig(BaseModel):
    """Product metadata. Unknown fields stay unknown rather than being invented."""

    name: str
    application_number: str | None = None
    sponsor: str | None = None
    indication: str | None = None
    unknown_fields: list[str] = Field(default_factory=list)


class SectionConfig(BaseModel):
    """One section of a report.

    `requires` is the whole point: it decouples sections from analyses. Two
    report types can share an analysis without sharing a section, and a section
    can be reordered or dropped without touching any computation.
    """

    id: str
    title: str
    mode: SectionMode = "generated"
    requires: list[str] = Field(default_factory=list)
    analysis_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    prompt: str | None = None
    renderer: str | None = None
    max_words: int = 300
    max_buckets: int | None = 12
    rules: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, v: str) -> str:
        if not v.replace("_", "").isalnum():
            raise ValueError(f"section id must be a slug, got {v!r}")
        return v


class ReportConfig(BaseModel):
    """A complete report type definition."""

    report_type: str
    title: str
    regulatory_basis: str | None = None
    product: ProductConfig
    model: ModelConfig = Field(default_factory=ModelConfig)
    global_rules: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    output_formats: list[str] = Field(default_factory=lambda: ["markdown"])
    sections: list[SectionConfig]

    config_path: Path | None = None

    @field_validator("sections")
    @classmethod
    def _sections_unique_and_present(
        cls, v: list[SectionConfig]
    ) -> list[SectionConfig]:
        if not v:
            raise ValueError("a report must define at least one section")
        ids = [s.id for s in v]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate section ids: {sorted(dupes)}")
        return v

    @property
    def required_analyses(self) -> list[str]:
        """Union of every section's declared evidence, in first-seen order.

        The runner computes exactly this set — no more, so nothing is calculated
        that no section can quote, and no less, so no section is generated
        against a thinner packet than it declared.
        """
        seen: dict[str, None] = {}
        for section in self.sections:
            for key in section.requires:
                seen.setdefault(key, None)
        return list(seen)

    @property
    def analysis_params(self) -> dict[str, dict[str, Any]]:
        """Merged parameters, later sections winning on conflict."""
        merged: dict[str, dict[str, Any]] = {}
        for section in self.sections:
            for key, params in section.analysis_params.items():
                merged.setdefault(key, {}).update(params)
        return merged

    def section(self, section_id: str) -> SectionConfig:
        for s in self.sections:
            if s.id == section_id:
                return s
        raise KeyError(
            f"unknown section '{section_id}'. defined: {[s.id for s in self.sections]}"
        )


class ConfigError(ValueError):
    """Raised when a config is structurally valid YAML but semantically wrong."""


def load_config(path: str | Path, *, validate_analyses: bool = True) -> ReportConfig:
    """Load and validate a report config.

    When `validate_analyses` is set, every key in every section's `requires` is
    checked against the analysis registry before anything else happens. This is
    the load-time gate: a typo in a YAML file surfaces here, not after the model
    has been called.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a mapping")

    config = ReportConfig(**raw, config_path=path)

    if validate_analyses:
        from evidentia.analyses import registered

        known = set(registered())
        problems: list[str] = []
        for section in config.sections:
            for key in section.requires:
                if key not in known:
                    problems.append(f"  section '{section.id}' requires '{key}'")
            for key in section.analysis_params:
                if key not in section.requires:
                    problems.append(
                        f"  section '{section.id}' parameterises '{key}' "
                        "but does not require it"
                    )
        if problems:
            raise ConfigError(
                f"{path} references analyses that are not registered:\n"
                + "\n".join(problems)
                + f"\nregistered: {sorted(known)}"
            )

    root = path.parent.parent
    for section in config.sections:
        if section.mode == "generated":
            if not section.prompt:
                raise ConfigError(
                    f"section '{section.id}' is generated but declares no prompt"
                )
            if not (root / section.prompt).exists():
                raise ConfigError(
                    f"section '{section.id}' prompt not found: {root / section.prompt}"
                )
            if not section.requires:
                raise ConfigError(
                    f"section '{section.id}' is generated but requires no evidence; "
                    "a generated section with an empty packet is the condition "
                    "under which a model invents content"
                )

    return config
