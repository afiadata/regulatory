"""Security tests for the risk engine and ingestion framework.

§4a: No hardcoded SQL string concatenation (ruff S608).
§4b: The regulatory_readonly role cannot write to any regulated table.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest  # noqa: F401 — used for skipif decorator

# ---------------------------------------------------------------------------
# Test: ruff S608 — no hardcoded SQL string concatenation in source
# ---------------------------------------------------------------------------


def test_no_string_concatenation_in_queries() -> None:
    """ruff S608 check must pass with zero violations across the entire src/ tree."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "S608", "src/regulatory/"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ruff S608 violations found:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test: regulatory_readonly role cannot INSERT/UPDATE/DELETE on risk tables
# ---------------------------------------------------------------------------

_REGULATED_TABLES = [
    "documents",
    "manufacturers",
    "counties",
    "suppliers",
    "county_supply",
    "risk_signals",
    "risk_signal_events",
]

_WRITE_OPERATIONS = ["INSERT", "UPDATE", "DELETE"]


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — skipping live DB security test",
)
def test_readonly_role_cannot_write() -> None:
    """regulatory_readonly role must be denied INSERT/UPDATE/DELETE on all regulated tables."""
    import psycopg2  # type: ignore[import]

    db_url = os.environ["TEST_DATABASE_URL"]

    # Connect as superuser and create the readonly role if it doesn't exist.
    admin_conn = psycopg2.connect(db_url)
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN"
            "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_readonly')"
            "  THEN CREATE ROLE regulatory_readonly; END IF;"
            " END $$;"
        )
        # Grant CONNECT + USAGE so we can actually connect.
        cur.execute("SELECT current_database()")
        db_name: str = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(f"GRANT CONNECT ON DATABASE {db_name} TO regulatory_readonly;")  # noqa: S608
        cur.execute("GRANT USAGE ON SCHEMA public TO regulatory_readonly;")
        # Explicitly REVOKE write privileges to be safe.
        for table in _REGULATED_TABLES:
            cur.execute(
                f"REVOKE INSERT, UPDATE, DELETE ON TABLE {table}"  # noqa: S608
                f" FROM regulatory_readonly;"
            )
    admin_conn.close()

    # Now connect as the readonly role and attempt writes — all must fail.
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    denied: list[str] = []
    allowed: list[str] = []

    with conn.cursor() as cur:
        for table in _REGULATED_TABLES:
            for op in _WRITE_OPERATIONS:
                # Re-issue SET ROLE each iteration: SET ROLE is transaction-local
                # and is rolled back after each conn.rollback() call below.
                cur.execute("SET ROLE regulatory_readonly;")
                try:
                    if op == "INSERT":
                        cur.execute(f"INSERT INTO {table} DEFAULT VALUES;")  # noqa: S608
                    elif op == "UPDATE":
                        cur.execute(f"UPDATE {table} SET id = id WHERE FALSE;")  # noqa: S608
                    else:
                        cur.execute(f"DELETE FROM {table} WHERE FALSE;")  # noqa: S608
                    allowed.append(f"{op} on {table}")
                except psycopg2.errors.InsufficientPrivilege:
                    denied.append(f"{op} on {table}")
                finally:
                    conn.rollback()

    conn.close()

    assert allowed == [], (
        f"regulatory_readonly was able to perform write operations: {allowed}"
    )
    assert len(denied) == len(_REGULATED_TABLES) * len(_WRITE_OPERATIONS)
