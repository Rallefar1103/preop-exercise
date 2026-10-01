from __future__ import annotations

from triage.facts import Field, normalize
from triage.gates import gates


def _submission(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "procedure": {"procedure_date": "2026-03-11", "procedure_risk": "HIGH"},
        "vitals": [
            {"type": "blood_pressure", "systolic": 120, "diastolic": 80, "date": "2026-03-01"},
            {"type": "temperature", "value_f": 98.6, "date": "2026-03-01"},
        ],
        "labs": [],
        "medications": [],
        "documents": [],
    }
    base.update(overrides)
    return base


def test_no_gates_fire_when_everything_required_is_present() -> None:
    issues, unavailable = gates(normalize(_submission()))
    assert issues == []
    assert unavailable == frozenset()


def test_null_procedure_date_gates_and_disables_proc_date() -> None:
    issues, unavailable = gates(normalize(_submission(procedure={"procedure_date": None, "procedure_risk": "HIGH"})))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.category == "MISSING_REQUIRED_DATA"
    assert issue.description == "Missing procedure date"
    assert issue.source == "procedure.procedure_date"
    assert issue.details == "procedure.procedure_date is null"
    assert unavailable == frozenset({Field.PROC_DATE})


def test_null_procedure_risk_gates_and_disables_proc_risk() -> None:
    issues, unavailable = gates(normalize(_submission(procedure={"procedure_date": "2026-03-11", "procedure_risk": None})))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.category == "MISSING_REQUIRED_DATA"
    assert issue.description == "Missing procedure risk"
    assert issue.source == "procedure.procedure_risk"
    assert issue.details == "procedure.procedure_risk is null"
    assert unavailable == frozenset({Field.PROC_RISK})


def test_warfarin_active_null_gates_as_missing_and_does_not_disable_any_field() -> None:
    issues, unavailable = gates(
        normalize(_submission(medications=[{"name": "warfarin", "active": None}]))
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.category == "MISSING_REQUIRED_DATA"
    assert issue.description == "Unknown anticoagulant active status"
    assert issue.source == "medications[0]"
    assert issue.details == "Medication warfarin has active=null; cannot determine if currently taking"
    # Unknown anticoag status is its own issue; it does not disable any rule.
    assert unavailable == frozenset()


def test_missing_bp_and_temp_are_two_separate_issues() -> None:
    issues, unavailable = gates(normalize(_submission(vitals=[])))
    assert [issue.description for issue in issues] == [
        "Missing latest blood pressure",
        "Missing latest temperature",
    ]
    for issue in issues:
        assert issue.category == "MISSING_REQUIRED_DATA"
        assert issue.source == "vitals"
    assert unavailable == frozenset({Field.BP, Field.TEMP})


def test_active_anticoagulant_does_not_gate() -> None:
    issues, _unavailable = gates(
        normalize(_submission(medications=[{"name": "apixaban", "active": True}]))
    )
    assert issues == []


def test_latest_bp_present_but_missing_values_gates_as_missing() -> None:
    # Most-recent reduction picks vitals[0] (it has a valid date); it is the
    # *only* blood_pressure vital, so there is no fallback to consider. It
    # just lacks the numeric values a safety check needs.
    issues, unavailable = gates(
        normalize(
            _submission(
                vitals=[
                    {"type": "blood_pressure", "systolic": None, "diastolic": None, "date": "2026-03-01"},
                    {"type": "temperature", "value_f": 98.6, "date": "2026-03-01"},
                ]
            )
        )
    )
    assert [issue.description for issue in issues] == ["Missing latest blood pressure"]
    issue = issues[0]
    assert issue.category == "MISSING_REQUIRED_DATA"
    assert issue.source == "vitals[0]"
    assert issue.details == "Latest blood_pressure vital (vitals[0]) has null systolic/diastolic"
    assert unavailable == frozenset({Field.BP})


def test_latest_bp_present_with_one_null_value_still_gates_as_missing() -> None:
    issues, unavailable = gates(
        normalize(
            _submission(
                vitals=[
                    {"type": "blood_pressure", "systolic": 184, "diastolic": None, "date": "2026-03-01"},
                    {"type": "temperature", "value_f": 98.6, "date": "2026-03-01"},
                ]
            )
        )
    )
    assert [issue.description for issue in issues] == ["Missing latest blood pressure"]
    assert unavailable == frozenset({Field.BP})


def test_latest_temp_present_but_missing_value_gates_as_missing() -> None:
    issues, unavailable = gates(
        normalize(
            _submission(
                vitals=[
                    {"type": "blood_pressure", "systolic": 120, "diastolic": 80, "date": "2026-03-01"},
                    {"type": "temperature", "value_f": None, "date": "2026-03-01"},
                ]
            )
        )
    )
    assert [issue.description for issue in issues] == ["Missing latest temperature"]
    issue = issues[0]
    assert issue.category == "MISSING_REQUIRED_DATA"
    assert issue.source == "vitals[1]"
    assert issue.details == "Latest temperature vital (vitals[1]) has null value_f"
    assert unavailable == frozenset({Field.TEMP})


def test_unparseable_procedure_date_is_reported_as_unparseable_not_missing() -> None:
    issues, unavailable = gates(normalize(_submission(procedure={"procedure_date": "March 13, 2026", "procedure_risk": "HIGH"})))
    assert [(i.category, i.description) for i in issues] == [("MISSING_REQUIRED_DATA", "Unparseable procedure date")]
    assert "'March 13, 2026'" in issues[0].details
    assert unavailable == frozenset({Field.PROC_DATE})


def test_unrecognized_procedure_risk_is_reported_and_disables_proc_risk() -> None:
    issues, unavailable = gates(normalize(_submission(procedure={"procedure_date": "2026-03-11", "procedure_risk": "URGENT"})))
    assert [(i.category, i.description) for i in issues] == [("MISSING_REQUIRED_DATA", "Unrecognized procedure risk")]
    assert unavailable == frozenset({Field.PROC_RISK})


def test_lowercase_procedure_risk_passes_the_gate() -> None:
    issues, unavailable = gates(normalize(_submission(procedure={"procedure_date": "2026-03-11", "procedure_risk": "high"})))
    assert issues == []
    assert unavailable == frozenset()


def test_celsius_temperature_in_value_f_is_gated_as_implausible() -> None:
    issues, unavailable = gates(
        normalize(
            _submission(
                vitals=[
                    {"type": "blood_pressure", "systolic": 120, "diastolic": 80, "date": "2026-03-01"},
                    {"type": "temperature", "value_f": 38.9, "date": "2026-03-01"},
                ]
            )
        )
    )
    assert [(i.description, i.source) for i in issues] == [("Implausible latest temperature", "vitals[1]")]
    assert unavailable == frozenset({Field.TEMP})


def test_implausible_blood_pressure_is_gated() -> None:
    issues, unavailable = gates(
        normalize(
            _submission(
                vitals=[
                    {"type": "blood_pressure", "systolic": 1200, "diastolic": 80, "date": "2026-03-01"},
                    {"type": "temperature", "value_f": 98.6, "date": "2026-03-01"},
                ]
            )
        )
    )
    assert [i.description for i in issues] == ["Implausible latest blood pressure"]
    assert unavailable == frozenset({Field.BP})


def test_real_extreme_but_plausible_values_are_not_gated() -> None:
    # 210/125 and 104 F are dangerous, not implausible: they must reach the
    # safety rules, not be swallowed by the plausibility gate.
    issues, unavailable = gates(
        normalize(
            _submission(
                vitals=[
                    {"type": "blood_pressure", "systolic": 210, "diastolic": 125, "date": "2026-03-01"},
                    {"type": "temperature", "value_f": 104.0, "date": "2026-03-01"},
                ]
            )
        )
    )
    assert issues == []
    assert unavailable == frozenset()
