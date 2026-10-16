#!/usr/bin/env bash
# Create the local development and test databases on an existing PostgreSQL 16 (run as a superuser, e.g. `sudo -u postgres`).
set -euo pipefail
psql -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wayam') THEN
  CREATE ROLE wayam WITH LOGIN NOSUPERUSER CREATEDB CREATEROLE PASSWORD 'wayam'; END IF; END $$;
SQL
for db in wayam_connectors wayam_connectors_test; do
  psql -tc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1 || psql -c "CREATE DATABASE $db OWNER wayam;"
done
echo "databases ready: wayam_connectors, wayam_connectors_test (role wayam / password wayam, local only)"
