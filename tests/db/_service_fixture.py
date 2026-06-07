# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixture helpers for integration tests that boot the Java service.

RDR-152 bead nexus-net63: the Java service now runs Liquibase at startup
(SchemaMigrator.migrate) before binding the HTTP port.  The changelog's
grants-nexus-svc.xml changeset runs with runAlways=true and issues:

    GRANT ... TO nexus_svc;

This GRANT fails if `nexus_svc` does not exist at migration time, causing
System.exit(1) before the HTTP port opens.

All integration test fixtures that boot the JAR must create `nexus_svc`
as a superuser step BEFORE starting the process.  This module centralises
that contract so it lives in ONE place.

Usage (in a pytest fixture):
    from tests.db._service_fixture import provision_service_roles, SERVICE_ROLES_SQL

    # In pg_instance fixture, after createdb, before yielding:
    _psql(pg, SERVICE_ROLES_SQL)

CONTRACT:
    NX_DB_USER should be set to the OS superuser (trust auth) so that both
    Liquibase (schema DDL) and the HTTP service (DML under FORCE RLS) work
    without an RLS-subject application role.  Tests that need RLS verification
    should use an explicit NOSUPERUSER role and NX_DB_ADMIN_* for migration.

    For MOST integration tests, the simplest setup is:
        NX_DB_USER  = pg_user  (OS superuser, trust auth, no password needed)
        NX_DB_PASS  = ""
    The superuser bypasses FORCE RLS — this is acceptable for tests that are
    NOT specifically testing RLS (scoring, repos, catalog API correctness).
    RLS-specific tests (cross-tenant) must use a NOSUPERUSER role.
"""
from __future__ import annotations

# SQL to create nexus_svc (NOSUPERUSER NOBYPASSRLS LOGIN).
# Applied by the test pg_instance fixture as superuser BEFORE the JAR starts.
# Idempotent: DO-block guards against re-creation.
SERVICE_ROLES_SQL = """\
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN
    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;
"""
