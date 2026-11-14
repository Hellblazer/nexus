# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup``: an explicit, user-run dump of the local engine's Postgres (nexus-f3yqd.4).

THE RULE (Sam, 2026-11-10): NO automatic backup, ever. The only caller of anything in this
module is the ``nx backup`` verb (``nexus.commands.backup_cmd``); upgrade, doctor, init, self,
the hooks and the daemons never import it. ``tests/test_nx_backup_pin.py`` fails on a Python import of
this module or of the verb, and on a subprocess argv, shell string, hook or script command line
that runs ``nx backup``, under the repository's code directories (the test lists them); it cannot see a command
assembled at run time. They may SUGGEST the verb in text (the scan matches command shapes, not words).

The dump is ``pg_dump -Fc --compress=0`` from the bundled Postgres the engine runs (the same
``discover_pg_binaries`` resolver, ``.exe`` on Windows), against the running local cluster:
one repeatable-read snapshot, so it is consistent while the engine keeps serving. The bundle is
built without zlib, so the dump is NOT compressed (``--compress=0`` says so and keeps pg_dump from
warning). It connects as the cluster's bootstrap superuser with the password the provisioner
recorded in ``pg_credentials``, which bypasses row-level security, so every tenant is in the dump.

The output is written to a ``.partial`` sibling, checked with the bundled ``pg_restore -l``, and
moved into place only when both succeeded and nothing exists at the destination; any failure or
interrupt removes the partial.

Restore is deliberately not a verb (a restore over a live database is destructive). The runbook
(``docs/operations/backup-restore.md``) restores into a FRESH database and swaps names;
:func:`restore_sections` renders its commands, the ``nx backup`` epilogue prints them with the
real bundle path, and the runbook's fenced blocks are tested to equal the placeholder rendering.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from nexus.redact import redact_credentials

_log = structlog.get_logger(__name__)

#: The default destination directory, under the user's home and outside the engine's data directory.
BACKUP_DIRNAME = "nexus-backups"
#: pg_dump gets this long to reach the cluster before it gives up (seconds).
_CONNECT_TIMEOUT_S = "10"
_SIZE_QUERY_TIMEOUT_S = 30.0
_REAP_TIMEOUT_S = 5.0
#: pg_dump gives up on a table lock after this long instead of waiting behind a migration forever.
LOCK_WAIT_TIMEOUT = "60s"
_VERIFY_TIMEOUT_S = 300.0

MANAGED_REFUSAL = (
    "This resolver finds the LOCAL engine's database, and this install is in managed (cloud) mode, which has "
    "none. `nx backup` takes the cloud arm there (an export of your tenant); nothing should reach here."
)
NOT_RUNNING_REFUSAL = (
    "The local database is not accepting connections on 127.0.0.1:{port}, so there is nothing to "
    "dump. Start the engine with `nx daemon service start`, then run `nx backup` again."
)
LOCK_TIMEOUT_HINT = (
    "pg_dump could not lock the tables within {timeout}. Something holds locks on the database, most "
    "likely a schema migration the engine is running (`nx doctor` says so). Wait for it to finish, then "
    "run `nx backup` again."
)
#: Said at the end of every successful run, in --help, the CLI reference, the changelog and the runbook.
ONLY_COPY_SENTENCE = (
    "There is no automatic backup; this file is the only copy besides the live database. "
    "Copy it to another disk or machine."
)
NOT_PROVISIONED_REFUSAL = (
    "No local database is set up under {config_dir} (no pg_credentials with a PG_PORT), so there is "
    "nothing to back up. `nx init` sets one up."
)


class BackupRefused(Exception):
    """A precondition failed before any dump began; the message is the whole story."""


class BackupFailed(Exception):
    """The dump ran and did not produce a backup. ``detail`` is the tool's own output, credentials redacted."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def _windows(platform: str | None) -> bool:
    return (platform == "win32") if platform is not None else (os.name == "nt")


def tool_path(bin_dir: Path, name: str, platform: str | None = None) -> Path:
    """A bundle executable under *bin_dir*, ``.exe`` on Windows: the same rule ``PgBinaries.from_dir``
    applies to initdb and pg_ctl, for the two tools it does not list (pg_dump, pg_restore)."""
    return bin_dir / f"{name}{'.exe' if _windows(platform) else ''}"


@dataclass(frozen=True)
class BackupTarget:
    """Where the dump comes from and with what: every field is resolved, none is looked up later."""

    bin_dir: Path
    pg_dump: Path
    pg_restore: Path
    psql: Path
    host: str
    port: int
    user: str
    dbname: str
    password: str | None = None

    @classmethod
    def from_bin_dir(
        cls, bin_dir: Path, *, host: str, port: int, user: str, dbname: str,
        password: str | None = None, platform: str | None = None,
    ) -> BackupTarget:
        return cls(
            bin_dir=bin_dir, pg_dump=tool_path(bin_dir, "pg_dump", platform),
            pg_restore=tool_path(bin_dir, "pg_restore", platform), psql=tool_path(bin_dir, "psql", platform),
            host=host, port=port, user=user, dbname=dbname, password=password,
        )


@dataclass(frozen=True)
class DumpResult:
    path: Path
    size: int
    seconds: float


def default_destination(*, now: datetime | None = None) -> Path:
    """``~/nexus-backups/nexus-<UTC yyyymmddThhmmssZ>.dump``."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / BACKUP_DIRNAME / f"nexus-{stamp}.dump"


def resolve_target(config_dir: Path | None = None) -> BackupTarget:
    """The running local cluster, or :class:`BackupRefused` saying why not.

    Managed mode is refused first, before any local file is read. Binaries come from
    ``discover_pg_binaries`` (``NEXUS_PG_BIN``, then the extracted bundle): never a host Postgres.
    """
    from nexus import config as _config  # noqa: PLC0415 - read through the module, so a test's mode patch is seen
    from nexus.db import pg_provision as pgp  # noqa: PLC0415 - deferred: heavy import

    if not _config.is_local_mode():
        raise BackupRefused(MANAGED_REFUSAL)
    cfg = config_dir if config_dir is not None else _config.nexus_config_dir()
    creds_path = cfg / pgp.CREDENTIALS_FILENAME
    creds = pgp._read_credentials(creds_path) if creds_path.is_file() else {}
    port_text = creds.get("PG_PORT", "")
    if not port_text.isdigit():
        raise BackupRefused(NOT_PROVISIONED_REFUSAL.format(config_dir=cfg))
    port = int(port_text)
    if not pgp._port_accepting("127.0.0.1", port):
        raise BackupRefused(NOT_RUNNING_REFUSAL.format(port=port))
    try:
        bins = pgp.discover_pg_binaries(cfg)
    except pgp.PgBinaryNotFoundError as exc:
        raise BackupRefused(str(exc)) from None
    target = BackupTarget.from_bin_dir(
        bins.bin_dir, platform=None, host="127.0.0.1", port=port, user=pgp.bootstrap_superuser(),
        dbname=pgp.NEXUS_DB_NAME, password=creds.get(pgp.SUPERUSER_PASS_KEY) or None,
    )
    for tool in (target.pg_dump, target.pg_restore):
        if not tool.is_file():
            raise BackupRefused(
                f"The Postgres bundle at {bins.bin_dir} has no {tool.name}, so nx backup cannot dump or "
                "restore with it. `nx init` fetches a complete bundle."
            )
    return target


def _client_env(target: BackupTarget, tool: Path) -> dict[str, str]:
    """The child's environment: the bundle's own libraries findable, the password in the env only
    (never argv, which any local account can read from ps)."""
    from nexus.db.pg_provision import _bundle_lib_env  # noqa: PLC0415 - the engine's one lib-path rule, reused

    env = _bundle_lib_env([str(tool)], None)
    env["PGCONNECT_TIMEOUT"] = _CONNECT_TIMEOUT_S
    if target.password:
        env["PGPASSWORD"] = target.password
    return env


def database_size(target: BackupTarget) -> int | None:
    """``pg_database_size`` in bytes: the database's size ON DISK, tables, indexes and bloat. Indexes are
    not dumped, so the dump can be smaller or larger than this. None when it cannot be read; never raises."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 - deferred: stdlib-light module, hot-path import avoided

    argv = [
        str(target.psql), "-X", "-w", "-h", target.host, "-p", str(target.port), "-U", target.user,
        "-d", target.dbname, "-t", "-A", "-c", "SELECT pg_database_size(current_database())",
    ]
    try:
        proc = run_bounded(argv, timeout=_SIZE_QUERY_TIMEOUT_S, env=_client_env(target, target.psql))
        return int(proc.stdout.strip()) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def free_space_warning(dest: Path, db_size: int | None) -> str | None:
    """A warning (never a refusal) when the destination's disk has less free space than the database
    occupies: the dump is uncompressed and about that size. None when there is room or no size is known."""
    if not db_size:
        return None
    probe = dest.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None
    if free >= db_size:
        return None
    return (
        f"{_human(free)} are free where {dest} will be written and the database occupies {_human(db_size)}; "
        "the dump is not compressed, so it may not fit. Continuing; pick another disk with --to if it fails."
    )


def _human(n: float) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{int(n)} B"


def already_there(dest: Path) -> BackupRefused:
    return BackupRefused(
        f"{dest} already exists. nx backup never overwrites a file: pick another path with --to, "
        "or move that one away."
    )


def cannot_write(dest: Path, exc: BaseException) -> BackupRefused:
    reason = getattr(exc, "strerror", None) or str(exc)
    return BackupRefused(f"cannot write the backup at {dest}: {reason}. Pick another path with --to.")


def _publish(tmp: Path, dest: Path) -> None:
    """Move *tmp* to *dest* unless something is at *dest*, never overwriting.

    A hard link fails if the name exists, which a rename would not on POSIX. A filesystem without
    hard links (exFAT, an SMB share) takes the fallback: reserve *dest* with ``O_EXCL``, so a file that
    appears at any moment is refused, then rename *tmp* over our own empty placeholder; the placeholder
    is removed again if the rename fails or an interrupt lands before it completes."""
    from nexus._winsec import open_private  # noqa: PLC0415 - as in run_dump

    try:
        os.link(tmp, dest)
    except FileExistsError:
        raise already_there(dest) from None
    except OSError:
        try:
            try:
                fd = open_private(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise already_there(dest) from None
            except OSError as exc:
                raise cannot_write(dest, exc) from None
            os.close(fd)
            os.replace(tmp, dest)
        except BackupRefused:  # nothing was claimed
            raise
        except BaseException:  # an interrupt or a failed rename, any time after the claim: no empty file is left at dest
            _drop_empty_placeholder(dest)
            raise
        return
    tmp.unlink()


def _drop_empty_placeholder(dest: Path) -> None:
    """Remove *dest* if it is the empty placeholder :func:`_publish` just claimed. A finished dump is never
    empty, so a file with bytes in it (another run's) is left alone."""
    with contextlib.suppress(OSError):
        if dest.stat().st_size == 0:
            dest.unlink()


def _stderr_text(handle: object) -> str:
    handle.seek(0)  # type: ignore[attr-defined]
    return redact_credentials(handle.read().decode("utf-8", errors="replace").strip())  # type: ignore[attr-defined]


def _verify(target: BackupTarget, path: Path) -> None:
    """``pg_restore -l`` of the finished file with the bundled binary: proves the archive's table of
    contents reads. Raises :class:`BackupFailed` when it does not."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 - deferred, as above

    try:
        proc = run_bounded(
            [str(target.pg_restore), "-l", str(path)], timeout=_VERIFY_TIMEOUT_S, stdout=subprocess.DEVNULL,
            env=_client_env(target, target.pg_restore),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupFailed(f"could not check the dump with {target.pg_restore.name} -l: {exc}") from None
    if proc.returncode != 0:
        detail = redact_credentials((proc.stderr or "").strip())
        first = detail.splitlines()[0] if detail else f"exit {proc.returncode}"
        raise BackupFailed(f"the finished dump does not verify ({target.pg_restore.name} -l said: {first})", detail)


def run_dump(
    target: BackupTarget,
    dest: Path,
    *,
    on_progress: Callable[[int], None] | None = None,
    poll_s: float = 0.5,
    lock_wait_timeout: str = LOCK_WAIT_TIMEOUT,
) -> DumpResult:
    """Dump *target* to *dest* (``pg_dump -Fc``, uncompressed), calling ``on_progress(bytes_written)`` every *poll_s*.

    Refuses an existing or unwritable *dest* (:class:`BackupRefused`). Any other failure is
    :class:`BackupFailed`; an interrupt propagates. In every failure case pg_dump is gone and no partial
    file remains. The finished file is checked with the bundled ``pg_restore -l`` before it is published.
    *lock_wait_timeout* is pg_dump's ``--lock-wait-timeout`` (a test seam: the verb always uses the default).
    """
    from nexus._winsec import open_private  # noqa: PLC0415 - the one place that makes a file owner-only on both platforms
    from nexus.bounded_subprocess import kill_child_and_descendants  # noqa: PLC0415 - deferred, as above
    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 - deferred, as above
    from nexus.util.process_group import contain, isolation_popen_kwargs, release  # noqa: PLC0415 - deferred, as above

    try:
        if os.path.lexists(dest):
            raise already_there(dest)
        parent = dest.parent
        if not parent.exists():
            parent.mkdir(parents=True)
            if os.name != "nt":
                parent.chmod(0o700)  # the dump holds every tenant's data (on Windows the folder's ACL decides)
        tmp = parent / f".{dest.name}.partial-{os.getpid()}"
        fd = open_private(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # owner-only before the first byte, Windows too
        os.close(fd)
    except (OSError, ValueError) as exc:  # a read-only or missing parent, a too-long name, a NUL byte
        raise cannot_write(dest, exc) from None
    started = time.monotonic()
    argv = [
        str(target.pg_dump), "-Fc", "--compress=0", "-w", f"--lock-wait-timeout={lock_wait_timeout}",
        "-h", target.host, "-p", str(target.port), "-U", target.user, "-f", str(tmp), target.dbname,
    ]
    errors = tempfile.TemporaryFile()  # noqa: SIM115 - closed in the finally below
    proc: subprocess.Popen[bytes] | None = None
    job: int | None = None
    try:
        with _interrupts_held():
            try:
                proc = subprocess.Popen(  # noqa: S603 - argv is a list built from resolved paths
                    argv_off_cwd(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                    env=_client_env(target, target.pg_dump), cwd=parent,  # the destination's folder: it exists, and a deleted worktree is not the cwd
                    **isolation_popen_kwargs(),
                )
            except OSError as exc:
                raise BackupFailed(f"could not start {target.pg_dump}: {exc}") from None
            job = contain(proc)
        while True:
            returncode = proc.poll()
            if on_progress is not None:
                on_progress(_size(tmp))
            if returncode is not None:
                break
            time.sleep(poll_s)
        if returncode != 0:
            detail = _stderr_text(errors)
            first = detail.splitlines()[0] if detail else f"pg_dump exited {returncode}"
            if is_lock_timeout(detail):
                first = LOCK_TIMEOUT_HINT.format(timeout=lock_wait_timeout)
            raise BackupFailed(first, detail)
        size = _size(tmp)
        if size == 0:
            raise BackupFailed("pg_dump exited 0 but wrote nothing")
        _verify(target, tmp)
        _publish(tmp, dest)
        return DumpResult(dest, size, time.monotonic() - started)
    except BaseException:
        if proc is not None and proc.poll() is None:
            kill_child_and_descendants(proc, job)
            job = None
            with contextlib.suppress(Exception):
                proc.wait(timeout=_REAP_TIMEOUT_S)
        raise
    finally:
        if job is not None:
            release(job)
        errors.close()
        with contextlib.suppress(OSError):
            tmp.unlink()  # gone after a publish; the partial after anything else


#: The signals the verb turns into KeyboardInterrupt (backup_cmd): held while pg_dump is spawned.
_HELD_SIGNALS: frozenset[int] = frozenset(
    getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)
)


@contextlib.contextmanager
def _interrupts_held() -> Iterator[None]:
    """Defer SIGINT, SIGTERM and SIGHUP from the moment pg_dump is spawned until ``proc`` and its
    containment exist. A signal whose handler ran inside ``Popen`` raised KeyboardInterrupt before ``proc``
    was assigned, so the cleanup could not kill pg_dump and it ran on, orphaned (nexus-f3yqd.4, develop CI).
    A thread signal mask does not close that window: the mask is per thread, the kernel gives a
    process-directed signal to a thread that does not block it (an xdist worker has execnet's), and CPython
    still runs the Python handler in the main thread at its next bytecode. So the handlers themselves are
    swapped for ones that only record the signal; afterwards the previous handlers come back and each
    recorded signal is raised again, with ``proc`` set. Python handlers run only in the main thread, which
    is also the only thread that may install them, so off it this does nothing (the verb installs no
    handlers there either)."""
    if threading.current_thread() is not threading.main_thread() or not _HELD_SIGNALS:
        yield
        return
    seen: list[int] = []

    def _record(signum: int, frame: object) -> None:
        seen.append(signum)

    previous: dict[int, Any] = {}
    try:
        for sig in sorted(_HELD_SIGNALS):
            previous[sig] = signal.signal(sig, _record)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
        for sig in dict.fromkeys(seen):  # once each, in arrival order
            signal.raise_signal(sig)


def is_lock_timeout(detail: str) -> bool:
    """Is this pg_dump's own report that ``--lock-wait-timeout`` ran out? The bundled pg_dump 17 prints
    ``query failed: ERROR:  canceling statement due to statement timeout`` and, on the next line,
    ``Query was: LOCK TABLE <t> IN ACCESS SHARE MODE``: a statement timeout alone could be anything, so both."""
    low = detail.lower()
    return ("statement timeout" in low and "lock table" in low) or "lock timeout" in low or "could not obtain lock" in low


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# -- the restore runbook's commands: one renderer for the docs, the epilogue and the tests -----------

#: Placeholders the runbook shows where this machine's values go.
RESTORE_PLACEHOLDERS = {
    "bin_dir": "<pg_bin>", "port": "<port>", "user": "<user>", "dump": "<dump>", "pgdata": "<pgdata>",
}


def _quote(value: str, platform: str | None) -> str:
    """Double quotes round a path (the common case, and what a reader expects); a value with a
    character that is special inside them gets the platform's single-quote form."""
    win = _windows(platform)
    special = '"`$' if win else '"`$\\!'
    if not any(c in value for c in special):
        return f'"{value}"'
    if win:
        return "'" + value.replace("'", "''") + "'"
    import shlex  # noqa: PLC0415 - rare branch

    return shlex.quote(value)


def restore_sections(
    bin_dir: str, port: object, user: str, dump: str, *, platform: str | None = None, host: str = "127.0.0.1",
    live_db: str = "nexus", restored_db: str = "nexus_restored", old_db: str = "nexus_before_restore",
    pgdata: str = "<pgdata>",
) -> dict[str, str]:
    """The runbook's commands, rendered for *platform* (``"win32"`` or anything else for POSIX; None
    is this machine). Keys, in the order a restore uses them:

    ``restore``       create a fresh database and ``pg_restore`` into it; overwrites nothing.
    ``pg_up``         ``SELECT 1`` through the bundled psql: is Postgres answering?
    ``pg_start``      start Postgres alone with the argv the engine's supervisor uses (for when it is not).
    ``check``         how many connections the live database has; must be 0 before the swap.
    ``swap``          ONE statement batch: rename the live database aside, the restored one into its place.
    ``undo``          the same in the other direction, to go back.
    ``drop_restored`` delete the restored database (a restore that failed or was swapped back).
    ``drop``          delete the database that was set aside.

    Every tool is the bundle's, named by its path under *bin_dir*: never a bare name on PATH.
    """
    win = _windows(platform)
    sep, ext, lead = ("\\", ".exe", "& ") if win else ("/", "", "")
    if win:  # one separator throughout, whatever the caller's spelling (the supervisor's argv is all backslashes)
        from pathlib import PureWindowsPath  # noqa: PLC0415 - one use

        bin_dir, dump, pgdata = (str(PureWindowsPath(p)) for p in (bin_dir, dump, pgdata))

    def tool(name: str) -> str:
        return lead + _quote(f"{bin_dir}{sep}{name}{ext}", platform)

    conn = f"-h {host} -p {port} -U {user}"
    psql = f"{tool('psql')} {conn} -d postgres"

    def sql(statement: str, flags: str = "") -> str:
        return f'{psql}{flags} -c "{statement}"'

    return {
        "restore": "\n".join([
            f"{tool('createdb')} {conn} {restored_db}",
            f"{tool('pg_restore')} {conn} -d {restored_db} --exit-on-error {_quote(dump, platform)}",
        ]),
        "pg_up": sql("SELECT 1", " -t -A"),
        "pg_start": f"{tool('pg_ctl')} -D {_quote(pgdata, platform)} -l "
                    f"{_quote(pgdata + sep + 'pg.log', platform)} -o \"-p {port}\" start -w",
        "check": sql(f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{live_db}'", " -t -A"),
        "swap": sql(f"ALTER DATABASE {live_db} RENAME TO {old_db}; ALTER DATABASE {restored_db} RENAME TO {live_db}"),
        "undo": sql(f"ALTER DATABASE {live_db} RENAME TO {restored_db}; ALTER DATABASE {old_db} RENAME TO {live_db}"),
        "drop_restored": sql(f"DROP DATABASE {restored_db}"),
        "drop": sql(f"DROP DATABASE {old_db}"),
    }
