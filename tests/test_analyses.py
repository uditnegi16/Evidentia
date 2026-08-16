"""Phase 2 tests.

The load-bearing test in this file is test_no_case_ids_reach_the_prompt. Every
other test checks a number; that one checks a boundary, and the boundary is the
architecture.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evidentia.analyses import catalogue, registered, run_analyses
from evidentia.evidence import Bucket, EvidenceItem, EvidenceStore, Provenance
from evidentia.ingest import load_cases

DATA = Path(
    os.environ.get("EVIDENTIA_DATA", "data/Bisoprolol_icsr_sample_1068rows.xlsx")
)
needs_data = pytest.mark.skipif(
    not DATA.exists(), reason=f"dataset not present at {DATA}"
)

ALL = [
    "period_bounds",
    "total_cases",
    "total_reaction_events",
    "serious_split",
    "seriousness_criteria",
    "alert_cases",
    "age_bands",
    "sex_distribution",
    "country_distribution",
    "reporter_type",
    "top_reactions",
    "top_reactions_by_case",
    "top_serious_reactions",
    "reaction_outcomes",
    "reactions_by_sex",
    "monthly_case_volume",
    "monthly_top_reactions",
    "safety_actions",
    "data_quality",
    "case_index",
]


@pytest.fixture(scope="module")
def frame():
    if not DATA.exists():
        pytest.skip("dataset not present")
    return load_cases(DATA)


@pytest.fixture(scope="module")
def store(frame) -> EvidenceStore:
    return run_analyses(frame, ALL)


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


@needs_data
def test_no_case_ids_reach_the_prompt(store):
    """The projection must strip provenance IDs from every item.

    Case IDs exist so a human can audit a claim. They must never enter the
    model's context: they are pure noise to a language task and they inflate
    every call. This asserts the boundary structurally rather than trusting
    that no caller ever serialises the wrong object.
    """
    real_ids = {
        cid
        for item in store.items.values()
        for cid in item.provenance.case_ids
    }
    assert real_ids, "fixture is meaningless if no item carries IDs"

    for key, item in store.items.items():
        payload = json.dumps(item.to_prompt_dict(), default=str)
        assert "case_ids" not in payload, f"{key} leaked the case_ids field"
        for cid in list(real_ids)[:200]:
            assert str(cid) not in payload, f"{key} leaked case id {cid}"


@needs_data
def test_audit_dump_retains_case_ids(store):
    """The other half of the boundary: the full record keeps everything.

    case_ids live on provenance, so model_dump() nests them one level down.
    """
    item = store.get("total_cases")
    assert len(item.provenance.case_ids) == 1024
    dumped = item.model_dump()
    assert "case_ids" in dumped["provenance"]
    assert len(dumped["provenance"]["case_ids"]) == 1024


@needs_data
def test_table_rows_never_reach_the_prompt(store):
    """A 1,024-row listing is rendered, not narrated."""
    payload = store.get("case_index").to_prompt_dict()
    assert "rows" not in payload
    assert payload["row_count"] == 1024
    assert len(store.get("case_index").rows) == 1024


@needs_data
def test_prompt_dict_can_truncate_long_distributions(store):
    full = store.get("top_reactions").to_prompt_dict()
    short = store.get("top_reactions").to_prompt_dict(max_buckets=5)
    assert len(short["buckets"]) == 5
    assert short["buckets_omitted"] == len(full["buckets"]) - 5


@needs_data
def test_every_item_declares_its_unit_to_the_model(store):
    """Mixing case and reaction-event counts is the arithmetic failure mode."""
    for key, item in store.items.items():
        prov = item.to_prompt_dict()["provenance"]
        assert prov["unit"] in {"case", "reaction_event"}, key


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------


@needs_data
def test_total_cases(store):
    assert store.get("total_cases").value == 1024


@needs_data
def test_total_reaction_events(store):
    assert store.get("total_reaction_events").value == 3429


@needs_data
def test_reaction_outcomes_partition_all_events(store):
    got = {b.label: b.count for b in store.get("reaction_outcomes").buckets}
    assert got["recovered/resolved"] == 1257
    assert got["unknown"] == 1086
    assert got["not recovered/not resolved/ongoing"] == 512
    assert got["recovering/resolving"] == 406
    assert got["fatal"] == 134
    assert got["recovered/resolved with sequelae"] == 34
    assert sum(got.values()) == 3429


@needs_data
def test_serious_split(store):
    b = {x.label: x.count for x in store.get("serious_split").buckets}
    assert b["serious"] == 1023
    assert b["not serious"] == 1


@needs_data
def test_alert_cases(store):
    assert store.get("alert_cases").value == 1023


@needs_data
def test_seriousness_criteria_overlap(store):
    item = store.get("seriousness_criteria")
    total = sum(b.count for b in item.buckets)
    assert total > 1023, "criteria are not mutually exclusive"
    assert any("not sum to 100" in n for n in item.provenance.notes)


@needs_data
def test_reporting_period(store):
    assert store.get("period_bounds").value == "2024-12-27 to 2025-12-26"


@needs_data
def test_top_reactions_match_the_reference_pader(store):
    """Post-dedup case counts against the reference PADER's Case Presentation.

    Reference states: AKI 80, Drug ineffective 53, Hypotension 46,
    Drug interaction 43, Fatigue 33. Four of five reproduce exactly; Drug
    ineffective differs by one and is recorded as an open discrepancy (E-011).
    Pre-dedup these were 81/60/48/45/35 — all wrong — so this is the assertion
    that validates the version-dedup policy end to end.
    """
    got = {b.label: b.count for b in store.get("top_reactions_by_case").buckets}
    assert got["Acute kidney injury"] == 80
    assert got["Hypotension"] == 46
    assert got["Drug interaction"] == 43
    assert got["Fatigue"] == 33
    assert got["Drug ineffective"] == 54  # reference says 53; see E-011


@needs_data
def test_event_and_case_counts_coincide_on_this_dataset(store):
    """Documents a data property that must never become a silent assumption.

    No case reports the same PT twice here, so event-level and case-level PT
    counts are equal. If a future dataset breaks this, the two analyses diverge
    and this test says so rather than letting a 'N cases of X' sentence quietly
    read an event count.
    """
    events = {b.label: b.count for b in store.get("top_reactions").buckets}
    cases = {b.label: b.count for b in store.get("top_reactions_by_case").buckets}
    shared = set(events) & set(cases)
    assert shared
    for pt in shared:
        assert events[pt] == cases[pt], f"{pt} diverges: {events[pt]} vs {cases[pt]}"


@needs_data
def test_case_level_reactions_declare_case_unit(store):
    """The unit is what stops a model dividing 80 by 3429."""
    item = store.get("top_reactions_by_case")
    assert item.to_prompt_dict()["provenance"]["unit"] == "case"
    assert item.provenance.denominator == 1024


@needs_data
def test_distributions_sum_to_their_denominator(store):
    """Partitions must be exhaustive. Non-partitions are excluded by name."""
    partitions = [
        "serious_split",
        "age_bands",
        "sex_distribution",
        "reporter_type",
        "reaction_outcomes",
        "monthly_case_volume",
    ]
    for key in partitions:
        item = store.get(key)
        assert sum(b.count for b in item.buckets) == item.provenance.denominator, key


@needs_data
def test_safety_actions_states_absence_rather_than_omitting(store):
    item = store.get("safety_actions")
    assert item.provenance.n_contributing == 0
    assert "No safety-related action data" in str(item.value)


@needs_data
def test_age_bands_cover_every_case(store):
    labels = {b.label for b in store.get("age_bands").buckets}
    assert labels <= {"<18", "18-44", "45-64", "65-74", "75-84", "85+", "unknown"}


@needs_data
def test_buckets_carry_traceable_case_ids(store):
    for b in store.get("age_bands").buckets:
        if b.count and b.label != "unknown":
            assert b.case_ids, f"bucket {b.label} has no provenance"


@needs_data
def test_case_index_traces_every_case(store, frame):
    ids = {r["case_id"] for r in store.get("case_index").rows}
    assert ids == {int(c) for c in frame.cases["safetyreportid"]}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_all_expected_analyses_are_registered():
    assert set(ALL) <= set(registered())


def test_catalogue_is_serialisable():
    entries = catalogue()
    assert entries
    json.dumps(entries)
    assert all({"key", "label", "unit"} <= set(e) for e in entries)


def test_unknown_analysis_raises_at_selection_time():
    from evidentia.analyses.registry import get_spec

    with pytest.raises(KeyError, match="unknown analysis"):
        get_spec("does_not_exist")


def test_duplicate_registration_is_rejected():
    from evidentia.analyses.registry import analysis

    with pytest.raises(ValueError, match="already registered"):

        @analysis("total_cases", "dupe", unit="case")
        def _dupe(frame):
            raise NotImplementedError


@needs_data
def test_params_override_registry_defaults(frame):
    s = run_analyses(frame, ["top_reactions"], params={"top_reactions": {"top_n": 3}})
    assert len(s.get("top_reactions").buckets) == 3


@needs_data
def test_requesting_the_same_analysis_twice_is_idempotent(frame):
    s = run_analyses(frame, ["total_cases", "total_cases"])
    assert len(s) == 1


# --------------------------------------------------------------------------
# Store and contracts — no dataset required
# --------------------------------------------------------------------------


def _item(key: str = "k", value: int = 10) -> EvidenceItem:
    return EvidenceItem(
        key=key,
        label="L",
        kind="scalar",
        value=value,
        provenance=Provenance(
            unit="case",
            method="m",
            source_columns=["c"],
            n_contributing=1,
            denominator=100,
            case_ids=[999001, 999002],
        ),
    )


def test_store_rejects_duplicate_keys():
    s = EvidenceStore()
    s.add(_item())
    with pytest.raises(ValueError, match="duplicate evidence key"):
        s.add(_item())


def test_store_subset_raises_on_missing_key():
    s = EvidenceStore()
    s.add(_item("a"))
    with pytest.raises(KeyError, match="not computed"):
        s.subset(["a", "b"])


def test_store_subset_returns_only_requested():
    s = EvidenceStore()
    s.add(_item("a"))
    s.add(_item("b"))
    assert set(s.subset(["a"])) == {"a"}


def test_numeric_claims_include_value_and_denominator():
    assert {10.0, 100.0} <= _item().numeric_claims()


def test_numeric_claims_include_bucket_counts_and_pcts():
    item = EvidenceItem(
        key="k",
        label="L",
        kind="distribution",
        buckets=[Bucket(label="x", count=7, pct=12.5)],
        provenance=Provenance(
            unit="case", method="m", source_columns=[], n_contributing=7
        ),
    )
    claims = item.numeric_claims()
    assert 7.0 in claims
    assert 12.5 in claims


def test_prompt_dict_omits_none_value():
    item = EvidenceItem(
        key="k",
        label="L",
        kind="distribution",
        provenance=Provenance(
            unit="case", method="m", source_columns=[], n_contributing=0
        ),
    )
    assert "value" not in item.to_prompt_dict()


# --------------------------------------------------------------------------
# Packet figures the model may legitimately quote (E-015)
# --------------------------------------------------------------------------


def test_numeric_claims_harvest_bucket_labels():
    """A band called "65-74" puts 65 and 74 in front of the model."""
    item = EvidenceItem(
        key="age_bands",
        label="Age",
        kind="distribution",
        buckets=[Bucket(label="65-74", count=300), Bucket(label="85+", count=90)],
        provenance=Provenance(
            unit="case", method="m", source_columns=[], n_contributing=390
        ),
    )
    claims = item.numeric_claims()
    assert {65.0, 74.0, 85.0} <= claims


def test_numeric_claims_harvest_month_labels():
    item = EvidenceItem(
        key="monthly_case_volume",
        label="Monthly",
        kind="timeseries",
        buckets=[Bucket(label="2025-03", count=88)],
        provenance=Provenance(
            unit="case", method="m", source_columns=[], n_contributing=88
        ),
    )
    assert {2025.0, 3.0, 88.0} <= item.numeric_claims()


def test_n_contributing_is_claimable_and_visible_to_the_model():
    """Withholding it made a model reconstruct it by subtraction."""
    item = EvidenceItem(
        key="age_bands",
        label="Age",
        kind="distribution",
        buckets=[Bucket(label="unknown", count=86)],
        provenance=Provenance(
            unit="case",
            method="m",
            source_columns=[],
            n_contributing=938,
            denominator=1024,
        ),
    )
    assert 938.0 in item.numeric_claims()
    assert item.to_prompt_dict()["provenance"]["n_contributing"] == 938


def test_label_harvesting_does_not_admit_arbitrary_numbers():
    item = EvidenceItem(
        key="k",
        label="L",
        kind="distribution",
        buckets=[Bucket(label="65-74", count=300)],
        provenance=Provenance(
            unit="case", method="m", source_columns=[], n_contributing=300
        ),
    )
    claims = item.numeric_claims()
    assert 999.0 not in claims
    assert 1024.0 not in claims


@needs_data
def test_age_known_count_is_available_without_arithmetic(store):
    assert 938.0 in store.get("age_bands").numeric_claims()


@needs_data
def test_data_quality_labels_are_readable_not_internal_codes(store):
    """The model can only be as readable as its packet.

    Raw issue codes produced prose like "the duplicate_flag_present warning was
    raised", which is log output, not a regulatory sentence.
    """
    labels = [b.label for b in store.get("data_quality").buckets]
    assert labels, "data quality should report findings"
    for label in labels:
        assert "_" not in label, f"internal code leaked into the packet: {label}"
        assert "(" not in label, f"severity marker leaked into the packet: {label}"
    assert any("duplicate flag" in x for x in labels)
    assert any("superseded" in x for x in labels)
