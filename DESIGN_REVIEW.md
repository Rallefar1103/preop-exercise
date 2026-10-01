# Design Review: Decisions, Critique, and Productionization

This document builds on `README.md`, `NOTES.md` and `PIPELINE.md`. It covers four questions:
- What did we decide, and what did each decision cost?
- Where is the pipeline brittle?
- What doesn't scale?
- What would a production version look like?

Every failure example in §3 was **actually run**. Each one takes the READY case `case_00012`, changes one thing, and runs it through `triage.pipeline.run` with the fixture classifier (no LLM). For each, we show the decision the pipeline returned.

---

## 0. The lens: errors are not symmetric

Every critique below is judged by one question: **which direction does the error go?**

| Error | Consequence | Severity |
|---|---|---|
| False `NEEDS_FOLLOW_UP` (flags a problem that isn't there) | A coordinator spends a few minutes checking | Annoying, costs operations time |
| False `NOT_CLEARED` | A case is delayed or rescheduled | Costly, but safe |
| **False `READY`** (misses a real problem) | **A patient goes to surgery without a required safeguard** | **Patient-safety risk** |

The design mostly fails safe: missing data pushes a case toward follow-up. The most important findings below are the few paths where messy data **produces a false READY**.

---

## 1. Key decisions and trade-offs we own

| Decision | Why | What it costs | Would I keep it? |
|---|---|---|---|
| **Policy in code; the LLM only reads documents** | Rules are deterministic, testable and auditable. Thresholds can't be "hallucinated". | Every policy change needs a code change and a deploy. Document understanding is only as good as one prompt. | **Yes.** This is the most important decision, and it's right. |
| **The model returns pointers; code reads the values** (e.g. "`documents[0]` is the H&P", then code reads its date) | The model never does date math or reads the policy. | Code has to trust the model's choice of document. | **Yes.** |
| **The `requires` / `unavailable` skip mechanism** | No rule guesses about data it doesn't have. Missing data is reported once, by the gate. | Each rule has to declare its dependencies correctly. If one forgets, it silently works on `None`. | **Yes.** It's simple and composable. |
| **Separate existence and window checks** | "CBC missing" still fires when the risk is unknown. | More rules to maintain. | **Yes.** |
| **Short-circuit on NOT_CLEARED** (skip the LLM) | Saves an LLM call (7 of 50 cases). The decision can't change. | Document issues aren't listed on NOT_CLEARED cases. Once the safety issue is fixed, the coordinator finds out about the missing consent in a *second* round. | **I'd flip the default in production.** One LLM call is cheap. A second round of follow-ups with the clinic is expensive. |
| **Choose the current H&P by content** (case_00002) | Clinically correct: the old "retained prior" H&P is not the current one. | Loses 1 category point against the labels. | **Yes.** Correct beats matching the labels. |
| **Copy the labels' citation strings exactly** | Maximizes the category and description match. | Inherits the labels' grounding failures (56% grounding). | **No, for production.** It's fine for the exercise. In production I'd write citations that actually point to the evidence. The change is local to `cite.py`. |
| **Process-wide response cache** | Repeat calls are identical, so the determinism check reads 100%. | **It hides model variance.** The determinism number measures the cache, not the model. | Keep a cache for idempotency, but measure variance separately (§5). |
| **Fail safe when the LLM is down** ("Document review unavailable") | A case is never READY without document review. | During an outage, every case becomes `NEEDS_FOLLOW_UP` with an output that *looks* like a normal result. | **Yes**, but an outage must be a distinct, alerting state, not just another issue (§5). |
| **Order issues by values reverse-engineered from the labels** | Output order matches the labels. | Couples the code to one label set's ordering. | Fine. It's cosmetic. |

**What's genuinely good and doesn't need changing:**
- The narrow LLM boundary: the model never sees vitals, medications or policy.
- `validate()` checks that document indexes exist and excerpts are word-for-word.
- The 218 network-free tests.
- An LLM failure can never produce READY.

---

## 2. Gaps in the evaluation

These aren't code bugs. They limit how far you can trust the 93% score.

1. **The LLM has never been measured.** Every score uses the fixture classifier, whose labels were hand-written by us. The live run hasn't been done. All the real uncertainty in this system is in the one component that hasn't been tested.
2. **There's no held-out data.** The rules and order values were tuned against the same 50 cases they're scored on, so 100% decision match is a training-set number.
3. **The READY path is barely tested.** Only **3 of 50** cases are READY (40 NEEDS_FOLLOW_UP, 7 NOT_CLEARED). The most dangerous kind of error, a false READY, has the least evidence behind it.
4. **The sample data is unrealistically clean:**
   - Documents average **72 characters** (max 107), with 2–5 per case. Real H&Ps run to pages, often as scanned PDFs.
   - Every lab has `status: "final"`.
   - Every timestamp is in UTC (`Z`).
   - Medication names are always lowercase generic names.
   - Vital types are always exactly `blood_pressure` / `temperature`.

   The pipeline is strict exactly where the sample happens to be clean, which is why it scores well. §3 shows what happens when it isn't.

---

## 3. How messy or different data breaks it (all verified by running)

### 3.1 Paths that produce a false READY (the dangerous ones)

| Change made to a READY case | Result | Why |
|---|---|---|
| Add `{"name": "apixaban", "active": true}`, no plan | `NEEDS_FOLLOW_UP` ✅ | This is the control: the generic name is recognized. |
| Add **`"Eliquis"`** (the brand name for apixaban), active, no plan | **`READY`** ❌ | `ANTICOAGULANTS = {"apixaban", "warfarin"}` is an exact-match lookup. |
| Add **`"rivaroxaban"`** (Xarelto), active | **`READY`** ❌ | It's not in the two-drug list. The same applies to dabigatran, enoxaparin and heparin (and antiplatelets like clopidogrel, if the policy is meant to cover them). |
| Add **`"apixaban 5mg"`** | **`READY`** ❌ | A dose in the name breaks the exact match. |
| Set every lab to **`status: "cancelled"`** | **`READY`** ❌ | `status` is never checked, so a cancelled CBC counts as done. |
| `procedure_risk: "high"` (lowercase), via `pipeline.run` | **`READY`** ❌ | `facts.risk == "HIGH"` is false, so the case is treated as LOW/MODERATE: 30-day CBC window, no CMP required. Via `core.triage_submission` the same input raises a pydantic `ValidationError` instead, so the result depends on which entry point is used. |
| Consent text says *"patient has NOT signed consent yet"*, but the LLM returns `SIGNED` with the excerpt `"signed consent"` | **`READY`** ❌ | `validate()` only checks that the excerpt appears word-for-word. `"signed consent"` does appear, inside "NOT signed consent". **A verbatim excerpt proves the quote exists, not that it supports the claim.** |

**Not run, but follows from the code:** a fever entered in Celsius in the `value_f` field (e.g. 38.9) passes the `> 100.4` check. Units are never checked, and pydantic silently drops unknown fields such as `unit`.

**The pattern:** the recognized-values lists (anticoagulants, lab codes, vital types, risk levels) are **closed lists with silent fallthrough**. Anything unrecognized is treated as if it weren't there. For a gate, "absent" means follow-up, which is safe. For an **anticoagulant**, "absent" means "no anticoagulant, so no plan needed", which is the opposite of safe.

### 3.2 Paths that fail safe but give the wrong reason

| Change made | Result | Problem |
|---|---|---|
| BP 210/125 recorded with `type: "BP"` | `NEEDS_FOLLOW_UP`: "Missing latest blood pressure" | It should be `NOT_CLEARED`. The dangerous BP is invisible, and the coordinator is told the BP is *missing*. |
| H&P dated `"03/03/2026"` (not ISO) | `NEEDS_FOLLOW_UP`: "H&P missing" | The H&P exists. The date failed to parse, and the case reports it as "missing". |
| `procedure_date: "March 13, 2026"` | `NEEDS_FOLLOW_UP`: "Missing procedure date" | Same issue: "missing" and "unparseable" are indistinguishable. |
| Consent text has a double space or line break the LLM normalized away in its excerpt | `NEEDS_FOLLOW_UP`: "consent missing" | The claim is dropped by the strict substring check. This will be common with OCR'd scans. |

These are safe, but noisy. At scale, wrong reasons waste coordinator time and teach people to ignore the system's output.

### 3.3 Other messy-data risks (from reading the code, not run)

- **Wrong-patient documents.** Nothing checks that a document belongs to this patient or this procedure. A misfiled signed consent for another surgery satisfies the consent rule.
- **The plan isn't linked to the drug.** `_clear_plan_exists` accepts *any* clear plan. A patient on two anticoagulants with a plan for only one of them passes.
- **Timezones.** NOTES says timestamps are normalized to UTC. That's only true for choosing the *most recent* item. Window math uses the date as written in the string (`value[:10]`). With mixed offsets, the same moment can land on different calendar days, which matters exactly at the day-14 and day-30 boundaries.
- **`is_current` depends on wording.** The prompt only marks an H&P not current if the text *explicitly* says it's a retained prior. An old H&P without that phrase would be treated as current. It would only be caught if its date falls outside the window.
- **Conflicting claims.** Nothing stops the model returning two claims with different roles for one document. Both would be used.

---

## 4. What does not scale as it stands

The rules engine itself scales fine: it's pure Python and takes microseconds per case. The scaling problems are all around the LLM call and the fixed lists.

| Area | Current state | Why it breaks at volume |
|---|---|---|
| **One LLM call per package** | Every document goes in one prompt | Real packages contain pages of OCR'd text. Cost and latency grow with total document size, and you'll hit context limits. **One failure loses review of every document in the case.** |
| **Cache granularity** | Keyed on the whole package | Adding one document invalidates the cache for all of them. A resubmission with one new consent re-reads the unchanged 40-page H&P. |
| **Cache storage** | Unbounded in-memory `dict` | It grows without limit in a long-running service. It isn't shared between workers and is lost on restart. |
| **HTTP client** | A new `OpenAI()` client for each `triage_submission` | No connection reuse. |
| **Timeouts and backpressure** | SDK defaults (long timeout, 2 retries), no rate limiting | A traffic burst means rate-limit errors (429), then mass "Document review unavailable", which looks like ordinary output. A hung call ties up a worker for minutes. |
| **Execution model** | Synchronous; `run_baseline.py` processes cases one after another | Throughput is about one case per LLM round trip. |
| **Policy** | Thresholds and drug lists are Python constants | A second surgical center with a different policy, or a policy revision, needs a code fork or deploy. There's no `policy_version` in the output, so past decisions can't be explained against the policy in force at the time. |
| **Recognized-value lists** | Two drugs, two lab aliases, two vital types | Every new data source brings new spellings. Each one is a silent miss (§3.1) until someone notices. |
| **Observability** | One `logger.warning` on LLM failure | Nothing records which rules ran or were skipped, what the LLM claimed, what `validate()` dropped, or which model or prompt version was used. That isn't enough for a clinical audit, and nothing would detect drift. |
| **PHI handling** | Full document text, including patient names, is sent to OpenAI | This is fine for synthetic data. In production it needs a BAA or a HIPAA-eligible deployment, and identifiers should be removed before the call. |

---

## 5. Targeted improvements (prioritized)

> **Status:** P0 items 1–5 (with a wider lookup table instead of RxNorm) and P1 items 6–8 are implemented on `feat/triage-hardening`; see NOTES.md §8. P1 item 9 and all P2 items are still open.

**P0: close the false-READY paths**
1. **Normalize medications against a drug database.** Map names to RxNorm, then check drug class (the ATC code group B01, antithrombotics) instead of a two-name set. Brand names, doses and new drugs are then handled.
2. **Fail closed on unrecognized values.** An unrecognized medication, lab status, vital type or risk value should produce an explicit issue such as "Unrecognized medication 'Xarelto 20mg': confirm anticoagulant status". It should never fall through silently.
3. **Validate input once, strictly, at the boundary.** Use one versioned schema (the pydantic `PatientSubmission`, extended with a lab `status` enum, units and a normalized risk), applied the same way whatever the entry point.
4. **Check lab `status`.** Only `final` (and possibly `amended`) results count.
5. **Be stricter on positive claims than negative ones.** A wrong "signed" or "clear plan" is the dangerous direction, so:
   - Require the excerpt to contain the actual evidence (e.g. a signature line or the hold/resume instructions).
   - Add a deterministic check for negation phrases ("not signed", "pending", "unsigned"). If one appears, downgrade the claim to UNCLEAR.
   - Optionally, run a second verification call just for positive claims.

**P1: make the reasons right**
6. **Distinguish "missing" from "present but unparseable"** for dates and values.
7. **Normalize whitespace and Unicode** before the verbatim excerpt check (and do the same on the excerpt).
8. **Fix the timezone semantics.** Define the window in the surgical center's local timezone, convert to it, then take the date.
9. **Turn full document review back on for NOT_CLEARED cases**, so every follow-up item is listed in one round.

**P2: make it measurable**
10. **Run the live LLM and score it per document.** Report precision and recall per role, with special attention to the false-SIGNED and false-clear-plan rates. This is the single most informative experiment not yet run.
11. **Build a held-out set and an adversarial set:** brand-name drugs, negated consents, OCR noise, misfiled documents, long documents.
12. **Measure model variance** with the cache turned off (N runs per case, agreement rate). Report it separately from cache repeatability.

---

## 6. High-level production design

### 6.1 Shape

```
EHR / scheduling system
        │  submission event (FHIR / HL7 / API)
        ▼
┌───────────────────┐   invalid    ┌──────────────┐
│ Ingest + validate  │ ───────────▶ │ Reject / DLQ  │  (versioned schema contract)
└───────────────────┘              └──────────────┘
        │ valid; idempotency key = hash(submission)
        ▼
┌───────────────────┐
│ Normalize          │  RxNorm (meds), LOINC (labs), UCUM (units), timezone policy
│                    │  unrecognized → explicit issue, never silent
└───────────────────┘
        │
        ▼
┌───────────────────┐  safety fired?  (still reviews documents; see §5 item 9)
│ Rules engine (pure)│◀─────────────────────────────────────────────┐
│ policy = versioned │                                               │
│ config             │                                               │
└───────────────────┘                                               │
        │ needs document claims                                      │
        ▼                                                            │
┌──────────────────────────────────────────┐                        │
│ Document service (async workers)          │                        │
│  OCR → remove identifiers → per-document  │── claims ──────────────┘
│  classify → validate → verify positive    │
│  claims. Cache per document content hash. │
│  Timeouts, circuit breaker, rate limiter. │
└──────────────────────────────────────────┘
        │
        ▼
┌───────────────────┐     ┌────────────────────────────────┐
│ Decision + trace   │ ──▶ │ Results store (encrypted)       │
│                    │     │ decision, issues, full trace,    │
└───────────────────┘     │ policy, model and prompt versions│
        │                  └────────────────────────────────┘
        ▼
Coordinator work queue (NEEDS_FOLLOW_UP / NOT_CLEARED; in early rollout, also READY)
        │  coordinator overrides → labels → eval set
```

### 6.2 Key components

- **Queue-based orchestration** (e.g. SQS with workers, or a workflow engine such as Temporal or Step Functions). Triage is asynchronous, not a blocking request. Each step can be retried on its own, and an idempotency key makes resubmissions safe.
- **Classify each document separately.** Calls run in parallel, have bounded size, and can be retried individually. A cache keyed on each document's content hash means a resubmission only re-reads the documents that changed. Long documents are split into chunks or searched for the relevant section. This replaces the one-big-prompt call.
- **Policy as versioned configuration.** Windows, thresholds and drug classes live in a reviewed config file (e.g. YAML) per facility. Each output records the `policy_version` it was decided under. The rule *logic* stays in tested code; only the *parameters* move to config.
- **A trace for every decision**: normalized facts, every rule run or skipped (and why), raw and validated LLM claims, what `validate()` dropped, and model, prompt and policy versions. This is the audit record, and it's also how you debug a single case.
- **A distinct degraded state.** If the document service is down, the case is marked `PENDING_DOCUMENT_REVIEW` and retried. It isn't sent out as an ordinary NEEDS_FOLLOW_UP, and an alert fires.

### 6.3 Validation and testing

| Layer | What |
|---|---|
| Unit | The existing 218 tests, plus property-based tests (e.g. with `hypothesis`) on windows and boundaries: day 0, the window end, one day past it, timezone offsets. |
| Contract | Input-schema tests for each upstream source. Unknown values must produce explicit issues. |
| Golden set | Labeled cases, **with a held-out split**. CI fails if decision match or the false-READY rate gets worse. |
| LLM eval | Per-document role and field accuracy, run whenever the **model, prompt or schema changes**. The model version is pinned to a fixed snapshot, and every change goes through this eval. |
| Adversarial | Brand-name drugs, negated consents, misfiled documents, OCR noise, long documents. These are the §3 cases turned into permanent tests. |

### 6.4 Monitoring

- **Decision distribution** per facility over time. A sudden jump in NEEDS_FOLLOW_UP usually means an upstream data change or an LLM outage.
- **Rate of "Document review unavailable"**, LLM error rate, latency (p50 and p99) and cost per case.
- **`validate()` drop rate.** This is a direct proxy for hallucination or formatting drift.
- **Rate of unrecognized values** (new medication names, vital types, lab codes). This is an early warning of new data sources.
- **How often each rule fires.** A rule that suddenly never fires is as suspicious as one that always does.
- **Coordinator override rate**, especially overrides of READY. This is the real-world false-READY signal, and it feeds the eval set.

### 6.5 Deployment and rollout

1. **Shadow mode.** Run alongside human triage. Compare decisions and review the differences. No effect on scheduling.
2. **Assistive mode.** The system pre-fills issues, and a coordinator confirms every case, including READY.
3. **Partial automation.** READY is trusted only on the low-risk tier, and only after the measured false-READY rate is below an agreed threshold.

Other deployment practices:
- Containerized, stateless workers.
- Canary releases for model, prompt and policy changes.
- Feature flags for new rules.
- A HIPAA-eligible LLM endpoint (BAA, in-region, no data retention).
- Encrypted results store with access logging.

---

## 7. Summary

The core design is sound and I'd defend it. Policy lives in deterministic code, the LLM is narrowly scoped to return pointers, a single `requires`/`unavailable` mechanism handles missing data, and the pipeline never returns READY without document review.

Its weaknesses:
- **Closed recognized-value lists with silent fallthrough.** These create real false-READY paths: brand-name anticoagulants, cancelled labs, lowercase risk.
- **A verbatim-excerpt check that proves a quote exists, not that it supports the claim.**
- **An evaluation that hasn't yet measured the LLM** and has almost no READY cases.

To scale, the main changes are: classify each document separately with per-document caching, run asynchronously from a queue, move policy parameters into versioned config, and record a full trace for every decision.
