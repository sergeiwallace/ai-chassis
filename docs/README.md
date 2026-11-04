# Documentation Guide

This directory holds the harness's durable project documentation. Use repository search to find a
specific topic; the sections below orient you to the kinds of material kept in each area.

The workstation profile is named in the machine registry. Historical records may use earlier
names; history decoders map them through the session-name migration config. Held-store
directories move separately during the coordinated cutover.

| Directory | Purpose |
| --- | --- |
| `analysis` | Time-bounded investigations, reconciliations, and assessments of repository or fleet state. |
| `architecture` | The canonical `architecture` doc kind (template, stub, authoring prompt): how a system is built, level by level (context, containers, components), diagrams first. Scaffold with `/doc --kind architecture`; review against `procedures/visual-doc-quality-rubric.md`. |
| `archive` | Retained historical documentation that is no longer active but remains useful for context. |
| `audits` | Formal reviews of designs, procedures, configuration, and operational posture, including findings and follow-up evidence. |
| `bugs` | Reports and investigations of defects, regressions, and operational failures. |
| `conversations` | Captured discussions whose decisions or context merit durable reference. |
| `decisions` | Focused records of technical or operational choices and their rationale. |
| `designs` | Architecture and design proposals, tradeoff analysis, and design templates. |
| `docket` | Structured work-organization material for work awaiting triage, scheduling, or review. |
| `exports` | Verbatim exports of outside source material, one subdirectory per origin: `chatgpt` (ChatGPT conversations), `google-ai-mode` (Google AI mode transcripts) and `google-docs` (documents originally authored in Google Docs). Source records, not authored docs. |
| `feedback` | Review feedback, responses, and templates for recording it consistently. |
| `git-review` | Repository and fleet Git-state reviews, including branches, worktrees, and cleanup assessments. |
| `handoffs` | Context packages that transfer active work, decisions, and next steps between sessions or owners. |
| `held` | Outputs the maintainer must act on (decisions, blockers, `/exit-goal` reports, fleet briefs), held in per-session files instead of chat; one directory per session, `open` → `answered` → retired. See `held/README.md`. |
| `plans` | Implementation plans that translate approved work into phases, tasks, and validation criteria. |
| `procedures` | Maintained operating procedures and authoring standards for harness development and documentation. |
| `prompts` | Reusable prompt material and prompt-authoring guidance for harness workflows. |
| `research` | Evidence-based investigations, landscape surveys, and technical comparisons used to inform decisions. |
| `roadmap` | The canonical `roadmap` doc kind (template, stub, authoring prompt): a visual-first program or system roadmap whose map and zoom panels reuse the architecture document's diagrams. Scaffold with `/doc --kind roadmap`; review against `procedures/visual-doc-quality-rubric.md`. Superseded harness roadmaps live in `roadmap/archive/`. |
| `social` | Draft or reusable material intended for public or social communication. |
| `specs` | Requirement-oriented specifications and templates for defining scoped work. |
| `templates` | Reusable documentation and contribution templates that do not belong to a more specific document kind. |
| `test` | Test plans, test findings, and templates for documenting verification work. |
| `tools` | Documentation for project tooling, commands, and tool-related investigations. |
