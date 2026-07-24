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
import os
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


def pg_bin_dir() -> Path:
    """PostgreSQL bin dir for the self-provisioning integration fixtures.

    nexus-f4wcg: ~20 tests/db fixtures hardcoded the macOS Homebrew PG16
    path (``/opt/homebrew/opt/postgresql@16/bin``), so on any other
    platform — the nightly linux gate in particular — the whole family
    silently skipped. Resolve through the product's own discovery instead:
    ``NEXUS_PG_BIN`` override first (the nightly gate points this at the
    CA-3 PG+pgvector bundle), then config-dir bundle / Homebrew / system
    dirs / PATH, exactly as ``nx init`` would. Called at module import
    (collection) time, so the bundle leg resolves against the AMBIENT
    config dir, not a per-test isolated one — fine: these are stateless
    CLI binaries, same class of fixed non-isolated location as the old
    hardcoded Homebrew path.

    SELF-PROVISIONING LEG (RDR-155 P4b P0a'; Hal directive: we BUILD our
    PG — tests never depend on a pre-existing host install): when
    discovery misses, download the sigstore-verified ``nexus-pg-<target>``
    bundle for :data:`~nexus.daemon.binary_install.PINNED_SERVICE_TAG`
    through the product's OWN install seam (``install_pg_bundle`` +
    ``ensure_pg_bundle``) into a dedicated per-tag test cache
    (``~/.cache/nexus-test-substrate/<tag>/`` — NEVER the live config
    dir). Keyed on the immutable tag: warm cache = extract-marker no-op;
    a floor bump re-provisions exactly once. This is what retired the
    silent mass-skip on boxes with no host PG (2026-07-23: EVERY
    tests/db module had been skipping on the dev box).

    Returns a nonexistent sentinel path only when self-provisioning
    ITSELF fails (offline box, no cached bundle) so the per-module
    ``.exists()`` prereq checks skip cleanly. A SET-but-broken
    ``NEXUS_PG_BIN`` re-raises at import/collection time (product
    policy: a misconfigured explicit override is a user error — fail
    loud, never mass-skip).
    """
    from nexus.db.pg_provision import PgBinaryNotFoundError, discover_pg_binaries

    try:
        return discover_pg_binaries().initdb.parent
    except PgBinaryNotFoundError:
        if os.environ.get("NEXUS_PG_BIN", "").strip():
            raise
        provisioned = _self_provision_pg_bundle()
        if provisioned is not None:
            return provisioned
        return Path("/nexus-pg-binaries-not-found")


def _self_provision_pg_bundle() -> Path | None:
    """Fetch + extract OUR pinned PG bundle into the per-tag test cache.

    Product seams only: ``install_pg_bundle`` (sigstore-verified release
    download) and ``ensure_pg_bundle`` (idempotent extract). Returns the
    bundle ``bin/`` dir, or ``None`` when provisioning is impossible
    (no pinned tag, offline) — the caller then falls back to the
    skip-sentinel. Never touches the live config dir.
    """
    from nexus.daemon.binary_install import PINNED_SERVICE_TAG, install_pg_bundle
    from nexus.db.pg_bundle import ensure_pg_bundle, extracted_bin_dir

    if not PINNED_SERVICE_TAG:
        return None
    cache_dir = Path.home() / ".cache" / "nexus-test-substrate" / PINNED_SERVICE_TAG
    cached = extracted_bin_dir(cache_dir)
    if cached is not None:
        return cached
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "service").mkdir(exist_ok=True)
        install_pg_bundle(PINNED_SERVICE_TAG, cache_dir)
        return ensure_pg_bundle(
            cache_dir, search_dirs=[cache_dir / "service"]
        )
    except Exception as exc:  # noqa: BLE001 — connectivity-class miss degrades to the documented skip-sentinel; verification failures re-raise below
        # Review finding (P0 remainder, Important 2): a signature/digest
        # verification failure is a security signal, categorically NOT a
        # benign offline-box condition — fail loud, never skip past it.
        name = type(exc).__name__
        if "Verification" in name or "sha256" in str(exc).lower():
            raise
        import warnings

        warnings.warn(
            f"PG-bundle self-provisioning failed ({exc}); PG-dependent "
            "tests will skip. Fix connectivity or set NEXUS_PG_BIN.",
            stacklevel=2,
        )
        return None


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
