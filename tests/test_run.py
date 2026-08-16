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


# --------------------------------------------------------------------------
# Evaluation tiers wired into the runner
# --------------------------------------------------------------------------


CLEAN_JUDGE = json.dumps(
    {
        "overreach": [],
        "interpretation": [],
        "unit_errors": [],
        "missing": [],
        "notes": "No concerns.",
    }
)


class EvalClient(EchoClient):
    """Returns section prose for generation calls and rubric JSON for judge calls."""

    def complete(self, messages: list[dict[str, str]], **kw: Any) -> LLMResponse:
        self.calls += 1
        is_judge = "auditing one section" in messages[0]["content"]
        return LLMResponse(
            content=CLEAN_JUDGE
            if is_judge
            else json.dumps(
                {
                    "prose": "During the interval the supplied data was reviewed.",
                    "evidence_used": [],
                    "flags": [],
                }
            ),
            model=kw["model"],
        )


def test_selection_always_includes_flagged_sections():
    from evidentia.grounding import GroundingIssue, GroundingResult
    from evidentia.run import select_for_evaluation

    flagged = GroundingResult(
        section_id="a",
        issues=[GroundingIssue(code="model_flag", severity="review", detail="x")],
    )
    clean = {k: GroundingResult(section_id=k) for k in "bcdefghij"}
    results = {"a": flagged, **clean}

    chosen = select_for_evaluation(results, sample=0.0)
    assert chosen == ["a"], "flagged sections are never sampled out"

    chosen = select_for_evaluation(results, sample=0.2)
    assert "a" in chosen
    assert 2 <= len(chosen) <= 4


def test_selection_is_deterministic_for_a_given_seed():
    from evidentia.grounding import GroundingResult
    from evidentia.run import select_for_evaluation

    results = {k: GroundingResult(section_id=k) for k in "abcdefghij"}
    assert select_for_evaluation(results, 0.3, seed=7) == select_for_evaluation(
        results, 0.3, seed=7
    )


def test_evaluation_is_off_by_default(tmp_path):
    from evidentia.run import run_evaluation

    assert run_evaluation(None, {}, {}, {}, mode="none", sample=1.0, client=None) == {}


@needs_all
def test_judge_runs_and_records_findings(tmp_path):
    result = run(CONFIG, DATA, tmp_path, client=EvalClient(), evaluate="judge")
    assert (tmp_path / "evaluation.json").exists()
    summary = result.evaluation["summary"]
    assert summary["sections_evaluated"] == 7
    assert summary["judge_concerns"] == 0
    assert result.manifest["evaluation"]["sections_evaluated"] == 7


@needs_all
def test_evaluation_never_blocks_the_render(tmp_path):
    """Tier 3 concerns must not stop a report that tier 1 passed."""
    concerned = json.dumps(
        {
            "overreach": ["During the interval the supplied data was reviewed."],
            "interpretation": [],
            "unit_errors": [],
            "missing": ["everything"],
            "notes": "Bad.",
        }
    )

    class Harsh(EvalClient):
        def complete(self, messages, **kw):
            if "auditing one section" in messages[0]["content"]:
                self.calls += 1
                return LLMResponse(content=concerned, model=kw["model"])
            return super().complete(messages, **kw)

    result = run(CONFIG, DATA, tmp_path, client=Harsh(), evaluate="judge")
    assert result.outputs, "advisory findings must not block the render"
    assert result.evaluation["summary"]["judge_concerns"] > 0


@needs_all
def test_a_failing_evaluator_does_not_fail_the_run(tmp_path):
    """An advisory tier that crashes would outrank tier 1, which is backwards."""

    class Broken(EvalClient):
        def complete(self, messages, **kw):
            if "auditing one section" in messages[0]["content"]:
                raise RuntimeError("judge exploded")
            return super().complete(messages, **kw)

    result = run(CONFIG, DATA, tmp_path, client=Broken(), evaluate="judge")
    assert result.outputs
    entries = result.evaluation["sections"].values()
    assert all("judge_error" in e for e in entries)


class CitingClient(EchoClient):
    """Cites evidence, so sections come back clean rather than review-flagged.

    EchoClient returns an empty evidence_used, which trips `no_evidence_cited`
    on every section. That is correct behaviour, but it leaves no unflagged
    sections for sampling to select from.
    """

    def complete(self, messages: list[dict[str, str]], **kw: Any) -> LLMResponse:
        self.calls += 1
        if "auditing one section" in messages[0]["content"]:
            return LLMResponse(content=CLEAN_JUDGE, model=kw["model"])
        # Cite a key this section actually declared. Hardcoding one key blocks
        # any section that did not declare it — which is the gate working.
        import re

        found = re.findall(r'^\s{2}"(\w+)":', messages[1]["content"], re.MULTILINE)
        return LLMResponse(
            content=json.dumps(
                {
                    "prose": "During the interval the supplied data was reviewed.",
                    "evidence_used": found[:1],
                    "flags": [],
                }
            ),
            model=kw["model"],
        )


@needs_all
def test_sampling_limits_evaluation_cost(tmp_path):
    """sample=0 evaluates only what tier 1 already flagged."""
    result = run(
        CONFIG,
        DATA,
        tmp_path,
        client=CitingClient(),
        evaluate="judge",
        evaluate_sample=0.0,
    )
    flagged = set(result.needs_review)
    evaluated = set(result.evaluation.get("sections", {}))
    assert evaluated == flagged
    assert len(evaluated) < 7, "some sections should be clean under CitingClient"


@needs_all
def test_full_sample_evaluates_every_section(tmp_path):
    result = run(
        CONFIG,
        DATA,
        tmp_path,
        client=CitingClient(),
        evaluate="judge",
        evaluate_sample=1.0,
    )
    assert result.evaluation["summary"]["sections_evaluated"] == 7


@needs_all
def test_flagged_sections_are_evaluated_even_at_zero_sample(tmp_path):
    """The policy that makes sampling safe: flags are never sampled out."""
    result = run(
        CONFIG,
        DATA,
        tmp_path,
        client=EchoClient(),
        evaluate="judge",
        evaluate_sample=0.0,
    )
    assert set(result.evaluation["sections"]) == set(result.needs_review)
