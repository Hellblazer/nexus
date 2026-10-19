# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pytest basetemp and ``TMPDIR`` on tmpfs inside a lab-run grant (nexus-kyujr).

One CI-shaped Python shard wrote 442 MiB to chas's NVMe and 227 MB of that was
pytest's basetemp under ``~/tmp`` (ext4), which pytest then keeps for the last three
runs. Nothing in a test's scratch tree has to survive the run, so inside a grant the
run's scratch goes on ``/dev/shm``, charged to the grant, and is deleted when the
run ends. The same shape as the cluster root (``tests/_engine_substrate.py``,
nexus-me3gm / vo9qn): decided once per run by the pytest controller, inherited by
xdist workers through the environment, logged, and fail-safe to disk.

What moves: ``TMPDIR`` (so ``tempfile`` and every child process follow) and
``PYTEST_DEBUG_TEMPROOT`` (the root pytest puts ``pytest-of-<user>/pytest-<n>``
under, which is what ``tmp_path`` and ``tmp_path_factory`` use). What does not: the
engine JVM's ``java.io.tmpdir`` (the JDK ignores ``TMPDIR`` on Linux and uses
``/tmp``; it holds hsperfdata and a one-file uid probe) and ONNX Runtime's native
cache (``NX_ORT_NATIVE_DIR``, pinned to the real home so it is extracted once, not
per run).

Layout, all owned by the running user::

    /dev/shm/nexus-tmp-<uid>/            0700, per-uid, a sibling of nexus-test-pg-<uid>
        r<8 random>/                     one per run; the whole run is deleted at its end
            owner.json                   {"pid", "starttime"} of the controller
            t/                           TMPDIR
            p/                           PYTEST_DEBUG_TEMPROOT

Gates (any failing one leaves the run exactly as before, with the reason in the
summary line, and a warning where a grant expected tmpfs): ``LAB_CPUS`` set (every
lab-run grant exports it; a CI job on the pool has it too), Linux, ``/dev/shm`` a
writable tmpfs not mounted ``noexec`` (tests write and run scripts), 3 GiB free
there, 3 GiB of cgroup memory headroom (the same floors and credited-page-cache
figure the cluster root uses), and one real script executed from the new directory.

Overrides: ``NX_TEST_TMP_ROOT=default`` keeps the old behaviour; ``=<dir>`` uses that
directory as the root (its ``t`` and ``p`` subdirectories; kept after the run, for
inspecting a failure, and swept by nobody). ``NX_TEST_PG_ROOT=default`` also keeps the
tempdir, because that setting says clusters belong in it.

Two plugins (the DDL counter and the storage-touch guard) write their ``nx-*``
reports into the controller's basetemp by default, after the session finishes. The
run dir therefore goes in ``pytest_unconfigure`` (after the terminal summary), and
just before it the controller basetemp's top-level ``nx-*`` files are copied to
``<original tempdir>/nexus-test-reports-<uid>/<run>`` (newest three kept, like
pytest's basetemp retention). A run that sets ``NX_DDL_REPORT_DIR`` or
``NX_STORAGE_TOUCH_REPORT_DIR`` keeps writing where it said.

A run that is killed leaves its ``r*`` directory behind. The next run in a grant
removes every directory under the same per-uid root whose controller is gone (pid
dead, or the pid's start time differs), first handing its ``p`` tree to the daemon
reaper so test daemons stranded under it are signalled (the same scoped reaper the
start-of-run drain uses); a directory with no owner record is removed once it is
10 minutes old. Nothing here signals by pattern and nothing touches a directory the
running user does not own.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import warnings
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import structlog

from tests import _engine_substrate as sub

_log = structlog.get_logger(__name__)

#: ``default`` keeps the old tempdir; any other value is a root directory.
TMP_ROOT_ENV = "NX_TEST_TMP_ROOT"
#: Run-wide decision, inherited by xdist workers and nested pytest: ``tmpfs=<run dir>``,
#: ``forced=<root>`` or ``disk=<reason>``.
SESSION_ENV = "NX_TEST_TMP_SESSION"
#: Test seam: a directory standing in for ``/dev/shm`` that is treated as an
#: exec-capable tmpfs on any platform, so the whole mechanism runs on a laptop.
SHM_DIR_SEAM_ENV = "NX_TEST_TMP_SHM_DIR"
ORIG_ENV = sub._TMP_ORIG_ENV

_GRANT_ENV = "LAB_CPUS"
_ROOT_PREFIX = "nexus-tmp"
_RUN_PREFIX = "r"
_OWNER_FILE = "owner.json"
#: A run dir with no owner record (killed between mkdtemp and the record write)
#: is reaped once this old.
BARE_MIN_AGE_S = 600.0
#: Runs of ``nx-*`` report files kept on disk (as pytest keeps three basetemps).
REPORT_KEEP = 3
_REPORT_DIR_PREFIX = "nexus-test-reports"
#: The cluster root's floors, reused so the two decisions cannot disagree.
MIN_FREE_BYTES = sub._TMPFS_MIN_FREE_BYTES


@dataclass(frozen=True)
class TmpDecision:
    """``root`` is the directory a run dir goes under (None: leave the tempdir
    alone); ``reason`` says why it was not tmpfs (None when it is, or when forced)."""

    root: Path | None
    reason: str | None = None
    forced: bool = False
    #: A grant on Linux that should have got tmpfs and did not: worth a warning.
    expected_tmpfs: bool = False


def _mount_options(mounts_text: str, mountpoint: str) -> set[str] | None:
    """The option set of the last ``/proc/mounts`` entry for *mountpoint*."""
    found: set[str] | None = None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == mountpoint:
            found = set(fields[3].split(","))
    return found


def decide_tmp_root(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    mounts_text: str | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    writable: Callable[[Path], bool] | None = None,
    cgroup_headroom: Callable[[], int | None] | None = None,
    uid: int | str | None = None,
) -> TmpDecision:
    """Pure decision (creates and warns about nothing); every input injectable."""
    env = os.environ if env is None else env
    override = (env.get(TMP_ROOT_ENV) or "").strip()
    if override == "default":
        return TmpDecision(None, f"{TMP_ROOT_ENV}=default")
    if override:
        return TmpDecision(sub._abs(override), forced=True)
    if (env.get(sub._PG_ROOT_ENV) or "").strip() == "default":
        return TmpDecision(None, f"{sub._PG_ROOT_ENV}=default (clusters use the tempdir)")
    if not (env.get(_GRANT_ENV) or "").strip():
        return TmpDecision(None, f"not inside a lab-run grant ({_GRANT_ENV} unset)")
    seam = (env.get(SHM_DIR_SEAM_ENV) or "").strip()
    shm_dir = Path(seam) if seam else sub._SHM_DIR
    if not seam:
        import platform  # noqa: PLC0415 — only needed outside the seam

        if (platform.system() if system is None else system) != "Linux":
            return TmpDecision(None, "not Linux")
        if mounts_text is None:
            mounts_text = sub._read_mounts()
            if mounts_text is None:
                return TmpDecision(None, "cannot read /proc/mounts", expected_tmpfs=True)
        fstype = sub._mount_fstype(mounts_text, str(shm_dir))
        if fstype != "tmpfs":
            return TmpDecision(None, f"{shm_dir} is not tmpfs ({fstype or 'no mount entry'})", expected_tmpfs=True)
        if "noexec" in (_mount_options(mounts_text, str(shm_dir)) or set()):
            return TmpDecision(None, f"{shm_dir} is mounted noexec (tests run scripts from their tmp dirs)", expected_tmpfs=True)
    can_write = writable or (lambda p: os.access(p, os.W_OK | os.X_OK))
    if not can_write(shm_dir):
        return TmpDecision(None, f"{shm_dir} is not writable by this user", expected_tmpfs=True)
    try:
        free = (free_bytes or (lambda p: shutil.disk_usage(p).free))(shm_dir)
    except OSError as exc:
        return TmpDecision(None, f"cannot read free space of {shm_dir} ({exc})", expected_tmpfs=True)
    if free < MIN_FREE_BYTES:
        return TmpDecision(
            None,
            f"{shm_dir} has {free // 1024**2} MiB free, below the {MIN_FREE_BYTES // 1024**2} MiB floor",
            expected_tmpfs=True,
        )
    headroom = (cgroup_headroom or (lambda: sub._cgroup_headroom(credit_reclaimable=True)))()
    if headroom is not None and headroom < MIN_FREE_BYTES:
        return TmpDecision(
            None,
            f"the cgroup memory headroom is {headroom // 1024**2} MiB, below the "
            f"{MIN_FREE_BYTES // 1024**2} MiB floor (tmpfs pages are charged to it)",
            expected_tmpfs=True,
        )
    root = shm_dir / f"{_ROOT_PREFIX}-{sub._current_uid() if uid is None else uid}"
    if not seam and not sub._socket_path_fits(root / (_RUN_PREFIX + "x" * 8) / "t"):
        return TmpDecision(None, f"a unix socket path under {root} would pass the sun_path limit", expected_tmpfs=True)
    return TmpDecision(root)


# --- run directories -------------------------------------------------------------


def _rm_tree(path: Path) -> None:
    """``rmtree`` that also removes what a test left read-only. Never raises."""

    def _onexc(func, p, _exc) -> None:  # noqa: ANN001 — shutil's callback shape
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(p) or ".", 0o700)
            os.chmod(p, 0o700)
            func(p)

    shutil.rmtree(path, onexc=_onexc)


def _process_start(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` (clock ticks since boot at start), or None
    where there is no procfs. Tells a recycled pid from the one that made a record."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _write_owner_record(run: Path, pid: int) -> None:
    rec = {"pid": pid, "starttime": _process_start(pid)}
    tmp = run / (_OWNER_FILE + ".tmp")
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, run / _OWNER_FILE)


def _owner_is_live(rec: dict) -> bool:
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, someone else's: treat as live (the safe direction)
    except (OSError, OverflowError):
        return False
    recorded = rec.get("starttime")
    now = _process_start(pid)
    return recorded is None or now is None or recorded == now


def _prepare_root(root: Path) -> None:
    """Create the per-uid root 0700 and refuse one that is not ours: /dev/shm is
    world-writable and the name is predictable."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = root.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"{root} is not a real directory")
    if st.st_uid != int(sub._current_uid()):
        raise PermissionError(f"{root} is owned by uid {st.st_uid}")
    if st.st_mode & 0o077:
        root.chmod(0o700)


def _exec_probe(directory: Path) -> None:
    """Run one real script from *directory*: a mount option is not the only thing
    that stops execution there (a security module can too)."""
    script = directory / ".exec-probe.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o700)
    try:
        r = subprocess.run([str(script)], capture_output=True, timeout=20, check=False)
    finally:
        with contextlib.suppress(OSError):
            script.unlink()
    if r.returncode != 0:
        raise PermissionError(f"a script cannot run from {directory} (exit {r.returncode})")


def make_run_dir(root: Path, *, pid: int | None = None, probe: bool = True) -> Path:
    """A fresh run dir under *root* (``r<random>/{t,p}`` plus the owner record)."""
    _prepare_root(root)
    run = Path(tempfile.mkdtemp(prefix=_RUN_PREFIX, dir=root))
    try:
        _write_owner_record(run, os.getpid() if pid is None else pid)
        (run / "t").mkdir(mode=0o700)
        (run / "p").mkdir(mode=0o700)
        if probe:
            _exec_probe(run / "t")
    except BaseException:
        _rm_tree(run)
        raise
    return run


def sweep_stale_runs(
    root: Path,
    *,
    drain: Callable[[Path], object] | None = None,
    now: float | None = None,
) -> list[Path]:
    """Remove the run dirs under *root* whose controller is gone; returns them.
    Only real directories named ``r*`` that the running user owns are looked at.
    Never raises: a failure on one dir is a warning and the sweep moves on."""
    removed: list[Path] = []
    clock = time.time() if now is None else now
    try:
        entries = sorted(root.iterdir())
        me = int(sub._current_uid())
    except (OSError, ValueError):
        return removed
    for d in entries:
        try:
            if not d.name.startswith(_RUN_PREFIX):
                continue
            st = d.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != me:
                continue
            try:
                rec = json.loads((d / _OWNER_FILE).read_text())
            except (OSError, ValueError):
                rec = None
            if rec is None:
                if clock - st.st_mtime < BARE_MIN_AGE_S:
                    continue
            elif _owner_is_live(rec):
                continue
            if drain is not None and (d / "p").is_dir():
                with contextlib.suppress(Exception):  # a drain is never a verdict
                    drain(d / "p")
            _rm_tree(d)
            removed.append(d)
            _log.info("nexus_test_tmp.stale_run_removed", path=str(d), bead="nexus-kyujr")
        except Exception as exc:  # noqa: BLE001 — loud, never fatal
            warnings.warn(f"nexus test tmp: error sweeping {d}: {exc}", stacklevel=2)
    return removed


def tree_bytes(path: Path) -> int:
    """Sum of file sizes under *path* (the run's footprint). 0 on any error."""
    total = 0
    try:
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(dirpath, f)).st_size
    except OSError:
        return 0
    return total


# --- the pytest plugin -----------------------------------------------------------


def _encode(decision: TmpDecision, run: Path | None) -> str:
    if run is not None:
        return f"tmpfs={run}"
    if decision.forced and decision.root is not None:
        return f"forced={decision.root}"
    return f"disk={decision.reason or ''}"


class TmpRootPlugin:
    """Registered by ``tests/conftest.py``; sandbox conftests register the same class.

    ``pytest_configure`` takes the decision on the controller (or in a serial run)
    before any worker is spawned; workers and nested pytest runs find
    :data:`SESSION_ENV` and leave everything alone. ``pytest_terminal_summary``
    prints the one line; ``pytest_unconfigure`` (after every plugin has written its
    summary-time reports) copies the ``nx-*`` reports to disk and removes the run dir.
    """

    def __init__(
        self,
        *,
        drain: Callable[[Path], object] | None = None,
        env: MutableMapping[str, str] | None = None,
        decide: Callable[..., TmpDecision] = decide_tmp_root,
    ) -> None:
        self._drain = drain
        self._env = os.environ if env is None else env
        self._decide = decide
        #: True only in the process that took the decision (it alone prints and cleans up).
        self.decided = False
        self.decision: TmpDecision | None = None
        self.run: Path | None = None
        self.final_bytes: int | None = None
        self.run_removed = False
        self._orig_tempdir = sub._original_tempdir()
        self._given_basetemp: str | None = None
        self._saved: dict[str, str | None] = {}

    # -- decision --

    def pytest_configure(self, config: pytest.Config) -> None:
        if hasattr(config, "workerinput") or self._env.get(SESSION_ENV) is not None:
            return
        try:
            self._given_basetemp = getattr(config.option, "basetemp", None) and str(config.option.basetemp)
        except Exception:  # noqa: BLE001
            self._given_basetemp = None
        self._begin()

    def _begin(self) -> None:
        env = self._env
        self._orig_tempdir = Path(env.get("TMPDIR") or tempfile.gettempdir())
        try:
            decision = self._decide(env=env)
        except Exception as exc:  # noqa: BLE001 — never fail a run on the decision
            decision = TmpDecision(None, f"the controller could not decide ({type(exc).__name__}: {exc})", expected_tmpfs=True)
        run: Path | None = None
        if decision.root is not None:
            try:
                if decision.forced:
                    run = decision.root
                    (run / "t").mkdir(mode=0o700, parents=True, exist_ok=True)
                    (run / "p").mkdir(mode=0o700, parents=True, exist_ok=True)
                else:
                    if decision.root.is_dir():
                        sweep_stale_runs(decision.root, drain=self._drain)
                    run = make_run_dir(decision.root)
            except OSError as exc:
                if decision.forced:
                    raise
                decision = TmpDecision(None, f"cannot use {decision.root} ({exc})", expected_tmpfs=True)
                run = None
        self.decision = decision
        self.decided = True
        self.run = run
        if run is not None:
            self._saved = {k: env.get(k) for k in ("TMPDIR", "PYTEST_DEBUG_TEMPROOT", ORIG_ENV)}
            env[ORIG_ENV] = str(self._orig_tempdir)
            env["TMPDIR"] = str(run / "t")
            if not self._given_basetemp:
                env["PYTEST_DEBUG_TEMPROOT"] = str(run / "p")
            if env is os.environ:
                tempfile.tempdir = None  # gettempdir() caches; re-read TMPDIR
        elif decision.expected_tmpfs:
            _log.warning("nexus_test_tmp.tmpfs_refused", reason=decision.reason, bead="nexus-kyujr")
            warnings.warn(
                f"nexus test tmp: not putting basetemp and TMPDIR on tmpfs: {decision.reason}; "
                f"they stay on disk ({TMP_ROOT_ENV}=<dir> chooses a root)",
                stacklevel=2,
            )
        env[SESSION_ENV] = _encode(decision, run)
        _log.info(
            "nexus_test_tmp.decision", run=str(run) if run else None, forced=decision.forced,
            reason=decision.reason, bead="nexus-kyujr",
        )

    # -- end of run --

    def _report_home(self) -> Path | None:
        """Where the run's ``nx-*`` report files are copied before its basetemp goes
        (tmpfs mode only): ``<original tempdir>/nexus-test-reports-<uid>/<run>``."""
        if self.run is None or self.decision is None or self.decision.forced:
            return None
        return self._orig_tempdir / f"{_REPORT_DIR_PREFIX}-{sub._current_uid()}" / self.run.name

    def _harvest_reports(self) -> int:
        """Copy the controller basetemp's top-level ``nx-*`` files (the DDL counter's
        and the storage-touch guard's reports, which default to the basetemp and are
        written after the session finishes) to the disk report home and keep the
        newest :data:`REPORT_KEEP` runs there, as pytest keeps basetemps. Never raises."""
        dest = self._report_home()
        if dest is None or self.run is None:
            return 0
        files = [
            f for pd in (self.run / "p").glob("pytest-of-*") for bt in pd.glob("pytest-*")
            if bt.is_dir() and not bt.is_symlink() for f in bt.glob("nx-*") if f.is_file()
        ]
        if not files:
            return 0
        try:
            _prepare_root(dest.parent)
            dest.mkdir(mode=0o700)
            for f in files:
                shutil.copy2(f, dest / f.name)
            kept = sorted(
                (d for d in dest.parent.iterdir() if d.is_dir() and not d.is_symlink()),
                key=lambda d: d.stat().st_mtime, reverse=True,
            )
            for old in kept[REPORT_KEEP:]:
                _rm_tree(old)
        except OSError as exc:
            warnings.warn(f"nexus test tmp: could not keep the run's reports under {dest}: {exc}", stacklevel=2)
            return 0
        return len(files)

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        """After the terminal summary (the report plugins write there): copy the
        reports out, remove the run dir, put the environment back."""
        if not self.decided or self.run is None or self.decision is None or self.decision.forced:
            return
        self._harvest_reports()
        _rm_tree(self.run)
        env = self._env
        for k, v in self._saved.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        env.pop(SESSION_ENV, None)
        self.run_removed = True
        if env is os.environ:
            tempfile.tempdir = None

    def summary_line(self) -> str | None:
        if not self.decided or self.decision is None:
            return None
        d = self.decision
        basetemp = (
            f"basetemp {self._given_basetemp} (--basetemp) and TMPDIR" if self._given_basetemp
            else "basetemp and TMPDIR"
        )
        if self.run is not None and d.forced:
            return f"nexus test tmp: {basetemp} under {self.run} (forced by {TMP_ROOT_ENV}, kept after the run)"
        if self.run is not None:
            size = tree_bytes(self.run) // 1024**2 if not self.run_removed else None
            self.final_bytes = size if size is not None else self.final_bytes
            how = f", {self.final_bytes} MiB at the end of the run" if self.final_bytes is not None else ""
            return (
                f"nexus test tmp: {basetemp} on tmpfs {self.run}{how}, removed at exit; "
                f"nx-* reports are copied to {self._report_home()}"
            )
        return f"nexus test tmp: {basetemp} on disk {self._orig_tempdir} because: {d.reason}"

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config) -> None:  # noqa: ANN001
        line = self.summary_line()
        if line:
            terminalreporter.write_line(line)
