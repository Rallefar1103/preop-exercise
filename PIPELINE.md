# How the Triage Pipeline Works

`triage_submission(...)` in `core.py` takes one pre-op submission and returns `READY`, `NEEDS_FOLLOW_UP` or `NOT_CLEARED`, with a list of issues and an explanation. Python code makes every decision. The LLM only reads free-text documents.

```
submission → normalize → gates → code-only rules (testing, safety)
           ├─ safety exclusion fired? → NOT_CLEARED (LLM skipped)
           └─ else → LLM classifies documents → validate → document rules → decide
```

## Steps

1. **Normalize** (`triage/facts.py`)
   - Turn the raw submission into typed `Facts`, each with a reference to its source path for citations.
   - Keep only the most recent result for each lab and vital. Older results are never used as a fallback.
   - Map lab-code aliases (`LAB-CBC` → `CBC`).
   - Flag anticoagulants (apixaban, warfarin) and ignore other medications.

2. **Gates** (`triage/gates.py`)
   - Check for missing procedure date or risk, missing or valueless latest BP or temperature, and anticoagulants with unknown `active` status.
   - Each gap adds `MISSING_REQUIRED_DATA` and marks that field unavailable.

3. **Code-only rules** (`triage/rules.py`)
   - Testing: CBC within 30 days (LOW/MODERATE risk) or 14 days (HIGH risk). CMP required only for HIGH risk, within 14 days.
   - Safety: BP ≥ 180 systolic or ≥ 110 diastolic, or temperature > 100.4 °F, means `NOT_CLEARED`.
   - Each rule declares the fields it requires and is skipped if any of them is unavailable.
   - "Lab exists" and "lab is in window" are separate checks, so a missing lab is still flagged when the risk is unknown.

4. **Short-circuit** (`triage/pipeline.py`)
   - If a safety rule fired, return `NOT_CLEARED` without calling the LLM.
   - `review_documents_on_not_cleared=True` turns full document review back on.

5. **Classify documents with the LLM** (`triage/documents.py`)
   - The model sees only `{index, type, date, text}` for each document.
   - It returns each document's role (H&P, consent, anticoag plan, other), whether the H&P is current, whether consent is signed (`SIGNED` / `UNSIGNED` / `UNCLEAR`), and whether the anticoag plan is clear.
   - The model only identifies which document is which. Code reads the dates and values.
   - Responses are cached by model, prompt version and document content.

6. **Validate**
   - Drop any claim that points to a document index that doesn't exist, or whose excerpt is empty or not word-for-word in the document.
   - The model's reasoning and excerpts never appear in the output.

7. **Document rules** (`triage/rules.py`)
   - H&P: must exist and be dated within 30 days of the procedure.
   - Consent: must exist and be signed. Missing and unsigned are separate issues.
   - Anticoagulation: an active anticoagulant requires a clear plan document.

8. **Decide and explain**
   - Collect all issues, then take the most severe: `NOT_CLEARED` > `NEEDS_FOLLOW_UP` > `READY`.
   - Sort issues by each rule's fixed `order`. `triage/cite.py` builds each issue's evidence `source` and `details` strings.
   - Build the explanation from the issues.

## If the LLM is unavailable

The pipeline logs a warning and adds "Document review unavailable". Safety rules still run, and a case can never be `READY` without document review.

## Conventions

- Date windows are calendar days and inclusive (`0 ≤ days ≤ N`). A result dated after the procedure is out of window.
- Undated items count as absent. Timestamps are normalized to UTC before comparison.
- An H&P with an unknown current status is treated as not current.
