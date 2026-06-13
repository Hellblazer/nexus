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

import json as _json
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICE_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
# Source trees whose mtime must predate the jar; a newer file means the jar is
# stale and the integration test would run against pre-change routes/handlers.
# Deliberately scoped to the trees that affect RUNTIME behaviour (Java handlers
# /routes + Liquibase changelog schema) — NOT all of resources/. In particular
# service/src/main/resources/META-INF/native-image/traced/reachability-metadata.json
# is refreshed by native-image profiling runs (not by source edits) and is
# committed; including it would fire chronic false stale-skips after any native
# build (it bit this session). Schema-affecting db/changelog is included.
_SERVICE_SRC_DIRS = (
    _REPO_ROOT / "service" / "src" / "main" / "java",
    _REPO_ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog",
)


def _rel(p: Path) -> str:
    """Repo-relative path for messages, falling back to the full path when p
    is outside the repo (only happens in unit tests passing a synthetic jar)."""
    try:
        return str(p.relative_to(_REPO_ROOT))
    except ValueError:
        return str(p)


def jar_freshness_skip_reason(jar: Path = _SERVICE_JAR) -> str | None:
    """Return a skip reason if the shaded service jar is missing or STALE,
    else ``None`` (jar is current and safe to launch).

    nexus-todyv: the ``-m integration`` fixtures launch the prebuilt jar but do
    NOT rebuild it. A jar built before a handler/route change yields false 404s
    (or false passes if a route was removed) — my own nexus-gmiaf.14
    verification hit a false 404 from a stale jar. This compares the jar mtime
    against the newest ``service/src/{main/java,main/resources}`` file so a
    forgotten ``mvn -f service/pom.xml package -DskipTests`` skips loudly
    instead of testing pre-change sources.
    """
    if not jar.exists():
        return (
            f"service jar not built: {_rel(jar)} "
            "(run: mvn -f service/pom.xml package -DskipTests)"
        )
    jar_mtime = jar.stat().st_mtime
    newest_src = 0.0
    newest_src_path: Path | None = None
    found_any_src = False
    for src_dir in _SERVICE_SRC_DIRS:
        if not src_dir.exists():
            continue
        for f in src_dir.rglob("*"):
            if f.is_file():
                found_any_src = True
                m = f.stat().st_mtime
                if m > newest_src:
                    newest_src, newest_src_path = m, f
    if not found_any_src:
        # No source files to compare against (e.g. a checkout without the Java
        # service tree). Freshness is unverifiable — skip rather than claim fresh.
        return (
            "service source tree not found under "
            f"{[ _rel(d) for d in _SERVICE_SRC_DIRS ]} — cannot verify jar "
            "freshness (is the service module checked out?)"
        )
    if newest_src > jar_mtime:
        rel = _rel(newest_src_path) if newest_src_path else "?"
        return (
            f"service jar is STALE: {_rel(jar)} predates "
            f"{rel} — rebuild before integration run "
            "(run: mvn -f service/pom.xml package -DskipTests)"
        )
    return None


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


# ── Phase-E token provisioning helpers (RDR-152 nexus-gmiaf.32.5) ──────────────
#
# Post-Phase-E the service binds every bearer to exactly ONE tenant and IGNORES
# the X-Nexus-Tenant header (AuthFilter Decision 1). The bootstrap token
# (NX_SERVICE_TOKEN) is bound to the `default` tenant only. Integration fixtures
# that exercise cross-tenant RLS therefore need a SECOND, genuinely
# other-tenant-bound bearer — minted over HTTP exactly as `nx tenant create`
# does, not faked with the default bearer + a tenant header (which silently
# resolves back to `default`).
#
# T1 scratch additionally requires a MINTED session token: the AuthFilter
# require-minted gate 401s any X-Nexus-T1-Session header that does not resolve
# to a live session_tokens row. Production mints via the MCP lifespan; tests
# mint via /v1/sessions/start exactly the same way.


def _post_json(base_url: str, path: str, bearer: str, body: dict) -> dict:
    """POST *body* as JSON to *base_url+path* with a bearer; return parsed JSON.

    Raises RuntimeError with the status + response text on any non-2xx, so a
    provisioning failure surfaces loudly in the fixture rather than as a later
    opaque 401 in the test body.
    """
    data = _json.dumps(body).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:  # pragma: no cover - fixture failure path
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(
            f"provisioning POST {path} -> HTTP {exc.code}: {detail}"
        ) from exc


def create_tenant_token(base_url: str, root_token: str, tenant: str) -> str:
    """Create *tenant* and return a bearer bound to it (POST /v1/tenants/create).

    Mirrors ``nx tenant create``: the bootstrap root token is the admin
    credential (Phase C authorization note), and the raw token is returned ONCE.
    Idempotent enough for tests — a second call mints another live token for the
    same tenant, which is harmless (service_tokens permits multiple live rows).
    """
    resp = _post_json(base_url, "/v1/tenants/create", root_token, {"name": tenant})
    token = resp.get("token")
    if not token:
        raise RuntimeError(f"/v1/tenants/create returned no token: {resp}")
    return token


def mint_session(base_url: str, bearer: str, session_id: str,
                 ttl_seconds: int | None = None) -> str:
    """Mint a session token for *session_id* (POST /v1/sessions/start).

    The session is bound to *bearer*'s tenant (the body carries only the
    session_id). Returns the raw session_token to send as X-Nexus-T1-Session.
    Re-minting the same (tenant, session_id) is safe — session_tokens has
    UNIQUE(tenant_id, session_id) with ON CONFLICT DO UPDATE, so a second mint
    replaces the row rather than erroring.
    """
    body: dict = {"session_id": session_id}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    resp = _post_json(base_url, "/v1/sessions/start", bearer, body)
    token = resp.get("session_token")
    if not token:
        raise RuntimeError(f"/v1/sessions/start returned no session_token: {resp}")
    return token
