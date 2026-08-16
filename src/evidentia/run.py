"""Phase 6 — the runner.

One command from raw data to a rendered report. Also the place the human review
gate is enforced and the run manifest is written.

    load → analyse → assemble → generate → ground → review gate → render

Every intermediate artifact is written to disk. That is not debug output: it is
the audit trail. A reviewer can open the packet that produced a sentence, and
the manifest records which dataset, config, prompts and model were involved, so
a report can be attributed rather than merely trusted.

Review model (deliberately simple, as the brief allows):

    every generated section starts unapproved
    --require-approval refuses to render until each is approved in review.json
    without the flag, the report renders and is stamped DRAFT

The default is permissive so the pipeline is demonstrable in one command, but
"final" is never reachable without a human. Blocking grounding failures stop the
run regardless of the flag — a human may approve prose, not fabricated numbers.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evidentia.analyses import run_analyses
from evidentia.assembler import Assembler, SectionPacket
from evidentia.config import ReportConfig, load_config
from evidentia.evaluate import Evaluator
from evidentia.generate import GeneratedSection, Generator, LLMClient
from evidentia.grounding import GroundingResult, GroundingValidator
from evidentia.ingest import load_cases
from evidentia.render import (
    build_sections,
    render_report,
    utc_stamp,
    write_case_index_csv,
)

REVIEW_FILE = "review.json"


def select_for_evaluation(
    grounding: dict[str, GroundingResult],
    sample: float,
    seed: int = 7,
) -> list[str]:
    """Which sections get tier 2 and 3 treatment.

    The policy that makes this affordable at 1,000 reports rather than 1:

        every section flagged by tier 1   always — it is already suspect
        a deterministic sample of the rest   `sample` fraction

    Sampling is seeded so a run is reproducible. Flagged sections are never
    sampled out; spending the budget on sections nothing has questioned while
    skipping one that raised a flag would invert the point.
    """
    flagged = [k for k, r in grounding.items() if r.needs_review]
    rest = [k for k in grounding if k not in flagged]
    if sample >= 1.0:
        chosen = rest
    elif sample <= 0.0:
        chosen = []
    else:
        rng = random.Random(seed)
        n = max(1, round(len(rest) * sample)) if rest else 0
        chosen = rng.sample(rest, min(n, len(rest)))
    return [k for k in grounding if k in set(flagged) | set(chosen)]


def run_evaluation(
    config: ReportConfig,
    packets: dict[str, SectionPacket],
    generated: dict[str, GeneratedSection],
    grounding: dict[str, GroundingResult],
    *,
    mode: str,
    sample: float,
    client: LLMClient | None,
) -> dict[str, Any]:
    """Tiers 2 and 3. Advisory only — this function cannot block a render."""
    if mode == "none" or not generated:
        return {}

    targets = select_for_evaluation(grounding, sample, config.model.seed or 7)
    if not targets:
        return {}

    evaluator = Evaluator(Generator(config.model, client=client), client)
    cross_model = config.model.cross_check_model
    out: dict[str, Any] = {"mode": mode, "sample": sample, "sections": {}}

    print(
        f"\nevaluation ({mode}) on {len(targets)} of {len(generated)} sections",
        file=sys.stderr,
    )

    for sid in targets:
        packet, section = packets[sid], generated[sid]
        entry: dict[str, Any] = {}

        if mode in {"cross", "full"} and cross_model:
            try:
                r = evaluator.cross_check(packet, section, cross_model)
                entry["cross_check"] = json.loads(r.model_dump_json())
                print(f"  {r.summary()}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                # An advisory tier that crashes the run would give tier 2 more
                # authority than tier 1, which is exactly backwards.
                entry["cross_check_error"] = str(exc)
                print(f"  {sid}: cross-check failed — {exc}", file=sys.stderr)

        if mode in {"judge", "full"}:
            try:
                r = evaluator.judge(packet, section)
                entry["judge"] = json.loads(r.model_dump_json())
                print(f"  {r.summary()}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                entry["judge_error"] = str(exc)
                print(f"  {sid}: judge failed — {exc}", file=sys.stderr)

        out["sections"][sid] = entry

    judged = [v.get("judge") for v in out["sections"].values() if v.get("judge")]
    crossed = [
        v.get("cross_check")
        for v in out["sections"].values()
        if v.get("cross_check")
    ]
    out["summary"] = {
        "sections_evaluated": len(targets),
        "judge_concerns": sum(
            len(j["overreach"])
            + len(j["interpretation"])
            + len(j["unit_errors"])
            + len(j["missing"])
            for j in judged
        ),
        "judge_sections_with_concerns": sum(
            1
            for j in judged
            if j["overreach"] or j["interpretation"] or j["unit_errors"] or j["missing"]
        ),
        "judge_quotes_unverified": sum(
            1 for j in judged if not j["quotes_verified"]
        ),
        "cross_check_divergences": sum(
            1 for c in crossed if c["only_in_a"] or c["only_in_b"]
        ),
        "cross_check_ungrounded": sum(1 for c in crossed if not c["b_grounded"]),
    }
    return out


class RunResult:
    def __init__(
        self,
        config: ReportConfig,
        packets: dict[str, SectionPacket],
        sections: dict[str, GeneratedSection],
        grounding: dict[str, GroundingResult],
        manifest: dict[str, Any],
        outputs: list[Path],
        evaluation: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.packets = packets
        self.sections = sections
        self.grounding = grounding
        self.manifest = manifest
        self.outputs = outputs
        self.evaluation = evaluation or {}

    @property
    def blocked(self) -> list[str]:
        return [k for k, r in self.grounding.items() if not r.passed]

    @property
    def needs_review(self) -> list[str]:
        return [k for k, r in self.grounding.items() if r.needs_review]


def load_review(out_dir: Path) -> dict[str, Any]:
    path = out_dir / REVIEW_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_review(out_dir: Path, review: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REVIEW_FILE).write_text(
        json.dumps(review, indent=2, default=str), encoding="utf-8"
    )


def run(
    config_path: str | Path,
    data_path: str | Path,
    out_dir: str | Path = "outputs",
    *,
    client: LLMClient | None = None,
    require_approval: bool = False,
    sections_only: list[str] | None = None,
    model: str | None = None,
    evaluate: str = "none",
    evaluate_sample: float = 1.0,
) -> RunResult:
    started = datetime.now(UTC)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(config_path)
    frame = load_cases(data_path)
    store = run_analyses(frame, config.required_analyses, config.analysis_params)

    assembler = Assembler(config)
    packets = {s.id: assembler.assemble(s.id, store) for s in config.sections}

    # Full evidence, case IDs retained. This is the audit record, never a prompt.
    (out_dir / "evidence.json").write_text(
        store.model_dump_json(indent=2), encoding="utf-8"
    )
    packet_dir = out_dir / "packets"
    packet_dir.mkdir(exist_ok=True)
    for sid, packet in packets.items():
        (packet_dir / f"{sid}.json").write_text(
            packet.model_dump_json(indent=2), encoding="utf-8"
        )
        if packet.instructions:
            (packet_dir / f"{sid}.prompt.txt").write_text(
                packet.instructions, encoding="utf-8"
            )

    all_generated = [s for s in config.sections if s.mode == "generated"]
    total_generated = len(all_generated)
    targets = [
        s for s in all_generated if not sections_only or s.id in sections_only
    ]
    # A partial run is a development aid, not a report. Rendering a document
    # missing most of its sections would produce something that looks complete
    # and is not, which is the most dangerous artifact this system could emit.
    partial = bool(sections_only) and len(targets) < total_generated
    if sections_only:
        unknown = set(sections_only) - {s.id for s in all_generated}
        if unknown:
            raise RuntimeError(
                f"unknown or non-generated sections requested: {sorted(unknown)}"
            )
    generator = Generator(config.model, client=client)
    validator = GroundingValidator()

    generated: dict[str, GeneratedSection] = {}
    grounding: dict[str, GroundingResult] = {}
    for section in targets:
        packet = packets[section.id]
        gen = generator.generate(packet, model=model)
        generated[section.id] = gen
        grounding[section.id] = validator.validate(gen, packet)
        print(f"  {grounding[section.id].summary()}", file=sys.stderr)

    section_dir = out_dir / "sections"
    section_dir.mkdir(exist_ok=True)
    for sid, gen in generated.items():
        (section_dir / f"{sid}.json").write_text(
            gen.model_dump_json(indent=2), encoding="utf-8"
        )
    (out_dir / "grounding.json").write_text(
        json.dumps(
            {k: json.loads(v.model_dump_json()) for k, v in grounding.items()},
            indent=2,
        ),
        encoding="utf-8",
    )

    blocked = [k for k, r in grounding.items() if not r.passed]
    if blocked:
        raise RuntimeError(
            "grounding failed; report not rendered. Blocked sections: "
            + ", ".join(blocked)
            + "\nSee grounding.json. A human may approve prose, not numbers."
        )

    evaluation = run_evaluation(
        config,
        packets,
        generated,
        grounding,
        mode=evaluate,
        sample=evaluate_sample,
        client=client,
    )
    if evaluation:
        (out_dir / "evaluation.json").write_text(
            json.dumps(evaluation, indent=2), encoding="utf-8"
        )

    review = load_review(out_dir)
    unapproved = [
        sid
        for sid in generated
        if review.get(sid, {}).get("status") != "approved"
    ]
    if require_approval and unapproved:
        save_review(
            out_dir,
            {
                sid: review.get(
                    sid, {"status": "pending", "reviewer": None, "note": ""}
                )
                for sid in generated
            },
        )
        raise RuntimeError(
            "approval required but these sections are unapproved: "
            + ", ".join(unapproved)
            + f"\nReview them in {out_dir / REVIEW_FILE} or run the Streamlit app."
        )

    case_item = store.items.get("case_index")
    if case_item is not None:
        write_case_index_csv(
            case_item.rows, case_item.columns, out_dir / "case_index.csv"
        )
        from evidentia.render import render_case_table

        render_case_table._rows = case_item.rows

    status = "FINAL" if (not unapproved and generated) else "DRAFT — not human approved"
    if partial:
        status = (
            f"PARTIAL — {len(generated)} of {total_generated} generated sections; "
            "not a report"
        )
    manifest = {
        "status": status,
        "generated_at": utc_stamp(),
        "duration_seconds": round(
            (datetime.now(UTC) - started).total_seconds(), 1
        ),
        "report_type": config.report_type,
        "config": str(config_path),
        "dataset": frame.validation.source_file,
        "dataset_sha256": frame.validation.source_sha256,
        "period": f"{frame.validation.period_start} to {frame.validation.period_end}",
        "cases": frame.n_cases,
        "reaction_events": frame.n_reaction_events,
        "model": config.model.name,
        "temperature": config.model.temperature,
        "seed": config.model.seed,
        "analyses_run": len(store),
        "sections_generated": len(generated),
        "sections_deterministic": sum(
            1 for s in config.sections if s.mode == "deterministic"
        ),
        "numbers_checked": sum(len(r.numbers_found) for r in grounding.values()),
        "numbers_ungrounded": sum(
            len(r.numbers_ungrounded) for r in grounding.values()
        ),
        "sections_needing_review": [
            k for k, r in grounding.items() if r.needs_review
        ],
        "output_modes": sorted({g.output_mode for g in generated.values()}),
        "prompt_tokens": sum(g.prompt_tokens for g in generated.values()),
        "completion_tokens": sum(g.completion_tokens for g in generated.values()),
        "evaluation": evaluation.get("summary", {"mode": "not run"}),
        "section_hashes": {
            sid: {"packet": g.packet_sha256[:16], "prompt": g.prompt_sha256[:16]}
            for sid, g in generated.items()
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    if partial:
        print(
            f"\npartial run: {len(generated)} of {total_generated} sections. "
            "No report rendered — artifacts are in "
            f"{out_dir / 'sections'} and {out_dir / 'packets'}.",
            file=sys.stderr,
        )
        return RunResult(config, packets, generated, grounding, manifest, [], evaluation)

    rendered = build_sections(config, packets, generated)
    outputs = render_report(config, rendered, manifest, out_dir)

    save_review(
        out_dir,
        {
            sid: review.get(
                sid, {"status": "pending", "reviewer": None, "note": ""}
            )
            for sid in generated
        },
    )

    return RunResult(
        config, packets, generated, grounding, manifest, outputs, evaluation
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evidentia",
        description="Generate a controlled, traceable regulatory report.",
    )
    parser.add_argument("--config", default="configs/pader_fda.yaml")
    parser.add_argument(
        "--data", default="data/Bisoprolol_icsr_sample_1068rows.xlsx"
    )
    parser.add_argument("--out", default="outputs")
    parser.add_argument("--model", default=None, help="override the config model")
    parser.add_argument(
        "--sections", nargs="*", default=None, help="generate only these sections"
    )
    parser.add_argument(
        "--evaluate",
        choices=["none", "cross", "judge", "full"],
        default="none",
        help=(
            "advisory tiers 2 and 3; never blocks. cross=second model, "
            "judge=rubric, full=both"
        ),
    )
    parser.add_argument(
        "--evaluate-sample",
        type=float,
        default=1.0,
        help=(
            "fraction of unflagged sections to evaluate; sections already "
            "flagged by grounding are always included"
        ),
    )
    parser.add_argument(
        "--require-approval",
        action="store_true",
        help="refuse to render until every section is approved in review.json",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass

    print(f"evidentia: {args.config} + {args.data}", file=sys.stderr)
    try:
        result = run(
            args.config,
            args.data,
            args.out,
            require_approval=args.require_approval,
            sections_only=args.sections,
            model=args.model,
            evaluate=args.evaluate,
            evaluate_sample=args.evaluate_sample,
        )
    except RuntimeError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1

    m = result.manifest
    print(
        f"\n{m['status']} — {m['sections_generated']} generated, "
        f"{m['sections_deterministic']} deterministic, "
        f"{m['numbers_checked']} numbers checked, "
        f"{m['numbers_ungrounded']} ungrounded",
        file=sys.stderr,
    )
    if result.needs_review:
        print(f"needs review: {', '.join(result.needs_review)}", file=sys.stderr)
    if result.evaluation:
        s = result.evaluation["summary"]
        print(
            f"evaluation: {s['sections_evaluated']} sections, "
            f"{s['judge_concerns']} judge concerns, "
            f"{s['cross_check_divergences']} cross-check divergences "
            "(advisory, non-blocking)",
            file=sys.stderr,
        )
    for path in result.outputs:
        print(f"  {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
