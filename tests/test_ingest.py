"""Phase 1 tests.

Two kinds of test here:

  unit          run on synthetic frames, always execute, no dataset needed
  ground-truth  assert the exact figures established by Phase 0 profiling,
                skipped automatically when the dataset is absent (CI has no data)

The ground-truth tests are the point. They pin the numbers the whole report
depends on, so a regression in ingest fails loudly instead of producing a
plausible but wrong report.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from evidentia.ingest import _explode_reactions, _split, band_age, load_cases

DATA = Path(
    os.environ.get("EVIDENTIA_DATA", "data/Bisoprolol_icsr_sample_1068rows.xlsx")
)
needs_data = pytest.mark.skipif(
    not DATA.exists(), reason=f"dataset not present at {DATA}"
)


@pytest.fixture(scope="module")
def frame():
    if not DATA.exists():
        pytest.skip("dataset not present")
    return load_cases(DATA)


# --------------------------------------------------------------------------
# Ground truth — Phase 0 profiling, docs/OUTCOMES.md
# --------------------------------------------------------------------------


@needs_data
def test_raw_row_count(frame):
    assert frame.validation.raw_rows == 1068


@needs_data
def test_unique_case_count(frame):
    assert frame.n_cases == 1024
    assert frame.validation.rows_dropped_as_superseded == 44


@needs_data
def test_reporting_period(frame):
    assert str(frame.validation.period_start) == "2024-12-27"
    assert str(frame.validation.period_end) == "2025-12-26"


@needs_data
def test_reaction_events_match_reference_pader(frame):
    """The load-bearing assertion.

    Comma-splitting the reaction column across all raw rows yields exactly the
    total reported by the supplied reference PADER. If this breaks, the split
    model is wrong and every reaction figure in the report is wrong with it.
    """
    assert frame.validation.reaction_events_raw == 3648


@needs_data
def test_deduped_reaction_events(frame):
    """3,429 after version-dedup, against 3,648 raw.

    The reference PADER reports 3,648, i.e. it does not collapse superseded
    report versions. We diverge deliberately (D-012, D-017): 44 superseded rows
    carried 219 reaction events that would otherwise be double-counted.
    """
    assert frame.n_reaction_events == 3429
    assert frame.validation.reaction_events_raw - frame.n_reaction_events == 219


@needs_data
def test_data_quality_issues_are_all_surfaced(frame):
    codes = {i.code for i in frame.validation.issues}
    assert codes == {
        "superseded_versions_dropped",
        "age_unit_unrecognised",
        "age_unavailable",
        "duplicate_flag_present",
        "country_disagreement",
        "outcome_misaligned",
    }
    counts = {i.code: i.count for i in frame.validation.issues}
    assert counts["age_unavailable"] == 86
    assert counts["duplicate_flag_present"] == 197
    assert counts["outcome_misaligned"] == 6


@needs_data
def test_serious_case_split(frame):
    counts = frame.cases["serious"].value_counts()
    assert counts.get("serious", 0) == 1023
    assert counts.get("not serious", 0) == 1


@needs_data
def test_seriousness_flags_overlap(frame):
    """Flags are independent, not mutually exclusive (Appendix A)."""
    flags = [
        "seriousnessdeath",
        "seriousnesslifethreatening",
        "seriousnesshospitalization",
        "seriousnessdisabling",
        "seriousnesscongenitalanomali",
        "seriousnessother",
    ]
    total = sum((frame.cases[f] == "yes").sum() for f in flags)
    serious = (frame.cases["serious"] == "serious").sum()
    assert total > serious


@needs_data
def test_age_is_normalised_not_raw(frame):
    """Month/week/day-unit rows must not appear as adult ages."""
    ages = frame.cases["age_years"].dropna()
    assert ages.max() <= 120
    assert ages.min() >= 0
    assert frame.cases["age_band"].isin(
        {"<18", "18-44", "45-64", "65-74", "75-84", "85+", "unknown"}
    ).all()


@needs_data
def test_corrupt_age_unit_quarantined(frame):
    codes = {i.code for i in frame.validation.issues}
    assert "age_unit_unrecognised" in codes


@needs_data
def test_duplicate_flag_surfaced_not_removed(frame):
    codes = {i.code for i in frame.validation.issues}
    assert "duplicate_flag_present" in codes
    assert frame.n_cases == 1024, "duplicate-flagged cases must NOT be dropped"


@needs_data
def test_every_reaction_traces_to_a_case(frame):
    assert set(frame.reactions["safetyreportid"]) <= set(
        frame.cases["safetyreportid"]
    )


@needs_data
def test_outcome_vocabulary_is_small_after_split(frame):
    """Pre-split the column had 251 uniques; post-split it is a real vocabulary."""
    assert frame.reactions["reaction_outcome"].nunique() <= 12


@needs_data
def test_validation_report_is_serialisable(frame):
    payload = frame.validation.model_dump_json()
    assert "source_sha256" in payload


# --------------------------------------------------------------------------
# Unit tests — no dataset required
# --------------------------------------------------------------------------


def test_split_handles_blanks_and_whitespace():
    assert _split("Headache, Nausea ,Fatigue") == ["Headache", "Nausea", "Fatigue"]
    assert _split(None) == []
    assert _split(float("nan")) == []
    assert _split("") == []


@pytest.mark.parametrize(
    "years,expected",
    [
        (0.5, "<18"),
        (17.9, "<18"),
        (18.0, "18-44"),
        (64.9, "45-64"),
        (65.0, "65-74"),
        (85.0, "85+"),
        (104.0, "85+"),
        (None, "unknown"),
        (float("nan"), "unknown"),
    ],
)
def test_age_banding(years, expected):
    assert band_age(years) == expected


def _mini(pt: str, outcome: str) -> pd.DataFrame:
    return pd.DataFrame(
        [{"safetyreportid": 1, "patient_reaction_reactionmeddrapt": pt,
          "patient_reaction_reactionoutcome": outcome}]
    )


def test_explode_aligns_equal_length_lists():
    out = _explode_reactions(_mini("A,B", "fatal,unknown"), [])
    assert list(out["reaction_pt"]) == ["A", "B"]
    assert list(out["reaction_outcome"]) == ["fatal", "unknown"]
    assert out["outcome_aligned"].all()


def test_explode_broadcasts_single_outcome():
    out = _explode_reactions(_mini("A,B,C", "unknown"), [])
    assert list(out["reaction_outcome"]) == ["unknown"] * 3


def test_explode_flags_misalignment_instead_of_truncating():
    issues = []
    out = _explode_reactions(_mini("A,B,C", "fatal,unknown"), issues)
    assert len(out) == 3, "must not silently drop the third reaction"
    assert list(out["reaction_outcome"]) == ["unknown"] * 3
    assert not out["outcome_aligned"].any()
    assert any(i.code == "outcome_misaligned" for i in issues)


def test_explode_skips_rows_with_no_reaction():
    assert _explode_reactions(_mini("", "fatal"), []).empty
