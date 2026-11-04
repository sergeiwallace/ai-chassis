---
description: Secret-output, DB-DSN, and migration safety rules. Path-scoped — loads when editing sensitive configuration or database files.
paths:
  - "**/.env"
  - "**/.env.*"
  - "**/*secret*"
  - "**/*credential*"
  - "**/*database*"
  - "**/alembic/**"
  - "**/migrations/**"
  - "**/*.sql"
---

# Sensitive configuration and database safety

- **DB DSNs: never hand a `+driver`-scheme DSN to a raw client (scheme-echo leak).** A `postgresql+psycopg://…` DSN passed to raw `psql`/`psycopg` echoes the full DSN, password included. Read the DSN from env, strip `+driver` to plain `postgresql://`, print `type(e).__name__` not `str(e)`, prefer a SQLAlchemy engine. Rotate on exposure.
- **DB migrations**: migrate schema, never delete; `ALTER TABLE RENAME COLUMN` for SQLite renames.
