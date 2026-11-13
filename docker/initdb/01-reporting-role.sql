-- Read-only database role for the reporting connector (Chapter 6).
-- Even if the connector were compromised, this role cannot INSERT/UPDATE/DELETE/DROP.
CREATE ROLE reporting LOGIN PASSWORD 'reporting';
GRANT CONNECT ON DATABASE orders TO reporting;
GRANT USAGE ON SCHEMA public TO reporting;
-- Applies to tables the migration user creates LATER (migrations run after this script).
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO reporting;
