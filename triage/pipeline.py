"""Orchestrates gates -> deterministic rules -> (maybe) document classification
-> document rules -> decision fold, and assembles the output dict.

Short-circuit (documented trade-off in NOTES.md): the full deterministic rule
set always runs first, so testing/missing categories are reported even on a
NOT_CLEARED case. If any ACUTE_SAFETY_EXCLUSION fired, the LLM call is skipped
entirely and document rules are marked unavailable -- the decision is already
NOT_CLEARED regardless of what the documents say. ``review_documents_on_not_cleared``
turns this off. Separately, if there are zero documents, the LLM call is
skipped too, but document rules still run (with no claims) so they correctly
report the missing-H&P / missing-consent issues instead of being suppressed.
"""

from __future__ import annotations

from . import cite
from . import gates as gates_mod
from . import rules as rules_mod
from .documents import Classifier, DocClaim, build_documents_input, validate
from .facts import Field, normalize

SAFETY_SKIPPED_NOTE = (
    "Document review was skipped because an acute safety exclusion already applies."
)


def run(
    raw: dict[str, object],
    classifier: Classifier,
    *,
    review_documents_on_not_cleared: bool = False,
) -> dict[str, object]:
    # First normalize the raw input into facts and get the most recent values for each field.
    facts = normalize(raw)
    # Then run the gates on the facts to get any issues or unavailable fields.
    issues, unavailable = gates_mod.gates(facts)

    # Then run the non-document related rules on the facts and unavailable fields. You load all rules but only keep the ones that don't require documents.
    # At this step each rule is only using the facts to build the issue description, if the rule is violated.
    issues.extend(
        rules_mod.run_rules(
            rules_mod.non_document_specs(), facts, unavailable, claims=[]
        )
    )

    safety_fired = any(
        issue.category == rules_mod.CATEGORY_ACUTE_SAFETY_EXCLUSION for issue in issues
    )
    review_skipped_note: str | None = None
    claims: list[DocClaim] = []
    doc_unavailable = unavailable

    raw_docs = list(raw.get("documents") or [])

    # if the safety exclusion rule fired, all document rules are skipped (union of unavailable fields and DOC_ROLES)
    if safety_fired and not review_documents_on_not_cleared:
        doc_unavailable = unavailable | {Field.DOC_ROLES}
        review_skipped_note = SAFETY_SKIPPED_NOTE
    elif not raw_docs:
        pass  # nothing to classify; document rules run with no candidates.
    else:
        result = classifier.classify(build_documents_input(raw_docs))
        if result is None:
            # if the LLM classifier returns None, the call failed (no key, network error, bad response) and no document review happened.
            # Only the document rules are skipped below; the non-document rules have already run.
            issues.append(
                rules_mod.make_issue(
                    rules_mod.CATEGORY_MISSING_REQUIRED_DATA,
                    rules_mod.ORDER_DOC_REVIEW_UNAVAILABLE,
                    cite.document_review_unavailable(),
                )
            )
            # Document rules are switched off because the LLM did not return a result based on the documents.
            doc_unavailable = unavailable | {Field.DOC_ROLES}
        else:
            # if the LLM classifier returns a result, the document review is valid and the claims are validated against any hallucinations.
            claims = validate(result, raw_docs)

    # Then run the document related rules on the facts and claims and unavailable fields. You load all rules but only keep the ones that require documents.
    # At this step each rule is using the facts and claims to build the issue description, if the rule is violated.
    issues.extend(
        rules_mod.run_rules(rules_mod.document_specs(), facts, doc_unavailable, claims)
    )

    issues.sort(key=lambda issue: issue.order)
    decision = rules_mod.decide(issues)
    explanation = rules_mod.build_explanation(
        issues, review_skipped_note=review_skipped_note
    )

    return {
        "decision": decision,
        "issues": [rules_mod.issue_to_dict(issue) for issue in issues],
        "explanation": explanation,
    }
