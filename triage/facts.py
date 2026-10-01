"""Raw submission JSON -> typed facts with provenance.

The structured sections of a submission (``procedure``, ``vitals``, ``labs``,
``medications``) are expected to be close to standardized, but not exactly:
normalization maps known spellings (lab-code aliases, vital-type aliases, risk
case, anticoagulant brand names and doses) to canonical values, and keeps the
raw value whenever a field is present but unusable, so the gates can report it
as such instead of letting it fall through silently. Dates are ISO only; there
is no fuzzy date repair.

Only ``documents[].type`` is free text (100+ variants observed) and is
deliberately never string-matched by this module -- document meaning is read by
the LLM in ``documents.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from datetime import date, datetime, time, timezone
from enum import StrEnum
from typing import Literal

AnticoagStatus = Literal["active", "inactive", "unknown"]

# Both forms of these codes appear in the sample data for the same underlying test.
LAB_ALIASES: dict[str, str] = {
    "LAB-CBC": "CBC",
    "LAB-CMP": "CMP",
}

# The only labs the policy cares about. HBA1C appears throughout the data as a
# distractor and is intentionally never tracked.
TRACKED_LAB_CODES: frozenset[str] = frozenset({"CBC", "CMP"})

# Anticoagulant lookup: generic and brand name -> generic. The policy says "an
# anticoagulant", not "apixaban or warfarin", and a brand name or a dose in the
# name ("Eliquis", "apixaban 5mg") must not fall through as "not an
# anticoagulant" -- that path yields a false READY. Matching is per word token,
# so "warfarin sodium" and "Xarelto 20 mg" both resolve. Antiplatelets
# (clopidogrel, aspirin) are deliberately excluded: they are not anticoagulants.
# Lisinopril (BP) and metformin (diabetes) remain distractors and never match.
# Production would replace this table with an RxNorm / ATC B01A class lookup.
ANTICOAGULANTS: dict[str, str] = {
    "apixaban": "apixaban",
    "eliquis": "apixaban",
    "rivaroxaban": "rivaroxaban",
    "xarelto": "rivaroxaban",
    "dabigatran": "dabigatran",
    "pradaxa": "dabigatran",
    "edoxaban": "edoxaban",
    "savaysa": "edoxaban",
    "lixiana": "edoxaban",
    "warfarin": "warfarin",
    "coumadin": "warfarin",
    "jantoven": "warfarin",
    "enoxaparin": "enoxaparin",
    "lovenox": "enoxaparin",
    "dalteparin": "dalteparin",
    "fragmin": "dalteparin",
    "tinzaparin": "tinzaparin",
    "heparin": "heparin",
    "fondaparinux": "fondaparinux",
    "arixtra": "fondaparinux",
}

PROCEDURE_RISKS: frozenset[str] = frozenset({"LOW", "MODERATE", "HIGH"})

# Vital types are matched after lowercasing and collapsing spaces/hyphens to
# "_", then through this alias table. An unrecognized type is still ignored
# (and the BP/temp gate then reports the vital as missing), but common
# spellings like "BP" or "Blood Pressure" no longer hide a dangerous reading.
VITAL_TYPE_ALIASES: dict[str, str] = {
    "blood_pressure": "blood_pressure",
    "bp": "blood_pressure",
    "temperature": "temperature",
    "temp": "temperature",
    "body_temperature": "temperature",
}

# Lab statuses that represent a resulted test. A cancelled, preliminary or
# entered-in-error result is not a completed test and must not satisfy Rule 2.
# A lab with no status at all is accepted (the field carries no information
# either way); every other explicit status is excluded.
ACCEPTED_LAB_STATUSES: frozenset[str] = frozenset({"final", "amended", "corrected"})


class Field(StrEnum):
    """Facts a rule may depend on. Gates and the pipeline mark these unavailable
    so a rule can declare ``requires`` and be skipped cleanly instead of each
    rule null-checking its own inputs."""

    PROC_DATE = "proc_date"
    PROC_RISK = "proc_risk"
    BP = "bp"
    TEMP = "temp"
    DOC_ROLES = "doc_roles"


@dataclass(frozen=True)
class Ref:
    """Provenance pointer: which raw JSON object (and array path) a fact came
    from. Citations are assembled from these, never reconstructed after the fact."""

    path: str
    obj: dict[str, object]


@dataclass(frozen=True)
class Facts:
    proc_date: date | None
    risk: str | None
    latest_bp: Ref | None
    latest_temp: Ref | None
    latest_lab: dict[str, Ref]
    anticoags: list[tuple[Ref, AnticoagStatus]]
    documents: list[Ref]
    # Raw values kept so the gates can tell "missing" from "present but
    # unusable" (unparseable date, unrecognized risk) and say which.
    proc_date_raw: object = None
    risk_raw: object = None
    # Tracked labs dropped because of their status, by canonical code, so
    # "CBC missing" can say a cancelled CBC exists instead of implying none.
    excluded_labs: dict[str, list[Ref]] = field(default_factory=dict)


def canonical_lab_code(code: object) -> str | None:
    if not isinstance(code, str) or not code:
        return None
    return LAB_ALIASES.get(code, code)


def parse_item_date(value: object) -> date | None:
    """Parse a raw date/datetime string to a calendar date for window math.

    A date-only string is used as written. A datetime with a UTC offset is
    converted to UTC before taking the date, so the same instant always lands
    on the same calendar day regardless of the offset it was recorded in (a
    naive datetime is taken as UTC). Undated or unparsable values count as
    absent -- there is no fuzzy repair.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "T" not in text:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date()


def canonical_vital_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    return VITAL_TYPE_ALIASES.get(key)


def canonical_risk(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    risk = value.strip().upper()
    return risk if risk in PROCEDURE_RISKS else None


def anticoagulant_generic(name: object) -> str | None:
    """Generic name of the anticoagulant in a medication name, or None.

    Matches whole word tokens, so doses, salts and brand names resolve
    ("Eliquis 5 mg" -> "apixaban") without substring false positives."""

    if not isinstance(name, str):
        return None
    for token in re.findall(r"[a-z]+", name.lower()):
        if token in ANTICOAGULANTS:
            return ANTICOAGULANTS[token]
    return None


def lab_status_accepted(status: object) -> bool:
    if status is None:
        return True
    return isinstance(status, str) and status.strip().lower() in ACCEPTED_LAB_STATUSES


def _sort_key(value: object) -> datetime | None:
    """Full timestamp (falling back to midnight for date-only strings), used only
    to pick the single most-recent item within a group -- never for window math.

    Normalized to timezone-aware UTC: a date-only string parses naive (midnight)
    and an offset-bearing string (e.g. trailing ``Z``) parses aware, and ``max()``
    over a mix of the two raises ``TypeError``. Treating a naive result as UTC
    midnight makes every value comparable without changing same-format
    comparisons (all-naive or all-aware groups sort exactly as before).
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            parsed = datetime.combine(date.fromisoformat(text[:10]), time.min)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _most_recent(refs: list[Ref], date_field: str) -> Ref | None:
    """Most-recent reduction with no fallback: an item with no parsable date
    cannot be "the most recent" of anything, so it is dropped, not skipped-over."""

    dated = [
        (ref, key)
        for ref in refs
        if (key := _sort_key(ref.obj.get(date_field))) is not None
    ]
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[1])[0]


def normalize(raw: dict[str, object]) -> Facts:
    """Raw submission dict -> Facts. Strict parsing; see module docstring."""

    procedure = raw.get("procedure") or {}
    proc_date_raw = procedure.get("procedure_date")
    proc_date = parse_item_date(proc_date_raw)
    risk_raw = procedure.get("procedure_risk")

    vitals = raw.get("vitals") or []
    bp_refs = [
        Ref(path=f"vitals[{i}]", obj=vital)
        for i, vital in enumerate(vitals)
        if isinstance(vital, dict) and canonical_vital_type(vital.get("type")) == "blood_pressure"
    ]
    temp_refs = [
        Ref(path=f"vitals[{i}]", obj=vital)
        for i, vital in enumerate(vitals)
        if isinstance(vital, dict) and canonical_vital_type(vital.get("type")) == "temperature"
    ]
    latest_bp = _most_recent(bp_refs, "date")
    latest_temp = _most_recent(temp_refs, "date")

    labs_by_code: dict[str, list[Ref]] = {}
    excluded_labs: dict[str, list[Ref]] = {}
    for i, lab in enumerate(raw.get("labs") or []):
        if not isinstance(lab, dict):
            continue
        code = canonical_lab_code(lab.get("code"))
        if code not in TRACKED_LAB_CODES:
            continue
        ref = Ref(path=f"labs[{i}]", obj=lab)
        # Status filtering happens before most-recent reduction: a cancelled
        # order is not a result, so it can't be "the most recent result".
        if lab_status_accepted(lab.get("status")):
            labs_by_code.setdefault(code, []).append(ref)
        else:
            excluded_labs.setdefault(code, []).append(ref)
    latest_lab = {
        code: ref
        for code, refs in labs_by_code.items()
        if (ref := _most_recent(refs, "effective_at")) is not None
    }

    anticoags: list[tuple[Ref, AnticoagStatus]] = []
    for i, med in enumerate(raw.get("medications") or []):
        if not isinstance(med, dict):
            continue
        if anticoagulant_generic(med.get("name")) is None:
            continue
        active = med.get("active")
        status: AnticoagStatus
        if active is None:
            status = "unknown"
        elif active:
            status = "active"
        else:
            status = "inactive"
        anticoags.append((Ref(path=f"medications[{i}]", obj=med), status))

    documents = [
        Ref(path=f"documents[{i}]", obj=doc)
        for i, doc in enumerate(raw.get("documents") or [])
        if isinstance(doc, dict)
    ]

    return Facts(
        proc_date=proc_date,
        risk=canonical_risk(risk_raw),
        latest_bp=latest_bp,
        latest_temp=latest_temp,
        latest_lab=latest_lab,
        anticoags=anticoags,
        documents=documents,
        proc_date_raw=proc_date_raw,
        risk_raw=risk_raw,
        excluded_labs=excluded_labs,
    )
