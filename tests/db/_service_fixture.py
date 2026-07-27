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
import signal
import socket
import subprocess
import tempfile
import time
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

    USABILITY, NOT MERE PRESENCE (nexus-eqxxh, PR #1426). Discovery is not
    trusted on its own: a discovered PG that cannot load pgvector is rejected
    and the self-provisioning leg runs instead. The paragraph above used to be
    the whole story, and it made the self-provisioning leg conditional on
    discovery FAILING — which is a property of the box, not of the substrate's
    needs. On a dev box with no host PG that reads as correct; on a GitHub
    runner, which ships PostgreSQL, discovery succeeded and handed back a
    pgvector-less system PG, so every engine-substrate test died at boot and
    the pinned bundle was never downloaded. See :func:`_has_pgvector`.
    """
    from nexus.db.pg_provision import PgBinaryNotFoundError, discover_pg_binaries

    # An EXPLICIT override is a user statement and is honoured verbatim, before
    # any usability opinion is applied. The pgvector guard below deliberately
    # does NOT gate it: callers point NEXUS_PG_BIN at synthetic trees that never
    # launch PG (path-resolution tests do exactly this), and silently
    # substituting the bundle would hand a developer debugging one PG the
    # results of a different one. Making the guard raise here instead broke
    # test_pg_bin_dir_honors_nexus_pg_bin_override and 5 sibling fixtures on the
    # first attempt (PR #1426 run 2) — the override contract predates the guard
    # and outranks it.
    explicit = os.environ.get("NEXUS_PG_BIN", "").strip()

    try:
        discovered = discover_pg_binaries().initdb.parent
    except PgBinaryNotFoundError:
        if explicit:
            raise
        discovered = None

    # nexus-eqxxh: an AMBIENT PG is only usable if it can load pgvector.
    # Discovery answering first is what made this a CI-only defect: on a dev box
    # with no host PG it raises and the self-provision leg runs, but GitHub
    # runners ship PostgreSQL, so discovery succeeded, returned a pgvector-less
    # system PG, and every engine-substrate test died at boot with
    # `CREATE EXTENSION IF NOT EXISTS vector` -> "extension vector is not
    # available" (PR #1426 run 1: 73 errors on BOTH python jobs, 0 test-logic
    # failures). The pinned bundle was never even downloaded.
    #
    # Finding a PG is therefore not the question — finding one that can SERVE is.
    # An engine substrate without pgvector cannot run at all, so an ambient PG
    # missing it is not a usable answer and we fall through to our own bundle.
    # This is the standing rule that functional gates are self-provisioning
    # scripts, never ambient machine state; discovery-first quietly inverted it.
    if explicit:
        return discovered
    if discovered is not None and _has_pgvector(discovered):
        return discovered

    provisioned = _self_provision_pg_bundle()
    if provisioned is not None:
        return provisioned
    return Path("/nexus-pg-binaries-not-found")


def _has_pgvector(bin_dir: Path) -> bool:
    """Can the PG at *bin_dir* load the ``vector`` extension?

    nexus-eqxxh. Presence of ``vector.control`` in the sharedir is the same
    check ``scripts/build_pg_bundle.sh`` asserts after building the bundle, and
    the same one that showed the shipped bundles carry exactly
    ``pg_trgm``/``plpgsql``/``vector`` — so the two agree by construction on
    what "has the extension" means.

    Resolved via ``pg_config --sharedir`` when that binary exists, because
    PostgreSQL reports paths relative to the executable and a RELOCATED bundle's
    compiled-in sharedir is wrong. Falls back to the conventional
    ``<prefix>/share/postgresql`` and ``<prefix>/share`` layouts for trees whose
    ``pg_config`` was stripped (the zonky-style reduced builds RDR-157 rejected
    for exactly this reason).

    Returns False rather than raising on any probe failure: an unusable answer
    and an unreadable one lead to the same decision — fall through to the
    bundle.
    """
    candidates: list[Path] = []
    pg_config = bin_dir / "pg_config"
    if pg_config.exists():
        try:
            out = subprocess.run(
                [str(pg_config), "--sharedir"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                candidates.append(Path(out.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    prefix = bin_dir.parent
    candidates += [prefix / "share" / "postgresql", prefix / "share"]

    return any((c / "extension" / "vector.control").exists() for c in candidates)


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


# ── service spawn: FILE-backed output, never a PIPE (nexus-lom9g / j0nec) ────


def spawn_service(
    cmd: list[str],
    env: dict[str, str],
    *,
    log_dir: str | Path | None = None,
) -> tuple[subprocess.Popen, Path]:
    """Spawn the engine with its output going to a FILE, and return the log path.

    THE PIPE IS THE BUG. ``stdout=subprocess.PIPE`` without a reader deadlocks
    the service: the 64KB pipe buffer fills with Logback console output,
    ``write(2)`` blocks while holding the PrintStream monitor, and every
    logging thread pins behind it — the process wedges before it ever binds
    its port, so the caller sees only a timeout. This is nexus-j0nec, already
    diagnosed by jstack (pin in ``FileOutputStream.writeBytes`` from
    ``TokenStore.issueToken``'s log.info; draining exactly the 65,702 buffered
    bytes instantly unwedged it) and already fixed in
    ``tests/_engine_substrate.py`` — but that fix landed in ONE copy while 22
    per-file fixtures under tests/db/ kept the un-fixed form. Hoisted here so
    there is a single owner, per the standing rule that lifecycle fixes live
    in the shared primitive rather than one tier's copy.

    No timeout value fixes a wedge, which is why raising the per-file 40s
    waits would not have helped.

    Returns ``(proc, log_path)``. Pair with :func:`wait_for_service`, which
    surfaces the tail of *log_path* when the port never opens — the diagnostic
    the PIPE form threw away.
    """
    d = Path(log_dir) if log_dir is not None else Path(tempfile.mkdtemp(prefix="nexus-svc-log-"))
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / "engine.log"
    # Deliberately not a context manager: the handle's lifetime is the
    # service process's, and the caller owns termination.
    fh = open(log_path, "wb")  # noqa: SIM115 — lifetime spans the spawned process
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    return proc, log_path


def wait_for_service(
    host: str,
    port: int,
    *,
    proc: subprocess.Popen | None = None,
    log_path: str | Path | None = None,
    timeout: float = 60.0,
) -> None:
    """Block until *port* accepts a connection, or raise WITH the engine log.

    The per-file ``_wait_tcp`` helpers this replaces raised a bare
    ``TimeoutError: port N on H not reachable after 40.0s`` and discarded
    everything the service had said — which is why nexus-lom9g sat for a day
    with a guessed mechanism instead of a known one. On timeout this kills the
    process group and includes the log tail, matching what
    ``tests/_engine_substrate.py`` already does.

    Default is 60s, the same budget the shared session engine uses; the copies
    used 40s.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)

    detail = ""
    if proc is not None:
        rc = proc.poll()
        detail += f"\nprocess exit code: {rc if rc is not None else 'still running (wedged?)'}"
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if log_path is not None:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-2000:]
            detail += f"\nTail of engine log ({log_path}):\n{tail}"
        except OSError as exc:
            detail += f"\n(engine log unreadable: {exc})"
    raise TimeoutError(
        f"port {port} on {host} not reachable after {timeout}s{detail}"
    )
