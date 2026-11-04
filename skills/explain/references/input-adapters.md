# Input adapters

Choose one primary adapter from the resolved target. Add a secondary lens only when it is necessary
to answer the user's stated relationship. For every adapter, inspect the target first and then only
the minimum sufficient evidence. A missing or ambiguous target produces one focused clarification or
an explicit limitation, never a substituted target.

## Adapter selection

1. Use the type or direct target the user explicitly supplies.
2. Otherwise let a resolvable document path, issue ID, code selection, or symbol determine the route.
3. Treat a bare term as a technical concept unless repository evidence establishes a local meaning.
4. For mixed targets, keep one primary adapter and add only the necessary secondary evidence.

## Design document

**Inspect:** the problem and context, constraints, decisions, alternatives, non-goals, linked
evidence, open questions, status, and implementation audit when present. Follow a linked research
record only when it establishes the relationship being asked about. If current material does not
record intent, use Git history as a targeted fallback, not as automatic repository archaeology.

**Explain:** problem → constraints → decision → tradeoffs → consequences. Distinguish proposed design,
recorded decision, and shipped implementation. Name what the design deliberately excludes.

**Do not:** present a proposal as shipped behavior, treat an unchecked audit row as verification, or
invent author intent. If no source records the rationale, label the best supported account as
**Inference** or say **Unknown**.

## Plan document

**Inspect:** objective, ordered steps, dependencies, gates, risks, acceptance criteria, current
status, and linked design or implementation evidence needed for the question.

**Explain:** why the sequence exists, what each step unlocks, the critical path, how a gate can fail,
and what evidence completes it. Separate planned work from observed completion.

**Do not:** reword the checklist without causal links, infer completion from a checked box alone, or
expand into implementation advice the user did not request.

## Beads issue

**Inspect:** live fields, dependency edges, acceptance criteria, comments, and related artifacts.
Use the local `bd show <issue>` surface when the active environment can access its database. Read
only the issue neighborhood needed for the requested blocker, purpose, or completion relationship.

**Explain:** purpose, expected versus current state, scope, actual dependency direction, blockers,
and definition of done. Prefer current live fields over copied prose.

**Unavailable path:** if live fields are unavailable, disclose exactly that limit. Do not present
stale prose as current state, and never treat a `related` edge as a blocking dependency. You may
explain a supplied snapshot as a snapshot if you label it with its observed date or revision.

**Do not:** invent missing status, ownership, comments, acceptance criteria, or dependency direction.

## Code

**Resolve:** require selected code, a resolvable file/symbol, or repository evidence that uniquely
identifies the requested target. Do not invent a code target from a name alone. If two matches would
produce materially different traces, ask which one.

**Inspect:** the selected symbol, direct callers and callees needed for the question, relevant types,
tests, configuration, and targeted history only when current evidence lacks recorded rationale. Do
not read the whole repository by default.

**Explain:** purpose, inputs and outputs, control and data flow, state changes, invariants, errors, and
a concrete trace when useful. Ground behavior in implementation and tests rather than identifier names.

**Do not:** narrate every line, infer runtime behavior from a function name, or present a commit
message as stronger evidence than current code and tests.

## Technical concept

**Inspect:** an authoritative stable definition when the request requires external factual grounding,
plus repository-local usage when one exists. If authoritative material cannot be accessed, bound the
answer to stable knowledge and disclose any claim that could not be verified. A bare concept does not
need an invented local citation.

**Explain:** prerequisites, mechanism, a useful contrast, a worked example, and limitations. When an
analogy helps, state its mapping and the point where it stops matching the mechanism.

**Do not:** merge a repository-specific convention into the general definition, hide a disputed
definition, use an unbounded analogy, or silently turn the request into `/summarize` or new research.
