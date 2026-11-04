# Code quality steering

Revision: `quality-steering-v1`

Use this compact policy when authoring or reviewing repository code. Repository contracts, tests,
and required behavior remain authoritative.

1. Reuse an existing capability.
2. Extend an existing capability.
3. Compose existing primitives.
4. Use an existing dependency.
5. Add a dependency only with repository-policy and evidence.
6. Write the smallest local implementation.
7. Create a new abstraction last.

Inspect adjacent implementation, tests, and contracts before naming an abstraction or dependency.
Implement semantic behavior first, run focused verification, then run the repository's declared
format, type, lint, and test commands before administrative completion. Do not use an AI-origin
score, a universal style score, or deletion solely to shrink a patch.

## Diagnostic contract

Every quality diagnostic must identify all of the following:

1. The violated invariant.
2. File-and-line evidence, or the precise evidence unavailable for an aggregate finding.
3. A safe, local remediation that preserves repository contracts, tests, and required behavior.
4. A prohibited metric-gaming workaround (for example, splitting code only to lower a numeric
   score, or deleting behavior merely to shrink a patch).
5. The documented suppression or escalation path.

If a diagnostic cannot provide evidence or a safe remediation, it is advisory only and must not
block. Escalate instead when a required behavior, contract, or product decision is ambiguous.

## Evidence-mining registry

`quality-steering-manifest.toml` revision `quality-steering-evidence-v1` is the versioned record
of the 2026-09-02 source review. It has one closed-schema disposition for every Comparison row,
every currently enumerated fleet-stack item, every named blindspot, and each bespoke-rule candidate.
Allowed dispositions are `adopted`, `covered`, `pilot`, `deferred`, `rejected`, and
`not-applicable`; every record names an owner, rationale, and source evidence.

The current research has 21 Comparison rows, 13 stack items (not the design's stale count of 12),
and 20 blindspots. A bespoke-rule candidate is non-rejected only when its manifest record contains
at least two independently reviewed repository occurrences with reviewer dispositions. Otherwise
it is rejected before Phase 6 can author an adapter, registry row, or fixture. The manifest validator
and focused regression fixture enforce the closed schema and inventory completeness.
