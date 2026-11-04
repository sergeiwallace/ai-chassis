## Claude-specific role guidance

Use specialized agents when independent research, implementation, or review materially improves the
result. The primary roles are researcher (read-only evidence gathering), engineer (scoped changes),
reviewer (read-only correctness and security review), architect (design analysis), and auditor
(evidence-backed conformance review). Report the selected role and model when delegating, keep the
delegated scope bounded, and verify returned claims before integration.

Claude's built-in worktree isolation may be used when repository policy permits it. Built-in features
must be verified against current product documentation rather than inferred from local command files.
Some fleet-specific operational content was excluded.
