"""Phase 3 — context assembly.

Builds the packet a single section is allowed to see. This is the step the
challenge brief calls "most of the exercise": not the prompt wording, but the
decision about exactly what goes into it.

    EvidenceStore (all 20 analyses)
            │
            │  section.requires  ──► scope
            ▼
    SectionPacket (3-5 items, projected, no case IDs)
            │
            ▼
        one LLM call

Two properties matter and both are enforced here rather than trusted:

  scoping     a section receives what it declared and nothing else, so an
              unrelated figure cannot appear in its prose by accident
  allowance   the packet computes the set of numbers the section may legally
              state, which Phase 5 checks the output against
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field

from evidentia.config import ReportConfig, SectionConfig
from evidentia.evidence import EvidenceStore


class SectionPacket(BaseModel):
    """The Phase 3 -> Phase 4 boundary. Everything one section may use."""

    section_id: str
    title: str
    mode: str
    report_type: str
    product_name: str
    period_start: date | None = None
    period_end: date | None = None

    evidence: dict[str, Any] = Field(default_factory=dict)
    instructions: str = ""
    rules: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    max_words: int = 300

    allowed_numbers: list[float] = Field(default_factory=list)
    evidence_keys: list[str] = Field(default_factory=list)

    def to_messages(self) -> list[dict[str, str]]:
        """Render as chat messages.

        System carries what is constant for the report type; user carries what
        is assembled per section. Splitting them this way means the system half
        is cacheable and the diff between two sections is visible at a glance.
        """
        system = (
            f"You write sections of a {self.report_type} regulatory safety "
            f"report for {self.product_name}.\n\n"
            "Absolute constraints:\n"
            + "\n".join(f"- {r}" for r in self.rules)
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": self.instructions},
        ]

    def evidence_digest(self) -> str:
        """Human-readable packet, for the review UI and for the README."""
        import json

        return json.dumps(self.evidence, indent=2, default=str)


class Assembler:
    """Turns config plus evidence into per-section packets."""

    def __init__(self, config: ReportConfig, root: Path | None = None) -> None:
        self.config = config
        if root is not None:
            self.root = Path(root)
        elif config.config_path is not None:
            self.root = Path(config.config_path).parent.parent
        else:
            self.root = Path.cwd()
        self.env = Environment(
            loader=FileSystemLoader(str(self.root)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
        )

    def assemble(self, section_id: str, store: EvidenceStore) -> SectionPacket:
        section = self.config.section(section_id)
        scoped = store.subset(section.requires)

        evidence = {
            key: item.to_prompt_dict(max_buckets=section.max_buckets)
            for key, item in scoped.items()
        }

        allowed: set[float] = set()
        for item in scoped.values():
            allowed |= item.numeric_claims()

        rules = list(self.config.global_rules) + list(section.rules)

        instructions = ""
        if section.mode == "generated" and section.prompt:
            instructions = self._render(section, evidence, rules)

        return SectionPacket(
            section_id=section.id,
            title=section.title,
            mode=section.mode,
            report_type=self.config.report_type,
            product_name=self.config.product.name,
            period_start=store.period_start,
            period_end=store.period_end,
            evidence=evidence,
            instructions=instructions,
            rules=rules,
            forbidden_phrases=list(self.config.forbidden_phrases),
            max_words=section.max_words,
            allowed_numbers=sorted(allowed),
            evidence_keys=list(section.requires),
        )

    def assemble_all(self, store: EvidenceStore) -> list[SectionPacket]:
        return [self.assemble(s.id, store) for s in self.config.sections]

    def _render(
        self,
        section: SectionConfig,
        evidence: dict[str, Any],
        rules: list[str],
    ) -> str:
        import json

        template = self.env.get_template(section.prompt)
        return template.render(
            section_title=section.title,
            report_type=self.config.report_type,
            product=self.config.product,
            regulatory_basis=self.config.regulatory_basis,
            evidence=evidence,
            evidence_json=json.dumps(evidence, indent=2, default=str),
            rules=rules,
            max_words=section.max_words,
        ).strip()
