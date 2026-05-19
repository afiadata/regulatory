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

It has **no** `INSERT`, `UPDATE`, or `DELETE` grants on any table.
`risk_signal_events` is append-only by policy; the application role writes it
but `regulatory_readonly` can only read it.
