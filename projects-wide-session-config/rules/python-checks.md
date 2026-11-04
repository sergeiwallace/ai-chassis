---
description: Python automated-checks hard gate (ruff + pytest). Path-scoped — loads only when touching Python files.
paths:
  - "**/*.py"
---

# Automated Checks (dev-workflow step 13)

**Hard gate**: `ruff check src/ tests/ && ruff format --check src/ tests/ && pytest` must pass.
