-- nexus-sz651: installs the DDL probe (tests/_ddl_counter.py PROBE_SQL) in template1 and the
-- container's default database, so every database made later (the shared cluster's
-- CREATE DATABASE ... TEMPLATE clones included) logs each DDL command it runs, nested or not.
-- The image entrypoint runs this once, against an empty data directory, before the real server
-- starts; tests/test_ddl_counter.py keeps the SQL below equal to PROBE_SQL. The probe's mark() logs
-- the WAL position and the summed database size, so the bytes a container writes are the difference
-- between two marks (nexus-sz651).
\set ON_ERROR_STOP on
SELECT current_database() AS nx_db \gset
\connect template1
CREATE SCHEMA IF NOT EXISTS nx_ddl_probe;
CREATE OR REPLACE FUNCTION nx_ddl_probe.fire() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
  SET log_error_verbosity = terse SET log_min_error_statement = panic AS $probe$
BEGIN
  RAISE LOG 'nx_ddl: %', tg_tag;
END
$probe$;
CREATE OR REPLACE FUNCTION nx_ddl_probe.mark(label text) RETURNS void
LANGUAGE plpgsql SET log_error_verbosity = terse SET log_min_error_statement = panic AS $probe$
DECLARE
  wal numeric := pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0');
  dbsize numeric := -1;
BEGIN
  BEGIN
    SELECT coalesce(sum(pg_database_size(oid)), 0) INTO dbsize FROM pg_database;
  EXCEPTION WHEN OTHERS THEN
    dbsize := -1;
  END;
  RAISE LOG 'nx_ddl_mark: % wal=% dbsize=%', label, wal, dbsize;
END
$probe$;
CREATE EVENT TRIGGER nx_ddl_probe ON ddl_command_end EXECUTE FUNCTION nx_ddl_probe.fire();
\connect :nx_db
CREATE SCHEMA IF NOT EXISTS nx_ddl_probe;
CREATE OR REPLACE FUNCTION nx_ddl_probe.fire() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
  SET log_error_verbosity = terse SET log_min_error_statement = panic AS $probe$
BEGIN
  RAISE LOG 'nx_ddl: %', tg_tag;
END
$probe$;
CREATE OR REPLACE FUNCTION nx_ddl_probe.mark(label text) RETURNS void
LANGUAGE plpgsql SET log_error_verbosity = terse SET log_min_error_statement = panic AS $probe$
DECLARE
  wal numeric := pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0');
  dbsize numeric := -1;
BEGIN
  BEGIN
    SELECT coalesce(sum(pg_database_size(oid)), 0) INTO dbsize FROM pg_database;
  EXCEPTION WHEN OTHERS THEN
    dbsize := -1;
  END;
  RAISE LOG 'nx_ddl_mark: % wal=% dbsize=%', label, wal, dbsize;
END
$probe$;
CREATE EVENT TRIGGER nx_ddl_probe ON ddl_command_end EXECUTE FUNCTION nx_ddl_probe.fire();
-- nx-ddl-probe-end
-- The container's own boot ends here: DdlLogParser counts nothing before this mark, so the
-- probe's install above is never part of a count. Not part of the block SharedCluster installs
-- into an empty database (DdlCounts.probeInstallBlock stops at the line above).
SELECT nx_ddl_probe.mark('start');
