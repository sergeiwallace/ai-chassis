# Examples and evaluation

Use these examples to calibrate branching, not as fixed response templates. Replace fictional names
and evidence with inspected sources. Run the scenario matrix when changing procedure, adapter, depth,
or faithfulness rules.

## Branching examples

### Quick refresher

**Request:** `/explain quick refresher: why does the release plan verify before tagging?`

**Answer shape:** The verification gate prevents a tag from asserting that an untested revision is a
release candidate. The build and tests produce evidence about the exact revision; tagging afterward
binds the release name to that already-checked state. If the tag came first, a failed gate would leave
a misleading or mutable release marker.

**Evidence:** the plan's ordered release steps and tag gate. Stop here unless the user asks for the
rollback path or CI trace.

### Novice concept with a bounded analogy

**Request:** `/explain optimistic locking; I know SQL but not concurrency control`

**Answer shape:** Optimistic locking lets readers proceed without holding a lock, then rejects a write
if the record changed since it was read. The usual mechanism includes a version in the update
condition: `UPDATE ... WHERE id = ? AND version = ?`; updating zero rows means another writer won.

**Mapping:** Think of the version as the revision number on a shared draft: you may submit changes
only against the revision you actually reviewed.

**Where it stops:** A database performs an atomic conditional write; a human document workflow may
merge changes or allow overwrites. The analogy does not model transaction isolation or retry policy.

### Code trace

**Request:** `/explain trace RetryClient.send through one timeout`

**Answer shape:** Resolve `RetryClient.send`, its timeout classifier, the retry-policy type, and the
tests for timeout behavior. Trace one concrete call: input enters `send`; the transport raises the
observed timeout; the classifier marks it retryable; policy state increments; configuration admits or
rejects the next attempt; the final result or error returns. Name each inspected symbol and do not
infer retryability from a helper called `is_retryable` without reading it.

**Evidence:** exact file/symbol, policy type, configuration key, and timeout test references.

### Design rationale with no recorded decision

**Request:** `/explain why this design chose polling over events`

**Answer shape:** The design proposes polling and records constraints on deployment simplicity, but it
contains no decision rationale comparing polling with events.

**Inference, not recorded rationale:** Polling may have been selected because it satisfies the stated
deployment constraint with fewer components. This follows from the constraint and architecture, not
from an authored decision record.

**Unknown:** Whether delivery guarantees, latency, operating cost, or team familiarity drove the
choice. Offer to inspect linked research or targeted Git history; do not convert the inference into
author intent.

## Evaluation method

For each row, give one reviewer the same scenario and evidence packet twice: once with the generic
baseline prompt `Explain this.` and once through `/explain`. Keep target content, available tools, and
model settings fixed. Score both responses independently before comparing them. A style preference is
not a pass condition.

Use a 0–2 scale: 0 = missing or contradicted, 1 = partial, 2 = complete for the requested depth.
Record a short evidence note for every zero or one.

- **Faithfulness** — concrete claims follow inspected evidence; observed facts, recorded rationale,
  inference, and unknowns are not conflated.
- **Added understanding** — the response adds mechanisms, relationships, or implications rather than
  paraphrasing the target.
- **Calibration** — it honors explicit signals or uses the technical-but-unfamiliar default without an
  unnecessary intake question.
- **Causal completeness** — it exposes enough of the chain to predict the requested consequence.
- **Uncertainty** — it identifies missing, stale, disputed, or unavailable evidence at the point it
  matters.
- **Navigation** — evidence references and at most two deeper routes let the reader verify or continue.
- **Depth** — density and examples fit the request without weakening rigor or emitting empty layers.

Always score faithfulness and depth separately: a concise answer can be faithful, and a detailed
answer can still invent rationale.

## Scenario matrix

| ID | Adapter | Calibration | Prompt and evidence hazard | Required result |
|---|---|---|---|---|
| S1 | design | explicit | “Quickly explain why D-2 won”; decision and alternatives are recorded | Name the recorded rationale and one consequence without expanding every section |
| S2 | design | absent | “Explain the choice of queues”; absent rationale despite a proposed queue | Label intent as inference or unknown; do not present the proposal as shipped |
| S3 | plan | explicit | “Deep trace of why migration precedes cleanup”; dependencies and rollback gate supplied | Explain ordering, unlocks, failure gate, and verification evidence |
| S4 | plan | absent | “Explain this plan”; long checklist with one critical dependency | Lead with objective and critical path instead of rewording each item |
| S5 | issue | explicit | “Briefly explain the blocker”; stale issue prose conflicts with live status | Prefer live fields, state actual dependency direction, and flag the stale prose |
| S6 | issue | absent | “Explain issue X”; live fields cannot be opened and only a `related` edge is visible | Disclose unavailable live data and do not call `related` blocking |
| S7 | code | explicit | “Teach me by tracing one error”; misleading code names disagree with tests | Follow implementation and tests, show one trace, and label the name mismatch |
| S8 | code | absent | “Explain RetryClient”; two symbols match in different modules | Ask one focused target clarification rather than inventing a symbol |
| S9 | concept | explicit | “Explain leases to a novice with an analogy”; local use differs from the stable definition | State mapping and stopping point; separate stable definition from local use |
| S10 | concept | absent | “Explain idempotency”; no repository-specific use or local source | Use the conservative default and do not invent a local citation |

The adversarial paths are: **stale issue prose** (S5), **misleading code names** (S7), and
**absent rationale** (S2). Each must force an uncertainty or provenance label.

## Contract-level acceptance review

The following review scores the checked-in expected-result contract against the generic baseline's
single instruction. It is not a live-model benchmark. Each tuple is `Faithfulness / Added
understanding / Calibration / Causal completeness / Uncertainty / Navigation / Depth`.

| Scenario | Generic baseline contract | `/explain` contract | Result |
|---|---|---|---|
| S1 | 1 / 1 / 0 / 1 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: explicit quick depth and recorded rationale are required |
| S2 | 0 / 1 / 1 / 1 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: absent intent must be inference or unknown |
| S3 | 1 / 1 / 0 / 1 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: sequence, unlocks, gate, and evidence are required |
| S4 | 1 / 0 / 1 / 0 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: objective and critical path replace checklist narration |
| S5 | 0 / 1 / 1 / 1 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: live status and stale prose conflict remain visible |
| S6 | 0 / 1 / 1 / 0 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: unavailable fields and non-blocking `related` edge are explicit |
| S7 | 0 / 1 / 0 / 1 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: tests outrank misleading names and learning mode gets a trace |
| S8 | 0 / 0 / 1 / 0 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: material ambiguity produces one focused question |
| S9 | 1 / 1 / 0 / 1 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: analogy mapping, limit, and local/general split are required |
| S10 | 1 / 1 / 1 / 1 / 0 / 0 / 1 | 2 / 2 / 2 / 2 / 2 / 2 / 2 | Pass: conservative default and no invented citation are required |

Before broader release, run the same scenario set as a fresh-context live comparison. Preserve raw
responses and reviewer notes; do not replace measured scores with this contract-level review.
