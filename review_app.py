"""Human review.

Run:  streamlit run review_app.py

Shows each generated section beside the exact evidence packet that produced it,
the grounding verdict, and the prompt that was sent. Approve or flag writes to
review.json, which `python -m evidentia.run --require-approval` reads before it
will render anything as FINAL.

Why side-by-side: the reviewer's question is never "does this read well", it is
"is this sentence backed by something". Putting prose and evidence on one screen
makes that question answerable in seconds. Making the reviewer open two files to
answer it means they will stop answering it.

The app is a view over artifacts on disk. It runs no analyses and calls no
model, so review can happen on a different machine, later, by someone who never
ran the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Evidentia — review", layout="wide")

SEVERITY_ICON = {"blocking": "🛑", "review": "⚠️"}
STATUS_ICON = {"approved": "✅", "flagged": "🚩", "pending": "⬜"}


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_review(out_dir: Path, review: dict) -> None:
    (out_dir / "review.json").write_text(
        json.dumps(review, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Sidebar — pick a run
# --------------------------------------------------------------------------

st.sidebar.title("Evidentia")
out_dir = Path(st.sidebar.text_input("Run directory", "outputs"))

if not (out_dir / "manifest.json").exists():
    st.warning(
        f"No run found in `{out_dir}`.\n\n"
        "Generate one first:\n\n"
        "```\npython -m evidentia.run\n```"
    )
    st.stop()

manifest = read_json(out_dir / "manifest.json", {})
grounding = read_json(out_dir / "grounding.json", {})
review = read_json(out_dir / "review.json", {})

sections = {}
for path in sorted((out_dir / "sections").glob("*.json")):
    sections[path.stem] = read_json(path, {})

status = manifest.get("status", "unknown")
st.sidebar.markdown(f"**{manifest.get('report_type', '?')}** — {status}")
st.sidebar.caption(manifest.get("period", ""))

approved = sum(1 for v in review.values() if v.get("status") == "approved")
st.sidebar.progress(
    approved / len(sections) if sections else 0.0,
    text=f"{approved} of {len(sections)} approved",
)

with st.sidebar.expander("Run provenance"):
    for key in (
        "dataset",
        "dataset_sha256",
        "model",
        "temperature",
        "seed",
        "cases",
        "reaction_events",
        "analyses_run",
        "numbers_checked",
        "numbers_ungrounded",
        "output_modes",
        "generated_at",
    ):
        if key in manifest:
            value = manifest[key]
            if key.endswith("sha256"):
                value = str(value)[:16] + "…"
            st.caption(f"**{key}** · {value}")

st.sidebar.divider()
if st.sidebar.button("Approve all remaining", use_container_width=True):
    for sid in sections:
        if review.get(sid, {}).get("status") != "approved":
            review[sid] = {
                "status": "approved",
                "reviewer": "bulk",
                "note": "bulk approval",
            }
    save_review(out_dir, review)
    st.rerun()

if st.sidebar.button("Reset all to pending", use_container_width=True):
    save_review(
        out_dir,
        {s: {"status": "pending", "reviewer": None, "note": ""} for s in sections},
    )
    st.rerun()

reviewer = st.sidebar.text_input("Reviewer", "udit")

# --------------------------------------------------------------------------
# Main — one tab per section
# --------------------------------------------------------------------------

st.title(manifest.get("report_type", "Report") + " — section review")

if manifest.get("numbers_ungrounded", 0):
    st.error(
        f"{manifest['numbers_ungrounded']} ungrounded numbers in this run. "
        "Grounding failures block rendering and cannot be approved away."
    )
else:
    st.success(
        f"{manifest.get('numbers_checked', 0)} numeric claims checked, "
        "all traced to evidence. Approval covers wording and judgement only."
    )

tabs = st.tabs(
    [
        f"{STATUS_ICON.get(review.get(s, {}).get('status', 'pending'), '⬜')} "
        f"{s.replace('_', ' ')}"
        for s in sections
    ]
)

for tab, (sid, section) in zip(tabs, sections.items(), strict=False):
    with tab:
        packet = read_json(out_dir / "packets" / f"{sid}.json", {})
        verdict = grounding.get(sid, {})
        current = review.get(sid, {"status": "pending", "note": ""})

        left, right = st.columns([3, 2], gap="large")

        with left:
            st.subheader(section.get("title", sid))
            st.markdown(section.get("prose", "_no prose_"))

            st.caption(
                f"{section.get('word_count', len(section.get('prose', '').split()))} "
                f"words · {len(verdict.get('numbers_found', []))} numeric claims · "
                f"model {section.get('model', '?')} · "
                f"output mode {section.get('output_mode', '?')} · "
                f"attempt {section.get('attempts', 1)}"
            )

            issues = verdict.get("issues", [])
            if not issues:
                st.success("No grounding issues.")
            for issue in issues:
                icon = SEVERITY_ICON.get(issue["severity"], "•")
                line = f"{icon} **{issue['code']}** — {issue['detail']}"
                if issue["severity"] == "blocking":
                    st.error(line)
                else:
                    st.warning(line)

            st.divider()
            note = st.text_area(
                "Reviewer note", value=current.get("note", ""), key=f"note_{sid}"
            )
            c1, c2, c3 = st.columns(3)
            if c1.button("Approve", key=f"ok_{sid}", use_container_width=True):
                review[sid] = {
                    "status": "approved",
                    "reviewer": reviewer,
                    "note": note,
                }
                save_review(out_dir, review)
                st.rerun()
            if c2.button("Flag", key=f"flag_{sid}", use_container_width=True):
                review[sid] = {
                    "status": "flagged",
                    "reviewer": reviewer,
                    "note": note,
                }
                save_review(out_dir, review)
                st.rerun()
            if c3.button("Reset", key=f"reset_{sid}", use_container_width=True):
                review[sid] = {"status": "pending", "reviewer": None, "note": ""}
                save_review(out_dir, review)
                st.rerun()

            st.caption(
                f"Status: **{current.get('status', 'pending')}**"
                + (
                    f" by {current['reviewer']}"
                    if current.get("reviewer")
                    else ""
                )
            )

        with right:
            st.subheader("Evidence available to this section")
            st.caption(
                "This is everything the model could see. Any figure in the "
                "prose that is not here would have blocked the run."
            )
            for key, item in (packet.get("evidence") or {}).items():
                with st.expander(f"{key} · {item.get('provenance', {}).get('unit', '')}"):
                    st.json(item, expanded=False)

            allowed = packet.get("allowed_numbers", [])
            with st.expander(f"Permitted figures ({len(allowed)})"):
                st.write(
                    ", ".join(f"{n:g}" for n in allowed) or "none"
                )

            with st.expander("Prompt sent to the model"):
                st.code(packet.get("instructions", ""), language="text")

            with st.expander("Rules in force"):
                for rule in packet.get("rules", []):
                    st.caption(f"• {rule}")

st.divider()
st.caption(
    "Approval records a human judgement about wording and interpretation. "
    "It cannot override a grounding failure — fabricated figures block the "
    "render regardless of who approves them."
)
