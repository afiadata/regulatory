-- One-time setup script for the regulatory_readonly Postgres role.
-- Run once per environment BEFORE running `alembic upgrade head`.
-- The GRANT block in migration 0004 is guarded by an IF EXISTS check
-- and applies the grants when this role exists.
--
-- Usage (run as a superuser or a user with CREATEROLE privilege):
--   psql $DATABASE_URL -f scripts/ops/create_readonly_role.sql
--
-- To verify:
--   \du regulatory_readonly

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_readonly') THEN
    CREATE ROLE regulatory_readonly NOLOGIN;
    RAISE NOTICE 'Created role: regulatory_readonly';
  ELSE
    RAISE NOTICE 'Role regulatory_readonly already exists, skipping creation.';
  END IF;
END $$;

-- If the tables already exist (migrations already ran), apply grants now.
-- Otherwise migration 0004 applies them automatically on the next upgrade.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables WHERE table_name = 'risk_signals'
  ) THEN
    GRANT SELECT ON
      manufacturers,
      counties,
      suppliers,
      county_supply,
      risk_signals,
      risk_signal_events
    TO regulatory_readonly;
    RAISE NOTICE 'Applied SELECT grants to regulatory_readonly.';
  ELSE
    RAISE NOTICE 'Tables not yet created; grants will be applied by migration 0004.';
  END IF;
END $$;
