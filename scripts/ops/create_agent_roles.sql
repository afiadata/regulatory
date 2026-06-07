-- One-time setup script for the regulatory agent Postgres roles.
-- Run once per environment BEFORE running `alembic upgrade head`.
-- The GRANT block in migration 0007 is guarded by IF EXISTS checks
-- and applies the grants automatically if these roles exist.
--
-- Usage (run as a superuser or a user with CREATEROLE privilege):
--   psql $DATABASE_URL -f scripts/ops/create_agent_roles.sql
--
-- To verify:
--   \du regulatory_agent_writer
--   \du regulatory_ops

-- regulatory_agent_writer: allowed only to INSERT into agent_audit_log.
-- The agent runner uses a connection as this role (or a login role that is
-- a member of this role) for audit writes.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_agent_writer') THEN
    CREATE ROLE regulatory_agent_writer NOLOGIN;
    RAISE NOTICE 'Created role: regulatory_agent_writer';
  ELSE
    RAISE NOTICE 'Role regulatory_agent_writer already exists, skipping creation.';
  END IF;
END $$;

-- regulatory_ops: allowed only to SELECT from agent_audit_log.
-- Used by ops/forensic review tooling to inspect agent conversations.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_ops') THEN
    CREATE ROLE regulatory_ops NOLOGIN;
    RAISE NOTICE 'Created role: regulatory_ops';
  ELSE
    RAISE NOTICE 'Role regulatory_ops already exists, skipping creation.';
  END IF;
END $$;

-- If the table already exists (migrations already ran), apply grants now.
-- Otherwise migration 0007 applies them automatically on the next upgrade.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_audit_log'
  ) THEN
    GRANT INSERT ON agent_audit_log TO regulatory_agent_writer;
    GRANT SELECT ON agent_audit_log TO regulatory_ops;
    RAISE NOTICE 'Applied INSERT/SELECT grants on agent_audit_log.';
  ELSE
    RAISE NOTICE 'Table agent_audit_log not yet created; grants applied by migration 0007.';
  END IF;
END $$;
