---
title: "Research Grounding Anchor"
category: procedures
tags: [prompt-engineering, research, grounding]
status: archived
---

# Research Grounding Anchor

Rigid citation demands can make a reasoning model optimize for compliance rather than truth. Append
this anchor to research prompts when factual fidelity and explicit uncertainty matter.

```xml
<grounding_instructions>
Before answering, identify the facts required, draft a tentative answer, and critically check it.
Remove claims that cannot be supported by the available evidence.

For every major claim or recommendation, label it as one of:
- [VERIFIABLE FACT]: provide a direct primary source URL or DOI.
- [INDUSTRY HEURISTIC]: a useful practice without a specific scholarly source.
- [SYNTHESIZED INFERENCE]: a conclusion derived from the supplied evidence; explain the rationale.

Never invent paper titles, identifiers, citations, or URLs. If evidence is unavailable, say
[NO SOURCE FOUND] or state that the data is not verifiable. Use text-based diagrams and equations.

For technology evaluations, state a failure mode for each recommendation, distinguish sourced from
estimated performance claims, compare options against explicit criteria, and identify what must be
validated before adoption.
</grounding_instructions>
```

## Temporal and follow-up research

For a time-bounded request, name the time window and state that a thin period may correctly yield
"no significant new developments found". Do not backfill the answer with older material merely to
avoid an empty result.

For a follow-up, provide a short background section listing established findings and direct the model
to investigate the delta rather than repeat the earlier survey.
