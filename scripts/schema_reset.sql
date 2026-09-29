-- ============================================================
-- SuperTeacher backend: one-shot schema reset for super_teacher_db
--
-- Run this FILE while connected to the database super_teacher_db
-- AS the 'postgres' role (pgAdmin Query Tool or psql).
--
-- It removes the stale empty tables owned by 'postgres' and gives
-- the schema and database to 'super_admin', the role our backend
-- connects with.
--
-- psql equivalent:
--   "C:/Program Files/PostgreSQL/18/bin/psql.exe" -h 127.0.0.1 -U postgres -d super_teacher_db -f schema_reset.sql
-- ============================================================

-- 1) Remove every stale table (they are empty; verified before use)
DROP SCHEMA public CASCADE;

-- 2) Recreate the schema owned by the app role
CREATE SCHEMA public AUTHORIZATION super_admin;
GRANT ALL ON SCHEMA public TO super_admin;

-- 3) Hand the database itself to the app role
ALTER DATABASE super_teacher_db OWNER TO super_admin;

-- ============================================================
-- 4) SELF-CHECK — run/inspect the result in the SAME tab.
-- Expected output rows:
--   schema_owner   -> super_admin
--   can_create     -> t        (true)
--   table_count    -> 0
--   db_owner       -> super_admin
-- ============================================================
SELECT
    (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public') AS schema_owner,
    (SELECT has_schema_privilege('super_admin', 'public', 'CREATE'))               AS can_create,
    (SELECT count(*) FROM pg_class c
      JOIN pg_namespace ns ON ns.oid = c.relnamespace
      WHERE ns.nspname = 'public' AND c.relkind = 'r')                             AS table_count,
    (SELECT pg_get_userbyid(datdba) FROM pg_database
      WHERE datname = 'super_teacher_db')                                          AS db_owner;
