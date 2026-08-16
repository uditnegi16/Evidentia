"""Phase 3 tests.

Most of these run without the dataset, because config validation and context
scoping are pure functions over an EvidenceStore. That is deliberate: the layer
where context-engineering mistakes happen is fully testable offline.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from evidentia.assembler import Assembler
from evidentia.config import ConfigError, ReportConfig, load_config
from evidentia.evidence import Bucket, EvidenceItem, EvidenceStore, Provenance

CONFIG = Path("configs/pader_fda.yaml")
DATA = Path(
    os.environ.get("EVIDENTIA_DATA", "data/Bisoprolol_icsr_sample_1068rows.xlsx")
)
needs_config = pytest.mark.skipif(
    not CONFIG.exists(), reason="run from the repo root"
)
needs_data = pytest.mark.skipif(not DATA.exists(), reason="dataset not present")


# --------------------------------------------------------------------------
# Synthetic store — lets the assembler be tested with no data and no API key
# --------------------------------------------------------------------------


def _scalar(key: str, value: int, unit: str = "case") -> EvidenceItem:
    return EvidenceItem(
        key=key,
        label=key.replace("_", " "),
        kind="scalar",
        value=value,
        provenance=Provenance(
            unit=unit,
            method="synthetic",
            source_columns=["c"],
            n_contributing=value,
            denominator=1000,
            case_ids=[900001, 900002, 900003],
        ),
    )


def _dist(key: str, pairs: list[tuple[str, int]], unit: str = "case") -> EvidenceItem:
    return EvidenceItem(
        key=key,
        label=key.replace("_", " "),
        kind="distribution",
        buckets=[
            Bucket(label=lbl, count=n, pct=round(n / 10, 1), case_ids=[900001])
            for lbl, n in pairs
        ],
        provenance=Provenance(
            unit=unit,
            method="synthetic",
            source_columns=["c"],
            n_contributing=sum(n for _, n in pairs),
            denominator=1000,
        ),
    )


@pytest.fixture
def store() -> EvidenceStore:
    s = EvidenceStore()
    s.add(_scalar("total_cases", 1024))
    s.add(_scalar("alert_cases", 1023))
    s.add(_dist("serious_split", [("serious", 1023), ("not serious", 1)]))
    s.add(_dist("age_bands", [("65-74", 300), ("75-84", 250), ("unknown", 86)]))
    s.add(_dist("top_reactions", [(f"PT{i}", 100 - i) for i in range(20)],
                unit="reaction_event"))
    return s


@pytest.fixture
def mini_config(tmp_path: Path) -> ReportConfig:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "configs").mkdir()
    (tmp_path / "prompts" / "p.jinja").write_text(
        "Section: {{ section_title }}\n{{ evidence_json }}\n"
        "{% for r in rules %}- {{ r }}\n{% endfor %}Limit {{ max_words }}."
    )
    cfg = {
        "report_type": "TEST",
        "title": "Test report",
        "product": {"name": "Testomab"},
        "global_rules": ["global rule"],
        "forbidden_phrases": ["no safety concerns"],
        "sections": [
            {
                "id": "a",
                "title": "Section A",
                "mode": "generated",
                "prompt": "prompts/p.jinja",
                "requires": ["total_cases", "serious_split"],
                "rules": ["section rule"],
                "max_words": 120,
            },
            {
                "id": "b",
                "title": "Section B",
                "mode": "deterministic",
                "requires": ["age_bands"],
            },
        ],
    }
    path = tmp_path / "configs" / "t.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return load_config(path, validate_analyses=False)


# --------------------------------------------------------------------------
# Scoping — the core property
# --------------------------------------------------------------------------


def test_section_receives_only_what_it_declared(mini_config, store):
    packet = Assembler(mini_config).assemble("a", store)
    assert set(packet.evidence) == {"total_cases", "serious_split"}
    assert "age_bands" not in packet.evidence
    assert "top_reactions" not in packet.evidence


def test_undeclared_evidence_is_absent_from_the_rendered_prompt(mini_config, store):
    packet = Assembler(mini_config).assemble("a", store)
    assert "age_bands" not in packet.instructions
    assert "300" not in packet.instructions  # an age_bands-only figure


def test_missing_evidence_raises_rather_than_thinning_the_packet(mini_config):
    empty = EvidenceStore()
    empty.add(_scalar("total_cases", 1024))
    with pytest.raises(KeyError, match="not computed"):
        Assembler(mini_config).assemble("a", empty)


def test_case_ids_never_enter_the_packet(mini_config, store):
    packet = Assembler(mini_config).assemble("a", store)
    blob = packet.evidence_digest() + packet.instructions
    assert "900001" not in blob
    assert "case_ids" not in blob


def test_allowed_numbers_come_only_from_scoped_evidence(mini_config, store):
    packet = Assembler(mini_config).assemble("a", store)
    assert 1024.0 in packet.allowed_numbers
    assert 1023.0 in packet.allowed_numbers
    assert 300.0 not in packet.allowed_numbers  # age_bands, not declared
    assert 100.0 not in packet.allowed_numbers  # top_reactions, not declared


def test_deterministic_section_gets_evidence_but_no_prompt(mini_config, store):
    packet = Assembler(mini_config).assemble("b", store)
    assert packet.evidence
    assert packet.instructions == ""
    assert packet.mode == "deterministic"


def test_rules_are_global_then_section(mini_config, store):
    packet = Assembler(mini_config).assemble("a", store)
    assert packet.rules == ["global rule", "section rule"]


def test_messages_split_constant_from_assembled(mini_config, store):
    msgs = Assembler(mini_config).assemble("a", store).to_messages()
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "global rule" in msgs[0]["content"]
    assert "Testomab" in msgs[0]["content"]
    assert "total_cases" in msgs[1]["content"]


def test_max_buckets_truncates_long_distributions(tmp_path, store):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "configs").mkdir()
    (tmp_path / "prompts" / "p.jinja").write_text("{{ evidence_json }}")
    cfg = {
        "report_type": "T",
        "title": "T",
        "product": {"name": "X"},
        "sections": [
            {
                "id": "a",
                "title": "A",
                "mode": "generated",
                "prompt": "prompts/p.jinja",
                "requires": ["top_reactions"],
                "max_buckets": 5,
            }
        ],
    }
    path = tmp_path / "configs" / "t.yaml"
    path.write_text(yaml.safe_dump(cfg))
    packet = Assembler(load_config(path, validate_analyses=False)).assemble("a", store)
    assert len(packet.evidence["top_reactions"]["buckets"]) == 5
    assert packet.evidence["top_reactions"]["buckets_omitted"] == 15


def test_assemble_all_covers_every_section(mini_config, store):
    packets = Assembler(mini_config).assemble_all(store)
    assert [p.section_id for p in packets] == ["a", "b"]


# --------------------------------------------------------------------------
# Config validation — fail at load, not at render
# --------------------------------------------------------------------------


def test_unknown_analysis_fails_at_load(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.jinja").write_text("x")
    cfg = {
        "report_type": "T",
        "title": "T",
        "product": {"name": "X"},
        "sections": [
            {
                "id": "a",
                "title": "A",
                "mode": "generated",
                "prompt": "prompts/p.jinja",
                "requires": ["total_cases", "not_a_real_analysis"],
            }
        ],
    }
    path = tmp_path / "configs" / "t.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="not_a_real_analysis"):
        load_config(path)


def test_generated_section_without_evidence_is_rejected(tmp_path):
    """The condition under which a model invents content."""
    (tmp_path / "configs").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.jinja").write_text("x")
    cfg = {
        "report_type": "T",
        "title": "T",
        "product": {"name": "X"},
        "sections": [
            {
                "id": "a",
                "title": "A",
                "mode": "generated",
                "prompt": "prompts/p.jinja",
                "requires": [],
            }
        ],
    }
    path = tmp_path / "configs" / "t.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="requires no evidence"):
        load_config(path)


def test_generated_section_without_prompt_is_rejected(tmp_path):
    (tmp_path / "configs").mkdir()
    cfg = {
        "report_type": "T",
        "title": "T",
        "product": {"name": "X"},
        "sections": [
            {"id": "a", "title": "A", "mode": "generated", "requires": ["total_cases"]}
        ],
    }
    path = tmp_path / "configs" / "t.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="declares no prompt"):
        load_config(path)


def test_missing_prompt_file_is_rejected(tmp_path):
    (tmp_path / "configs").mkdir()
    cfg = {
        "report_type": "T",
        "title": "T",
        "product": {"name": "X"},
        "sections": [
            {
                "id": "a",
                "title": "A",
                "mode": "generated",
                "prompt": "prompts/absent.jinja",
                "requires": ["total_cases"],
            }
        ],
    }
    path = tmp_path / "configs" / "t.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="prompt not found"):
        load_config(path)


def test_duplicate_section_ids_rejected(tmp_path):
    (tmp_path / "configs").mkdir()
    cfg = {
        "report_type": "T",
        "title": "T",
        "product": {"name": "X"},
        "sections": [
            {"id": "a", "title": "A", "mode": "deterministic"},
            {"id": "a", "title": "A2", "mode": "deterministic"},
        ],
    }
    path = tmp_path / "configs" / "t.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(Exception, match="duplicate section ids"):
        load_config(path, validate_analyses=False)


def test_required_analyses_are_deduped_in_order(mini_config):
    assert mini_config.required_analyses == [
        "total_cases",
        "serious_split",
        "age_bands",
    ]


# --------------------------------------------------------------------------
# The real PADER config
# --------------------------------------------------------------------------


@needs_config
def test_pader_config_loads_and_validates():
    cfg = load_config(CONFIG)
    assert cfg.report_type == "PADER"
    assert len(cfg.sections) == 9


@needs_config
def test_pader_covers_every_section_the_brief_requires():
    ids = {s.id for s in load_config(CONFIG).sections}
    assert {
        "reporting_period",
        "narrative_summary",
        "summary_analysis_of_cases",
        "reaction_analysis",
        "serious_cases_alerts",
        "trends_observations",
        "history_of_actions",
        "case_index",
    } <= ids


@needs_config
def test_pader_mixes_generated_and_deterministic_sections():
    cfg = load_config(CONFIG)
    modes = {s.mode for s in cfg.sections}
    assert modes == {"generated", "deterministic"}
    det = [s.id for s in cfg.sections if s.mode == "deterministic"]
    assert set(det) == {"reporting_period", "case_index"}


@needs_config
def test_pader_forbids_the_phrase_named_in_the_brief():
    assert "no safety concerns" in load_config(CONFIG).forbidden_phrases


@needs_config
def test_no_section_requests_every_analysis():
    """Scoping is real, not nominal."""
    cfg = load_config(CONFIG)
    total = len(cfg.required_analyses)
    for s in cfg.sections:
        assert len(s.requires) < total, f"{s.id} asks for everything"
        assert len(s.requires) <= 7, f"{s.id} packet is too broad"


@needs_config
@needs_data
def test_end_to_end_packet_assembly():
    from evidentia.analyses import run_analyses
    from evidentia.ingest import load_cases

    cfg = load_config(CONFIG)
    frame = load_cases(DATA)
    store = run_analyses(frame, cfg.required_analyses, cfg.analysis_params)
    packets = Assembler(cfg).assemble_all(store)

    assert len(packets) == 9
    for p in packets:
        if p.mode == "generated":
            assert p.instructions, p.section_id
            assert p.allowed_numbers, p.section_id
            assert "{{" not in p.instructions, f"{p.section_id} left a jinja tag"
            assert "case_ids" not in p.instructions


@needs_config
@needs_data
def test_narrative_packet_allows_its_figures_and_nothing_else():
    from evidentia.analyses import run_analyses
    from evidentia.ingest import load_cases

    cfg = load_config(CONFIG)
    store = run_analyses(
        load_cases(DATA), cfg.required_analyses, cfg.analysis_params
    )
    p = Assembler(cfg).assemble("narrative_summary", store)
    assert 1024.0 in p.allowed_numbers
    assert 1023.0 in p.allowed_numbers
    assert 80.0 in p.allowed_numbers
    # country distribution was not declared by this section
    assert "country_distribution" not in p.evidence
