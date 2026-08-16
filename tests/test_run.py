"""Phase 6 tests.

The runner is exercised end to end with a fake LLM client, so the whole
pipeline — ingest, analyse, assemble, generate, ground, render — is verified
with no API key. Only the dataset is required.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evidentia.config import load_config
from evidentia.generate import LLMResponse
from evidentia.render import (
    DETERMINISTIC_RENDERERS,
    RENDERERS,
    MarkdownRenderer,
    RenderedSection,
    render_header_block,
)
from evidentia.run import run

CONFIG = Path("configs/pader_fda.yaml")
DATA = Path(
    os.environ.get("EVIDENTIA_DATA", "data/Bisoprolol_icsr_sample_1068rows.xlsx")
)
needs_all = pytest.mark.skipif(
    not (CONFIG.exists() and DATA.exists()),
    reason="needs repo root and dataset",
)
needs_config = pytest.mark.skipif(not CONFIG.exists(), reason="run from repo root")


class EchoClient:
    """Returns prose built only from figures the packet allows.

    Deliberately grounded: the runner's job is orchestration, and a client that
    fabricated numbers would test the grounding gate instead, which has its own
    suite.
    """

    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        return LLMResponse(
            content=json.dumps(
                {
                    "prose": (
                        "During the reporting interval the supplied data was "
                        "reviewed and the figures below were recorded."
                    ),
                    "evidence_used": [],
                    "flags": [],
                }
            ),
            model=model,
            prompt_tokens=10,
            completion_tokens=5,
        )


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


@needs_config
def test_every_deterministic_section_has_a_known_renderer():
    cfg = load_config(CONFIG)
    for s in cfg.sections:
        if s.mode == "deterministic":
            assert s.renderer in DETERMINISTIC_RENDERERS, s.id


@needs_config
def test_every_output_format_has_a_renderer():
    for fmt in load_config(CONFIG).output_formats:
        assert fmt in RENDERERS


@needs_config
def test_header_block_names_unknown_fields_rather_than_inventing():
    from evidentia.assembler import SectionPacket

    cfg = load_config(CONFIG)
    packet = SectionPacket(
        section_id="reporting_period",
        title="Reporting Period",
        mode="deterministic",
        report_type="PADER",
        product_name="Bisoprolol",
        evidence={"total_cases": {"value": 1024}},
    )
    out = render_header_block(packet, cfg)
    assert "Bisoprolol" in out
    assert "1024" in out
    assert "not supplied" in out
    assert "product label / CCDS" in out


@needs_config
def test_markdown_has_contents_and_provenance():
    cfg = load_config(CONFIG)
    sections = [RenderedSection("a", "Section A", "Body text.")]
    out = MarkdownRenderer().render(cfg, sections, {"model": "m", "cases": 1024})
    assert "## Contents" in out
    assert "1. Section A" in out
    assert "## Provenance" in out
    assert "**model**: m" in out


# --------------------------------------------------------------------------
# End-to-end run
# --------------------------------------------------------------------------


@needs_all
def test_full_run_produces_every_configured_format(tmp_path):
    result = run(CONFIG, DATA, tmp_path, client=EchoClient())
    names = {p.name for p in result.outputs}
    assert names == {"report.md", "report.html", "report.docx"}
    for p in result.outputs:
        assert p.exists() and p.stat().st_size > 500


@needs_all
def test_run_writes_the_full_audit_trail(tmp_path):
    run(CONFIG, DATA, tmp_path, client=EchoClient())
    for name in (
        "evidence.json",
        "grounding.json",
        "manifest.json",
        "review.json",
        "case_index.csv",
    ):
        assert (tmp_path / name).exists(), name
    assert len(list((tmp_path / "packets").glob("*.json"))) == 9
    assert len(list((tmp_path / "sections").glob("*.json"))) == 7


@needs_all
def test_audit_evidence_keeps_case_ids_but_packets_do_not(tmp_path):
    run(CONFIG, DATA, tmp_path, client=EchoClient())
    evidence = (tmp_path / "evidence.json").read_text()
    assert "case_ids" in evidence
    for packet in (tmp_path / "packets").glob("*.json"):
        assert '"case_ids"' not in packet.read_text(), packet.name


@needs_all
def test_prompts_are_written_for_inspection(tmp_path):
    run(CONFIG, DATA, tmp_path, client=EchoClient())
    prompts = list((tmp_path / "packets").glob("*.prompt.txt"))
    assert len(prompts) == 7
    assert "Approved evidence" in prompts[0].read_text()


@needs_all
def test_manifest_records_provenance_for_attribution(tmp_path):
    m = run(CONFIG, DATA, tmp_path, client=EchoClient()).manifest
    assert m["cases"] == 1024
    assert m["reaction_events"] == 3429
    assert len(m["dataset_sha256"]) == 64
    assert m["numbers_ungrounded"] == 0
    assert m["analyses_run"] == 20
    assert m["sections_generated"] == 7
    assert m["sections_deterministic"] == 2


@needs_all
def test_unapproved_run_is_stamped_draft(tmp_path):
    m = run(CONFIG, DATA, tmp_path, client=EchoClient()).manifest
    assert m["status"].startswith("DRAFT")


@needs_all
def test_require_approval_refuses_to_render(tmp_path):
    with pytest.raises(RuntimeError, match="approval required"):
        run(CONFIG, DATA, tmp_path, client=EchoClient(), require_approval=True)
    assert not (tmp_path / "report.md").exists()
    assert (tmp_path / "review.json").exists()


@needs_all
def test_approval_unlocks_final(tmp_path):
    with pytest.raises(RuntimeError):
        run(CONFIG, DATA, tmp_path, client=EchoClient(), require_approval=True)

    review = json.loads((tmp_path / "review.json").read_text())
    for sid in review:
        review[sid] = {"status": "approved", "reviewer": "udit", "note": "ok"}
    (tmp_path / "review.json").write_text(json.dumps(review))

    m = run(
        CONFIG, DATA, tmp_path, client=EchoClient(), require_approval=True
    ).manifest
    assert m["status"] == "FINAL"


@needs_all
def test_grounding_failure_blocks_render_even_without_approval(tmp_path):
    class Fabricator(EchoClient):
        def complete(self, messages, **kw):
            return LLMResponse(
                content=json.dumps(
                    {
                        "prose": "A total of 9999 cases were received.",
                        "evidence_used": [],
                        "flags": [],
                    }
                ),
                model=kw["model"],
            )

    with pytest.raises(RuntimeError, match="grounding failed"):
        run(CONFIG, DATA, tmp_path, client=Fabricator())
    assert not (tmp_path / "report.md").exists()
    assert (tmp_path / "grounding.json").exists()


@needs_all
def test_partial_run_generates_only_the_named_section_and_renders_nothing(tmp_path):
    """A report missing seven of nine sections must never render.

    Something that looks like a complete regulatory document but is not is the
    most dangerous artifact this system could emit, so a partial run returns
    inspectable artifacts and no report.
    """
    client = EchoClient()
    result = run(
        CONFIG,
        DATA,
        tmp_path,
        client=client,
        sections_only=["history_of_actions"],
    )
    assert client.calls == 1
    assert result.outputs == []
    assert not (tmp_path / "report.md").exists()
    assert result.manifest["status"].startswith("PARTIAL")
    # artifacts for the section that did run are still written
    assert (tmp_path / "sections" / "history_of_actions.json").exists()


@needs_all
def test_unknown_section_name_fails_fast(tmp_path):
    with pytest.raises(RuntimeError, match="unknown or non-generated"):
        run(CONFIG, DATA, tmp_path, client=EchoClient(), sections_only=["nope"])


@needs_all
def test_requesting_a_deterministic_section_is_rejected(tmp_path):
    """case_index is rendered, not generated; asking to 'generate' it is an error."""
    with pytest.raises(RuntimeError, match="unknown or non-generated"):
        run(CONFIG, DATA, tmp_path, client=EchoClient(), sections_only=["case_index"])


@needs_all
def test_requesting_every_generated_section_is_not_partial(tmp_path):
    cfg = load_config(CONFIG)
    ids = [s.id for s in cfg.sections if s.mode == "generated"]
    result = run(CONFIG, DATA, tmp_path, client=EchoClient(), sections_only=ids)
    assert result.outputs, "asking for all sections should still render"
    assert not result.manifest["status"].startswith("PARTIAL")


@needs_all
def test_case_index_csv_has_every_case(tmp_path):
    import csv

    run(CONFIG, DATA, tmp_path, client=EchoClient())
    with (tmp_path / "case_index.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1024
    assert "reactions" in rows[0]
