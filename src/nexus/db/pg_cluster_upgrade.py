# SPDX-License-Identifier: AGPL-3.0-or-later
"""Upgrade a local nx cluster made by PostgreSQL below 17 with the bundle's ``pg_upgrade`` (nexus-j738q).

The design of record is the runbook section "A local cluster older than PostgreSQL 17"
(``docs/migration-runbook.md``); this module is that script with its failure paths made code:

* the old server must already be down (checked with the OLD ``pg_ctl``) and so must the service: both are refused, not stopped;
* the new cluster is made with :func:`nexus.db.pg_provision._initdb_argv`, the options ``nx`` creates its own
  clusters with, plus ``--auth=trust`` for the upgrade (the next service start converts it to scram-sha-256);
* the superuser password reaches ``pg_upgrade``, ``psql`` and ``vacuumdb`` as ``PGPASSWORD`` in the CHILD's
  environment only: never argv, never output, never a log, and never the service's environment;
* any failure after the data directory is moved aside removes the new directory and moves the original back.
  An interrupt (Ctrl-C, SIGTERM, SIGHUP) first kills and reaps the running child's process group, so the
  rollback never runs while ``pg_upgrade`` or a postmaster it started can still write.

Three modes, in increasing reach: :meth:`ClusterUpgrade.dry_run` changes nothing and prints the plan;
:meth:`ClusterUpgrade.check_only` runs ``initdb`` into a temporary directory (never the real data directory)
and ``pg_upgrade --check`` against it; :meth:`ClusterUpgrade.apply` does the upgrade.

``--apply`` refuses while the storage service or the old server is running (it stops neither; the message names
the command to run) and starts the service again through an injected callable (``nx daemon service start``), so
the module never reaches a real service from a test.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from nexus.daemon.service_registry import SpawnLockHold

from nexus.db.pg_provision import (
    _INITDB_TIMEOUT_S,
    _PG_CTL_WAIT_TIMEOUT_S,
    CREDENTIALS_FILENAME,
    SUPERUSER_PASS_KEY,
    PgBinaries,
    PgBinaryNotFoundError,
    PgRootUserError,
    _initdb_argv,
    _read_credentials,
    _run,
    bootstrap_superuser,
    discover_pg_binaries,
    refuse_root,
)

_log = structlog.get_logger(__name__)

#: Backstops, not budgets: a large cluster's ``pg_upgrade`` and ``vacuumdb --analyze-in-stages`` take hours.
_PG_UPGRADE_TIMEOUT_S: float = 6 * 3600.0
_VACUUMDB_TIMEOUT_S: float = 6 * 3600.0
_PSQL_FOLLOWUP_TIMEOUT_S: float = 3600.0
_PG_CTL_STATUS_TIMEOUT_S: float = 60.0

#: ``pg_ctl status`` exits 0 for a running server, 3 for none and 4 for a data directory it cannot read.
_PG_CTL_RUNNING = 0
_PG_CTL_NOT_RUNNING = 3
_PG_CTL_DOWN = frozenset({3, 4})

_RUNBOOK = (
    "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md "
    "(section 'A local cluster older than PostgreSQL 17')"
)

#: The limit of what is known about this command. Shown in the help, the docs, the dry run and before the move.
#: Remove it once the command has run against a real pre-17 cluster.
UNTESTED_NOTE = "not yet run against a real pre-17 cluster; copy the data directory first"
#: What the service lock guarantees and what it does not. ``--apply`` and ``--check-only`` hold the storage
#: service's spawn lock (the one a supervisor's ``start()`` takes) for the whole run, so a concurrent
#: ``nx daemon service start`` waits on it instead of starting a supervisor on the half-built cluster. It is
#: freed just before this command starts the service again: a supervisor that child ``start`` spawns would
#: otherwise wait on its own parent. Said wherever the untested note is.
LOCK_NOTE = ("the storage service's spawn lock is held for the whole run (freed just before the service is started "
             "again), so a concurrent `nx daemon service start` waits instead of starting on the half-built cluster. "
             "--apply refuses while the service or the old PostgreSQL server is running (it stops neither: run "
             "`nx daemon service stop` first); the lock does not keep a server that another manager (brew services, "
             "systemd) restarts from coming back")

#: The registry tier whose supervisor takes the spawn lock (``storage_service_daemon._REGISTRY_TIER``; a test pins them equal).
_SERVICE_TIER = "storage_service"

#: What a failure before the data directory moves leaves behind: nothing was stopped, so nothing needs starting.
_NOT_MOVED = "The data directory was not moved, and nothing was stopped."

#: The work directory keeps pg_upgrade's logs, which include the globals dump with the roles' password hashes.
_WORKDIR_HASH_NOTE = (
    "It can hold the roles' SCRAM password hashes (not the password itself): delete it once you have read the logs "
    "or the upgrade is confirmed."
)

#: ``verb`` is ``"stop"`` or ``"start"``; the callable runs ``nx daemon service <verb>`` and returns its exit code.
ServiceControl = Callable[[str, Path], int]


def _hold_service_lock(plan: "UpgradePlan") -> "SpawnLockHold":
    """Take the storage service's spawn lock for *plan*'s config directory, or raise :class:`PgUpgradeRefused`.

    The lock itself lives in the shared lifecycle primitive (:meth:`ServiceRegistry.try_hold_spawn_lock`); this is
    only the translation of its refusal into this command's.
    """
    from nexus.daemon.service_registry import ServiceRegistry, SpawnLockBusyError  # noqa: PLC0415 — deferred, heavy; this command only

    try:
        return ServiceRegistry(dir=plan.config_dir, tier=_SERVICE_TIER).try_hold_spawn_lock()
    except SpawnLockBusyError as exc:
        raise PgUpgradeRefused(f"{exc}. Wait for it to finish and run this again. Nothing was changed.") from None
    except OSError as exc:
        raise PgUpgradeRefused(
            f"could not take the storage service's spawn lock in {plan.config_dir} ({exc.strerror}). Nothing was changed."
        ) from None


def _find_running_service(plan: "UpgradePlan") -> list[str]:
    """What ``nx daemon service stop`` would find running for *plan*'s config directory (empty: none). Read-only."""
    from nexus.daemon.storage_service_daemon import storage_service_running  # noqa: PLC0415 — deferred, heavy; this command only

    return storage_service_running(plan.config_dir)


class PgUpgradeRefused(Exception):
    """The upgrade was refused before anything changed."""


class PgUpgradeFailed(Exception):
    """The upgrade failed after it began; the message says what state the cluster is in."""


class _StepFailed(Exception):
    """One external step failed (internal; carries the scrubbed tail of its output)."""


class _Interrupted(KeyboardInterrupt):
    """A termination signal delivered during ``--apply``, raised so the run unwinds through the rollback."""


_FATAL_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name))


@contextlib.contextmanager
def _signals_blocked() -> Iterator[None]:
    """Block the fatal signals while handlers are swapped, so a second signal cannot land between two
    ``signal.signal`` calls (review r2). A pending one is delivered when the mask is restored."""
    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:  # no POSIX signal masks (Windows, which the command refuses anyway)
        yield
        return
    previous = mask(signal.SIG_BLOCK, _FATAL_SIGNALS)
    try:
        yield
    finally:
        mask(signal.SIG_SETMASK, previous)


@contextlib.contextmanager
def _interrupts_raise() -> Iterator[None]:
    """For the block: SIGINT, SIGTERM and SIGHUP raise :class:`_Interrupted` in the main thread.

    A signal whose disposition is already "ignore" is left alone (``nohup`` asked for exactly that).
    Off the main thread ``signal.signal`` refuses, and the block runs without handlers.
    """
    def handler(signum: int, _frame: object) -> None:
        raise _Interrupted(signal.Signals(signum).name)

    saved: dict[int, object] = {}
    with _signals_blocked():
        for sig in _FATAL_SIGNALS:
            try:
                if signal.getsignal(sig) == signal.SIG_IGN:
                    continue
                saved[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                break
    try:
        yield
    finally:
        with _signals_blocked():
            for sig, previous in saved.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, previous if previous is not None else signal.SIG_DFL)  # type: ignore[arg-type]


@contextlib.contextmanager
def _interrupts_held() -> Iterator[None]:
    """For the block: SIGINT, SIGTERM and SIGHUP are ignored, so a rollback cannot be cut in half."""
    saved: dict[int, object] = {}
    with _signals_blocked():
        for sig in _FATAL_SIGNALS:
            try:
                saved[sig] = signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                break
    try:
        yield
    finally:
        with _signals_blocked():
            for sig, previous in saved.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, previous if previous is not None else signal.SIG_DFL)  # type: ignore[arg-type]


@dataclass(frozen=True)
class UpgradePlan:
    """What an upgrade would do, resolved and validated; building one changes nothing."""

    config_dir: Path
    pgdata: Path
    pgdata_old: Path
    old_bin: Path
    new_bins: PgBinaries
    os_user: str
    cluster_major: int
    port: str | None
    #: The cluster's superuser password from ``pg_credentials``. Never printed or logged: hidden from repr.
    password: str | None = field(default=None, repr=False)

    @property
    def pg_upgrade(self) -> Path:
        return self.new_bins.bin_dir / "pg_upgrade"

    @property
    def vacuumdb(self) -> Path:
        return self.new_bins.bin_dir / "vacuumdb"


def _child_env(password: str | None = None) -> dict[str, str]:
    """A CHILD environment: the inherited one without any PGPASSWORD, plus *password* when given."""
    env = {k: v for k, v in os.environ.items() if k != "PGPASSWORD"}
    if password:
        env["PGPASSWORD"] = password
    return env


def _major(text: str) -> int | None:
    head = text.strip().split(".", 1)[0]
    return int(head) if head.isascii() and head.isdigit() else None


def _tool_major(tool: Path) -> int | None:
    """The major version ``<tool> --version`` reports (``pg_ctl (PostgreSQL) 16.4`` -> 16), or None."""
    try:
        done = _run([str(tool), "--version"], check=False, timeout=_PG_CTL_STATUS_TIMEOUT_S, env=_child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    match = re.search(r"\)\s*(\d+)", done.stdout or "")
    return int(match.group(1)) if match else None


def _absolute(path: Path) -> Path:
    """*path* made absolute against the caller's cwd, without following symlinks.

    pg_upgrade runs from a work directory, so a relative path handed on would resolve there.
    """
    return Path(os.path.abspath(path))


def build_plan(config_dir: Path, old_bin: Path, *, require_no_old_dir: bool = True) -> UpgradePlan:
    """Validate everything that can be validated without changing anything; raise :class:`PgUpgradeRefused`.

    ``require_no_old_dir`` is False for ``--check-only``, which moves nothing and so does not care.
    """
    if sys.platform == "win32":
        raise PgUpgradeRefused(f"this upgrade is for Linux and macOS; see {_RUNBOOK}")
    try:
        refuse_root()
    except PgRootUserError as exc:
        raise PgUpgradeRefused(str(exc)) from None
    from nexus.health import PG_VERSION_FLOOR  # noqa: PLC0415 — deferred, heavy module; this command only

    config_dir = _absolute(config_dir)
    old_bin = _absolute(old_bin)
    creds_path = config_dir / CREDENTIALS_FILENAME
    try:
        creds = _read_credentials(creds_path)
    except (OSError, ValueError) as exc:
        raise PgUpgradeRefused(
            f"no readable {CREDENTIALS_FILENAME} in {config_dir} ({type(exc).__name__}): "
            "there is no nx-managed local cluster here to upgrade"
        ) from None
    pgdata = _absolute(Path(creds.get("PG_DATA") or (config_dir / "postgres")))
    try:
        version_text = (pgdata / "PG_VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise PgUpgradeRefused(f"{pgdata / 'PG_VERSION'} is not readable: there is no cluster to upgrade") from None
    cluster_major = _major(version_text)
    if cluster_major is None:
        raise PgUpgradeRefused(f"{pgdata / 'PG_VERSION'} holds {version_text!r}, which is not a PostgreSQL version")
    if cluster_major >= PG_VERSION_FLOOR:
        raise PgUpgradeRefused(
            f"the cluster at {pgdata} is already PostgreSQL {version_text} (the engine needs "
            f"{PG_VERSION_FLOOR} or newer): there is nothing to upgrade"
        )

    old_pg_ctl = old_bin / "pg_ctl"
    if not old_pg_ctl.is_file():
        raise PgUpgradeRefused(f"--old-bin {old_bin} has no pg_ctl: it must be the bin/ of the PostgreSQL that made the cluster")
    old_major = _tool_major(old_pg_ctl)
    if old_major is None:
        raise PgUpgradeRefused(f"{old_pg_ctl} --version did not report a PostgreSQL version")
    if old_major != cluster_major:
        raise PgUpgradeRefused(
            f"--old-bin is PostgreSQL {old_major} but the cluster's PG_VERSION is {version_text}: "
            "give the bin/ of the PostgreSQL that created the cluster"
        )

    try:
        new_bins = discover_pg_binaries(config_dir)  # the bundle beside the cluster, not the process's default dir
        if not new_bins.bin_dir.is_absolute():  # review r2: a relative NEXUS_PG_BIN would break once cwd moves
            new_bins = type(new_bins).from_dir(_absolute(new_bins.bin_dir))
    except PgBinaryNotFoundError as exc:
        raise PgUpgradeRefused(f"the bundled PostgreSQL {PG_VERSION_FLOOR} is not available: {exc}") from None
    for name in ("pg_upgrade", "vacuumdb"):
        if not (new_bins.bin_dir / name).is_file():
            raise PgUpgradeRefused(f"the bundle at {new_bins.bin_dir} has no {name}")

    pgdata_old = pgdata.with_name(pgdata.name + ".old")
    if require_no_old_dir and os.path.lexists(pgdata_old):
        raise PgUpgradeRefused(_old_dir_text(pgdata, pgdata_old) + " Nothing was changed.")

    return UpgradePlan(
        config_dir=config_dir,
        pgdata=pgdata,
        pgdata_old=pgdata_old,
        old_bin=old_bin,
        new_bins=new_bins,
        os_user=bootstrap_superuser(),
        cluster_major=cluster_major,
        port=creds.get("PG_PORT") or None,
        password=creds.get(SUPERUSER_PASS_KEY) or None,
    )


def _old_dir_text(pgdata: Path, pgdata_old: Path) -> str:
    return (
        f"{pgdata_old} exists and is the pre-upgrade cluster: keep it. "
        "If the service never ran on the new cluster (an earlier attempt failed), back out: remove the "
        f"new {pgdata}, then `mv {pgdata_old} {pgdata}`, and run this again. If the service has run on the "
        f"new cluster, the upgrade already succeeded: keep both and do not run this again. See {_RUNBOOK}."
    )


def plan_lines(plan: UpgradePlan) -> list[str]:
    """The steps ``--apply`` would take, in order, as printable lines."""
    old_ctl, new = plan.old_bin / "pg_ctl", plan.new_bins.bin_dir
    return [
        f"NOTE: this command is {UNTESTED_NOTE} ({plan.pgdata}).",
        f"NOTE: {LOCK_NOTE}.",
        "",
        f"cluster      {plan.pgdata} (PostgreSQL {plan.cluster_major}, superuser {plan.os_user})",
        f"old bin      {plan.old_bin}",
        f"new bin      {new} (the bundle)",
        f"kept copy    {plan.pgdata_old}",
        "",
        "--apply would:",
        "  0. take the storage service's spawn lock (refuse if another process holds it); freed before step 6",
        f"  1. refuse if the storage service is running (it is not stopped: run `nx daemon service stop --config-dir {plan.config_dir}` first)",
        f"  2. refuse if a server runs on {plan.pgdata} (checked with {old_ctl} -D {plan.pgdata} status; it is not stopped: "
        f"`{old_ctl} -D {plan.pgdata} -m fast stop`, and stop its service manager)",
        f"  3. refuse if {plan.pgdata_old} exists; else mv {plan.pgdata} {plan.pgdata_old}",
        "  4. from a short temporary directory: initdb into a new data directory with the options nx creates "
        "its own clusters with, --auth=trust",
        "  5. pg_upgrade --check, then the real pg_upgrade (the password, "
        + ("from pg_credentials, " if plan.password else "none recorded, ")
        + "goes in the child's environment only)",
        f"     any failure from step 3 on: stop the child and any server it started, remove the new {plan.pgdata}, "
        f"mv {plan.pgdata_old} back",
        "  6. nx daemon service start",
        "  7. psql -f update_extensions.sql (when pg_upgrade wrote one; it says whether it found one), then "
        "vacuumdb --all --analyze-in-stages, both over TCP",
        f"  8. keep {plan.pgdata_old}; say how to remove it once the service has run",
        "",
        "--check-only runs pg_upgrade --check against the real cluster, with a temporary initdb as the new one. pg_upgrade --check "
        "starts and stops the old server on the real data directory and does not modify it, so the old server "
        "must not be running. If the check fails or is interrupted it stops the old server only when it started it.",
        "Nothing was changed. Re-run with --check-only to test it, or --apply to upgrade.",
    ]


#: Where the work directories go. Short on purpose: pg_upgrade puts its sockets in the cwd, and macOS caps a
#: socket path at 103 bytes, which the default per-user temp directory there would exceed.
SHORT_TMP_ROOT: Path = Path("/tmp")


def _short_tempdir() -> Path:
    """A fresh private directory under :data:`SHORT_TMP_ROOT`."""
    return Path(tempfile.mkdtemp(prefix="pgup.", dir=SHORT_TMP_ROOT if SHORT_TMP_ROOT.is_dir() else None))


class ClusterUpgrade:
    """Runs one plan. *say* prints a line; *service* starts the storage service again."""

    def __init__(
        self,
        plan: UpgradePlan,
        *,
        say: Callable[[str], None],
        service: ServiceControl,
        hold_lock: Callable[[UpgradePlan], "SpawnLockHold"] = _hold_service_lock,
        find_service: Callable[[UpgradePlan], list[str]] | None = None,
    ) -> None:
        self.plan = plan
        self.say = say
        self.service = service
        self._hold_lock = hold_lock
        # Looked up at call time, so a test (or a caller) can replace the module's finder.
        self._find_service = find_service or (lambda plan: _find_running_service(plan))
        self._lock: "SpawnLockHold | None" = None
        self._moved = False

    # ── helpers ────────────────────────────────────────────────────────────

    def _env(self, *, secret: bool) -> dict[str, str]:
        return _child_env(self.plan.password if secret else None)

    def _scrub(self, text: str) -> str:
        pw = self.plan.password
        return text.replace(pw, "***") if pw else text

    def _tail(self, done: subprocess.CompletedProcess, lines: int = 25) -> str:
        text = self._scrub(f"{done.stdout or ''}{done.stderr or ''}").strip()
        return "\n".join(text.splitlines()[-lines:])

    def _service(self, verb: str) -> int:
        """Run the service callable; an exception from it is a nonzero exit, with the type named."""
        try:
            return self.service(verb, self.plan.config_dir)
        except Exception as exc:  # noqa: BLE001 — a timeout or spawn failure is a failed verb, not a crash
            _log.error("pg_upgrade_service_verb_raised", verb=verb, error=type(exc).__name__)
            self.say(f"`nx daemon service {verb}` raised {type(exc).__name__}")
            return 1

    def _step(self, what: str, argv: list[str], *, timeout: float, cwd: Path | None = None, secret: bool = False) -> subprocess.CompletedProcess:
        """Run one command; raise :class:`_StepFailed` (with scrubbed output) on a nonzero exit, timeout or spawn failure.

        An interrupt kills and reaps the child's process group before it propagates.
        """
        try:
            done = _run(argv, check=False, timeout=timeout, cwd=cwd, env=self._env(secret=secret), kill_on_interrupt=True)
        except subprocess.TimeoutExpired:
            raise _StepFailed(f"{what} did not finish within {timeout:.0f} s") from None
        except OSError as exc:
            raise _StepFailed(f"{what} could not run: {type(exc).__name__}") from None
        if done.returncode != 0:
            raise _StepFailed(f"{what} failed (exit {done.returncode})\n{self._tail(done)}".rstrip())
        return done

    def _server_state(self, pg_ctl: Path, data: Path) -> int:
        """``pg_ctl status``'s exit code; a status that cannot run is a :class:`_StepFailed`."""
        try:
            done = _run([str(pg_ctl), "-D", str(data), "status"], check=False, timeout=_PG_CTL_STATUS_TIMEOUT_S,
                        env=self._env(secret=False))
        except Exception as exc:  # noqa: BLE001 — timeout, spawn failure, root: all mean "state unknown"
            raise _StepFailed(f"`{pg_ctl} -D {data} status` could not run ({type(exc).__name__})") from None
        return done.returncode

    def _stop_server(self, pg_ctl: Path, data: Path, *, mode: str) -> None:
        if self._server_state(pg_ctl, data) == _PG_CTL_RUNNING:
            self._step(f"{pg_ctl} stop", [str(pg_ctl), "-D", str(data), "-m", mode, "stop"], timeout=_PG_CTL_WAIT_TIMEOUT_S)

    def _is_down(self, pg_ctl: Path, data: Path) -> bool:
        """True only when ``pg_ctl status`` confirms no server runs there; unknown counts as not down."""
        try:
            return self._server_state(pg_ctl, data) in _PG_CTL_DOWN
        except _StepFailed:
            return False

    def _upgrade_argv(self, old_data: Path, new_data: Path, *, check: bool) -> list[str]:
        p = self.plan
        argv = [str(p.pg_upgrade), "-b", str(p.old_bin), "-B", str(p.new_bins.bin_dir),
                "-d", str(old_data), "-D", str(new_data), "-U", p.os_user]
        return [*argv, "--check"] if check else argv

    def _initdb(self, new_data: Path, workdir: Path) -> None:
        self._step(
            "initdb",
            _initdb_argv(self.plan.new_bins, new_data, self.plan.os_user, auth="trust"),
            timeout=_INITDB_TIMEOUT_S, cwd=workdir,
        )

    # ── modes ──────────────────────────────────────────────────────────────

    def dry_run(self) -> None:
        for line in plan_lines(self.plan):
            self.say(line)

    def check_only(self) -> None:
        """initdb into a temporary directory and ``pg_upgrade --check`` against it; the temporary directory goes."""
        self._lock = self._hold_lock(self.plan)
        try:
            self._check_only_locked()
        finally:
            self._release_lock()

    def _release_lock(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def _require_no_server(self) -> None:
        """Refuse (:class:`PgUpgradeRefused`) unless ``pg_ctl status`` says no server runs on the data directory."""
        p = self.plan
        old_ctl = p.old_bin / "pg_ctl"
        try:
            state = self._server_state(old_ctl, p.pgdata)
        except _StepFailed as exc:
            raise PgUpgradeRefused(f"{exc}: cannot tell whether a server runs on {p.pgdata}. Nothing was changed.") from None
        if state == _PG_CTL_RUNNING:
            raise PgUpgradeRefused(
                f"a server is running on {p.pgdata}: stop it with `{old_ctl} -D {p.pgdata} -m fast stop` "
                "(and its service manager, such as brew services or systemd, so it does not restart), then run again. "
                "Nothing was changed."
            )
        if state != _PG_CTL_NOT_RUNNING:
            raise PgUpgradeRefused(
                f"`{old_ctl} -D {p.pgdata} status` exited {state}: cannot tell whether a server runs there. Nothing was changed."
            )

    def _check_only_locked(self) -> None:
        p = self.plan
        self._require_no_server()
        with _interrupts_raise():
            with _interrupts_held():  # no window between the directory existing and workdir naming it
                try:
                    workdir = _short_tempdir()
                except OSError as exc:
                    raise PgUpgradeRefused(f"could not make a temporary directory under {SHORT_TMP_ROOT} ({exc.strerror})") from None
            # True from just before pg_upgrade --check runs, when no server's pid file was there: only then did
            # that step start the old server, so only then is a server running afterwards ours to stop.
            check_began = False
            try:
                new_data = workdir / "data"
                try:
                    self._initdb(new_data, workdir)
                    self.say("initdb: a temporary cluster was made; running pg_upgrade --check against it")
                    check_began = not (p.pgdata / "postmaster.pid").exists()
                    self._step("pg_upgrade --check", self._upgrade_argv(p.pgdata, new_data, check=True),
                               timeout=_PG_UPGRADE_TIMEOUT_S, cwd=workdir, secret=True)
                except _StepFailed as exc:
                    with _interrupts_held():
                        left = self._stop_check_server(check_began)
                        raise PgUpgradeFailed(
                            f"{exc}\n{p.pgdata} was not modified (pg_upgrade --check starts and stops the old server on it, "
                            f"and changes nothing). If it reports an encoding or locale mismatch, the old "
                            f"cluster was made with other options; see {_RUNBOOK}." + (f"\n{left}" if left else "")
                        ) from None
                except BaseException as exc:  # an interrupt, or a bug: pg_upgrade --check may have left the old server up
                    with _interrupts_held():
                        left = self._stop_check_server(check_began)
                        if left:
                            self.say(left)
                        if isinstance(exc, Exception):
                            raise
                        self.say(f"interrupted ({exc}). {p.pgdata} was not modified; the temporary directory is removed.")
                        raise
            finally:
                with _interrupts_held():
                    shutil.rmtree(workdir, ignore_errors=True)
        self.say(f"pg_upgrade --check passed. The temporary cluster is removed and {p.pgdata} was not modified.")

    def _stop_check_server(self, check_began: bool) -> str | None:
        """After a ``--check-only`` that did not finish cleanly: stop the old server ``pg_upgrade --check`` started.

        *check_began* says that no server's pid file was there when ``pg_upgrade --check`` began, so a server up
        now is the one it started. When it is False (the check never began, or a server was already there), a
        server that runs now belongs to someone else (brew services, systemd, a person): it is left alone and the
        returned note says so. Stopped with the OLD ``pg_ctl``, ``fast`` and bounded; signals are ignored
        meanwhile. Returns a warning naming the command to run when a server of ours cannot be confirmed down.
        """
        p = self.plan
        old_ctl = p.old_bin / "pg_ctl"
        with _interrupts_held():
            if not check_began:
                try:
                    running = self._server_state(old_ctl, p.pgdata) == _PG_CTL_RUNNING
                except _StepFailed:
                    return None
                if running:
                    return (f"NOTE: a server is running on {p.pgdata} that this check did not start (a service manager "
                            f"such as brew services or systemd, or a person, started it): it was left running.")
                return None
            with contextlib.suppress(Exception):
                self._stop_server(old_ctl, p.pgdata, mode="fast")
            if self._is_down(old_ctl, p.pgdata):
                return None
        return (f"WARNING: a server may still be running on {p.pgdata} (pg_upgrade --check started it and it could "
                f"not be confirmed stopped): stop it with `{old_ctl} -D {p.pgdata} -m fast stop`. "
                f"It is still running, or `pg_ctl status` could not say.")

    def apply(self) -> None:
        p = self.plan
        if p.port is None:
            raise PgUpgradeRefused(f"{CREDENTIALS_FILENAME} has no PG_PORT: the follow-up steps could not reach the new cluster")
        with _interrupts_raise():
            self._lock = self._hold_lock(p)
            try:
                with _interrupts_held():  # no window between the directory existing and workdir naming it
                    try:
                        workdir = _short_tempdir()
                    except OSError as exc:
                        raise PgUpgradeRefused(
                            f"could not make a work directory under {SHORT_TMP_ROOT} ({exc.strerror}): nothing was changed"
                        ) from None
                self._apply(workdir)
            finally:
                self._release_lock()

    def _apply(self, workdir: Path) -> None:
        p = self.plan
        self.say(f"NOTE: this command is {UNTESTED_NOTE} ({p.pgdata}).")
        self.say(f"NOTE: {LOCK_NOTE}.")
        try:
            self._check_idle_and_move()
            self._upgrade(workdir)
        except BaseException as exc:
            # The rename may have landed with the flag not yet set (an interrupt in between): the directory
            # layout says so too. A refusal has the original in place and a pre-existing .old, so it cannot match.
            moved = self._moved or (not os.path.lexists(p.pgdata) and os.path.lexists(p.pgdata_old))
            if not moved:
                with _interrupts_held():
                    shutil.rmtree(workdir, ignore_errors=True)
                if isinstance(exc, (PgUpgradeRefused, PgUpgradeFailed)):
                    raise
                if isinstance(exc, _StepFailed):
                    raise PgUpgradeRefused(f"{exc}\n{_NOT_MOVED}") from None
                if isinstance(exc, Exception):
                    self._log_unexpected(exc)
                    raise PgUpgradeRefused(f"the upgrade stopped on {type(exc).__name__}\n{_NOT_MOVED}") from None
                self.say(f"interrupted ({exc}). {_NOT_MOVED}")
                raise
            outcome = self._rollback(workdir)
            if isinstance(exc, _StepFailed):
                raise PgUpgradeFailed(f"{exc}\n{outcome}") from None
            if isinstance(exc, Exception):
                self._log_unexpected(exc)
                raise PgUpgradeFailed(f"the upgrade stopped on {type(exc).__name__}\n{outcome}") from None
            self.say(f"interrupted ({exc}). {outcome}")
            raise
        try:
            problems = self._finish(workdir)
        except Exception as exc:  # noqa: BLE001 — pg_upgrade succeeded: whatever else breaks, the upgrade stands
            self._log_unexpected(exc)
            problems = [f"the follow-up steps stopped on {type(exc).__name__}"]
        except BaseException as exc:
            self.say(f"interrupted ({exc}). {self._in_place_text(workdir)}\n"
                     "Start the service (`nx daemon service start`) and run vacuumdb --all --analyze-in-stages by hand.")
            raise
        if problems:
            raise PgUpgradeFailed(
                f"{self._in_place_text(workdir)}\nA follow-up step failed:\n" + "\n".join(problems)
            )
        shutil.rmtree(workdir, ignore_errors=True)
        self.say("")
        self.say(f"Upgraded {p.pgdata} to the bundle's PostgreSQL.")
        self.say("Verify with `nx doctor`: 'Local PostgreSQL version' passes and "
                 "'Local PostgreSQL authentication' shows scram-sha-256.")
        self.say(f"The pre-upgrade cluster is kept at {p.pgdata_old}. Once the service has run and your data reads "
                 f"back, remove it by hand: rm -rf {p.pgdata_old}")

    # ── apply internals ────────────────────────────────────────────────────

    def _log_unexpected(self, exc: BaseException) -> None:
        """A bug in this code reaches the user as a type name; the traceback goes to the log."""
        _log.error("pg_upgrade_unexpected_exception", error=type(exc).__name__, exc_info=exc)

    def _in_place_text(self, workdir: Path) -> str:
        p = self.plan
        return (
            f"pg_upgrade succeeded and the upgraded cluster is in place at {p.pgdata}. "
            f"The pre-upgrade cluster is kept at {p.pgdata_old}. Work directory kept: {workdir}\n{_WORKDIR_HASH_NOTE}"
        )

    def _check_idle_and_move(self) -> None:
        """Refuse while the service or the old server runs, then move the data directory aside. Stops nothing.

        Raises before the move on any problem. The service is looked for the way ``nx daemon service stop`` finds it.
        """
        p = self.plan
        running = self._find_service(p)
        if running:
            raise PgUpgradeRefused(
                f"the storage service is running ({'; '.join(running)}): stop it with "
                f"`nx daemon service stop --config-dir {p.config_dir}`, then run this again. Nothing was changed."
            )
        self._require_no_server()
        if os.path.lexists(p.pgdata_old):
            raise PgUpgradeRefused(f"{_old_dir_text(p.pgdata, p.pgdata_old)}\n{_NOT_MOVED}")
        try:
            os.rename(p.pgdata, p.pgdata_old)
        except OSError as exc:
            raise PgUpgradeFailed(f"could not move {p.pgdata} to {p.pgdata_old} ({exc.strerror}). {_NOT_MOVED}") from None
        self._moved = True
        self.say(f"moved {p.pgdata} to {p.pgdata_old}")

    def _upgrade(self, workdir: Path) -> None:
        p = self.plan
        self.say("creating the new cluster (initdb, --auth=trust for the upgrade)")
        self._initdb(p.pgdata, workdir)
        self.say("pg_upgrade --check")
        self._step("pg_upgrade --check", self._upgrade_argv(p.pgdata_old, p.pgdata, check=True),
                   timeout=_PG_UPGRADE_TIMEOUT_S, cwd=workdir, secret=True)
        self.say("pg_upgrade (this takes a while on a large cluster)")
        self._step("pg_upgrade", self._upgrade_argv(p.pgdata_old, p.pgdata, check=False),
                   timeout=_PG_UPGRADE_TIMEOUT_S, cwd=workdir, secret=True)

    def _rollback(self, workdir: Path) -> str:
        """Remove the new data directory and move the original back; return what happened, in a sentence or two.

        Signals are ignored for the duration: a rollback cut in half is worse than one that finishes.
        """
        with _interrupts_held():
            return self._rollback_held(workdir)

    def _rollback_held(self, workdir: Path) -> str:
        p = self.plan
        self.say("failed: rolling back")
        # A failed pg_upgrade can leave a server it started. Stop it before the directory goes: the new one with
        # `immediate` (it is discarded), the ORIGINAL with `fast` (`immediate` leaves it needing crash recovery).
        running: list[tuple[Path, Path]] = []
        for pg_ctl, data, mode in ((p.new_bins.pg_ctl, p.pgdata, "immediate"), (p.old_bin / "pg_ctl", p.pgdata_old, "fast")):
            with contextlib.suppress(Exception):
                self._stop_server(pg_ctl, data, mode=mode)
            if not self._is_down(pg_ctl, data):
                running.append((pg_ctl, data))
        # pg_upgrade keeps its logs inside the new directory (PostgreSQL 15 and later); save them first.
        logs = p.pgdata / "pg_upgrade_output.d"
        with contextlib.suppress(OSError, shutil.Error):
            if logs.is_dir():
                shutil.copytree(logs, workdir / "pg_upgrade_output.d")
        kept = f" The work directory with pg_upgrade's logs is kept: {workdir}\n{_WORKDIR_HASH_NOTE}"
        if running:
            stops = "; ".join(f"`{ctl} -D {data} -m fast stop`" for ctl, data in running)
            where = " and ".join(str(data) for _, data in running)
            return (
                f"ROLLBACK INCOMPLETE: a server may still be running on {where} (it did not stop, or `pg_ctl status` "
                f"could not confirm it is down), so nothing was removed or moved back. Your original cluster is at "
                f"{p.pgdata_old}. Stop the server ({stops}), remove the new {p.pgdata}, then "
                f"`mv {p.pgdata_old} {p.pgdata}`.{kept}"
            )
        try:
            if os.path.lexists(p.pgdata):
                shutil.rmtree(p.pgdata)
        except OSError as exc:
            return (
                f"ROLLBACK INCOMPLETE: could not remove the new {p.pgdata} ({exc.strerror}). Your original cluster is "
                f"intact at {p.pgdata_old}. Remove {p.pgdata} by hand, then `mv {p.pgdata_old} {p.pgdata}`.{kept}"
            )
        try:
            os.rename(p.pgdata_old, p.pgdata)
        except OSError as exc:
            return (
                f"ROLLBACK INCOMPLETE: the new directory is removed but {p.pgdata_old} could not be moved back "
                f"({exc.strerror}). Your original cluster is intact there: `mv {p.pgdata_old} {p.pgdata}`.{kept}"
            )
        return (
            f"Rolled back: the new directory was removed and the original cluster is back at {p.pgdata}, intact (an interrupted old server needs crash recovery on its next start; its files were not changed). "
            f"The service is stopped and cannot start a PostgreSQL below 17; fix the cause and run this again.{kept}"
        )

    def _find_update_extensions(self, workdir: Path) -> Path | None:
        """pg_upgrade writes ``update_extensions.sql`` into its cwd; PostgreSQL 15 and later may keep it with its logs."""
        direct = workdir / "update_extensions.sql"
        if direct.is_file():
            return direct
        logs = self.plan.pgdata / "pg_upgrade_output.d"
        with contextlib.suppress(OSError):
            for found in sorted(logs.rglob("update_extensions.sql")):
                if found.is_file():
                    return found
        return None

    def _finish(self, workdir: Path) -> list[str]:
        """Start the service and run pg_upgrade's follow-ups; return the problems (empty on success)."""
        p = self.plan
        # The cluster is whole now. Free the lock before the service starts: `nx daemon service start` spawns a
        # supervisor that takes it, and would wait on this process (60 s, then fail) if we still held it.
        self._release_lock()
        problems: list[str] = []
        host = ["-h", "127.0.0.1", "-p", str(p.port), "-U", p.os_user]
        update = self._find_update_extensions(workdir)
        psql_argv = [str(p.new_bins.psql), "-w", *host, "-d", "postgres", "-f", str(update)] if update else None
        vacuum_argv = [str(p.vacuumdb), "-w", *host, "--all", "--analyze-in-stages"]
        # The password lives only in each child's env below; the service starts from a clean one.
        self.say("starting the storage service")
        if self._service("start") != 0:
            by_hand = [shlex.join(a) for a in (psql_argv, vacuum_argv) if a]
            problems.append(
                "`nx daemon service start` failed: start it, then run these by hand, with PGPASSWORD set to the "
                "superuser password from pg_credentials (set it without printing it):\n  " + "\n  ".join(by_hand)
            )
        if psql_argv:
            self.say(f"pg_upgrade wrote {update}: running it")
            try:
                self._step("psql update_extensions.sql", psql_argv, timeout=_PSQL_FOLLOWUP_TIMEOUT_S, cwd=workdir, secret=True)
            except _StepFailed as exc:
                problems.append(str(exc))
        else:
            self.say(f"pg_upgrade wrote no update_extensions.sql (looked in {workdir} and {p.pgdata}/pg_upgrade_output.d): "
                     "if a later query fails on an extension function, run ALTER EXTENSION <name> UPDATE in that database")
        self.say("vacuumdb --all --analyze-in-stages")
        try:
            self._step("vacuumdb", vacuum_argv, timeout=_VACUUMDB_TIMEOUT_S, cwd=workdir, secret=True)
        except _StepFailed as exc:
            problems.append(str(exc))
        return problems
