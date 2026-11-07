# SPDX-License-Identifier: AGPL-3.0-or-later
"""Upgrade a local nx cluster made by PostgreSQL below 17 with the bundle's ``pg_upgrade`` (nexus-j738q).

The design of record is the runbook section "A local cluster older than PostgreSQL 17"
(``docs/migration-runbook.md``); this module is that script with its failure paths made code:

* the old server is stopped with the OLD ``pg_ctl`` and checked to be down;
* the new cluster is made with :func:`nexus.db.pg_provision._initdb_argv`, the options ``nx`` creates its own
  clusters with, plus ``--auth=trust`` for the upgrade (the next service start converts it to scram-sha-256);
* the superuser password reaches ``pg_upgrade``, ``psql`` and ``vacuumdb`` as ``PGPASSWORD`` in the CHILD's
  environment only: never argv, never output, never a log, and never the service's environment;
* any failure after the data directory is moved aside removes the new directory and moves the original back.

Three modes, in increasing reach: :meth:`ClusterUpgrade.dry_run` changes nothing and prints the plan;
:meth:`ClusterUpgrade.check_only` runs ``initdb`` into a temporary directory (never the real data directory)
and ``pg_upgrade --check`` against it; :meth:`ClusterUpgrade.apply` does the upgrade.

The service is stopped and started through an injected callable (``nx daemon service stop|start``), so the
module never reaches a real service from a test.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from nexus.db.pg_provision import (
    _INITDB_TIMEOUT_S,
    _PG_CTL_WAIT_TIMEOUT_S,
    CREDENTIALS_FILENAME,
    SUPERUSER_PASS_KEY,
    PgBinaries,
    PgBinaryNotFoundError,
    _initdb_argv,
    _read_credentials,
    _run,
    bootstrap_superuser,
    discover_pg_binaries,
)

#: Backstops, not budgets: a large cluster's ``pg_upgrade`` and ``vacuumdb --analyze-in-stages`` take hours.
_PG_UPGRADE_TIMEOUT_S: float = 6 * 3600.0
_VACUUMDB_TIMEOUT_S: float = 6 * 3600.0
_PSQL_FOLLOWUP_TIMEOUT_S: float = 3600.0
_PG_CTL_STATUS_TIMEOUT_S: float = 60.0

#: ``pg_ctl status`` exits 0 for a running server and 3 for none.
_PG_CTL_RUNNING = 0
_PG_CTL_NOT_RUNNING = 3

_RUNBOOK = (
    "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md "
    "(section 'A local cluster older than PostgreSQL 17')"
)

#: ``verb`` is ``"stop"`` or ``"start"``; the callable runs ``nx daemon service <verb>`` and returns its exit code.
ServiceControl = Callable[[str, Path], int]


class PgUpgradeRefused(Exception):
    """The upgrade was refused before anything changed."""


class PgUpgradeFailed(Exception):
    """The upgrade failed after it began; the message says what state the cluster is in."""


class _StepFailed(Exception):
    """One external step failed (internal; carries the scrubbed tail of its output)."""


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


def build_plan(config_dir: Path, old_bin: Path, *, require_no_old_dir: bool = True) -> UpgradePlan:
    """Validate everything that can be validated without changing anything; raise :class:`PgUpgradeRefused`.

    ``require_no_old_dir`` is False for ``--check-only``, which moves nothing and so does not care.
    """
    if sys.platform == "win32":
        raise PgUpgradeRefused(f"this upgrade is for Linux and macOS; see {_RUNBOOK}")
    from nexus.health import PG_VERSION_FLOOR  # noqa: PLC0415 — deferred, heavy module; this command only

    creds_path = config_dir / CREDENTIALS_FILENAME
    try:
        creds = _read_credentials(creds_path)
    except (OSError, ValueError) as exc:
        raise PgUpgradeRefused(
            f"no readable {CREDENTIALS_FILENAME} in {config_dir} ({type(exc).__name__}): "
            "there is no nx-managed local cluster here to upgrade"
        ) from None
    pgdata = Path(creds.get("PG_DATA") or (config_dir / "postgres"))
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
        new_bins = discover_pg_binaries()
    except PgBinaryNotFoundError as exc:
        raise PgUpgradeRefused(f"the bundled PostgreSQL {PG_VERSION_FLOOR} is not available: {exc}") from None
    for name in ("pg_upgrade", "vacuumdb"):
        if not (new_bins.bin_dir / name).is_file():
            raise PgUpgradeRefused(f"the bundle at {new_bins.bin_dir} has no {name}")

    pgdata_old = pgdata.with_name(pgdata.name + ".old")
    if require_no_old_dir and os.path.lexists(pgdata_old):
        raise PgUpgradeRefused(_old_dir_text(pgdata, pgdata_old))

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
        f"{pgdata_old} exists and is the pre-upgrade cluster: keep it, nothing was changed. "
        "If the service never ran on the new cluster (an earlier attempt failed), back out: remove the "
        f"new {pgdata}, then `mv {pgdata_old} {pgdata}`, and run this again. If the service has run on the "
        f"new cluster, the upgrade already succeeded: keep both and do not run this again. See {_RUNBOOK}."
    )


def plan_lines(plan: UpgradePlan) -> list[str]:
    """The steps ``--apply`` would take, in order, as printable lines."""
    old_ctl, new = plan.old_bin / "pg_ctl", plan.new_bins.bin_dir
    return [
        f"cluster      {plan.pgdata} (PostgreSQL {plan.cluster_major}, superuser {plan.os_user})",
        f"old bin      {plan.old_bin}",
        f"new bin      {new} (the bundle)",
        f"kept copy    {plan.pgdata_old}",
        "",
        "--apply would:",
        "  1. nx daemon service stop",
        f"  2. {old_ctl} -D {plan.pgdata} -m fast stop, then check with status that no server runs",
        f"  3. refuse if {plan.pgdata_old} exists; else mv {plan.pgdata} {plan.pgdata_old}",
        "  4. from a short temporary directory: initdb into a new data directory with the options nx creates "
        "its own clusters with, --auth=trust",
        "  5. pg_upgrade --check, then the real pg_upgrade (the password, "
        + ("from pg_credentials, " if plan.password else "none recorded, ")
        + "goes in the child's environment only)",
        f"     any failure from step 3 on: remove the new {plan.pgdata}, mv {plan.pgdata_old} back",
        "  6. nx daemon service start",
        "  7. psql -f update_extensions.sql (when pg_upgrade wrote one), then vacuumdb --all --analyze-in-stages, "
        "both over TCP",
        f"  8. keep {plan.pgdata_old}; say how to remove it once the service has run",
        "",
        "--check-only runs initdb into a temporary directory and pg_upgrade --check against it; the real "
        "data directory is only read, as pg_upgrade --check reads it.",
        "Nothing was changed. Re-run with --check-only to test it, or --apply to upgrade.",
    ]


#: Where the work directories go. Short on purpose: pg_upgrade puts its sockets in the cwd, and macOS caps a
#: socket path at 103 bytes, which the default per-user temp directory there would exceed.
SHORT_TMP_ROOT: Path = Path("/tmp")


def _short_tempdir() -> Path:
    """A fresh private directory under :data:`SHORT_TMP_ROOT`."""
    return Path(tempfile.mkdtemp(prefix="pgup.", dir=SHORT_TMP_ROOT if SHORT_TMP_ROOT.is_dir() else None))


class ClusterUpgrade:
    """Runs one plan. *say* prints a line; *service* stops and starts the storage service."""

    def __init__(self, plan: UpgradePlan, *, say: Callable[[str], None], service: ServiceControl) -> None:
        self.plan = plan
        self.say = say
        self.service = service

    # ── helpers ────────────────────────────────────────────────────────────

    def _env(self, *, secret: bool) -> dict[str, str]:
        return _child_env(self.plan.password if secret else None)

    def _scrub(self, text: str) -> str:
        pw = self.plan.password
        return text.replace(pw, "***") if pw else text

    def _tail(self, done: subprocess.CompletedProcess, lines: int = 25) -> str:
        text = self._scrub(f"{done.stdout or ''}{done.stderr or ''}").strip()
        return "\n".join(text.splitlines()[-lines:])

    def _step(self, what: str, argv: list[str], *, timeout: float, cwd: Path | None = None, secret: bool = False) -> subprocess.CompletedProcess:
        """Run one command; raise :class:`_StepFailed` (with scrubbed output) on a nonzero exit, timeout or spawn failure."""
        try:
            done = _run(argv, check=False, timeout=timeout, cwd=cwd, env=self._env(secret=secret))
        except subprocess.TimeoutExpired:
            raise _StepFailed(f"{what} did not finish within {timeout:.0f} s") from None
        except OSError as exc:
            raise _StepFailed(f"{what} could not run: {type(exc).__name__}") from None
        if done.returncode != 0:
            raise _StepFailed(f"{what} failed (exit {done.returncode})\n{self._tail(done)}".rstrip())
        return done

    def _server_state(self, pg_ctl: Path, data: Path) -> int:
        done = _run([str(pg_ctl), "-D", str(data), "status"], check=False, timeout=_PG_CTL_STATUS_TIMEOUT_S,
                    env=self._env(secret=False))
        return done.returncode

    def _stop_server(self, pg_ctl: Path, data: Path, *, mode: str) -> None:
        if self._server_state(pg_ctl, data) == _PG_CTL_RUNNING:
            self._step(f"{pg_ctl} stop", [str(pg_ctl), "-D", str(data), "-m", mode, "stop"], timeout=_PG_CTL_WAIT_TIMEOUT_S)

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
        p = self.plan
        old_ctl = p.old_bin / "pg_ctl"
        state = self._server_state(old_ctl, p.pgdata)
        if state == _PG_CTL_RUNNING:
            raise PgUpgradeRefused(
                f"a server is running on {p.pgdata}: stop it with `{old_ctl} -D {p.pgdata} -m fast stop` "
                "(and its service manager, such as brew services or systemd, so it does not restart), then run again"
            )
        if state != _PG_CTL_NOT_RUNNING:
            raise PgUpgradeRefused(f"`{old_ctl} -D {p.pgdata} status` exited {state}: cannot tell whether a server runs there")
        workdir = _short_tempdir()
        try:
            new_data = workdir / "data"
            try:
                self._initdb(new_data, workdir)
                self.say("initdb: a temporary cluster was made; running pg_upgrade --check against it")
                self._step("pg_upgrade --check", self._upgrade_argv(p.pgdata, new_data, check=True),
                           timeout=_PG_UPGRADE_TIMEOUT_S, cwd=workdir, secret=True)
            except _StepFailed as exc:
                raise PgUpgradeFailed(
                    f"{exc}\nNothing was changed in {p.pgdata}. If it reports an encoding or locale mismatch, the old "
                    f"cluster was made with other options; see {_RUNBOOK}."
                ) from None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        self.say(f"pg_upgrade --check passed. The temporary cluster is removed and {p.pgdata} was not changed.")

    def apply(self) -> None:
        p = self.plan
        old_ctl = p.old_bin / "pg_ctl"
        if p.port is None:
            raise PgUpgradeRefused(f"{CREDENTIALS_FILENAME} has no PG_PORT: the follow-up steps could not reach the new cluster")

        self.say("stopping the storage service")
        if self.service("stop", p.config_dir) != 0:
            raise PgUpgradeRefused("`nx daemon service stop` failed: nothing was changed")
        self.say(f"stopping the old server with {old_ctl}")
        try:
            self._stop_server(old_ctl, p.pgdata, mode="fast")
        except _StepFailed as exc:
            raise PgUpgradeRefused(f"{exc}\nNothing was changed.") from None
        if self._server_state(old_ctl, p.pgdata) != _PG_CTL_NOT_RUNNING:
            raise PgUpgradeRefused(
                f"a server still runs on {p.pgdata} (a service manager such as brew services or systemd may restart "
                "it: stop it there). Nothing was changed."
            )
        if os.path.lexists(p.pgdata_old):
            raise PgUpgradeRefused(_old_dir_text(p.pgdata, p.pgdata_old))

        try:
            os.rename(p.pgdata, p.pgdata_old)
        except OSError as exc:
            raise PgUpgradeFailed(f"could not move {p.pgdata} to {p.pgdata_old} ({exc.strerror}): nothing was changed") from None
        self.say(f"moved {p.pgdata} to {p.pgdata_old}")

        workdir = _short_tempdir()
        try:
            self._upgrade(workdir)
        except BaseException as exc:
            outcome = self._rollback(workdir)
            if isinstance(exc, _StepFailed):
                raise PgUpgradeFailed(f"{exc}\n{outcome}") from None
            if isinstance(exc, Exception):
                raise PgUpgradeFailed(f"the upgrade stopped on {type(exc).__name__}\n{outcome}") from None
            self.say(outcome)
            raise
        problems = self._finish(workdir)
        if problems:
            raise PgUpgradeFailed(
                "pg_upgrade succeeded and the upgraded cluster is in place at "
                f"{p.pgdata}, but a follow-up step failed:\n" + "\n".join(problems) +
                f"\nThe pre-upgrade cluster is kept at {p.pgdata_old}. Work directory kept: {workdir}"
            )
        shutil.rmtree(workdir, ignore_errors=True)
        self.say("")
        self.say(f"Upgraded {p.pgdata} to the bundle's PostgreSQL.")
        self.say("Verify with `nx doctor`: 'Local PostgreSQL version' passes and "
                 "'Local PostgreSQL authentication' shows scram-sha-256.")
        self.say(f"The pre-upgrade cluster is kept at {p.pgdata_old}. Once the service has run and your data reads "
                 f"back, remove it by hand: rm -rf {p.pgdata_old}")

    # ── apply internals ────────────────────────────────────────────────────

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
        """Remove the new data directory and move the original back; return what happened, in a sentence or two."""
        p = self.plan
        self.say("failed: rolling back")
        # A failed pg_upgrade can leave a server it started; stop it before the directory goes.
        for pg_ctl, data in ((p.new_bins.pg_ctl, p.pgdata), (p.old_bin / "pg_ctl", p.pgdata_old)):
            with contextlib.suppress(Exception):
                self._stop_server(pg_ctl, data, mode="immediate")
        # pg_upgrade keeps its logs inside the new directory (PostgreSQL 15 and later); save them first.
        logs = p.pgdata / "pg_upgrade_output.d"
        with contextlib.suppress(OSError, shutil.Error):
            if logs.is_dir():
                shutil.copytree(logs, workdir / "pg_upgrade_output.d")
        kept = f" The work directory with pg_upgrade's logs is kept: {workdir}"
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
            f"Rolled back: the new directory was removed and the original cluster is back at {p.pgdata}, as it was. "
            f"The service is stopped and cannot start a PostgreSQL below 17; fix the cause and run this again.{kept}"
        )

    def _finish(self, workdir: Path) -> list[str]:
        """Start the service and run pg_upgrade's follow-ups; return the problems (empty on success)."""
        p = self.plan
        problems: list[str] = []
        # The password lives only in each child's env below; the service starts from a clean one.
        self.say("starting the storage service")
        if self.service("start", p.config_dir) != 0:
            problems.append("`nx daemon service start` failed: start it, then run the two follow-ups below by hand")
        host = ["-h", "127.0.0.1", "-p", str(p.port), "-U", p.os_user]
        if (workdir / "update_extensions.sql").is_file():
            self.say("update_extensions.sql")
            try:
                self._step("psql update_extensions.sql",
                           [str(p.new_bins.psql), "-w", *host, "-d", "postgres", "-f", "update_extensions.sql"],
                           timeout=_PSQL_FOLLOWUP_TIMEOUT_S, cwd=workdir, secret=True)
            except _StepFailed as exc:
                problems.append(str(exc))
        self.say("vacuumdb --all --analyze-in-stages")
        try:
            self._step("vacuumdb", [str(p.vacuumdb), "-w", *host, "--all", "--analyze-in-stages"],
                       timeout=_VACUUMDB_TIMEOUT_S, cwd=workdir, secret=True)
        except _StepFailed as exc:
            problems.append(str(exc))
        return problems
