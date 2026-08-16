"""Phase 5 tests.

The central fixture is REAL_PROSE — the actual first output of the system,
produced by gpt-oss-120b from the narrative_summary packet. Testing the gate
against genuine model output rather than invented strings is the difference
between checking that the regex works and checking that the gate works.

Every test runs offline.
"""

from __future__ import annotations

import pytest

from evidentia.assembler import SectionPacket
from evidentia.generate import GeneratedSection
from evidentia.grounding import (
    GroundingValidator,
    extract_numbers,
    report_is_renderable,
)

REAL_PROSE = (
    "The reporting period was 2024-12-27 to 2025-12-26 and a total of 1024 cases "
    "were received. Of these cases, 1023 were classified as serious and 1 was "
    "classified as not serious; the near-total serious proportion is typical for "
    "spontaneous individual case safety report data. The most frequently reported "
    "reactions, counted by distinct cases, were acute kidney injury (80 cases), "
    "drug ineffective (54 cases), hypotension (46 cases), drug interaction "
    "(43 cases), dyspnoea (38 cases), bradycardia (37 cases), dizziness "
    "(36 cases), and fatigue (33 cases). Reaction outcomes, recorded by reaction "
    "event, were recovered/resolved (1257 events), unknown (1086 events), not "
    "recovered/not resolved/ongoing (512 events), recovering/resolving "
    "(406 events), fatal (134 events), and recovered/resolved with sequelae "
    "(34 events)."
)

ALLOWED = [
    1.0, 33.0, 34.0, 36.0, 37.0, 38.0, 43.0, 46.0, 54.0, 80.0,
    134.0, 406.0, 512.0, 1023.0, 1024.0, 1086.0, 1257.0, 3429.0,
]


def packet(**kw) -> SectionPacket:
    defaults = dict(
        section_id="narrative_summary",
        title="Narrative Summary and Analysis",
        mode="generated",
        report_type="PADER",
        product_name="Bisoprolol",
        evidence={
            "age_bands": {
                "buckets": [
                    {"label": "65-74", "count": 300},
                    {"label": "85+", "count": 90},
                    {"label": "<18", "count": 4},
                ]
            }
        },
        instructions="...",
        rules=[],
        forbidden_phrases=["no safety concerns", "confirmed signal", "causally related"],
        max_words=320,
        allowed_numbers=ALLOWED,
        evidence_keys=[
            "period_bounds",
            "total_cases",
            "serious_split",
            "top_reactions_by_case",
            "reaction_outcomes",
        ],
    )
    defaults.update(kw)
    return SectionPacket(**defaults)


def section(prose: str = REAL_PROSE, **kw) -> GeneratedSection:
    defaults = dict(
        section_id="narrative_summary",
        title="Narrative Summary and Analysis",
        prose=prose,
        evidence_used=[
            "period_bounds",
            "total_cases",
            "serious_split",
            "top_reactions_by_case",
            "reaction_outcomes",
        ],
        flags=[],
        output_mode="strict",
    )
    defaults.update(kw)
    return GeneratedSection(**defaults)


@pytest.fixture
def validator() -> GroundingValidator:
    return GroundingValidator()


# --------------------------------------------------------------------------
# The real output passes
# --------------------------------------------------------------------------


def test_real_model_output_is_fully_grounded(validator):
    result = validator.validate(section(), packet())
    assert result.passed, result.summary()
    assert result.numbers_ungrounded == []


def test_real_output_numbers_are_all_found(validator):
    """17 claims: 1024, 1023, 1, eight reaction counts, six outcome counts."""
    result = validator.validate(section(), packet())
    assert len(result.numbers_found) == 17
    assert 1024.0 in result.numbers_found
    assert 1257.0 in result.numbers_found


def test_dates_are_not_treated_as_claims():
    """2024, 12, 27 must not be extracted from an ISO date."""
    found = extract_numbers("period was 2024-12-27 to 2025-12-26", packet())
    assert found == []


def test_long_form_dates_are_not_claims():
    found = extract_numbers("from December 27, 2024 to December 26, 2025", packet())
    assert found == []


def test_age_band_labels_are_not_claims():
    """'65-74' is a category name quoted from the packet, not two numbers."""
    found = extract_numbers("the 65-74 band and the 85+ band and <18", packet())
    assert found == []


def test_regulatory_citations_are_not_claims():
    assert extract_numbers("in accordance with 21 CFR 314.80(c)(2)", packet()) == []


def test_thousands_separators_parse_as_one_number():
    assert extract_numbers("a total of 1,024 cases", packet()) == [1024.0]


def test_ordinals_are_not_claims():
    assert extract_numbers("the 1st and 2nd quarter", packet()) == []


# --------------------------------------------------------------------------
# Fabrication is blocked
# --------------------------------------------------------------------------


def test_single_fabricated_number_blocks_the_section(validator):
    bad = REAL_PROSE.replace("80 cases", "97 cases")
    result = validator.validate(section(bad), packet())
    assert not result.passed
    assert 97.0 in result.numbers_ungrounded
    assert any(i.code == "ungrounded_number" for i in result.blocking)


def test_plausible_arithmetic_is_still_blocked(validator):
    """99.9% is derivable from 1023/1024, but derivation is not grounding."""
    prose = REAL_PROSE + " This represents 99.9 percent of all cases."
    result = validator.validate(section(prose), packet())
    assert not result.passed
    assert 99.9 in result.numbers_ungrounded


def test_off_by_one_is_caught(validator):
    result = validator.validate(section(REAL_PROSE + " A further 2 cases."), packet())
    assert not result.passed
    assert 2.0 in result.numbers_ungrounded


def test_error_message_names_the_available_evidence(validator):
    result = validator.validate(section(REAL_PROSE + " And 555 more."), packet())
    detail = next(i.detail for i in result.blocking if i.code == "ungrounded_number")
    assert "555" in detail
    assert "total_cases" in detail


def test_rounding_tolerance_admits_percentage_rounding(validator):
    p = packet(allowed_numbers=[*ALLOWED, 99.9])
    result = validator.validate(section(REAL_PROSE + " That is 99.9 percent."), p)
    assert result.passed


def test_tolerance_is_too_tight_to_admit_a_fabrication(validator):
    p = packet(allowed_numbers=[*ALLOWED, 99.9])
    result = validator.validate(section(REAL_PROSE + " That is 99.5 percent."), p)
    assert not result.passed


# --------------------------------------------------------------------------
# Forbidden phrases
# --------------------------------------------------------------------------


def test_the_phrase_named_in_the_brief_is_blocked(validator):
    prose = REAL_PROSE + " Overall, no safety concerns were identified."
    result = validator.validate(section(prose), packet())
    assert not result.passed
    assert any(i.code == "forbidden_phrase" for i in result.blocking)


def test_forbidden_phrase_matching_is_case_insensitive(validator):
    result = validator.validate(section(REAL_PROSE + " CONFIRMED SIGNAL."), packet())
    assert not result.passed


def test_causal_language_is_blocked(validator):
    prose = REAL_PROSE + " These events were causally related to the product."
    assert not validator.validate(section(prose), packet()).passed


# --------------------------------------------------------------------------
# Evidence discipline
# --------------------------------------------------------------------------


def test_citing_undeclared_evidence_blocks(validator):
    s = section(evidence_used=["total_cases", "country_distribution"])
    result = validator.validate(s, packet())
    assert not result.passed
    assert any(i.code == "undeclared_evidence" for i in result.blocking)


def test_citing_no_evidence_escalates_but_does_not_block(validator):
    result = validator.validate(section(evidence_used=[]), packet())
    assert result.passed
    assert result.needs_review
    assert any(i.code == "no_evidence_cited" for i in result.issues)


# --------------------------------------------------------------------------
# Review-severity signals
# --------------------------------------------------------------------------


def test_model_flags_escalate_without_blocking(validator):
    s = section(flags=["needed the non-serious percentage; not in packet"])
    result = validator.validate(s, packet())
    assert result.passed
    assert result.needs_review
    assert any(i.code == "model_flag" for i in result.issues)


def test_over_length_escalates_without_blocking(validator):
    result = validator.validate(section(), packet(max_words=20))
    assert result.passed
    assert any(i.code == "over_length" for i in result.issues)


def test_degraded_output_mode_escalates(validator):
    result = validator.validate(section(output_mode="json_object"), packet())
    assert result.passed
    assert any(i.code == "degraded_output_mode" for i in result.issues)


def test_clean_section_needs_no_review(validator):
    assert not validator.validate(section(), packet()).needs_review


# --------------------------------------------------------------------------
# Report-level gate
# --------------------------------------------------------------------------


def test_one_blocked_section_blocks_the_whole_report(validator):
    good = validator.validate(section(), packet())
    bad = validator.validate(
        section(REAL_PROSE.replace("1024", "1025"), section_id="other"), packet()
    )
    assert report_is_renderable({"a": good}) is True
    assert report_is_renderable({"a": good, "b": bad}) is False


def test_validate_all_covers_every_section(validator):
    packets = {"narrative_summary": packet()}
    results = validator.validate_all([section()], packets)
    assert set(results) == {"narrative_summary"}


def test_summary_is_human_readable(validator):
    text = validator.validate(section(REAL_PROSE + " Plus 999."), packet()).summary()
    assert "BLOCKED" in text
    assert "999" in text


# --------------------------------------------------------------------------
# False positives found on live output (E-014)
# --------------------------------------------------------------------------


def test_en_dashed_age_bands_are_not_claims():
    """Models emit typographic dashes; a stored '45-64' arrives as '45\u201364'.

    This produced six false blocking issues on the first full run. The band is
    quoted from the packet, so it must mask regardless of dash character.
    """
    p = packet(
        evidence={
            "age_bands": {
                "buckets": [
                    {"label": "45-64", "count": 300},
                    {"label": "65-74", "count": 250},
                    {"label": "75-84", "count": 200},
                ]
            }
        }
    )
    text = "the 45\u201364 band, the 65\u201374 band and the 75\u201384 band"
    assert extract_numbers(text, p) == []


def test_every_dash_variant_normalises():
    p = packet(evidence={"age_bands": {"buckets": [{"label": "65-74", "count": 1}]}})
    for dash in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212-":
        assert extract_numbers(f"the 65{dash}74 group", p) == [], repr(dash)


def test_regulatory_interval_terms_are_not_claims():
    """'15-day Alert' names a reporting interval, not a count."""
    assert extract_numbers("submitted as 15-day Alert reports", packet()) == []
    assert extract_numbers("a 30 day follow-up window", packet()) == []
    assert extract_numbers("within 7 days of receipt", packet()) == []


def test_masking_does_not_hide_adjacent_real_claims():
    """Narrow masks only. A fabrication next to a masked term must survive."""
    p = packet(evidence={"age_bands": {"buckets": [{"label": "65-74", "count": 1}]}})
    found = extract_numbers("the 65\u201374 band held 999 cases", p)
    assert found == [999.0]


def test_interval_mask_does_not_swallow_the_following_number():
    found = extract_numbers("15-day Alert reports numbered 1023", packet())
    assert found == [1023.0]


# --------------------------------------------------------------------------
# Negated forbidden phrases (E-016)
# --------------------------------------------------------------------------


def test_instructed_negation_is_downgraded_not_blocked(validator):
    """The PSUR exposure section is told to say rates cannot be calculated.

    A naive substring match blocked that exact required sentence. Correct
    compliance must not be punished.
    """
    p = packet(forbidden_phrases=["reporting rate"])
    prose = (
        "No patient exposure data was supplied, so reporting rates cannot be "
        "calculated for this interval."
    )
    result = validator.validate(section(prose, evidence_used=["total_cases"]), p)
    assert result.passed
    assert any(i.code == "forbidden_phrase_negated" for i in result.issues)


def test_negated_phrase_is_still_surfaced_to_a_reviewer(validator):
    p = packet(forbidden_phrases=["reporting rate"])
    prose = "Reporting rates could not be established from the supplied data."
    result = validator.validate(section(prose, evidence_used=["total_cases"]), p)
    assert result.needs_review


def test_asserted_phrase_still_blocks(validator):
    """Negation awareness must not become a loophole."""
    p = packet(forbidden_phrases=["reporting rate"])
    prose = "The reporting rate for this interval was elevated across all regions."
    result = validator.validate(section(prose, evidence_used=["total_cases"]), p)
    assert not result.passed
    assert any(i.code == "forbidden_phrase" for i in result.blocking)


def test_the_brief_phrase_still_blocks_when_asserted(validator):
    prose = REAL_PROSE + " Overall, no safety concerns were identified."
    assert not validator.validate(section(prose), packet()).passed


def test_distant_negation_does_not_excuse_an_assertion(validator):
    """The window is tight so an unrelated denial cannot launder a claim."""
    p = packet(forbidden_phrases=["confirmed signal"])
    prose = (
        "Exposure data was not supplied. " + ("Filler text about the interval. " * 12)
        + "This is a confirmed signal requiring action."
    )
    result = validator.validate(section(prose, evidence_used=["total_cases"]), p)
    assert not result.passed


def test_benefit_risk_denial_is_permitted(validator):
    p = packet(forbidden_phrases=["benefit-risk balance remains favourable"])
    prose = (
        "A benefit-risk evaluation could not be performed; whether the "
        "benefit-risk balance remains favourable was not assessed."
    )
    assert validator.validate(section(prose, evidence_used=["total_cases"]), p).passed
