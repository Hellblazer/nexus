# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the .mcpb bundle's resolve-with-retry bootstrap (nexus-r433b).

The Claude Desktop extension resolves ``conexus[local]>=X.Y.Z`` from PyPI on
first launch. PyPI's simple index lags the upload by ~10-25 minutes after a
release (four consecutive releases measured), so an install inside that
window used to die with a bare resolver error before any of our code ran —
the resolution happened inside ``uv run src/server.py`` itself.

``mcpb/src/bootstrap.py`` now owns the resolution: ``uv sync`` with bounded
backoff on exactly the propagation-window failure class, then exec of the
real server. These tests pin the retry loop's classification and bounds
(injected runner/sleeper — no real uv, no network) and the manifest wiring
that makes Desktop launch the bootstrap outside the project (without
which uv would resolve the project BEFORE our retry code could run, which
is the exact defect this fixes).

The bootstrap is not part of the wheel (it ships only inside the .mcpb
zip), so it is loaded by file path rather than imported as a package.
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.util import win_job

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_PATH = REPO_ROOT / "mcpb" / "src" / "bootstrap.py"
MANIFEST_PATH = REPO_ROOT / "mcpb" / "manifest.json"


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap", BOOTSTRAP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── failure classification ──────────────────────────────────────────────────

_NO_SOLUTION_GE = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because only conexus<=7.24.1 is available and conexus-mcpb depends on
      conexus[local]>=7.25.0, we can conclude that conexus-mcpb's
      requirements are unsatisfiable.
"""

_NO_SOLUTION_EQ = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because there is no version of conexus==7.25.0 and you require
      conexus==7.25.0, we can conclude that your requirements are
      unsatisfiable.
"""


def test_no_solution_ge_is_propagation_class(bootstrap):
    assert bootstrap._is_resolution_unavailable(_NO_SOLUTION_GE) is True


def test_no_solution_eq_is_propagation_class(bootstrap):
    assert bootstrap._is_resolution_unavailable(_NO_SOLUTION_EQ) is True


def test_registry_miss_is_propagation_class(bootstrap):
    text = "error: Package `conexus` was not found in the package registry"
    assert bootstrap._is_resolution_unavailable(text) is True


def test_other_failures_are_not_retried_class(bootstrap):
    # Network down, permissions, disk: NOT the propagation window.
    assert bootstrap._is_resolution_unavailable("error: Permission denied (os error 13)") is False
    assert (
        bootstrap._is_resolution_unavailable(
            "error: Failed to fetch: `https://pypi.org/simple/conexus/`\n"
            "  Caused by: Connection reset by peer"
        )
        is False
    )


def test_no_solution_about_another_package_is_not_ours(bootstrap):
    text = "× No solution found when resolving dependencies: no version of leftpad==1.0"
    assert bootstrap._is_resolution_unavailable(text) is False


# ── retry loop bounds (injected runner/sleeper — no uv, no network) ─────────


def _proc(rc: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)


class _Runner:
    def __init__(self, procs):
        self.procs = list(procs)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self.procs.pop(0)


def test_immediate_success_never_sleeps(bootstrap):
    runner = _Runner([_proc(0)])
    sleeps: list[float] = []
    bootstrap._sync_with_retry("/bundle", run=runner, sleep=sleeps.append)
    assert sleeps == []
    assert runner.calls == [["uv", "sync", "--directory", "/bundle"]]


def test_propagation_failures_retry_with_backoff_then_succeed(bootstrap):
    runner = _Runner([_proc(1, _NO_SOLUTION_GE), _proc(1, _NO_SOLUTION_GE), _proc(0)])
    sleeps: list[float] = []
    bootstrap._sync_with_retry(
        "/bundle", run=runner, sleep=sleeps.append, sleeps=(60, 120, 240, 480)
    )
    assert sleeps == [60, 120]
    assert len(runner.calls) == 3


def test_retries_are_bounded_and_exhaustion_fails_loud(bootstrap, capsys):
    schedule = (1, 2, 3)
    runner = _Runner([_proc(1, _NO_SOLUTION_GE)] * (len(schedule) + 1))
    sleeps: list[float] = []
    with pytest.raises(SystemExit) as exc:
        bootstrap._sync_with_retry("/bundle", run=runner, sleep=sleeps.append, sleeps=schedule)
    assert exc.value.code == 1
    assert sleeps == [1, 2, 3]
    assert len(runner.calls) == len(schedule) + 1
    err = capsys.readouterr().err
    assert "PyPI" in err and "propagat" in err
    # uv's own output surfaces so the terminal failure is diagnosable.
    assert "No solution found" in err


def test_non_propagation_failure_fails_immediately(bootstrap, capsys):
    runner = _Runner([_proc(13, "error: Permission denied (os error 13)")])
    sleeps: list[float] = []
    with pytest.raises(SystemExit) as exc:
        bootstrap._sync_with_retry("/bundle", run=runner, sleep=sleeps.append)
    assert exc.value.code == 13
    assert sleeps == []
    assert len(runner.calls) == 1
    assert "Permission denied" in capsys.readouterr().err


def test_default_schedule_spans_the_measured_window(bootstrap):
    """The measured propagation window is ~10-25 min; a user typically lands
    mid-window. The default backoff must cover at least 10 minutes so the
    common case rides it out rather than exhausting early."""
    assert sum(bootstrap._RETRY_SLEEPS) >= 600


# ── manifest wiring ─────────────────────────────────────────────────────────


def test_manifest_launches_bootstrap_outside_the_project(bootstrap):
    """The launcher must not touch the bundle's project: a project-mode
    `uv run` resolves the bundle's deps BEFORE bootstrap.py executes, and the
    resolver failure then kills the extension before any retry code can run
    (the pre-r433b behavior). `uv tool run ... python` has no project at all,
    and, unlike the earlier `uv run --no-project`, never runs an interpreter
    from a `.venv` above the bundle (nexus-92gxf,
    tests/test_mcpb_launcher_venv.py)."""
    manifest = json.loads(MANIFEST_PATH.read_text())
    server = manifest["server"]
    assert server["entry_point"] == "src/bootstrap.py"
    assert server["mcp_config"]["command"] == "uv"
    assert server["mcp_config"]["args"] == [
        "tool",
        "run",
        "--directory",
        "${__dirname}",
        "--no-config",
        "--quiet",
        "--python",
        ">=3.12",
        "python",
        "src/bootstrap.py",
    ]


def test_bundle_ships_both_bootstrap_and_server(bootstrap):
    assert BOOTSTRAP_PATH.exists()
    # The exec target must still exist — bootstrap hands off to it.
    assert (REPO_ROOT / "mcpb" / "src" / "server.py").exists()
    # And .mcpbignore must not exclude either (they live in src/, only
    # caches and lockfiles are excluded).
    ignore = (REPO_ROOT / "mcpb" / ".mcpbignore").read_text()
    assert "server.py" not in ignore
    assert "bootstrap.py" not in ignore


def test_bootstrap_execs_uv_run_server(bootstrap, monkeypatch):
    """main() syncs then execs the real server through uv run (stdio must
    land on the server process for the MCP handshake — exec, not spawn)."""
    execs: list[list[str]] = []
    monkeypatch.setattr(bootstrap.os, "execvp", lambda prog, argv: execs.append([prog, *argv]))
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: None)
    monkeypatch.delenv("NX_MCPB_SKIP_RESOLVE_RETRY", raising=False)
    bootstrap.main(platform="linux")
    bundle = str(Path(BOOTSTRAP_PATH).parent.parent)
    assert execs == [["uv", "uv", "run", "--directory", bundle, "src/server.py"]]


def test_skip_env_bypasses_sync(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.os, "execvp", lambda prog, argv: None)
    called = []
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: called.append(d))
    monkeypatch.setenv("NX_MCPB_SKIP_RESOLVE_RETRY", "1")
    bootstrap.main(platform="linux")
    assert called == []


# ── Windows launch (nexus-ijue9.20, RDR-224 Gap 5) ──────────────────────────
#
# os.execvp on Windows spawns a child and exits the parent, so the host sees
# its MCP server die. Windows therefore runs the server as a child that
# inherits the bootstrap's own stdio handles. Every test below injects the
# platform, so both branches run on macOS and Linux.


def _exe(directory: Path, name: str, executable: bool = True) -> Path:
    path = directory / name
    path.write_text("")
    path.chmod(0o755 if executable else 0o644)
    return path


def test_windows_resolves_bare_name_through_pathext(bootstrap, tmp_path):
    uv = _exe(tmp_path, "uv.exe", executable=False)  # no exec bit on Windows
    got = bootstrap._resolve_executable(
        "uv", platform="win32", path=str(tmp_path), pathext=".com;.exe;.bat;.cmd"
    )
    assert got == str(uv)


def test_windows_pathext_order_decides_between_candidates(bootstrap, tmp_path):
    _exe(tmp_path, "uv.cmd")
    exe = _exe(tmp_path, "uv.exe")
    assert bootstrap._resolve_executable(
        "uv", platform="win32", path=str(tmp_path), pathext=".exe;.cmd"
    ) == str(exe)
    assert bootstrap._resolve_executable(
        "uv", platform="win32", path=str(tmp_path), pathext=".cmd;.exe"
    ) == str(tmp_path / "uv.cmd")


def test_windows_name_with_extension_and_path_order(bootstrap, tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    _exe(second, "uv.exe")
    want = _exe(first, "uv.exe")
    search = ";".join([str(first), str(second)])
    assert bootstrap._resolve_executable(
        "uv.exe", platform="win32", path=search, pathext=".exe"
    ) == str(want)
    assert bootstrap._resolve_executable(
        "uv", platform="win32", path=search, pathext=".exe"
    ) == str(want)


def test_windows_resolution_misses_return_none_and_skip_cwd(bootstrap, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _exe(tmp_path, "uv.exe")
    monkeypatch.chdir(tmp_path)  # a planted uv.exe in cwd must not win
    assert (
        bootstrap._resolve_executable("uv", platform="win32", path=str(elsewhere), pathext=".exe")
        is None
    )
    assert bootstrap._resolve_executable("nope", platform="win32", path="", pathext=".exe") is None


def test_windows_explicit_path_is_used_as_given_not_searched(bootstrap, tmp_path):
    exe = _exe(tmp_path, "server.exe")
    assert bootstrap._resolve_executable(str(exe), platform="win32", path="") == str(exe)
    assert bootstrap._resolve_executable(str(tmp_path / "gone.exe"), platform="win32", path="") is None


def test_posix_resolution_ignores_pathext_and_needs_exec_bit(bootstrap, tmp_path):
    _exe(tmp_path, "uv.exe")
    _exe(tmp_path, "tool", executable=False)
    assert bootstrap._resolve_executable("uv", platform="linux", path=str(tmp_path)) is None
    assert bootstrap._resolve_executable("tool", platform="linux", path=str(tmp_path)) is None
    plain = _exe(tmp_path, "uv")
    assert bootstrap._resolve_executable("uv", platform="linux", path=str(tmp_path)) == str(plain)


def test_launch_dispatches_on_platform(bootstrap):
    execs, wins = [], []
    argv = ["uv", "run", "x"]
    bootstrap._launch(
        argv,
        platform="linux",
        execvp=lambda p, a: execs.append((p, a)),
        launch_windows=lambda a: wins.append(a),
    )
    assert execs == [("uv", argv)] and wins == []
    execs.clear()
    rc = bootstrap._launch(
        argv,
        platform="win32",
        execvp=lambda p, a: execs.append((p, a)),
        launch_windows=lambda a: (wins.append(a), 5)[1],
    )
    assert execs == [] and wins == [argv] and rc == 5


class _Child:
    pid = 4242

    def __init__(self, codes):
        self.codes = list(codes)
        self.terminated = False

    def wait(self):
        code = self.codes.pop(0)
        if isinstance(code, BaseException):
            raise code
        return code

    def terminate(self):
        self.terminated = True


def _windows_launch(bootstrap, child, job=None, resolve=lambda n: "C:/uv/uv.exe"):
    calls = []

    def popen(cmd, **kw):
        calls.append((cmd, kw))
        return child

    rc = bootstrap._launch_windows(
        ["uv", "run", "src/server.py"], popen=popen, make_job=lambda: job, resolve=resolve
    )
    return rc, calls


def test_windows_launch_inherits_stdio_and_propagates_exit_code(bootstrap):
    rc, calls = _windows_launch(bootstrap, _Child([7]))
    assert rc == 7
    ((cmd, kw),) = calls
    assert cmd == ["C:/uv/uv.exe", "run", "src/server.py"]
    # The handles the host gave THIS process, by fd; never a PIPE relay.
    assert (kw["stdin"], kw["stdout"], kw["stderr"]) == (0, 1, 2)
    assert subprocess.PIPE not in kw.values()


def test_windows_launch_missing_uv_fails_loud_without_spawning(bootstrap, capsys):
    rc, calls = _windows_launch(bootstrap, _Child([0]), resolve=lambda n: None)
    assert rc == 127 and calls == []
    assert "uv" in capsys.readouterr().err


def test_windows_launch_ctrl_c_terminates_child(bootstrap):
    child = _Child([KeyboardInterrupt(), 1])
    rc, _ = _windows_launch(bootstrap, child)
    assert rc == 1 and child.terminated


class _Job:
    def __init__(self, self_ok=True, pid_ok=True):
        self.events = []
        self.self_ok, self.pid_ok = self_ok, pid_ok

    def assign_self(self):
        self.events.append("self")
        return self.self_ok

    def assign_pid(self, pid):
        self.events.append(("pid", pid))
        return self.pid_ok


def test_job_holds_this_process_before_the_child_is_spawned(bootstrap):
    job = _Job()
    order = []
    job.assign_self = lambda: order.append("assign_self") or True

    def popen(cmd, **kw):
        order.append("spawn")
        return _Child([0])

    bootstrap._launch_windows(["uv"], popen=popen, make_job=lambda: job, resolve=lambda n: n)
    assert order == ["assign_self", "spawn"]


def test_job_falls_back_to_assigning_the_child(bootstrap, capsys):
    job = _Job(self_ok=False)
    _windows_launch(bootstrap, _Child([0]), job=job)
    assert job.events == ["self", ("pid", 4242)]
    assert capsys.readouterr().err == ""


def test_job_failure_is_named_on_stderr(bootstrap, capsys):
    _windows_launch(bootstrap, _Child([0]), job=_Job(self_ok=False, pid_ok=False))
    assert "outlive" in capsys.readouterr().err


def test_main_on_windows_resolves_uv_via_pathext_and_exits_with_child_code(
    bootstrap, tmp_path, monkeypatch
):
    _exe(tmp_path, "uv.exe", executable=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".exe;.cmd")
    monkeypatch.delenv("NX_MCPB_SKIP_RESOLVE_RETRY", raising=False)
    synced = []
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d, uv="uv": synced.append(uv))
    monkeypatch.setattr(bootstrap, "_launch_windows", lambda argv: 9)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(platform="win32")
    assert exc.value.code == 9
    assert synced == [str(tmp_path / "uv.exe")]


def test_sync_uses_the_injected_uv_path(bootstrap):
    runner = _Runner([_proc(0)])
    bootstrap._sync_with_retry("/bundle", run=runner, sleep=lambda s: None, uv="C:/uv/uv.exe")
    assert runner.calls == [["C:/uv/uv.exe", "sync", "--directory", "/bundle"]]


# -- stdio survives the handoff: a REAL subprocess round trip, both branches --

_DRIVER = """\
import importlib.util, sys
spec = importlib.util.spec_from_file_location("mcpb_bootstrap", {bootstrap!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
code = mod._launch([sys.executable, {child!r}, "7"], platform={platform!r})
sys.exit(code)
"""

_ECHO_CHILD = """\
import sys
for line in sys.stdin:
    line = line.rstrip("\\n")
    if line == "quit":
        sys.exit(int(sys.argv[1]))
    sys.stdout.write("echo:" + line + "\\n")
    sys.stdout.flush()
"""


@pytest.mark.parametrize(
    "platform",
    [
        pytest.param(
            "linux",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="exec branch needs POSIX"),
        ),
        "win32",
    ],
)
def test_stdio_and_exit_code_survive_the_handoff(tmp_path, platform):
    """The host's pipes land on the server: bytes written to the bootstrap's
    stdin come back from the child on the bootstrap's stdout, and the child's
    exit code is the bootstrap's. exec on POSIX; child-with-inherited-handles
    (the Windows branch, injected) otherwise."""
    child = tmp_path / "echo_child.py"
    child.write_text(_ECHO_CHILD)
    driver = tmp_path / "driver.py"
    driver.write_text(
        _DRIVER.format(bootstrap=str(BOOTSTRAP_PATH), child=str(child), platform=platform)
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        proc.stdin.write("hello\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == "echo:hello\n"
        proc.stdin.write("again\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == "echo:again\n"
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        assert proc.wait(timeout=30) == 7
        assert proc.stderr.read() == ""
    finally:
        proc.kill()
        proc.wait()
        for f in (proc.stdin, proc.stdout, proc.stderr):
            f.close()


# -- Job Object: shape test against a fake kernel32, plus drift pin ---------


class _FakeKernel32:
    def __init__(self):
        self.calls = []
        self.limit_flags = None
        self.info_class = None

    def CreateJobObjectW(self, attrs, name):
        self.calls.append("CreateJobObjectW")
        return 77

    def SetInformationJobObject(self, handle, info_class, ptr, size):
        self.calls.append("SetInformationJobObject")
        self.info_class = info_class
        self.limit_flags = ptr.contents.BasicLimitInformation.LimitFlags
        self.size = size
        return 1

    def GetCurrentProcess(self):
        return -1

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, pid))
        return 99

    def AssignProcessToJobObject(self, job, proc):
        self.calls.append(("Assign", job, proc))
        return 1

    def CloseHandle(self, h):
        self.calls.append(("CloseHandle", h))
        return 1


def test_job_is_created_kill_on_close_and_assigns_self_and_pid(bootstrap):
    k = _FakeKernel32()
    job = bootstrap._KillOnCloseJob(kernel32=k)
    assert k.info_class == 9
    assert k.limit_flags == 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert job.assign_self() is True
    assert ("Assign", 77, -1) in k.calls
    assert job.assign_pid(555) is True
    assert ("OpenProcess", 0x0101, 555) in k.calls
    assert ("Assign", 77, 99) in k.calls
    assert ("CloseHandle", 99) in k.calls
    # The job handle is held for the life of the process, never closed here.
    assert ("CloseHandle", 77) not in k.calls


@pytest.mark.skipif(sys.platform == "win32", reason="asserts the off-Windows degrade")
def test_make_job_is_none_off_windows(bootstrap):
    assert bootstrap._make_job() is None


def test_job_constants_and_layout_match_nexus_win_job(bootstrap):
    """bootstrap.py cannot import nexus (it runs before conexus is installed),
    so it carries its own copy of nexus.util.win_job's Job Object constants and
    struct. They must not drift."""
    assert bootstrap._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == win_job._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert (
        bootstrap._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
        == win_job._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
    )
    assert bootstrap._PROCESS_SET_QUOTA == win_job._PROCESS_SET_QUOTA
    assert bootstrap._PROCESS_TERMINATE == win_job._PROCESS_TERMINATE
    ours = bootstrap._job_struct()
    theirs = win_job._JobObjectExtendedLimitInformation
    assert ctypes.sizeof(ours) == ctypes.sizeof(theirs)

    def flat(struct, prefix=""):
        out = []
        for name, typ in struct._fields_:
            if hasattr(typ, "_fields_"):
                out.extend(flat(typ, prefix + name + "."))
            else:
                out.append((prefix + name, typ.__name__))
        return out

    assert flat(ours) == flat(theirs)


@pytest.mark.skipif(sys.platform != "win32", reason="real Job Object needs Windows")
def test_real_job_object_is_created_on_windows(bootstrap):
    job = bootstrap._make_job()
    assert job is not None, "Job Object creation failed on a real Windows host"


# ── manifest platform gate ──────────────────────────────────────────────────


def test_manifest_admits_windows_from_the_windows_client_release(bootstrap):
    """The Windows client release (nexus-f9bgu.43) lifts the gate: the
    service starts on Windows (RDR-224 Phase 3) and engine-service-v0.1.149
    publishes the windows-x64 engine and PG bundle, so "win32" (the Claude
    Desktop process.platform token) joins the platforms. Before that release
    the gate stayed shut because a win32 bundle would install and then fail."""
    platforms = json.loads(MANIFEST_PATH.read_text())["compatibility"]["platforms"]
    assert sorted(platforms) == ["darwin", "linux", "win32"]
    assert callable(bootstrap._launch_windows) and callable(bootstrap._resolve_executable)
