# Example Claude machine profile

This is a starting template for one machine or organization-specific Claude
overlay. Copy it to `modules/claude-{machine}.md`, replace the synthetic
examples with facts that are useful to the local workflow, and enable the
overlay in the local configuration. Keep one overlay per distinct environment
when its operating instructions differ. That keeps the shared base useful
everywhere without making it guess which machine or organization is active.

## Machine identity and workspace

> Example only. This profile applies to `AI_HOST=acme-corp-workstation-01`, a
> Windows workstation used for Acme Corp work.

- Work repositories live under `~/work/`. The adjacent Acme-specific checkout
  is `~/work/acme-sibling-repo`.
- Use Git Bash for shell examples and project commands. Confirm the current
  repository and branch before editing files.
- Document a platform-specific tool location here when it changes the command
  an agent should use, such as `C:/Tools/acme-cli/bin` or a repository-local
  virtual environment.

## Access bootstrap

Before work that requires organization services, complete the approved SSO
sign-in flow for this machine. For example, a local runbook might direct a
developer to start a browser-based sign-in command and then confirm access at
`https://portal.example.internal`.

State only the workflow and the safe verification step. Keep account names,
tenant identifiers, device registration details, and browser-profile details
out of this file.

## Machine-specific operating notes

- Use the organization-approved package source for required workstation tools;
  keep project dependencies in the repository environment.
- If a tool has a Windows-specific path or shell requirement, record the
  portable instruction here. For example, note that a command must run from
  Git Bash rather than from another terminal.
- Link to a separately protected internal runbook when work needs details that
  are inappropriate for version control, such as service routing or recovery
  steps.

## Boundaries and maintenance

This overlay should describe behavior, not private inventory. Do not add
account names, network addresses, hostnames from a real environment, tokens,
secrets, customer data, or copied internal procedures. If an instruction is
useful on every machine, move it to the shared base or the Claude delta instead
of duplicating it here. Revisit this overlay when the machine, authentication
flow, workspace location, or toolchain changes.
