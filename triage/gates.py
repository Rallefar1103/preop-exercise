"""Null/missing-field gates: run unconditionally, before any rule.

Each gate emits a MISSING_REQUIRED_DATA issue for one missing fact and marks
that fact unavailable so window/threshold rules that need it are skipped
outright (rather than each rule re-deriving "missing" on its own). Anticoag
status is a tri-state per medication: an ``active=null`` anticoagulant is
reported here as MISSING and deliberately does NOT feed Rule 3 (see rules.py).

The latest BP/temp gates fire in two shapes: no dated vital of that type at
all (``facts.latest_bp``/``latest_temp`` is ``None``), or one exists -- and
most-recent reduction already picked it, with no fallback to an older
reading -- but its required numeric field(s) are null. Both shapes mark the
same ``Field`` unavailable, so ``rule_safety_bp``/``rule_safety_temp`` (which
``requires`` that field) are skipped either way instead of silently no-oping
into a false READY.

A third shape: the value is present but implausible (e.g. a Celsius
temperature entered as ``value_f``, which would otherwise pass the > 100.4 F
check). It is reported and gated the same way, because a threshold check on
a value we don't believe is not a safety check.

Procedure date and risk distinguish "null" from "present but unusable"
(unparseable date, unrecognized risk) in the issue text; both mark the field
unavailable.
"""

from __future__ import annotations

from . import cite
from .facts import Facts, Field
from .rules import (
    CATEGORY_MISSING_REQUIRED_DATA,
    ORDER_MISSING_ANTICOAG_UNKNOWN,
    ORDER_MISSING_BP,
    ORDER_MISSING_PROC_DATE,
    ORDER_MISSING_PROC_RISK,
    ORDER_MISSING_TEMP,
    Issue,
    make_issue,
)


# Physiologically plausible ranges. Values outside them are almost always an
# entry or unit error, not a real reading. Deliberately wide: the goal is to
# catch a wrong unit or a typo, not to second-guess a real extreme reading.
BP_SYSTOLIC_PLAUSIBLE = (40, 300)
BP_DIASTOLIC_PLAUSIBLE = (20, 200)
TEMP_F_PLAUSIBLE = (90.0, 110.0)


def _in_range(value: float, bounds: tuple[float, float]) -> bool:
    low, high = bounds
    return low <= value <= high


def _bp_has_values(obj: dict[str, object]) -> bool:
    systolic = obj.get("systolic")
    diastolic = obj.get("diastolic")
    return isinstance(systolic, (int, float)) and isinstance(diastolic, (int, float))


def _temp_has_value(obj: dict[str, object]) -> bool:
    return isinstance(obj.get("value_f"), (int, float))


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def gates(facts: Facts) -> tuple[list[Issue], frozenset[Field]]:
    issues: list[Issue] = []
    unavailable: set[Field] = set()

    if facts.proc_date is None:
        citation = (
            cite.missing_procedure_date()
            if _is_blank(facts.proc_date_raw)
            else cite.unparseable_procedure_date(raw=facts.proc_date_raw)
        )
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_PROC_DATE, citation))
        unavailable.add(Field.PROC_DATE)

    if facts.risk is None:
        citation = (
            cite.missing_procedure_risk()
            if _is_blank(facts.risk_raw)
            else cite.unrecognized_procedure_risk(raw=facts.risk_raw)
        )
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_PROC_RISK, citation))
        unavailable.add(Field.PROC_RISK)

    if facts.latest_bp is None:
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_BP, cite.missing_latest_bp()))
        unavailable.add(Field.BP)
    elif not _bp_has_values(facts.latest_bp.obj):
        # A dated blood_pressure vital exists and reduction already picked it
        # as the most recent -- per policy there is no fallback to an older
        # reading -- but it is missing the numeric values a safety check
        # needs. Treat it the same as "no latest BP" rather than silently
        # letting rule_safety_bp no-op into READY.
        citation = cite.missing_latest_bp_values(source=facts.latest_bp.path)
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_BP, citation))
        unavailable.add(Field.BP)
    elif not (
        _in_range(facts.latest_bp.obj["systolic"], BP_SYSTOLIC_PLAUSIBLE)
        and _in_range(facts.latest_bp.obj["diastolic"], BP_DIASTOLIC_PLAUSIBLE)
    ):
        citation = cite.implausible_latest_bp(
            source=facts.latest_bp.path,
            systolic=facts.latest_bp.obj["systolic"],
            diastolic=facts.latest_bp.obj["diastolic"],
        )
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_BP, citation))
        unavailable.add(Field.BP)

    if facts.latest_temp is None:
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_TEMP, cite.missing_latest_temp()))
        unavailable.add(Field.TEMP)
    elif not _temp_has_value(facts.latest_temp.obj):
        citation = cite.missing_latest_temp_values(source=facts.latest_temp.path)
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_TEMP, citation))
        unavailable.add(Field.TEMP)
    elif not _in_range(facts.latest_temp.obj["value_f"], TEMP_F_PLAUSIBLE):
        citation = cite.implausible_latest_temp(
            source=facts.latest_temp.path, value_f=facts.latest_temp.obj["value_f"]
        )
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_TEMP, citation))
        unavailable.add(Field.TEMP)

    for ref, status in facts.anticoags:
        if status != "unknown":
            continue
        name = str(ref.obj.get("name") or "")
        citation = cite.unknown_anticoag_status(source=ref.path, name=name)
        issues.append(make_issue(CATEGORY_MISSING_REQUIRED_DATA, ORDER_MISSING_ANTICOAG_UNKNOWN, citation))

    return issues, frozenset(unavailable)
