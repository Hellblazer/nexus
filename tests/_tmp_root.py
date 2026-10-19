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
under, which is what ``tmp_path`` and ``tmp_path_factory`` use). The engine JVM follows
too: the JDK ignores ``TMPDIR`` on Linux, so ``tests/db/_service_fixture.py``
(``run_tmpdir_arg``) passes ``-Djava.io.tmpdir=<TMPDIR>`` on every substrate engine launch
(netty and JNA extract natives there on each boot). What does not move: ONNX Runtime's
native cache (``NX_ORT_NATIVE_DIR``, pinned to the real home so it is extracted once, not
per run), a crashing JVM's hs_err report (``-XX:ErrorFile`` names the ORIGINAL tempdir;
the report is evidence and the run dir is deleted), clusters whose root decided on disk
(``tests/_engine_substrate.py`` puts them in the original tempdir, never in RAM by
accident), and the boot-slot directory.

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
pytest's basetemp retention); their summary lines print that copy's path
(:func:`kept_report_path`), not the tmpfs path that is gone by the time anyone reads
the line. A run that sets ``NX_DDL_REPORT_DIR`` or ``NX_STORAGE_TOUCH_REPORT_DIR``
keeps writing where it said.

A run that EXITS NON-ZERO also copies its failure evidence into
``<report home>/failed/`` before the run dir goes (:func:`copy_failure_evidence`): the
engine logs (``nexus-svc-*``), the ``tmp_path`` trees of the failed tests (found by
name from the failed node ids, controller and xdist workers alike), up to
:data:`FAILURE_COPY_CAP_BYTES`, and a ``SUMMARY.txt`` naming what was kept and what the
cap skipped; ``NX_KEEP_FENCED_HOME`` also copies the fenced home (uncapped: the operator
asked). A load-dependent failure cannot be reproduced under ``NX_TEST_TMP_ROOT=<dir>``,
so this is the only record of it. A passing run keeps only the reports.

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
import re
import shutil
import stat
import subprocess
import tempfile
import time
import warnings
from collections.abc import Callable, Mapping, MutableMapping, Sequence
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
#: A failed run's evidence (the failed tests' ``tmp_path`` trees and the engine logs)
#: is copied to disk up to this many bytes; what does not fit is named in SUMMARY.txt.
FAILURE_COPY_CAP_BYTES = 256 * 1024**2
#: Sub-directory of the report home that holds a failed run's evidence.
FAILED_SUBDIR = "failed"
FAILURE_SUMMARY_FILE = "SUMMARY.txt"
#: Exit codes that are not a failure: 0 (passed) and 5 (nothing collected).
_CLEAN_EXITS = (0, 5)
#: pytest cuts a test's name to this many characters for its ``tmp_path`` directory.
_TMP_NAME_MAX = 30
#: Top-level names in the run's ``TMPDIR`` that hold engine evidence: the per-engine
#: log directories (``spawn_service``) and the run's cluster-root log.
_ENGINE_EVIDENCE_GLOBS = ("nexus-svc-*", "nexus-pg-roots-*")
#: The fenced home's name (``tests/_fence_home.py``); kept on request, never capped.
_FENCED_HOME_GLOB = "nx-suite-home-*"
_KEEP_FENCED_HOME_ENV = "NX_KEEP_FENCED_HOME"


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
    """``rmtree`` that also removes what a test left unreadable or read-only. Never raises.

    The ``onexc`` handler never re-calls the ``func`` rmtree hands it: on POSIX the
    fd-based walk passes ``os.open`` (and ``os.scandir``, ``os.lstat``,
    ``os.path.islink``, ``os.close``), which need more than a path, so ``func(p)``
    raised TypeError past ``suppress(OSError)`` and crashed pytest_unconfigure after a
    green run (5588beead CI shard 3: a test left its tmp_path at mode 0o200). It
    restores owner access on the entry's parent and the entry, then removes the
    entry itself: a directory through one nested rmtree with this same handler,
    anything else through ``os.unlink``. Each path is retried once, so an entry this
    user cannot chmod ends that attempt instead of recursing.
    """
    retried: set[str] = set()

    def _onexc(_func, p, exc) -> None:  # noqa: ANN001 — shutil's callback shape
        if isinstance(exc, FileNotFoundError) or p in retried:
            return
        retried.add(p)
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(p) or ".", 0o700)
        try:
            is_dir = stat.S_ISDIR(os.lstat(p).st_mode)
        except OSError:
            return
        with contextlib.suppress(OSError):
            if is_dir:
                os.chmod(p, 0o700)
                shutil.rmtree(p, onexc=_onexc)
            else:
                os.unlink(p)

    try:
        shutil.rmtree(path, onexc=_onexc)
    except Exception as exc:  # noqa: BLE001 — cleanup at exit must never fail a run
        _log.warning("tmp_root_rm_tree_failed", path=str(path), error=repr(exc))


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


def _is_run_controller(run: Path, pid: int) -> bool:
    """True when *pid* is the controller that made *run* (its ``owner.json``). A nested
    pytest inherits the session variable and writes its reports into its own numbered
    basetemp under the same ``p``, but only the controller's basetemp is harvested."""
    try:
        rec = json.loads((run / _OWNER_FILE).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(rec, dict) and rec.get("pid") == pid


def kept_report_path(
    path: Path | str, env: Mapping[str, str] | None = None, *, pid: int | None = None,
) -> Path:
    """Where a top-level ``nx-*`` report written to *path* will be after the run.

    A report plugin prints its path in the terminal summary, but the run's basetemp
    is deleted at unconfigure and the report is copied to the report home first
    (:meth:`TmpRootPlugin._harvest`). Printing the tmpfs path would name a file that
    is gone by the time anyone reads the line. Anything the harvest does not copy
    (a report in a directory the operator chose, a run that is not on tmpfs, a NESTED
    pytest's report, which the harvest leaves alone and which exists until the outer
    run ends) comes back unchanged."""
    path = Path(path)
    env = os.environ if env is None else env
    session = env.get(SESSION_ENV) or ""
    if not session.startswith("tmpfs="):
        return path
    run = Path(session.removeprefix("tmpfs="))
    try:
        rel = path.relative_to(run / "p")
    except ValueError:
        return path
    if len(rel.parts) != 3 or not path.name.startswith("nx-"):
        return path  # the harvest copies only <p>/pytest-of-<user>/pytest-<n>/nx-*
    if not _is_run_controller(run, os.getpid() if pid is None else pid):
        return path  # a nested run's own basetemp: never harvested (nexus-kyujr)
    orig = Path(env.get(ORIG_ENV) or tempfile.gettempdir())
    return orig / f"{_REPORT_DIR_PREFIX}-{sub._current_uid()}" / run.name / path.name


def _tmp_dir_prefix(nodeid: str) -> str:
    """The name pytest's ``tmp_path`` gives the directory of the test *nodeid* names
    (before the number it appends): the test's own name, non-word characters to ``_``,
    cut to 30. A parametrize id may contain ``::``, so the name is cut at the first ``[``."""
    head, bracket, rest = nodeid.partition("[")
    return re.sub(r"\W", "_", head.rsplit("::", 1)[-1] + bracket + rest)[:_TMP_NAME_MAX]


def failed_tmp_dirs(p_root: Path, nodeids: Sequence[str]) -> list[Path]:
    """The ``tmp_path`` directories under the run's pytest root that belong to the failed
    tests (a superset: two tests whose names share their first 30 characters share a
    prefix). Looks at the controller basetemp and at each xdist worker's ``popen-gw*``."""
    patterns = [re.compile(re.escape(pre) + r"\d+") for pre in {_tmp_dir_prefix(n) for n in nodeids}]
    if not patterns:
        return []
    bases: list[Path] = []
    for user_dir in sorted(p_root.glob("pytest-of-*")):
        if user_dir.is_symlink() or not user_dir.is_dir():
            continue
        for bt in sorted(user_dir.glob("pytest-*")):
            if bt.is_symlink() or not bt.is_dir():
                continue
            bases.append(bt)
            bases.extend(w for w in sorted(bt.glob("popen-gw*")) if w.is_dir() and not w.is_symlink())
    found: list[Path] = []
    for base in bases:
        with contextlib.suppress(OSError):
            found.extend(
                d for d in sorted(base.iterdir())
                if d.is_dir() and not d.is_symlink() and any(rx.fullmatch(d.name) for rx in patterns)
            )
    return found


def _ignore_specials(directory: str, names: list[str]) -> list[str]:
    """``copytree`` ignore callback: sockets and FIFOs a test left behind cannot be copied."""
    skip = []
    for n in names:
        try:
            mode = os.lstat(os.path.join(directory, n)).st_mode
        except OSError:
            skip.append(n)
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            skip.append(n)
    return skip


def _size_of(path: Path) -> int:
    try:
        return path.lstat().st_size if not path.is_dir() else tree_bytes(path)
    except OSError:
        return 0


def copy_failure_evidence(
    run: Path,
    dest: Path,
    nodeids: Sequence[str],
    *,
    exitstatus: int,
    keep_fenced_home: bool = False,
    cap: int = FAILURE_COPY_CAP_BYTES,
) -> list[str]:
    """Copy what a FAILED run's run directory holds that explains the failure to *dest*,
    write ``SUMMARY.txt`` there, and return its note lines. Never raises.

    In order, until *cap* bytes: the engine logs and the cluster-root log from the run's
    ``TMPDIR`` (``nexus-svc-*``, ``nexus-pg-roots-*``), then the ``tmp_path``
    directories of the failed tests (:func:`failed_tmp_dirs`). The fenced home
    (``nx-suite-home-*``) is copied, uncapped, only when *keep_fenced_home* says the
    operator asked for it (``NX_KEEP_FENCED_HOME``). A crashing engine's hs_err report
    is not here: it is written straight to the original tempdir."""
    notes: list[str] = []
    used = 0

    def take(src: Path, rel: Path, *, capped: bool = True) -> None:
        nonlocal used
        size = _size_of(src)
        if capped and used + size > cap:
            notes.append(f"skipped {rel} ({size // 1024} KiB): over the {cap // 1024**2} MiB cap")
            return
        target = dest / rel
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, symlinks=True, ignore=_ignore_specials, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
        except (OSError, shutil.Error) as exc:
            notes.append(f"could not copy {rel}: {exc}")
            return
        used += size
        notes.append(f"kept {rel} ({size // 1024} KiB)")

    with contextlib.suppress(OSError):
        for pattern in _ENGINE_EVIDENCE_GLOBS:
            for src in sorted((run / "t").glob(pattern)):
                if not src.is_symlink():
                    take(src, Path("engine-logs") / src.name)
    try:
        for src in failed_tmp_dirs(run / "p", nodeids):
            take(src, Path("tmp_path") / src.relative_to(run / "p"))
    except OSError as exc:
        notes.append(f"could not list the failed tests' tmp dirs: {exc}")
    if keep_fenced_home:
        with contextlib.suppress(OSError):
            for src in sorted((run / "t").glob(_FENCED_HOME_GLOB)):
                if not src.is_symlink():
                    take(src, Path("fenced-home") / src.name, capped=False)
    elif nodeids or exitstatus:
        notes.append(f"the fenced home is removed unless {_KEEP_FENCED_HOME_ENV} is set")
    lines = [
        f"exit status {exitstatus}; {len(nodeids)} failing test report(s); the run directory {run} is removed after this copy",
        *(f"  failed: {n}" for n in list(nodeids)[:100]),
        *(["  ... (list cut at 100)"] if len(nodeids) > 100 else []),
        *(f"  {n}" for n in notes),
    ]
    with contextlib.suppress(OSError):
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        (dest / FAILURE_SUMMARY_FILE).write_text("\n".join(lines) + "\n")
    return notes


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
        #: The session's exit status (None until the controller's sessionfinish) and the
        #: node ids that reported a failure; a failed run's evidence is copied from them.
        self.exitstatus: int | None = None
        self.failed_nodeids: list[str] = []
        #: What the failed-run copy kept or skipped (see :func:`copy_failure_evidence`).
        self.evidence_notes: list[str] = []
        self._orig_tempdir = sub._original_tempdir()
        self._given_basetemp: str | None = None
        #: The controller's own basetemp (pytest's tmp_path factory), read at unconfigure;
        #: only its top-level ``nx-*`` reports are harvested.
        self._basetemp: Path | None = None
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

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001
        """Collect the node ids that failed (the controller sees every worker's reports)."""
        if self.decided and report.failed and report.nodeid not in self.failed_nodeids:
            self.failed_nodeids.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus) -> None:  # noqa: ANN001
        if self.decided:
            self.exitstatus = int(exitstatus)

    @property
    def failed_run(self) -> bool:
        return self.exitstatus is not None and self.exitstatus not in _CLEAN_EXITS

    def _report_files(self) -> list[Path]:
        """The top-level ``nx-*`` files of the CONTROLLER's basetemp. A nested pytest run
        inherits ``PYTEST_DEBUG_TEMPROOT`` and gets the next numbered basetemp under the
        same ``p``; its reports carry the same names and copying them too would overwrite
        the controller's (nexus-kyujr). No basetemp (the controller never made one) means
        no reports of its own."""
        bt = self._basetemp
        if self.run is None or bt is None or not bt.is_dir() or bt.is_symlink():
            return []
        try:
            bt.relative_to(self.run / "p")
        except ValueError:
            return []  # a --basetemp outside the run dir is not removed, so not harvested
        return [f for f in bt.glob("nx-*") if f.is_file()]

    def _harvest(self) -> int:
        """Copy what must outlive the run to the disk report home and keep the newest
        :data:`REPORT_KEEP` runs there, as pytest keeps basetemps. Never raises.

        Always: the controller basetemp's top-level ``nx-*`` files (the DDL counter's
        and the storage-touch guard's reports, which default to the basetemp and are
        written after the session finishes). On a FAILED run also: the engine logs, the
        failed tests' ``tmp_path`` trees and a ``SUMMARY.txt`` under ``failed/``
        (:func:`copy_failure_evidence`), and the fenced home when
        ``NX_KEEP_FENCED_HOME`` is set. The removal of the run directory would
        otherwise take the only evidence of a load-dependent failure with it."""
        dest = self._report_home()
        if dest is None or self.run is None:
            return 0
        files = self._report_files()
        failed = self.failed_run
        if not files and not failed:
            return 0
        try:
            _prepare_root(dest.parent)
            dest.mkdir(mode=0o700, exist_ok=True)
            for f in files:
                shutil.copy2(f, dest / f.name)
            if failed:
                self.evidence_notes = copy_failure_evidence(
                    self.run, dest / FAILED_SUBDIR, self.failed_nodeids,
                    exitstatus=int(self.exitstatus or 0),
                    keep_fenced_home=bool(self._env.get(_KEEP_FENCED_HOME_ENV)),
                )
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
        reports and, on a failed run, its evidence out, remove the run dir, put the
        environment back."""
        if not self.decided or self.run is None or self.decision is None or self.decision.forced:
            return
        bt = getattr(getattr(config, "_tmp_path_factory", None), "_basetemp", None)
        self._basetemp = Path(bt) if bt is not None else None
        self._harvest()
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
            evidence = (
                f"; this run FAILED, so the engine logs and the failed tests' tmp_path trees "
                f"(up to {FAILURE_COPY_CAP_BYTES // 1024**2} MiB) are copied to "
                f"{self._report_home() / FAILED_SUBDIR} (SUMMARY.txt lists them)"
                if self.failed_run else ""
            )
            return (
                f"nexus test tmp: {basetemp} on tmpfs {self.run}{how}, removed at exit; "
                f"nx-* reports are copied to {self._report_home()}{evidence}"
            )
        return f"nexus test tmp: {basetemp} on disk {self._orig_tempdir} because: {d.reason}"

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config) -> None:  # noqa: ANN001
        if self.decided:
            self.exitstatus = int(exitstatus)
        line = self.summary_line()
        if line:
            terminalreporter.write_line(line)
