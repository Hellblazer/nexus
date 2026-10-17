# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-x3m8e: honest MinerU server health.

``/health`` answered 200 from a server whose working directory was deleted and
from a machine with no POSIX semaphores left, while every task failed. The
probes below read those faults. The functions are imported at module top, so
they bind BEFORE the autouse ``_mineru_machine_probes_read_nothing`` fixture
(tests/conftest.py) replaces the module attributes for every other test.
"""
from __future__ import annotations

import errno
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus._mineru_health import (
    ALL_TASKS_FAILING_MIN_FAILED,
    SEMAPHORE_REMEDY,
    SEMAPHORE_REMEDY_LINUX,
    diagnose_server,
    inspect_process_cwd,
    is_server_file_not_found,
    looks_like_enospc,
    probe_semaphore,
    process_cwd_state,
    semaphore_remedy,
    semaphores_exhausted,
    tasks_all_failing,
)
from nexus.cli import main
from tests._module_seam import patch_in

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="cwd of a process: POSIX")


# ── error-text classifiers ───────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("FileNotFoundError: [Errno 2] No such file or directory", True),
    ("no such file or directory", True),
    ("CUDA out of memory", False),
    ("", False),
])
def test_file_not_found_classifier(text: str, expected: bool) -> None:
    assert is_server_file_not_found(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("OSError: [Errno 28] No space left on device", True),
    ("ENOSPC", True),
    ("FileNotFoundError", False),
    ("", False),
])
def test_enospc_classifier(text: str, expected: bool) -> None:
    assert looks_like_enospc(text) is expected


# ── process_cwd_state: a real child ──────────────────────────────────────────


def _child_in(directory: Path) -> subprocess.Popen:
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(directory), start_new_session=True,
    )
    time.sleep(0.5)  # exec done, cwd established
    return proc


@posix_only
def test_a_live_cwd_reads_ok_and_a_deleted_one_reads_gone(tmp_path: Path) -> None:
    here = tmp_path / "here"
    here.mkdir()
    proc = _child_in(here)
    try:
        assert process_cwd_state(proc.pid) == "ok"
        here.rmdir()
        assert process_cwd_state(proc.pid) == "gone"
    finally:
        proc.kill()
        proc.wait(timeout=10)


@posix_only
def test_a_process_that_does_not_exist_reads_unknown(tmp_path: Path) -> None:
    proc = _child_in(tmp_path)
    proc.kill()
    proc.wait(timeout=10)
    assert process_cwd_state(proc.pid) is None


def test_windows_reads_unknown() -> None:
    assert process_cwd_state(1, _platform="win32") is None


def test_a_missing_lsof_reads_unknown() -> None:
    def no_lsof(pid):
        raise FileNotFoundError("lsof")

    assert process_cwd_state(1, _lsof=no_lsof, _platform="darwin") is None


def test_lsof_output_with_a_deleted_path_reads_gone(tmp_path: Path) -> None:
    out = f"p123\nfcwd\nn{tmp_path / 'vanished'}\n"
    assert process_cwd_state(123, _lsof=lambda pid: out, _platform="darwin") == "gone"


def test_lsof_output_with_a_live_path_reads_ok(tmp_path: Path) -> None:
    out = f"p123\nfcwd\nn{tmp_path}\n"
    assert process_cwd_state(123, _lsof=lambda pid: out, _platform="darwin") == "ok"


def test_lsof_output_without_a_cwd_line_reads_unknown() -> None:
    assert process_cwd_state(123, _lsof=lambda pid: "p123\n", _platform="darwin") is None


def _lsof_fields(path: Path, *, device: int, inode: int) -> str:
    return f"p123\nfcwd\nD{hex(device)}\ni{inode}\nn{path}\n"


def test_lsof_names_the_old_path_of_a_recreated_directory_and_the_inode_says_gone(tmp_path: Path) -> None:
    """The path exists, but it is not the directory the process holds."""
    here = tmp_path / "wt"
    here.mkdir()
    st = here.stat()
    seen = _lsof_fields(here, device=st.st_dev, inode=st.st_ino + 1)
    assert process_cwd_state(123, _lsof=lambda pid: seen, _platform="darwin") == "gone"
    assert inspect_process_cwd(123, _lsof=lambda pid: seen, _platform="darwin") == (str(here), "gone")


def test_lsof_output_whose_device_and_inode_match_the_path_reads_ok(tmp_path: Path) -> None:
    st = tmp_path.stat()
    seen = _lsof_fields(tmp_path, device=st.st_dev, inode=st.st_ino)
    assert inspect_process_cwd(123, _lsof=lambda pid: seen, _platform="darwin") == (str(tmp_path), "ok")


def test_lsof_output_whose_device_differs_reads_gone(tmp_path: Path) -> None:
    st = tmp_path.stat()
    seen = _lsof_fields(tmp_path, device=st.st_dev + 1, inode=st.st_ino)
    assert process_cwd_state(123, _lsof=lambda pid: seen, _platform="darwin") == "gone"


@posix_only
def test_a_directory_deleted_and_recreated_at_the_same_path_reads_gone_for_a_real_child(
    tmp_path: Path,
) -> None:
    """The measured false negative: ``lsof`` keeps naming the OLD path and the
    recreated path is a directory, so an ``isdir`` check read ``ok`` for a
    process whose ``getcwd`` raises. A real child, the real probe."""
    here = tmp_path / "wt"
    here.mkdir()
    proc = _child_in(here)
    try:
        assert process_cwd_state(proc.pid) == "ok"
        here.rmdir()
        here.mkdir()  # same name, new inode: a worktree removed and made again
        assert here.is_dir()
        assert process_cwd_state(proc.pid) == "gone"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_linux_reads_the_deleted_suffix_whether_or_not_the_path_was_recreated(tmp_path: Path) -> None:
    deleted = f"{tmp_path}/wt (deleted)"
    assert inspect_process_cwd(
        5, _platform="linux", _readlink=lambda _p: deleted,
    ) == (f"{tmp_path}/wt", "gone")
    assert inspect_process_cwd(5, _platform="linux", _readlink=lambda _p: str(tmp_path)) == (str(tmp_path), "ok")
    assert inspect_process_cwd(5, _platform="linux", _readlink=lambda _p: (_ for _ in ()).throw(OSError())) == (None, None)


# ── tasks_all_failing ────────────────────────────────────────────────────────


@pytest.mark.parametrize("health,expected", [
    ({"failed_tasks": ALL_TASKS_FAILING_MIN_FAILED, "completed_tasks": 0}, True),
    ({"failed_tasks": ALL_TASKS_FAILING_MIN_FAILED + 4, "completed_tasks": 0}, True),
    ({"failed_tasks": ALL_TASKS_FAILING_MIN_FAILED - 1, "completed_tasks": 0}, False),
    ({"failed_tasks": 9, "completed_tasks": 1}, True),      # 90%: the bar
    ({"failed_tasks": 9, "completed_tasks": 2}, False),     # something is working
    ({"failed_tasks": 3, "completed_tasks": 20}, False),    # a few bad PDFs on a busy server
    ({"status": "healthy"}, False),                          # an older server: no counters
    ({"failed_tasks": "many", "completed_tasks": 0}, False),
    ({"failed_tasks": None, "completed_tasks": None}, False),
])
def test_tasks_all_failing(health: dict, expected: bool) -> None:
    assert tasks_all_failing(health) is expected


# ── semaphores_exhausted: injected ENOSPC ────────────────────────────────────


def _raises(err: BaseException):
    def create():
        raise err

    return create


def test_enospc_from_semaphore_creation_is_the_machine_message() -> None:
    got = semaphores_exhausted(
        _create=_raises(OSError(errno.ENOSPC, "No space left on device")), _platform="darwin",
    )
    assert got == SEMAPHORE_REMEDY
    assert "kern.posix.sem.max" in got
    assert "nx mineru stop && nx mineru start" in got, "the remedy must name a command that exists"


def test_any_other_oserror_is_not_this_fault() -> None:
    assert semaphores_exhausted(
        _create=_raises(OSError(errno.EACCES, "denied")), _platform="darwin",
    ) is None


def test_a_platform_without_a_fork_context_reads_no_fault() -> None:
    assert semaphores_exhausted(_create=_raises(ValueError("cannot find context")), _platform="darwin") is None


def test_creating_a_semaphore_works_reads_no_fault() -> None:
    assert semaphores_exhausted(_create=lambda: object(), _platform="darwin") is None


def test_windows_has_no_semaphores_to_exhaust() -> None:
    assert semaphores_exhausted(_create=_raises(OSError(errno.ENOSPC, "x")), _platform="win32") is None


@posix_only
def test_the_real_probe_on_this_machine_creates_and_drops_one_semaphore() -> None:
    """Not injected: the real probe. A box out of semaphores would make this the
    fault message, which is the probe working; anything else is None."""
    got = semaphores_exhausted()
    assert got is None or got == semaphore_remedy()


def test_the_remedy_is_chosen_by_platform() -> None:
    """Linux's limit is /dev/shm, whose leaked names are files an operator can
    list and remove; macOS has a fixed kernel table and no such handle."""
    assert semaphore_remedy("darwin") == SEMAPHORE_REMEDY
    assert semaphore_remedy("linux") == SEMAPHORE_REMEDY_LINUX
    assert "kern.posix.sem.max" in SEMAPHORE_REMEDY and "/dev/shm" not in SEMAPHORE_REMEDY
    assert "/dev/shm" in SEMAPHORE_REMEDY_LINUX and "kern.posix" not in SEMAPHORE_REMEDY_LINUX
    assert "nx mineru stop && nx mineru start" in SEMAPHORE_REMEDY_LINUX
    got = semaphores_exhausted(_create=_raises(OSError(errno.ENOSPC, "x")), _platform="linux")
    assert got == SEMAPHORE_REMEDY_LINUX


def test_a_build_without_the_semlock_module_reads_no_fault() -> None:
    """ImportError from the probe's own import is not a machine fault."""
    assert probe_semaphore(_create=_raises(ImportError("no _multiprocessing"))) is None
    assert semaphores_exhausted(_create=_raises(ImportError("x")), _platform="linux") is None


def test_the_probe_hands_back_the_kernels_error() -> None:
    err = OSError(errno.ENOSPC, "No space left on device")
    assert probe_semaphore(_create=_raises(err)) is err
    assert probe_semaphore(_create=lambda: object()) is None


#: Run in a FRESH interpreter so a resource tracker started by an earlier test
#: cannot mask one started by the probe. ``token_hex`` is pinned so the probe's
#: semaphore NAME is known and can be looked up by name afterwards.
_PROBE_AUDIT = r"""
import ctypes, json, secrets, sys
from multiprocessing import resource_tracker as rt

registered = []
_orig = rt.register
rt.register = lambda name, rtype: registered.append((name, rtype)) or _orig(name, rtype)
secrets.token_hex = lambda n=None: "c0ffee"

from nexus import _mineru_health as h

sem = h._create_probe_semaphore()
name_attr = sem.name
del sem

libc = ctypes.CDLL(None, use_errno=True)
libc.sem_open.restype = ctypes.c_void_p
libc.sem_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
import os
probe_name = f"/nxp-{os.getpid():x}-c0ffee".encode()
handle = libc.sem_open(probe_name, 0)
still_exists = handle not in (None, ctypes.c_void_p(-1).value)
print(json.dumps({
    "registered": registered,
    "name_attr": name_attr,
    "tracker_pid": rt._resource_tracker._pid,
    "still_exists": still_exists,
    "err_probe": h.probe_semaphore() is None,
}))
"""


@posix_only
def test_the_real_probe_registers_no_tracker_and_leaves_no_semaphore_behind() -> None:
    """The probe creates a throwaway named semaphore. It must unlink it in the
    same call (``SemLock.name`` is None, nothing registered with a resource
    tracker, no tracker process started) or the health check itself would leak
    one semaphore per call on the macOS table it is diagnosing."""
    out = subprocess.run(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", _PROBE_AUDIT], capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["registered"] == [], "the probe registered a semaphore with a resource tracker"
    assert report["name_attr"] is None, "the probe's semaphore kept its name: it was not unlinked"
    assert report["tracker_pid"] is None, "the probe started a resource tracker process"
    assert report["still_exists"] is False, "the probe's semaphore is still in the kernel"
    assert report["err_probe"] is True


def test_the_doctor_check_and_the_mineru_check_share_one_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two probes drift. ``nx doctor --check-resources`` asks ``probe_semaphore``
    too, applying its own policy (any OSError fails; the MinerU check wants ENOSPC)."""
    from nexus.commands import doctor

    boom = OSError(errno.EMFILE, "too many open files")
    # the MinerU check, given a non-ENOSPC error from the shared probe, reads no fault
    assert semaphores_exhausted(_create=_raises(boom), _platform="darwin") is None
    asked: list[str] = []
    with monkeypatch.context() as m:
        m.setattr("nexus._mineru_health.probe_semaphore", lambda **k: asked.append("probe") or boom)
        ok, msg = doctor._probe_semaphore_namespace()
    assert ok is False and asked == ["probe"]
    assert "too many open files" in msg.lower() or "EMFILE" in msg
    with monkeypatch.context() as m:
        m.setattr("nexus._mineru_health.probe_semaphore", lambda **k: None)
        assert doctor._probe_semaphore_namespace()[0] is True


# ── diagnose_server ──────────────────────────────────────────────────────────

_OK_HEALTH = {"failed_tasks": 0, "completed_tasks": 5}


def test_a_server_with_nothing_wrong_has_no_faults() -> None:
    assert diagnose_server(1, _OK_HEALTH, _cwd_state=lambda pid: "ok", _semaphores=lambda: None) == []


def test_each_fault_is_reported_cause_before_symptom() -> None:
    faults = diagnose_server(
        1, {"failed_tasks": 7, "completed_tasks": 0},
        _cwd_state=lambda pid: "gone", _semaphores=lambda: "no semaphores",
    )
    assert len(faults) == 3
    assert "working directory was deleted" in faults[0]
    assert "nx mineru stop && nx mineru start" in faults[0]
    assert faults[1] == "no semaphores"
    assert "every recent task failed" in faults[2] and "7 failed" in faults[2]


def test_an_unknown_cwd_state_is_not_a_fault() -> None:
    assert diagnose_server(1, _OK_HEALTH, _cwd_state=lambda pid: None, _semaphores=lambda: None) == []


def test_diagnose_resolves_the_module_probes_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conftest fixture works by replacing the module attributes; that only
    covers consumers that look them up at call time."""
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    monkeypatch.setattr("nexus._mineru_health.semaphores_exhausted", lambda: None)
    from nexus._mineru_health import diagnose_server as fresh

    assert len(fresh(1, _OK_HEALTH)) == 1


# ── nx mineru status: UNHEALTHY ──────────────────────────────────────────────

_REAL_HEALTH = {
    "status": "healthy", "version": "3.1.11", "protocol_version": 1,
    "queued_tasks": 0, "processing_tasks": 0, "completed_tasks": 0, "failed_tasks": 0,
    "max_concurrent_requests": 1, "processing_window_size": 8,
    "task_retention_seconds": 300, "task_cleanup_interval_seconds": 300,
}


@pytest.fixture
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".config" / "nexus"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "mineru.pid"
    path.write_text(json.dumps({"pid": 12345, "port": 8010, "started_at": "2026-04-02T12:00:00+00:00"}))
    return path


def _status(health: dict) -> object:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = health
    seam = ("nexus.commands.mineru", "nexus.daemon.service_registry", "nexus.util.process_group")
    with patch_in(seam, "os.kill"), patch("nexus.commands.mineru.httpx.get", return_value=resp):
        return CliRunner().invoke(main, ["mineru", "status"])


def test_status_says_unhealthy_for_a_deleted_cwd_even_though_health_is_200(
    pid_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    result = _status(dict(_REAL_HEALTH))
    assert result.exit_code == 1, result.output  # a script asking "is it usable?" gets no for an answer
    assert "UNHEALTHY" in result.output
    assert "working directory was deleted" in result.output
    assert "healthy (PID" not in result.output, "must not also claim the server is healthy"


def test_status_says_unhealthy_when_semaphores_are_gone(
    pid_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.semaphores_exhausted", lambda: SEMAPHORE_REMEDY)
    result = _status(dict(_REAL_HEALTH))
    assert result.exit_code == 1
    assert "UNHEALTHY" in result.output and "kern.posix.sem.max" in result.output


def test_status_says_unhealthy_when_every_recent_task_failed(pid_file: Path) -> None:
    result = _status({**_REAL_HEALTH, "failed_tasks": 5, "completed_tasks": 0})
    assert result.exit_code == 1
    assert "UNHEALTHY" in result.output
    assert "every recent task failed" in result.output
    assert "Failed: 5" in result.output


def test_status_still_says_healthy_when_nothing_is_wrong(pid_file: Path) -> None:
    result = _status({**_REAL_HEALTH, "completed_tasks": 5})
    assert result.exit_code == 0, result.output
    assert "UNHEALTHY" not in result.output
    assert "running and healthy" in result.output


def test_status_probes_are_neutral_by_default(pid_file: Path) -> None:
    """The conftest fixture: with no override a fake pid on this box reads as no
    fault, so the healthy path does not depend on the machine running the test."""
    result = _status({**_REAL_HEALTH, "completed_tasks": 1})
    assert "running and healthy" in result.output


def test_status_says_unhealthy_when_the_servers_interpreter_or_output_root_is_gone(
    pid_file: Path, tmp_path: Path,
) -> None:
    """The pid file records the interpreter, executable and output root the
    server was started with; a tool environment replaced under a running server
    takes its libraries with it while ``/health`` still answers 200."""
    kept = tmp_path / "kept"
    kept.mkdir()
    pid_file.write_text(json.dumps({
        "pid": 12345, "port": 8010,
        "python": str(tmp_path / "gone-tool" / "bin" / "python"),
        "executable": str(kept),
        "output_root": str(tmp_path / "gone-output"),
    }))
    result = _status(dict(_REAL_HEALTH))
    assert result.exit_code == 1, result.output
    assert "UNHEALTHY" in result.output
    assert f"interpreter {tmp_path}/gone-tool/bin/python" in result.output
    assert "output directory" in result.output
    assert "mineru-api executable" not in result.output, "an executable that exists is not a fault"


def test_diagnose_names_only_the_paths_that_are_missing(tmp_path: Path) -> None:
    here = tmp_path / "here"
    here.mkdir()
    info = {"python": str(here), "executable": str(here), "output_root": str(here)}
    assert diagnose_server(1, _OK_HEALTH, info=info, _cwd_state=lambda p: "ok", _semaphores=lambda: None) == []
    info["executable"] = str(tmp_path / "vanished")
    faults = diagnose_server(1, _OK_HEALTH, info=info, _cwd_state=lambda p: "ok", _semaphores=lambda: None)
    assert len(faults) == 1 and "mineru-api executable" in faults[0] and "interpreter" not in faults[0]


def test_status_exits_non_zero_when_health_does_not_answer_200(pid_file: Path) -> None:
    resp = MagicMock()
    resp.status_code = 503
    seam = ("nexus.commands.mineru", "nexus.daemon.service_registry", "nexus.util.process_group")
    with patch_in(seam, "os.kill"), patch("nexus.commands.mineru.httpx.get", return_value=resp):
        result = CliRunner().invoke(main, ["mineru", "status"])
    assert result.exit_code == 1 and "unhealthy" in result.output.lower()


# ── server_is_broken: which failures are the registered server's ─────────────

_REG = {"pid": 4242, "port": 8010, "output_root": "/var/nexus/x3m8e/mineru-output", "python": "/opt/tool/bin/python"}


def test_server_is_broken_needs_the_registered_server_and_a_server_side_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus._mineru_health import server_is_broken

    url = "http://127.0.0.1:8010/file_parse"
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    assert server_is_broken(url, "FileNotFoundError", _REG) is True
    assert server_is_broken("http://127.0.0.1:9999/file_parse", "FileNotFoundError", _REG) is False
    assert server_is_broken("http://10.0.0.5:8010/file_parse", "FileNotFoundError", _REG) is False
    assert server_is_broken(url, "FileNotFoundError", None) is False
    assert server_is_broken(url, "FileNotFoundError", {"port": 8010}) is False, "no pid: nothing to inspect"
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "ok")
    monkeypatch.setattr("nexus._mineru_health.process_cwd_path", lambda pid: "/")
    assert server_is_broken(url, "FileNotFoundError: No such file or directory", _REG) is False
    assert server_is_broken(url, "No such file or directory: '/var/nexus/x3m8e/mineru-output/a'", _REG) is True
    assert server_is_broken(url, "No such file or directory: '/opt/tool/lib/x.py'", _REG) is True


def test_a_live_interpreter_prefix_in_a_request_traceback_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Every frame of a real 409 traceback sits under the server's venv, so a
    # prefix that still exists says nothing about the server; a gone one does.
    from nexus._mineru_health import server_is_broken

    url = "http://127.0.0.1:8010/file_parse"
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "ok")
    monkeypatch.setattr("nexus._mineru_health.process_cwd_path", lambda pid: "/")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    info = {**_REG, "python": str(venv / "bin" / "python")}
    trace = f"File \"{venv}/lib/python3.12/site-packages/mineru/x.py\", line 9\nFileNotFoundError: in.pdf"
    assert server_is_broken(url, trace, info) is False
    import shutil

    shutil.rmtree(venv)
    assert server_is_broken(url, trace, info) is True


def test_a_server_started_from_the_root_is_not_named_by_every_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Servers this code starts have ``/`` as their cwd; ``/`` is a substring of
    every path, so it must not count as 'a path the server owns'."""
    from nexus._mineru_health import server_is_broken

    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "ok")
    monkeypatch.setattr("nexus._mineru_health.process_cwd_path", lambda pid: "/")
    assert server_is_broken(
        "http://127.0.0.1:8010/x", "FileNotFoundError: '/home/u/a.pdf'", {**_REG, "python": "/usr/bin/python3"},
    ) is False
