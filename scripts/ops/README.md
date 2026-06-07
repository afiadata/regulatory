# Ops Scripts

One-time environment setup scripts that require elevated Postgres privileges.
These are NOT run by Alembic — they must be executed manually by an operator.

## One-time setup order

```
1. Create the read-only role (requires CREATEROLE privilege):
   psql $DATABASE_URL -f scripts/ops/create_readonly_role.sql

2. Run migrations (applies GRANTs automatically if the role exists):
   alembic upgrade head
```

If you run migrations before step 1, re-run `create_readonly_role.sql` after
migration 0004 — the script's second `DO` block applies the grants retroactively.

## `create_readonly_role.sql`

Creates the `regulatory_readonly` Postgres role with `NOLOGIN`. The risk
engine's downstream consumers (Claude agent, future FastAPI service) should
authenticate as a login role that is a member of `regulatory_readonly`.

This role has `SELECT` on:
- `manufacturers`
- `counties`
- `suppliers`
- `county_supply`
- `risk_signals`
- `risk_signal_events`
- `documents` and `document_versions` (added in migration 0007 for agent tools)

It has **no** `INSERT`, `UPDATE`, or `DELETE` grants on any table.
`risk_signal_events` is append-only by policy; the application role writes it
but `regulatory_readonly` can only read it.

## `create_agent_roles.sql`

Creates two roles needed by the agent (added in migration 0007):

- `regulatory_agent_writer` — `INSERT`-only on `agent_audit_log`. The agent
  runner uses a connection as this role (or a login role that is a member) to
  append audit rows. No `UPDATE` or `DELETE` is ever granted.

- `regulatory_ops` — `SELECT`-only on `agent_audit_log`. Used by ops/forensic
  review tooling (`regulatory agent audit list/show/cost`).

## Updated setup order

```
1. Create roles (requires CREATEROLE privilege):
   psql $DATABASE_URL -f scripts/ops/create_readonly_role.sql
   psql $DATABASE_URL -f scripts/ops/create_agent_roles.sql

2. Run migrations (applies GRANTs automatically if the roles exist):
   alembic upgrade head
```

Run steps in order. If migrations ran before step 1, re-run both role scripts —
each script's second `DO` block applies missing grants retroactively.
