---
title: "Single source of truth for a merge-gate step chain (CI + local harness)"
category: research
status: draft
source: "Genericized from prior personal research; external claims cited inline."
template_version: "research-1.1.0"
---
<!-- doc:region name="context" kind="immutable" -->

## Context

(none — context not recorded in original doc)

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->


## Question

How should a repo maintain a **single source of truth** for a multi-step "merge gate" step chain so that changing it in one place updates **both** the CI workflow and the local developer harness (a shell script that runs the same gate), with minimal drift?

## Baseline — what tends to be duplicated

A typical gate is a small ordered chain run against a deployed (or locally-running) app, with identical pass/skip/gate semantics implemented twice — once in the CI workflow YAML, once in a local shell harness:

| Concern | CI workflow | Local harness (shell) |
|---|---|---|
| Step 1 | end-to-end test command | same command |
| Step 2 | an integration/bridge check with args | same argv |
| Step 3 | an evaluation check with a dataset + threshold | same argv, values from env |
| Key-guard | later steps gated on an API key being present (skip, non-blocking) | `if [ -n "$API_KEY" ]` around each (skip, non-blocking) |
| Exit contract | `0` pass · `1` regression → fail · `2` config/data → warning, non-blocking | same three-way contract |
| Gate mode | warn-mode via per-step `continue-on-error` | warn-mode by demoting non-zero to `exit 0` |
| TLS bypass | scoped env var on the relevant steps | same, only for `https://` targets |
| Credential source | secret manager via runner identity | env vars / running container |

The **chain, order, per-step flags, key-guard, gate-mode decision, and the evaluation exit-code contract** are the parts genuinely duplicated. The **credential source** differs by design (CI uses a secret manager; local reads env/container) and is *not* a candidate for sharing — but both should converge on the same env var names, which is the shared contract that makes the rest shareable.

A useful precedent: a **YAML-as-text parity test** — a unit test that reads the workflow file and asserts invariants (no plaintext secrets, the secret-fetch step is present, the key-guard `if:` appears the expected number of times). The same mechanism can guard chain parity.

## Approaches

| Approach | How it removes duplication | Must stay workflow-specific | Testability | Portability | Verdict |
|---|---|---|---|---|---|
| **A. Shared script/module orchestrator** (e.g. `run-gate.mjs`) that owns the ordered chain, flags, key-guard, and exit-contract; the YAML and the shell harness each shrink to *env/secret setup → one call → gate-mode wrapper* | Chain + order + flags + key-guard + exit-contract live once, as an exported data structure | Secret fetch, env/TLS setup, `continue-on-error` gate-mode, artifact upload, matrix | **Best** — the chain is a module, unit-testable with the project's existing test runner; no bash to test | The runtime is already present in both CI and dev; one script convention | **Recommended** |
| **B. Shared Bash orchestrator** called by both | Same de-dup as A | Same as A | Poor — bash logic is hard to unit-test; relies on the YAML-text parity test only | Bash present in CI and Git-Bash; but the exit-contract + key-guard in bash aren't unit-testable | Viable, weaker than A |
| **C. Task runner as the single entry** both invoke | CI step becomes a single task-runner call; local already is | GHA-native reporting still in YAML around the call; the recipe still wraps the same bash → duplication just moves into the recipe | Low — recipe is bash | Task runner would need installing on the CI runner | Complementary, not a solution by itself |
| **D. Declarative manifest** (JSON/YAML list of steps/flags/exit-contract) consumed by a thin runner in both places | Manifest is the one definition | Same GHA-native shell | Good if the runner is a real language | Bash can't read JSON without `jq`; forces a non-bash runner anyway — collapses into A with an external file instead of an exported constant | Use the *idea* (a data structure), but keep it as a module (A), not a standalone file bash must parse |
| **E. GitHub composite action** wrapping the chain | De-dups across **workflows** | N/A | Testable only via `act`/live | **Cannot be invoked by a local bash harness at all** — so it does not solve CI-vs-local drift | Reject for this use case |
| **F. `act` to run the real workflow locally** | Zero duplication (one YAML is the source) | — | — | `act` supports **only Ubuntu Docker runners**; multi-label self-hosted runners map only by a single label, no metadata server (secret fetch fails), concurrency ignored, default images lack tools ([act docs / user reports][act-limits]) | Reject as the mechanism; optional smoke-only convenience at best |

## Key tension: GHA-native reporting vs. a shared script

GitHub Actions' `::warning::`/`::error::` annotations, `$GITHUB_STEP_SUMMARY`, per-step `continue-on-error`, `if:` guards, `steps.<id>.outcome`, matrix, and secret masking are **runner-native workflow commands** — they require an active runner and mean nothing in a plain local shell ([GitHub docs: workflow commands][gha-wf-cmds]; [reusable workflows][gha-reusable]). This is why the chain can't simply "become" the workflow.

But the seam is cleaner than it looks, because the workflow commands **degrade gracefully**: `echo "::error title=X::msg"` and `echo "line" >> "$GITHUB_STEP_SUMMARY"` are just stdout writes. On the runner they render as annotations/summary; run locally they are inert plain text (or a harmless append to a temp file). So a **shared orchestrator can emit them itself** — CI gets real annotations, local gets readable log lines — *without* the YAML re-declaring the chain.

What genuinely **cannot** move into the shared script:

- **Secret fetch** — a secret-manager token (CI) vs env/container (local) are environment-specific. Keep separate; converge on the same exported env var names.
- **`continue-on-error` gate-mode demotion** — GHA expresses "non-blocking" as a per-step YAML attribute; a shell expresses it as "demote non-zero to `exit 0`". Both are ~2 lines wrapping the orchestrator's exit code.
- **Matrix / `if:` event filters / `upload-artifact`** — workflow-native plumbing.

Everything else — **the ordered step list, each step's argv/flags, the API-key-guard skip, and the evaluation exit-contract → block decision** — is pure logic that belongs in one place.

**The clean seam:** the shared orchestrator owns *"run these steps in this order with these flags, skip observability steps when the key is absent, interpret the evaluation exit code, and return a single block/no-block exit code."* Each caller owns only *"resolve credentials for my environment, set TLS, call the orchestrator, and wrap its exit code in my environment's non-blocking idiom."* The orchestrator may additionally emit `::` workflow commands + summary lines that are annotations in CI and plain text locally.

## Recommendation

Adopt **Approach A**: extract the chain into a single script/module orchestrator and reduce both callers to thin wrappers.

### What the orchestrator exposes

```js
// run-gate.mjs
// THE single definition of the gate chain. Any change to steps/flags/order/
// exit-contract happens here and here only.
export const GATE_STEPS = [
  { id: "e2e",    argv: ["<end-to-end test command>"],           requiresKey: false, block: (code) => code !== 0 },
  { id: "bridge", argv: ["<integration check>", "--url", TARGET], requiresKey: true,  block: (code) => code !== 0 },
  { id: "eval",   argv: ["<evaluation check>", "--dataset", DATASET, "--min-pass-rate", MIN_PASS],
                  requiresKey: true,
                  // Eval exit contract lives ONCE: 0 pass · 1 regression (block) · 2 config/data (warn)
                  block: (code) => code === 1, warn: (code) => code >= 2 },
];
// Reads TARGET/DATASET/MIN_PASS + the credential env vars from env.
// For each step: skip (non-blocking) if requiresKey && !API_KEY;
// else spawn argv, capture outcome, apply block()/warn(). Emit ::warning/::error
// + append a per-step line to GITHUB_STEP_SUMMARY when the var is set (plain-text
// no-op locally). Exit non-zero iff any step's block() is true. NEVER decides
// enforce-vs-warn — that is the caller's wrapper.
```

Exit contract of the orchestrator itself: **`0` = gate clear, non-zero = would-block** (aggregating the per-step `block()` results). Gate-mode (enforce/warn) is applied by the caller around this exit code.

### How the YAML calls it

The workflow keeps its secret-fetch steps, then collapses the gate steps into one:

```yaml
- name: Run merge gate
  id: gate
  continue-on-error: ${{ env.GATE_WARN == 'true' || inputs.override_gate == true }}
  run: node run-gate.mjs               # emits ::error/::warning + $GITHUB_STEP_SUMMARY itself
```

`continue-on-error` still delivers warn-mode; `upload-artifact` and the `if:` event filter are untouched.

### How the local harness calls it

Keep the local credential seam (validate target, resolve credentials from env/container, set TLS for https). Then replace the inline chain with:

```bash
node run-gate.mjs; gate_rc=$?
if [ "$gate_rc" -eq 0 ]; then echo "MERGE GATE: PASS"; exit 0; fi
if [ "$GATE_MODE" = warn ]; then echo "MERGE GATE: FAILED but warn — non-blocking"; exit 0; fi
echo "MERGE GATE: FAILED (enforce)"; exit 1
```

Any task-runner recipes that already wrap the harness are unchanged.

### What deliberately stays duplicated (and why)

| Stays duplicated | Why it can't move | Mitigation |
|---|---|---|
| Credential resolution (secret manager vs env/container) | Environment-specific; sharing would drag CI-specific metadata into local | Both export identical env var names → orchestrator reads one contract; parity test asserts both set the credential vars |
| Gate-mode demotion (`continue-on-error` vs bash `exit 0`) | GHA-native attribute vs shell idiom; ~2 lines each | Orchestrator owns the block/no-block *decision*; wrappers only demote. Parity test asserts both reference the gate-mode var |
| TLS bypass env | Correctly scoped differently (CI per-step; local https-only) | Comment + leave; low drift risk |

### Parity guard (catch drift mechanically)

Add a parity test extending the YAML-text-assertion pattern:

1. **Single-definition assertion** — import the gate-steps data structure from the orchestrator and assert its order/ids/flags (the step ids in order, the dataset/threshold values, the evaluation `block` firing on `1` not `2`). A real unit test (fails if the body is gutted), so the chain is a *code* change with a test, not copy-paste.
2. **No re-declaration in YAML** — assert the workflow invokes the orchestrator and does **not** contain the raw step commands as separate inline `run:` steps.
3. **No re-declaration in bash** — assert the local harness invokes the orchestrator and likewise doesn't re-list the commands.
4. **Shared knob names** — assert both files reference the gate-mode env var and that the orchestrator reads the same env var names both callers export.

This makes any future drift (a new step, a changed flag, a changed exit rule added in only one place) a red test rather than a silent divergence.

## Sources

- [GitHub Actions — Workflow commands (annotations, `$GITHUB_STEP_SUMMARY`, masking); require an active runner][gha-wf-cmds]
- [GitHub Actions — Reusing workflows (reusable workflows as single source of truth)][gha-reusable]
- [GitHub Actions — Creating a composite action][gha-composite]
- [GitHub Actions — `continue-on-error` and `steps.<id>.outcome`][gha-coe]
- [nektos/act — limitations: Ubuntu-only Docker runners, single-label self-hosted mapping, no services, concurrency ignored, default-image tool gaps][act-limits]

[gha-wf-cmds]: https://docs.github.com/en/actions/reference/workflow-commands-for-github-actions
[gha-reusable]: https://docs.github.com/en/actions/using-workflows/reusing-workflows
[gha-composite]: https://docs.github.com/en/actions/tutorials/create-actions/create-a-composite-action
[gha-coe]: https://docs.github.com/en/actions/reference/workflow-syntax-for-github-actions#jobsjob_idstepscontinue-on-error
[act-limits]: https://nektosact.com/

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

[Original prompt not recorded in this doc]

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

(none yet — auto-populated at commit from the validated provenance ledger)

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

<!-- /doc:region name="run_history" -->
