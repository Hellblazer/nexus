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
  The provisioner requires system-installed PostgreSQL 16 (or 15) binaries.
  Search order:
    1. ``NEXUS_PG_BIN`` env var override (tests + custom installs).
    2. ``/opt/homebrew/opt/postgresql@16/bin`` (macOS Homebrew PG 16).
    3. ``/opt/homebrew/opt/postgresql@15/bin`` (macOS Homebrew PG 15).
    4. ``initdb`` on PATH (Linux; ``shutil.which`` → parent directory).
    5. ``/usr/lib/postgresql/16/bin`` (Debian/Ubuntu system install).
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


def _install_hint() -> str:
    """Return a platform-appropriate install hint."""
    if sys.platform == "darwin":
        return (
            "Install PostgreSQL 16 with Homebrew:\n"
            "  brew install postgresql@16\n"
            "  brew services start postgresql@16\n"
            "Then re-run `nx init --service`."
        )
    return (
        "Install PostgreSQL 16:\n"
        "  # Debian/Ubuntu:\n"
        "  sudo apt-get install postgresql-16\n"
        "  # RHEL/Fedora:\n"
        "  sudo dnf install postgresql-server\n"
        "Then re-run `nx init --service`."
    )


_CANDIDATE_DIRS: list[Path] = [
    Path("/opt/homebrew/opt/postgresql@16/bin"),
    Path("/opt/homebrew/opt/postgresql@15/bin"),
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


def check_pgvector_available(bins: PgBinaries) -> None:
    """Fail loud when pgvector is not installed for THIS PostgreSQL.

    Checks for ``<sharedir>/extension/vector.control`` via ``pg_config``.
    Indeterminate (pg_config missing/failing) does NOT block — provisioning
    will fail loud at CREATE EXTENSION anyway; this gate exists to move the
    common failure earlier, not to add a new way to be wrong.
    """
    pg_config = bins.bin_dir / "pg_config"
    if not pg_config.is_file():
        _log.warning("pgvector_preflight_no_pg_config", bin_dir=str(bins.bin_dir))
        return
    try:
        result = subprocess.run(
            [str(pg_config), "--sharedir"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("pgvector_preflight_indeterminate", error=str(exc))
        return
    sharedir = result.stdout.strip()
    if result.returncode != 0 or not sharedir:
        _log.warning(
            "pgvector_preflight_indeterminate",
            returncode=result.returncode,
        )
        return
    control = Path(sharedir) / "extension" / "vector.control"
    if control.is_file():
        return
    raise PgVectorNotInstalledError(
        f"The pgvector extension is not installed for the PostgreSQL at "
        f"{bins.bin_dir} (no {control}).\n"
        "The Homebrew 'pgvector' formula targets the default postgresql "
        "major — for a versioned install (e.g. postgresql@16) build from "
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
    #: True when the cluster was already running and no work was needed.
    already_provisioned: bool = False
    #: Port the cluster is listening on.
    port: int = 0
    #: Path to the credentials file (0600).
    credentials_path: Path = field(default_factory=Path)


# ── Low-level helpers ──────────────────────────────────────────────────────────


def _run(cmd: list[str], *, check: bool = True, capture: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess, raising on non-zero exit when *check* is True."""
    _log.debug("pg_provision_run", cmd=cmd)
    kw: dict = dict(check=check, text=True)
    if capture:
        kw["capture_output"] = True
    if env is not None:
        kw["env"] = env
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
    """Append port and listen_addresses to postgresql.conf."""
    conf_path = pgdata / "postgresql.conf"
    conf_text = conf_path.read_text() if conf_path.exists() else ""
    # Remove any lines we previously wrote so re-running is idempotent.
    lines = [
        l for l in conf_text.splitlines()
        if not l.startswith("# nexus-managed:")
    ]
    lines += [
        f"# nexus-managed: port",
        f"port = {port}",
        f"# nexus-managed: listen_addresses",
        f"listen_addresses = '127.0.0.1'",
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
        from nexus.config import nexus_config_dir  # local import to avoid circular
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

    # ── Configure port (only for newly created clusters) ──────────────────────
    if result.cluster_created:
        _configure_cluster(pgdata, port)

    # ── Start cluster ──────────────────────────────────────────────────────────
    _start_cluster(bins, pgdata, port)
    result.port = port

    # ── Create database ────────────────────────────────────────────────────────
    result.db_created = _create_db(bins, port, os_user)

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
    )
    return result


def is_provisioned(config_dir: Path | None = None) -> bool:
    """Return True when the local cluster appears to be provisioned and running.

    Does NOT start the cluster — purely a state check.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir
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
