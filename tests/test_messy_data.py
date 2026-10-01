"""End-to-end regressions for the messy-data paths in DESIGN_REVIEW.md §3.

Each test takes the READY case ``case_00012``, changes one thing, and runs the
full pipeline with the hand-labeled fixture classifier (no network). Before
the hardening changes, every case marked "false READY" below returned READY.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from triage.documents import ConsentStatus, DocClaim, DocRole, FakeClassifier, load_fixture_claims
from triage.pipeline import run as run_pipeline

ROOT = Path(__file__).resolve().parent.parent
CASE_ID = "case_00012"
CASE = next(
    json.loads(line)
    for line in (ROOT / "data" / "patients_sample_50.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip() and json.loads(line)["case_id"] == CASE_ID
)
CLAIMS = load_fixture_claims(ROOT / "tests" / "fixtures" / "doc_claims.json")[CASE_ID]


def _run(submission: dict[str, object], claims: list[DocClaim] | None = None) -> dict[str, object]:
    return run_pipeline(submission, FakeClassifier(CLAIMS if claims is None else claims))


def _base() -> dict[str, object]:
    return copy.deepcopy(CASE["submission"])


def _issues(output: dict[str, object]) -> list[tuple[str, str]]:
    return [(i["category"], i["description"]) for i in output["issues"]]  # type: ignore[index]


def test_unmodified_case_is_ready() -> None:
    assert _run(_base())["decision"] == "READY"


@pytest.mark.parametrize("name", ["Eliquis", "apixaban 5mg", "rivaroxaban", "Xarelto 20 mg", "Coumadin", "Lovenox"])
def test_active_anticoagulant_by_any_name_needs_a_plan(name: str) -> None:
    # false READY before: only the exact strings "apixaban"/"warfarin" matched.
    submission = _base()
    submission["medications"].append({"name": name, "active": True})  # type: ignore[union-attr]
    output = _run(submission)
    assert output["decision"] == "NEEDS_FOLLOW_UP"
    assert _issues(output) == [("ANTICOAGULATION_MANAGEMENT", "Missing perioperative anticoagulation plan")]


def test_cancelled_labs_do_not_satisfy_required_testing() -> None:
    # false READY before: lab status was never checked.
    submission = _base()
    for lab in submission["labs"]:  # type: ignore[union-attr]
        lab["status"] = "cancelled"
    output = _run(submission)
    assert output["decision"] == "NEEDS_FOLLOW_UP"
    assert _issues(output) == [("REQUIRED_TESTING", "CBC missing")]
    assert "excluded by status" in output["issues"][0]["evidence"]["details"]  # type: ignore[index]


def test_lowercase_high_risk_applies_high_risk_rules() -> None:
    # false READY before: "high" != "HIGH", so the case got LOW/MODERATE rules
    # (30-day CBC, no CMP). This case has no CMP and a CBC 8 days out.
    submission = _base()
    submission["procedure"]["procedure_risk"] = "high"  # type: ignore[index]
    output = _run(submission)
    assert output["decision"] == "NEEDS_FOLLOW_UP"
    assert ("REQUIRED_TESTING", "CMP missing") in _issues(output)


def test_negated_consent_with_a_verbatim_signed_excerpt_is_not_ready() -> None:
    # false READY before: validate() only checked the excerpt was a substring.
    submission = _base()
    submission["documents"][2]["text"] = "Consent form prepared; patient has NOT signed consent yet."  # type: ignore[index]
    wrong = [
        DocClaim(c.index, c.role, None, ConsentStatus.SIGNED, None, "signed consent")
        if c.role is DocRole.SURGICAL_CONSENT
        else c
        for c in CLAIMS
    ]
    output = _run(submission, wrong)
    assert output["decision"] == "NEEDS_FOLLOW_UP"
    assert _issues(output) == [("REQUIRED_DOCUMENTATION", "Surgical consent not clearly signed")]


def test_celsius_temperature_is_not_silently_accepted() -> None:
    # false READY before: 38.9 (a Celsius fever) passed the > 100.4 F check.
    submission = _base()
    submission["vitals"][2]["value_f"] = 38.9  # type: ignore[index]
    output = _run(submission)
    assert output["decision"] == "NEEDS_FOLLOW_UP"
    assert _issues(output) == [("MISSING_REQUIRED_DATA", "Implausible latest temperature")]


def test_dangerous_bp_recorded_as_bp_is_not_cleared() -> None:
    # Before: type "BP" was unrecognized, so this became "Missing latest blood pressure".
    submission = _base()
    for vital in submission["vitals"]:  # type: ignore[union-attr]
        if vital["type"] == "blood_pressure":
            vital.update(type="BP", systolic=210, diastolic=125)
    output = _run(submission)
    assert output["decision"] == "NOT_CLEARED"
    assert _issues(output) == [("ACUTE_SAFETY_EXCLUSION", "Blood pressure meets exclusion threshold")]


def test_consent_text_with_extra_whitespace_still_matches_its_excerpt() -> None:
    # Before: the strict substring check dropped the claim -> "consent missing".
    submission = _base()
    submission["documents"][2]["text"] = "Signed  consent scanned\nand verified before scheduling."  # type: ignore[index]
    assert _run(submission)["decision"] == "READY"


def test_unparseable_procedure_date_says_unparseable() -> None:
    submission = _base()
    submission["procedure"]["procedure_date"] = "March 13, 2026"  # type: ignore[index]
    output = _run(submission)
    assert output["decision"] == "NEEDS_FOLLOW_UP"
    assert ("MISSING_REQUIRED_DATA", "Unparseable procedure date") in _issues(output)


def test_hp_with_a_non_iso_date_is_not_reported_as_missing() -> None:
    submission = _base()
    submission["documents"][0]["date"] = "03/03/2026"  # type: ignore[index]
    output = _run(submission)
    assert output["decision"] == "NEEDS_FOLLOW_UP"
    assert _issues(output) == [("REQUIRED_DOCUMENTATION", "History and Physical date missing or unparseable")]
    assert output["issues"][0]["evidence"]["source"] == "documents[0]"  # type: ignore[index]
