"""Version 1 — generalisation, tested rather than claimed.

The system's central architectural assertion is that a new report type is a
configuration change, not a code change. These tests make that assertion
falsifiable: they compare two report types that share no section identifiers and
verify the engine handles both without special-casing either.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evidentia.assembler import Assembler
from evidentia.config import load_config
from evidentia.generate import LLMResponse
from evidentia.render import DETERMINISTIC_RENDERERS, RENDERERS
from evidentia.run import run

PADER = Path("configs/pader_fda.yaml")
PSUR = Path("configs/psur_lite.yaml")
DATA = Path(
    os.environ.get("EVIDENTIA_DATA", "data/Bisoprolol_icsr_sample_1068rows.xlsx")
)
needs_configs = pytest.mark.skipif(
    not (PADER.exists() and PSUR.exists()), reason="run from repo root"
)
needs_all = pytest.mark.skipif(
    not (PSUR.exists() and DATA.exists()), reason="needs repo root and dataset"
)


class EchoClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[dict[str, str]], **kw: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=json.dumps(
                {
                    "prose": "The supplied data was reviewed for this interval.",
                    "evidence_used": [],
                    "flags": [],
                }
            ),
            model=kw["model"],
        )


@needs_configs
def test_the_two_report_types_share_no_sections():
    """If they overlapped, reuse would prove nothing."""
    a = {s.id for s in load_config(PADER).sections}
    b = {s.id for s in load_config(PSUR).sections}
    assert a & b == set()


@needs_configs
def test_psur_needs_no_analysis_the_pader_did_not_already_register():
    """The headline claim: a new report type required zero new Python."""
    pader = set(load_config(PADER).required_analyses)
    psur = set(load_config(PSUR).required_analyses)
    assert psur - pader == set()
    assert len(psur & pader) >= 15, "reuse should be substantial, not incidental"


@needs_configs
def test_report_types_carry_their_own_generation_settings():
    a, b = load_config(PADER), load_config(PSUR)
    assert a.model.temperature != b.model.temperature
    assert a.output_formats != b.output_formats
    assert set(b.forbidden_phrases) > set(a.forbidden_phrases)


@needs_configs
def test_psur_blocks_benefit_risk_language_the_pader_does_not_need():
    psur = load_config(PSUR)
    for phrase in ("benefit-risk balance remains favourable", "reporting rate"):
        assert phrase in psur.forbidden_phrases


@needs_configs
def test_psur_reuses_the_same_deterministic_renderers():
    for s in load_config(PSUR).sections:
        if s.mode == "deterministic":
            assert s.renderer in DETERMINISTIC_RENDERERS
    for fmt in load_config(PSUR).output_formats:
        assert fmt in RENDERERS


@needs_configs
def test_every_psur_section_is_scoped():
    cfg = load_config(PSUR)
    total = len(cfg.required_analyses)
    for s in cfg.sections:
        assert len(s.requires) < total, s.id


@needs_all
def test_psur_packets_assemble_from_the_same_store():
    from evidentia.analyses import run_analyses
    from evidentia.ingest import load_cases

    cfg = load_config(PSUR)
    store = run_analyses(
        load_cases(DATA), cfg.required_analyses, cfg.analysis_params
    )
    packets = Assembler(cfg).assemble_all(store)
    assert len(packets) == 8
    for p in packets:
        if p.mode == "generated":
            assert p.instructions and p.allowed_numbers
            assert "{{" not in p.instructions
            assert "case_ids" not in p.instructions


@needs_all
def test_psur_runs_end_to_end_through_the_unmodified_engine(tmp_path):
    result = run(PSUR, DATA, tmp_path, client=EchoClient())
    assert {p.name for p in result.outputs} == {"report.md", "report.html"}
    assert result.manifest["report_type"] == "PSUR-lite"
    assert result.manifest["sections_generated"] == 6
    assert result.manifest["sections_deterministic"] == 2
    assert result.manifest["numbers_ungrounded"] == 0


@needs_all
def test_both_report_types_run_from_the_same_dataset(tmp_path):
    a = run(PADER, DATA, tmp_path / "pader", client=EchoClient())
    b = run(PSUR, DATA, tmp_path / "psur", client=EchoClient())
    assert a.manifest["dataset_sha256"] == b.manifest["dataset_sha256"]
    assert a.manifest["cases"] == b.manifest["cases"] == 1024
    assert a.manifest["report_type"] != b.manifest["report_type"]
