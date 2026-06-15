# `get_session()` does not auto-commit; behavior across writers is inconsistent

> Status: open follow-up against `feat/nl-agent` PR, identified during Eval Round 2 closeout (2026-06).

## Problem

`get_session()` in `src/regulatory/db/session.py` uses `async with factory() as session:`.
`AsyncSession.__aexit__` calls `close()`, not `commit()`. Any caller that flushes but does
not explicitly commit will have its writes silently rolled back on session close.

The agent's `_write_audit` was hitting exactly this — flushing audit rows, then session-close
rolling them back, producing an empty `agent_audit_log` table despite the agent appearing to
log normally. Fixed in the `feat/nl-agent` PR by adding explicit `await session.commit()` in
`_write_audit` (immediately after `await session.flush()`).

The docstring on `get_session()` reads "committing on success and rolling back on error" — this
claim is false. The docstring must be corrected regardless of which resolution path is chosen.

## What needs investigation

Other writers in the codebase use the same `get_session()` helper and we know they persist data:

| Call site | File | Writes to |
|---|---|---|
| scheduler ingestion loop | `src/regulatory/ingestion/scheduler.py` | `documents`, `fetch_log`, `document_versions` |
| risk signal persistence | `src/regulatory/risk/persist.py` | `risk_signals`, `risk_signal_events` |
| manufacturer canonicalization | `src/regulatory/risk/canonicalize.py` | `manufacturers`, `documents.canonical_manufacturer_ids` |

Initial grep of `scheduler.py` shows explicit `await session.commit()` at three call sites
(lines 164, 191, 207 as of this PR). These writers know the helper doesn't auto-commit.
The remaining two files need the same inspection.

## Suggested resolution path

1. Grep `get_session()` usage across `src/regulatory/`. For each call site: is it read-only?
   If it writes, does it call `await session.commit()` before session close?
2. For sites that don't commit explicitly: verify against the DB whether writes actually persist.
3. Correct the `get_session()` docstring to remove the false "committing on success" claim.
4. Decide on one of:
   - **(a)** Leave `get_session()` as-is. Require every writer to commit explicitly.
     Document the contract loudly in the docstring and in CLAUDE.md house rules.
   - **(b)** Introduce `get_write_session()` that auto-commits (via `async with session.begin()`
     or explicit commit after yield). Migrate writers; leave `get_session()` for reads.
   - **(c)** Change `get_session()` to auto-commit. Audit all callers for breakage.

(b) is the cleanest long-term but requires migration effort. (a) is cheapest but error-prone
(the `_write_audit` bug is evidence). Decision should follow the inventory.

## Why not fixed in this PR

Changing shared session semantics affects every write path in the codebase. The risk of
breaking the scheduler/risk-engine/canonicalization paths in pursuit of a narrow audit-log
fix isn't justified inside the agent PR. The surgical fix in `_write_audit` resolves the
immediate bug; this issue tracks the broader investigation.
