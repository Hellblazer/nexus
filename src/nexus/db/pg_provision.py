# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local Postgres cluster provisioner for the RDR-152 Java service backend.

RDR-152 Phase 5 / bead nexus-gmiaf.31: provision an nx-managed local Postgres
cluster at ``nx init`` so Postgres is NOT a user prerequisite (mirrors how nx
manages the local ``chroma run`` child).

TWO-ROLE CONTRACT (net63):
  nexus_admin — NOSUPERUSER NOCREATEDB NOCREATEROLE LOGIN.
                Schema owner; has CREATE on the nexus DB so Liquibase DDL runs.
                Mapped to NX_DB_ADMIN_URL / NX_DB_ADMIN_USER / NX_DB_ADMIN_PASS.

  nexus_svc   — NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN.
                DML-only (SELECT/INSERT/UPDATE/DELETE); FORCE RLS applies.
                Mapped to NX_DB_URL / NX_DB_USER / NX_DB_PASS.

Both roles must be created BEFORE the first service start so the
grants-nexus-svc.xml changeset (runAlways=true) does NOT fail loud.

IDEMPOTENCY:
  Re-running ``nx init`` on an already-provisioned cluster is a no-op:
    - initdb is skipped when the cluster data directory already contains
      PG_VERSION.
    - createdb is skipped when the nexus database already exists.
    - CREATE ROLE is skipped when the roles already exist.
    - pg_ctl start is skipped when the cluster is already accepting
      connections on the provisioned port.
    - Credentials are NOT regenerated — the existing pg_credentials file
      is reused verbatim, preserving passwords already baked into the
      service's env configuration.

BINARY DISCOVERY:
  The provisioner requires system-installed PostgreSQL 17 (or 16/15) binaries.
  Search order:
    1. ``NEXUS_PG_BIN`` env var override (tests + custom installs).
    2. ``/opt/homebrew/opt/postgresql@17/bin`` (macOS Homebrew PG 17).
    3. ``/opt/homebrew/opt/postgresql@15/bin`` (macOS Homebrew PG 15).
    4. ``initdb`` on PATH (Linux; ``shutil.which`` → parent directory).
    5. ``/usr/lib/postgresql/17/bin`` (Debian/Ubuntu system install).
    6. ``/usr/lib/postgresql/15/bin`` (Debian/Ubuntu PG 15 fallback).

  Fails loudly with a platform-appropriate install hint when no binaries
  are found.  Bundling/embedded Postgres is a future option (not this
  bead).

OUTPUT FILES (all under ``nexus_config_dir()``):
  postgres/          — initdb cluster data directory.
  pg_credentials     — 0600 shell-env-file with all connection vars.

The pg_credentials file contains NX_DB_ADMIN_* and NX_DB_* variables that
the service daemon (bead .30) sources before starting the JVM.  It also
contains PG_DATA and PG_PORT for daemon lifecycle use.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

# ── Cluster constants ──────────────────────────────────────────────────────────

#: Database name created during provisioning.
NEXUS_DB_NAME: str = "nexus"

#: Marker file written by initdb; used to detect an existing cluster.
_PG_VERSION_MARKER: str = "PG_VERSION"

#: Name of the credentials env-file written under the config directory.
CREDENTIALS_FILENAME: str = "pg_credentials"

# ── Binary discovery ───────────────────────────────────────────────────────────


class PgVectorNotInstalledError(RuntimeError):
    """The pgvector extension is not installed for the discovered PostgreSQL
    (nexus-pebfx.5 pre-flight). Raised BEFORE any cluster work so the user
    gets the remedy instead of a mid-provision Liquibase failure."""


class PgBinaryNotFoundError(RuntimeError):
    """Raised when no Postgres binaries are found on the system."""


@dataclass(frozen=True)
class PgBinaries:
    """Resolved paths to the four required Postgres binaries."""

    bin_dir: Path
    initdb: Path
    pg_ctl: Path
    psql: Path
    createdb: Path

    @classmethod
    def from_dir(cls, d: Path) -> "PgBinaries":
        return cls(
            bin_dir=d,
            initdb=d / "initdb",
            pg_ctl=d / "pg_ctl",
            psql=d / "psql",
            createdb=d / "createdb",
        )

    def all_present(self) -> bool:
        return all(
            p.is_file() for p in [self.initdb, self.pg_ctl, self.psql, self.createdb]
        )

    def missing_names(self) -> list[str]:
        """Names of the required binaries that are not present on disk."""
        return [
            name
            for name, p in (
                ("initdb", self.initdb),
                ("pg_ctl", self.pg_ctl),
                ("psql", self.psql),
                ("createdb", self.createdb),
            )
            if not p.is_file()
        ]


def _install_hint() -> str:
    """Return a platform-appropriate install hint."""
    if sys.platform == "darwin":
        return (
            "Install PostgreSQL 17 with Homebrew:\n"
            "  brew install postgresql@17\n"
            "  brew services start postgresql@17\n"
            "Then re-run `nx init --service`."
        )
    return (
        "Install PostgreSQL 17:\n"
        "  # Debian/Ubuntu:\n"
        "  sudo apt-get install postgresql-17\n"
        "  # RHEL/Fedora:\n"
        "  sudo dnf install postgresql-server\n"
        "Then re-run `nx init --service`."
    )


# nexus is aligned on PG17 (matches the deployed conexus stack; nexus-41bso).
# 16/15 remain as fallbacks so an existing host install still works.
_CANDIDATE_DIRS: list[Path] = [
    Path("/opt/homebrew/opt/postgresql@17/bin"),
    Path("/opt/homebrew/opt/postgresql@16/bin"),
    Path("/opt/homebrew/opt/postgresql@15/bin"),
    Path("/usr/lib/postgresql/17/bin"),
    Path("/usr/lib/postgresql/16/bin"),
    Path("/usr/lib/postgresql/15/bin"),
]


def discover_pg_binaries() -> PgBinaries:
    """Locate PostgreSQL binaries.

    Search order:
    1. ``NEXUS_PG_BIN`` env var override.
    2. Fixed candidate directories (macOS Homebrew, Linux system).
    3. ``initdb`` on PATH via ``shutil.which`` (Linux PATH-based).

    Raises :class:`PgBinaryNotFoundError` with an install hint when nothing
    is found.
    """
    # 1. Explicit override — highest priority (tests + custom installs).
    #    If the env var is set but the directory does not contain the required
    #    binaries, fail loudly instead of silently falling back to system paths.
    #    A misconfigured NEXUS_PG_BIN is always a user error; using a different
    #    PG install silently would be more surprising than an explicit error.
    env_override = os.environ.get("NEXUS_PG_BIN", "").strip()
    if env_override:
        d = Path(env_override)
        bins = PgBinaries.from_dir(d)
        if bins.all_present():
            _log.debug("pg_binaries_from_env", bin_dir=str(d))
            return bins
        missing = [str(p) for p in [bins.initdb, bins.pg_ctl, bins.psql, bins.createdb] if not p.is_file()]
        raise PgBinaryNotFoundError(
            f"NEXUS_PG_BIN is set to '{env_override}' but the following required "
            f"binaries are missing: {', '.join(missing)}\n"
            "Fix NEXUS_PG_BIN or unset it to use auto-discovery.\n"
            + _install_hint()
        )

    # 1.5. Already-extracted ship-alongside PG bundle under the config dir
    #      (RDR-157 P3.4, bead nexus-vwvv5.13). This makes EVERY caller —
    #      not just the one-shot `nx init` process that extracted it — discover
    #      the bundle on a local-distribution machine, in particular the
    #      storage-service daemon's PG-restart path (`_ensure_pg_running`).
    #      Lazy import avoids a pg_bundle <-> pg_provision import cycle.
    from nexus.config import nexus_config_dir  # local import to avoid circular  # noqa: PLC0415 — deferred import — heavy/optional dep loaded only when provisioning runs
    from nexus.db.pg_bundle import extracted_bin_dir  # noqa: PLC0415 — deferred import — heavy/optional dep loaded only when provisioning runs

    bundle_bin = extracted_bin_dir(nexus_config_dir())
    if bundle_bin is not None:
        bins = PgBinaries.from_dir(bundle_bin)
        if bins.all_present():
            _log.debug("pg_binaries_from_bundle", bin_dir=str(bundle_bin))
            return bins
        # The bundle cache directory exists with a valid completion marker but
        # its binaries are gone (manually deleted / partial corruption). Falling
        # through to host PG silently would pick the WRONG PostgreSQL on a
        # local-distribution machine and fail late at CREATE EXTENSION vector.
        # Warn loud about why the bundle was not used (no silent downgrade).
        _log.warning(
            "pg_bundle_incomplete_cache",
            bin_dir=str(bundle_bin),
            missing=bins.missing_names(),
        )

    # 2. Fixed candidate directories.
    for d in _CANDIDATE_DIRS:
        bins = PgBinaries.from_dir(d)
        if bins.all_present():
            _log.debug("pg_binaries_found", bin_dir=str(d))
            return bins

    # 3. PATH-based discovery via shutil.which.
    initdb_path = shutil.which("initdb")
    if initdb_path:
        d = Path(initdb_path).parent
        bins = PgBinaries.from_dir(d)
        if bins.all_present():
            _log.debug("pg_binaries_from_path", bin_dir=str(d))
            return bins

    raise PgBinaryNotFoundError(
        "No PostgreSQL binaries found.\n" + _install_hint()
    )


# ── Port helpers ───────────────────────────────────────────────────────────────


def _pg_config_value(pg_config: Path, flag: str) -> str | None:
    """Return a ``pg_config <flag>`` value, or None when indeterminate."""
    cmd = [str(pg_config), flag]
    try:
        # env is now an os.environ SNAPSHOT (was: inherited live by reference).
        # subprocess.run is synchronous and os.environ is not mutated mid-call,
        # so this is equivalent in practice — the snapshot is to thread the
        # bundle lib path (code-review H2).
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=10,
            env=_bundle_lib_env(cmd, None),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("pgvector_preflight_indeterminate", error=str(exc), flag=flag)
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        _log.warning("pgvector_preflight_indeterminate", returncode=result.returncode, flag=flag)
        return None
    return value


def _candidate_sharedirs(pg_config: Path, bin_dir: Path, sharedir: str) -> list[Path]:
    """Sharedir locations to probe for ``extension/vector.control``.

    ``pg_config`` reports the **build-time absolute** sharedir. For a relocatable
    bundle extracted to a new prefix (RDR-157 local distribution) that path does
    not exist on the target — so we ALSO re-anchor the sharedir on the actual
    ``bin_dir`` using its offset from ``pg_config``'s reported ``--bindir``
    (PostgreSQL keeps the internal tree layout stable across relocation, which is
    how the server itself resolves paths via ``find_my_exec``). Both the reported
    and the re-anchored paths are probed; non-relocated installs collapse to one.
    """
    candidates = [Path(sharedir)]
    bindir = _pg_config_value(pg_config, "--bindir")
    if bindir:
        try:
            rel = os.path.relpath(sharedir, bindir)
            reanchored = (bin_dir / rel).resolve()
            if reanchored not in candidates:
                candidates.append(reanchored)
        except ValueError:
            pass  # e.g. different drives on Windows — skip re-anchoring
    return candidates


def check_pgvector_available(bins: PgBinaries) -> None:
    """Fail loud when pgvector is not installed for THIS PostgreSQL.

    Checks for ``<sharedir>/extension/vector.control``. ``pg_config`` reports the
    build-time sharedir, which is wrong for a relocated bundle, so we also probe
    the binary-relative (re-anchored) sharedir — see :func:`_candidate_sharedirs`.
    Indeterminate (pg_config missing/failing) does NOT block — provisioning
    will fail loud at CREATE EXTENSION anyway; this gate exists to move the
    common failure earlier, not to add a new way to be wrong.
    """
    pg_config = bins.bin_dir / "pg_config"
    if not pg_config.is_file():
        _log.warning("pgvector_preflight_no_pg_config", bin_dir=str(bins.bin_dir))
        return
    sharedir = _pg_config_value(pg_config, "--sharedir")
    if sharedir is None:
        return
    candidates = _candidate_sharedirs(pg_config, bins.bin_dir, sharedir)
    if any((c / "extension" / "vector.control").is_file() for c in candidates):
        return
    # Report EVERY probed location, not just candidates[0] — for a relocated
    # bundle candidates[0] is pg_config's build-time absolute sharedir, a path
    # that does not exist on the target machine and so misleads the user.
    probed = ", ".join(str(c / "extension" / "vector.control") for c in candidates)
    raise PgVectorNotInstalledError(
        f"The pgvector extension is not installed for the PostgreSQL at "
        f"{bins.bin_dir} (no vector.control under any of: {probed}).\n"
        "The Homebrew 'pgvector' formula targets the default postgresql "
        "major — for a versioned install (e.g. postgresql@17) build from "
        "source against THIS pg_config:\n"
        f"  git clone --branch v0.8.2 https://github.com/pgvector/pgvector.git\n"
        f"  cd pgvector && PG_CONFIG={pg_config} make && "
        f"PG_CONFIG={pg_config} make install\n"
        "then re-run: nx init --service"
    )


def _find_free_port() -> int:
    """Return an ephemeral TCP port not currently bound on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_accepting(host: str, port: int) -> bool:
    """Return True when *host*:*port* accepts TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


# ── Provisioning result ────────────────────────────────────────────────────────


@dataclass
class ProvisionResult:
    """Outcome of a :func:`provision` call."""

    #: True when a fresh cluster was initialised (first run).
    cluster_created: bool = False
    #: True when the nexus database was created (first run).
    db_created: bool = False
    #: True when nexus_admin was created (first run).
    admin_role_created: bool = False
    #: True when nexus_svc was created (first run).
    svc_role_created: bool = False
    #: True when the pgvector ``vector`` extension was created (first run).
    vector_extension_created: bool = False
    #: True when the cluster was already running and no work was needed.
    already_provisioned: bool = False
    #: Port the cluster is listening on.
    port: int = 0
    #: Path to the credentials file (0600).
    credentials_path: Path = field(default_factory=Path)


# ── Low-level helpers ──────────────────────────────────────────────────────────


def _bundle_lib_env(cmd: list[str], env: dict | None) -> dict:
    """Build a subprocess env that lets a relocatable PG binary find its own libs.

    The RDR-161 relocatable PG bundle ships its libraries in ``<bundle>/lib`` but
    its ``bin/`` binaries carry NO RPATH/RUNPATH (nexus-4mm24: caught on a minimal
    ``debian:trixie-slim`` where ``libpq.so.5`` is absent system-wide — ``initdb``
    exited 127). Point the dynamic loader at the binary's sibling ``lib/`` so
    ``initdb`` / ``pg_ctl`` / the started ``postgres`` (which inherits this env)
    resolve their bundled libs.

    SCOPE (reviewed, not bundle-gated): this fires for ANY ``cmd[0]`` with a
    sibling ``../lib`` dir, which also includes Homebrew PG on macOS. It is safe
    in every supported layout because the prepend only exposes the binary's OWN
    co-located libs earlier on the search path, never a foreign one:
      * nexus bundle ``<root>/bin/initdb`` → ``<root>/lib`` (the intended fix);
      * Debian/PGDG ``/usr/bin/initdb`` → resolves (symlink) to
        ``/usr/lib/postgresql/N/bin`` whose ``../lib`` does NOT exist (libs live
        in ``/usr/lib/<triplet>/``) → ``is_dir()`` False → no-op;
      * macOS (Homebrew or system) → ``dyld`` ignores ``LD_LIBRARY_PATH`` → no
        effect regardless.
    A non-symlink ``/usr/bin/initdb`` on Linux would prepend ``/usr/lib`` (already
    the default path) — a benign, persistent env on the started ``postgres``. The
    durable fix is an RPATH in the bundle build (nexus-iytd3); this consumer-side
    guard makes the already-published bundle work for nx-managed provisioning.
    """
    base = dict(os.environ if env is None else env)
    try:
        lib_dir = Path(cmd[0]).resolve().parent.parent / "lib"
    except (IndexError, OSError):
        return base
    if lib_dir.is_dir():
        existing = base.get("LD_LIBRARY_PATH", "")
        base["LD_LIBRARY_PATH"] = (
            f"{lib_dir}{os.pathsep}{existing}" if existing else str(lib_dir)
        )
    return base


def _run(cmd: list[str], *, check: bool = True, capture: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess, raising on non-zero exit when *check* is True."""
    _log.debug("pg_provision_run", cmd=cmd)
    kw: dict = dict(check=check, text=True, env=_bundle_lib_env(cmd, env))
    if capture:
        kw["capture_output"] = True
    return subprocess.run(cmd, **kw)  # type: ignore[call-overload]


def _psql(bins: PgBinaries, port: int, db: str, user: str, sql: str) -> subprocess.CompletedProcess:
    """Execute *sql* via psql against the local cluster."""
    return _run(
        [str(bins.psql), "-h", "127.0.0.1", "-p", str(port),
         "-U", user, "-d", db, "-c", sql],
    )


def _db_exists(bins: PgBinaries, port: int, superuser: str, dbname: str) -> bool:
    res = _psql(
        bins, port, "postgres", superuser,
        f"SELECT 1 FROM pg_database WHERE datname = '{dbname}'"
    )
    return "1 row" in res.stdout


def _role_exists(bins: PgBinaries, port: int, superuser: str, rolename: str) -> bool:
    res = _psql(
        bins, port, "postgres", superuser,
        f"SELECT 1 FROM pg_roles WHERE rolname = '{rolename}'"
    )
    return "1 row" in res.stdout


# ── Core provisioning steps ────────────────────────────────────────────────────


def _init_cluster(bins: PgBinaries, pgdata: Path, os_user: str) -> bool:
    """Run initdb to create a new cluster.

    Returns True when initdb ran (new cluster), False when the cluster
    already exists (PG_VERSION marker present — idempotent skip).
    """
    if (pgdata / _PG_VERSION_MARKER).exists():
        _log.info("pg_cluster_exists_skip_initdb", pgdata=str(pgdata))
        return False

    pgdata.mkdir(parents=True, exist_ok=True)
    _run([
        str(bins.initdb),
        "-D", str(pgdata),
        "--no-locale", "-E", "UTF8",
        "--auth=trust",
        "--username", os_user,
    ])
    _log.info("pg_cluster_initialised", pgdata=str(pgdata))
    return True


def _configure_cluster(pgdata: Path, port: int) -> None:
    """Append port, listen_addresses, and (empty) unix_socket_directories.

    We run TCP-only (``listen_addresses = '127.0.0.1'``; all clients connect via
    ``-h 127.0.0.1``). Omitting ``-k`` from pg_ctl does NOT suppress the Unix
    socket — Postgres still opens one in the config-default
    ``unix_socket_directories``, which on Debian/PGDG is the postgres-owned
    ``/var/run/postgresql``. A non-``postgres`` OS user then fails at startup
    (``could not create lock file …/.s.PGSQL.<port>.lock: Permission denied``).
    Setting it empty disables the socket entirely, which also sidesteps the
    macOS 104-char socket-path limit that motivated dropping ``-k``. (nexus-6laob)
    """
    conf_path = pgdata / "postgresql.conf"
    conf_text = conf_path.read_text() if conf_path.exists() else ""
    # Drop the entire prior nexus-managed block so re-running is idempotent.
    # The block is delimited by BEGIN/END sentinels — filtering only the comment
    # lines (the old behaviour) left the value lines to accumulate on every
    # re-run, duplicating directives (nexus-6laob).
    begin, end = "# nexus-managed: BEGIN", "# nexus-managed: END"
    lines: list[str] = []
    skipping = False
    for l in conf_text.splitlines():
        if l == begin:
            skipping = True
            continue
        if l == end:
            skipping = False
            continue
        if skipping:
            continue
        # Drop stale pre-sentinel managed comment lines (e.g. "# nexus-managed:
        # port") left by an older nexus. Their value lines (port =, listen_
        # addresses =) are left untouched — PG last-wins makes the new block
        # authoritative, and we must not clobber a user's own directive.
        if l.startswith("# nexus-managed:"):
            continue
        lines.append(l)
    if skipping:
        # An earlier write was truncated mid-block (BEGIN without END). Don't
        # silently swallow everything after it — surface the anomaly.
        _log.warning("pg_conf_unterminated_managed_block", path=str(conf_path))
    lines += [
        begin,
        f"port = {port}",
        f"listen_addresses = '127.0.0.1'",
        # Omitting -k does NOT disable the Unix socket; empty disables it (see
        # docstring). Required for non-postgres OS users on Debian/Ubuntu.
        f"unix_socket_directories = ''",
        end,
    ]
    conf_path.write_text("\n".join(lines) + "\n")


def _start_cluster(bins: PgBinaries, pgdata: Path, port: int) -> None:
    """Start the cluster if not already running.

    Uses pg_ctl status to detect a running cluster, then pg_ctl start -w
    (wait) to bring it up.  The pg log goes to pgdata/pg.log.

    UNIX SOCKET NOTE: macOS enforces a 104-character limit on UNIX domain
    socket paths.  When pgdata is deep inside a user's home or a pytest
    tmpdir (e.g. ``/private/var/folders/.../nexus_provision_test0/postgres``)
    the path easily exceeds the limit.  We therefore omit ``-k <pgdata>``
    from the pg_ctl startup options and rely entirely on TCP
    (``listen_addresses = '127.0.0.1'``, configured in postgresql.conf during
    cluster setup).  All client connections use ``-h 127.0.0.1``, so no
    UNIX socket is needed.
    """
    status = _run(
        [str(bins.pg_ctl), "-D", str(pgdata), "status"],
        check=False,
    )
    if status.returncode == 0:
        _log.info("pg_cluster_already_running", pgdata=str(pgdata))
        return

    pglog = str(pgdata / "pg.log")
    # No "-k <pgdata>" — avoids UNIX socket path length issues on macOS.
    # TCP-only: listen_addresses='127.0.0.1' is written to postgresql.conf.
    _run([
        str(bins.pg_ctl), "-D", str(pgdata),
        "-l", pglog,
        "-o", f"-p {port}",
        "start", "-w",
    ])
    # Confirm the port is accepting connections (belt-and-suspenders).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _port_accepting("127.0.0.1", port):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError(
            f"Postgres did not accept connections on 127.0.0.1:{port} within 30 s. "
            f"Check {pglog} for details."
        )
    _log.info("pg_cluster_started", port=port, pgdata=str(pgdata))


def _create_db(bins: PgBinaries, port: int, os_user: str) -> bool:
    """Create the nexus database if it does not exist.

    Returns True when the database was created, False on idempotent skip.
    """
    if _db_exists(bins, port, os_user, NEXUS_DB_NAME):
        _log.info("pg_db_exists_skip_createdb", dbname=NEXUS_DB_NAME)
        return False

    _run([
        str(bins.createdb),
        "-h", "127.0.0.1", "-p", str(port),
        "-U", os_user,
        NEXUS_DB_NAME,
    ])
    _log.info("pg_db_created", dbname=NEXUS_DB_NAME)
    return True


def _extension_exists(bins: PgBinaries, port: int, superuser: str, extname: str) -> bool:
    # ``extname`` must be a trusted literal — _psql shells out and does not
    # support parameterized queries, so the value is interpolated directly.
    # All callers pass the constant "vector".
    res = _psql(
        bins, port, NEXUS_DB_NAME, superuser,
        f"SELECT 1 FROM pg_extension WHERE extname = '{extname}'"
    )
    return "1 row" in res.stdout


def _create_vector_extension(bins: PgBinaries, port: int, os_user: str) -> bool:
    """Create the pgvector ``vector`` extension in the nexus database.

    CREATE EXTENSION requires superuser; provisioning owns the only superuser
    context (``os_user`` is the cluster's initdb owner). nexus_admin is
    NOSUPERUSER, so the Java service's Liquibase ``vectors-001`` changeset
    cannot create the extension itself — it fails with 'permission denied to
    create extension'. Creating it here, at provision time, closes that gap
    (nexus-jdpn9 item 3, hit on the 2026-06-10 production migration run).

    ``check_pgvector_available`` has already verified ``vector.control`` is
    installed for these binaries, so CREATE EXTENSION will not fail for a
    missing control file. Idempotent: ``IF NOT EXISTS`` is a no-op when the
    extension is already present.

    Returns True when the extension was freshly created, False on idempotent
    skip.
    """
    if _extension_exists(bins, port, os_user, "vector"):
        _log.info("pg_vector_extension_exists_skip")
        return False

    _psql(
        bins, port, NEXUS_DB_NAME, os_user,
        "CREATE EXTENSION IF NOT EXISTS vector",
    )
    _log.info("pg_vector_extension_created", db=NEXUS_DB_NAME)
    return True


def _create_roles(
    bins: PgBinaries,
    port: int,
    os_user: str,
    admin_pass: str,
    svc_pass: str,
) -> tuple[bool, bool]:
    """Create nexus_admin and nexus_svc roles, then synchronise passwords.

    nexus_admin — NOSUPERUSER NOCREATEDB NOCREATEROLE LOGIN.
                  Has CREATE ON DATABASE nexus (allows creating new schemas).
                  Has CREATE ON SCHEMA public (required for Liquibase: its
                  DATABASECHANGELOG / DATABASECHANGELOGLOCK tables land in
                  the public schema by default; on PG 15/16 the PUBLIC role
                  no longer holds CREATE on public, so nexus_admin needs it
                  explicitly — as validated by SchemaMigratorIntegrationTest).

    nexus_svc   — NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN.
                  DML-only; FORCE RLS subjects it to all row-level policies.

    Passwords are synchronised UNCONDITIONALLY after create/skip so that the
    credentials file always matches the DB state, even if a previous run
    created roles but crashed before writing credentials.

    Returns (admin_created, svc_created).
    """
    admin_created = False
    svc_created = False

    if not _role_exists(bins, port, os_user, "nexus_admin"):
        _psql(
            bins, port, NEXUS_DB_NAME, os_user,
            f"CREATE ROLE nexus_admin "
            f"NOSUPERUSER NOCREATEDB NOCREATEROLE LOGIN "
            f"PASSWORD '{admin_pass}'",
        )
        # Grant CREATE on the nexus database (allows creating new schemas).
        _psql(
            bins, port, NEXUS_DB_NAME, os_user,
            "GRANT CREATE ON DATABASE nexus TO nexus_admin",
        )
        # Grant CREATE on the public schema so Liquibase's tracking tables
        # (DATABASECHANGELOG, DATABASECHANGELOGLOCK) can be created there.
        # On PG 15/16 the PUBLIC role lost this privilege; nexus_admin is
        # NOSUPERUSER and not the DB owner, so it needs an explicit grant.
        # Evidence: SchemaMigratorIntegrationTest.java:120 issues this grant
        # in its bootstrap — the requirement is known and documented in the
        # net63 integration test.
        _psql(
            bins, port, NEXUS_DB_NAME, os_user,
            "GRANT CREATE ON SCHEMA public TO nexus_admin",
        )
        _log.info("pg_role_created", role="nexus_admin")
        admin_created = True
    else:
        _log.info("pg_role_exists_skip", role="nexus_admin")

    if not _role_exists(bins, port, os_user, "nexus_svc"):
        _psql(
            bins, port, NEXUS_DB_NAME, os_user,
            f"CREATE ROLE nexus_svc "
            f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN "
            f"PASSWORD '{svc_pass}'",
        )
        _log.info("pg_role_created", role="nexus_svc")
        svc_created = True
    else:
        _log.info("pg_role_exists_skip", role="nexus_svc")

    # Unconditional password sync: even if roles were created on a previous
    # run that crashed before writing credentials, this ensures DB state
    # matches the passwords we are about to persist.  ALTER ROLE … PASSWORD
    # is idempotent (updating to the current value is a no-op in PG).
    _psql(
        bins, port, NEXUS_DB_NAME, os_user,
        f"ALTER ROLE nexus_admin PASSWORD '{admin_pass}'",
    )
    _psql(
        bins, port, NEXUS_DB_NAME, os_user,
        f"ALTER ROLE nexus_svc PASSWORD '{svc_pass}'",
    )
    _log.debug("pg_role_passwords_synced")

    return admin_created, svc_created


def _write_credentials(
    creds_path: Path,
    pgdata: Path,
    port: int,
    admin_pass: str,
    svc_pass: str,
    service_token: str,
) -> None:
    """Write the credentials env-file at 0600.

    The file is consumed by the service daemon (bead .30) which sources it
    before starting the JVM.  It uses the NX_DB_ADMIN_* and NX_DB_* names
    that Main.java reads directly.

    PG_DATA and PG_PORT are written for the daemon's lifecycle operations
    (pg_ctl start/stop/status in bead .30).

    NX_SERVICE_TOKEN (gmiaf.32.5) is the persistent random root bearer token.
    It is generated once at provisioning time and is deliberately INDEPENDENT
    of the DB passwords: rotating ``NX_DB_PASS`` / ``NX_DB_ADMIN_PASS`` does not
    change the bearer token (retires the gmiaf.30 ``_derive_stable_token``
    coupling). The supervisor publishes it in the lease; Main.java seeds it as
    a bound ``default``-tenant row.
    """
    db_url = f"jdbc:postgresql://127.0.0.1:{port}/{NEXUS_DB_NAME}"
    content = (
        f"# nexus-managed Postgres credentials — DO NOT EDIT MANUALLY\n"
        f"# Re-run 'nx init --service' to regenerate.\n"
        f"PG_DATA={pgdata}\n"
        f"PG_PORT={port}\n"
        f"NX_DB_ADMIN_URL={db_url}\n"
        f"NX_DB_ADMIN_USER=nexus_admin\n"
        f"NX_DB_ADMIN_PASS={admin_pass}\n"
        f"NX_DB_URL={db_url}\n"
        f"NX_DB_USER=nexus_svc\n"
        f"NX_DB_PASS={svc_pass}\n"
        f"NX_SERVICE_TOKEN={service_token}\n"
    )
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then replace atomically so the file is never
    # left in a half-written state.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=creds_path.parent, prefix=".pg_creds_")
    try:
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(content)
        os.replace(tmp_path, creds_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _log.info("pg_credentials_written", path=str(creds_path))


def _persist_service_token(creds_path: Path, service_token: str) -> None:
    """Append ``NX_SERVICE_TOKEN`` to an existing 0600 credentials file.

    Used to backfill the persistent root token (gmiaf.32.5) for clusters
    provisioned before this field existed, without rewriting the whole file
    (which would require reconstructing every line). Atomic: writes a temp
    file then ``os.replace``.

    Idempotent: a no-op if ``NX_SERVICE_TOKEN`` is already present, so a double
    call (or a race between two ``provision`` runs) cannot append a second,
    conflicting token line that ``_read_credentials`` would silently shadow.
    """
    if "NX_SERVICE_TOKEN" in _read_credentials(creds_path):
        _log.info("pg_service_token_backfill_noop", path=str(creds_path))
        return
    existing = creds_path.read_text()
    if not existing.endswith("\n"):
        existing += "\n"
    content = existing + f"NX_SERVICE_TOKEN={service_token}\n"
    tmp_fd, tmp_path = tempfile.mkstemp(dir=creds_path.parent, prefix=".pg_creds_")
    try:
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(content)
        os.replace(tmp_path, creds_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _log.info("pg_service_token_backfilled", path=str(creds_path))


def _read_credentials(creds_path: Path) -> dict[str, str]:
    """Parse an existing credentials file into a {key: value} dict."""
    result: dict[str, str] = {}
    for line in creds_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def load_service_credentials_into_env(config_dir: Path | None = None) -> bool:
    """Load ``pg_credentials`` into ``os.environ`` for an in-process service flow.

    The manual upgrade path sources ``pg_credentials`` between ``nx init
    --service`` and ``nx migrate-to-service`` so the latter sees
    ``NX_SERVICE_TOKEN`` / ``NX_STORAGE_BACKEND``. ``nx guided-upgrade`` runs both
    in ONE process, so it must self-load the freshly-provisioned credentials
    before driving the migration — otherwise ``NX_SERVICE_TOKEN`` is absent and
    the migration fails. Uses ``setdefault`` for credential keys (a value the
    user already exported wins) and forces ``NX_STORAGE_BACKEND=service`` (the
    guided upgrade IS the service path). Returns True iff a token is present in
    the environment afterwards.

    No-op on the credential keys when the file is absent — the returned bool lets
    the caller decide whether a missing token is fatal.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415  — circular-dep avoidance (nexus.config)

        config_dir = nexus_config_dir()
    creds_path = config_dir / CREDENTIALS_FILENAME
    if creds_path.exists():
        for key, value in _read_credentials(creds_path).items():
            if key.startswith("NX_") or key.startswith("PG_"):
                os.environ.setdefault(key, value)
        os.environ["NX_STORAGE_BACKEND"] = "service"
    return bool(os.environ.get("NX_SERVICE_TOKEN", "").strip())


# ── Public API ─────────────────────────────────────────────────────────────────


def provision(
    config_dir: Path | None = None,
    *,
    force_new_port: bool = False,
) -> ProvisionResult:
    """Provision (or verify) the nx-managed local Postgres cluster.

    Idempotent: safe to call on every ``nx init --service`` run.

    Parameters
    ----------
    config_dir:
        Root of the nexus config directory.  Defaults to
        :func:`nexus.config.nexus_config_dir`.
    force_new_port:
        When True, pick a new free port even if a credentials file already
        exists.  Useful in tests that need a fresh cluster.

    Returns
    -------
    ProvisionResult
        Describes what was created / already existed.

    Raises
    ------
    PgBinaryNotFoundError
        When no PostgreSQL installation is found.
    RuntimeError
        When the cluster fails to start within the timeout.
    subprocess.CalledProcessError
        When any provisioning subprocess exits non-zero.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir  # local import to avoid circular  # noqa: PLC0415 — deferred import — heavy/optional dep loaded only when provisioning runs
        config_dir = nexus_config_dir()

    pgdata = config_dir / "postgres"
    creds_path = config_dir / CREDENTIALS_FILENAME
    os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"

    result = ProvisionResult(credentials_path=creds_path)

    # ── Fast idempotency path ──────────────────────────────────────────────────
    # If the credentials file exists, cluster is initialised, and the port is
    # accepting connections → nothing to do.
    if creds_path.exists() and not force_new_port:
        creds = _read_credentials(creds_path)
        port_str = creds.get("PG_PORT", "")
        if port_str.isdigit():
            stored_port = int(port_str)
            if (pgdata / _PG_VERSION_MARKER).exists() and _port_accepting("127.0.0.1", stored_port):
                # Backfill the persistent root token (gmiaf.32.5) for clusters
                # provisioned before NX_SERVICE_TOKEN existed in the file.
                if not creds.get("NX_SERVICE_TOKEN"):
                    _persist_service_token(creds_path, secrets.token_hex(32))
                # Backfill the pgvector extension for clusters provisioned
                # before this step existed (nexus-jdpn9 item 3). This makes a
                # re-run of `nx init --service` a reliable repair for the
                # original failure mode: cluster already up, Liquibase blocked
                # on a missing 'vector' extension. Discover binaries lazily so
                # the common already-extension-present case stays cheap.
                try:
                    _bins = discover_pg_binaries()
                    check_pgvector_available(_bins)
                    result.vector_extension_created = _create_vector_extension(
                        _bins, stored_port, os_user
                    )
                except PgBinaryNotFoundError:
                    # No binaries to repair with; the running cluster is serving
                    # via some other install. Leave the extension untouched.
                    _log.warning("pg_vector_extension_backfill_no_binaries")
                except PgVectorNotInstalledError:
                    # Binaries found but pgvector is not installed for them
                    # (e.g. postgresql@16 without the versioned pgvector
                    # formula). Cannot repair; warn loud with the install hint
                    # rather than crashing the idempotent re-run — this is the
                    # exact installed-user repair path (nexus-jdpn9).
                    _log.warning(
                        "pg_vector_extension_backfill_no_pgvector",
                        bin_dir=str(_bins.bin_dir),
                    )
                _log.info(
                    "pg_provision_no_op",
                    port=stored_port,
                    pgdata=str(pgdata),
                )
                result.already_provisioned = True
                result.port = stored_port
                return result

    # ── Discover binaries ──────────────────────────────────────────────────────
    bins = discover_pg_binaries()

    # ── pgvector pre-flight (nexus-pebfx.5) ────────────────────────────────────
    # CREATE EXTENSION vector otherwise fails much later (manually 2026-06-08;
    # rediscovered via a mid-provision Liquibase failure 2026-06-10): the
    # Homebrew pgvector formula targets the DEFAULT postgresql major, so with
    # postgresql@16 the control file lands in a different sharedir. Fail here,
    # before any cluster work, with the exact remedy.
    check_pgvector_available(bins)

    # ── Determine port ─────────────────────────────────────────────────────────
    # Reuse the port from an existing credentials file when possible so
    # subsequent service starts use the same address.
    port: int = 0
    if creds_path.exists() and not force_new_port:
        creds = _read_credentials(creds_path)
        port_str = creds.get("PG_PORT", "")
        if port_str.isdigit():
            port = int(port_str)
    if not port:
        port = _find_free_port()

    # ── Generate passwords (only when credentials file is absent) ──────────────
    if creds_path.exists() and not force_new_port:
        creds = _read_credentials(creds_path)
        admin_pass = creds.get("NX_DB_ADMIN_PASS") or secrets.token_hex(16)
        svc_pass = creds.get("NX_DB_PASS") or secrets.token_hex(16)
        # Reuse the persisted root token when present so the bearer survives a
        # re-provision; mint a fresh one otherwise (gmiaf.32.5).
        service_token = creds.get("NX_SERVICE_TOKEN") or secrets.token_hex(32)
    else:
        admin_pass = secrets.token_hex(16)
        svc_pass = secrets.token_hex(16)
        service_token = secrets.token_hex(32)

    # ── initdb ─────────────────────────────────────────────────────────────────
    result.cluster_created = _init_cluster(bins, pgdata, os_user)

    # ── Configure conf (port, TCP-only, socket-disabled) ──────────────────────
    # Unconditional (idempotent via the BEGIN/END sentinel block): re-provisioning
    # an EXISTING but stopped cluster must repair a conf written by an older nexus
    # that lacks unix_socket_directories='' — otherwise the start below fails on
    # Debian/Ubuntu for a non-postgres OS user (nexus-6laob). The fast path above
    # returns before here for an already-running cluster (its conf already works).
    _configure_cluster(pgdata, port)

    # ── Start cluster ──────────────────────────────────────────────────────────
    _start_cluster(bins, pgdata, port)
    result.port = port

    # ── Create database ────────────────────────────────────────────────────────
    result.db_created = _create_db(bins, port, os_user)

    # ── Create pgvector extension ──────────────────────────────────────────────
    # Must run as the cluster superuser (os_user); nexus_admin is NOSUPERUSER and
    # the service's Liquibase migration cannot create it (nexus-jdpn9 item 3).
    result.vector_extension_created = _create_vector_extension(bins, port, os_user)

    # ── Create roles ───────────────────────────────────────────────────────────
    result.admin_role_created, result.svc_role_created = _create_roles(
        bins, port, os_user, admin_pass, svc_pass
    )

    # ── Write credentials ──────────────────────────────────────────────────────
    _write_credentials(creds_path, pgdata, port, admin_pass, svc_pass, service_token)

    _log.info(
        "pg_provision_complete",
        port=port,
        cluster_created=result.cluster_created,
        db_created=result.db_created,
        admin_role_created=result.admin_role_created,
        svc_role_created=result.svc_role_created,
        vector_extension_created=result.vector_extension_created,
    )
    return result


def is_provisioned(config_dir: Path | None = None) -> bool:
    """Return True when the local cluster appears to be provisioned and running.

    Does NOT start the cluster — purely a state check.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import — heavy/optional dep loaded only when provisioning runs
        config_dir = nexus_config_dir()

    pgdata = config_dir / "postgres"
    creds_path = config_dir / CREDENTIALS_FILENAME
    if not creds_path.exists():
        return False
    if not (pgdata / _PG_VERSION_MARKER).exists():
        return False
    creds = _read_credentials(creds_path)
    port_str = creds.get("PG_PORT", "")
    if not port_str.isdigit():
        return False
    return _port_accepting("127.0.0.1", int(port_str))
