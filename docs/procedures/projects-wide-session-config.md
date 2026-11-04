# Projects-Wide Context Files

Projects-wide instruction files are compiled artifacts. Edit source modules, compile them, check
output limits, and install from a durable main checkout. Never edit generated instruction files
directly: the next compilation or installation replaces them.

## Architecture

```text
shared base + agent-specific delta + machine overlay
                  -> compiler -> generated context files -> installer links
```

Keep shared policy in the base, tool behavior in an agent delta, and platform facts in a machine
overlay. This gives each output a single, inspectable source chain.

## Edit and verify

1. Find the source module that owns the rule; do not patch compiled output.
2. Edit the module and run the repository compiler.
3. Run the line and character size audit.
4. Run focused compilation and installation tests.
5. Commit and integrate before a live installer.

```bash
python3 projects-wide-session-config/compile.py
python3 projects-wide-session-config/check-sizes.py
pytest tests/ -k 'install or session_config'
```

A temporary worktree may develop changes, but live links must target a durable checkout on the
integration branch. Do not point a user-facing symlink into a disposable worktree. Test compilation
into a temporary output directory when validating without touching live links.
