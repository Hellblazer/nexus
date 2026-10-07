# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/watched_pytest.py`` turns a hung or killed remote run into a loud, named failure (nexus-hlvg1).

Each case runs the real script over a real pytest subprocess on a generated test file.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "watched_pytest.py"
spec = importlib.util.spec_from_file_location("watched_pytest", SCRIPT)
assert spec is not None and spec.loader is not None
wp = importlib.util.module_from_spec(spec)
sys.modules["watched_pytest"] = wp  # dataclass field types resolve through sys.modules
spec.loader.exec_module(wp)

_HANG = '''
import os, subprocess, sys, time
def test_fast():
    pass
def test_hangs():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.environ["GRANDCHILD_PID_FILE"], "w").write(str(child.pid))
    time.sleep(120)
'''


def _watch(tmp_path: Path, body: str, *extra: str, env: dict[str, str] | None = None) -> tuple[int, dict, str]:
    (tmp_path / "test_case.py").write_text(body)
    status = tmp_path / "status.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--log", str(tmp_path / "run.log"), "--status", str(status),
         "--cwd", str(tmp_path), "--no-notify", *extra, "--",
         sys.executable, "-m", "pytest", "test_case.py", "-p", "no:cacheprovider", "-o", "addopts="],
        capture_output=True, text=True, timeout=120, env={**os.environ, **(env or {})},
    )
    return proc.returncode, json.loads(status.read_text()), proc.stdout


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_passing_run_reports_passed_and_exits_zero(tmp_path: Path) -> None:
    code, status, out = _watch(tmp_path, "def test_ok():\n    pass\n")
    assert (code, status["state"], status["exit_code"]) == (0, "passed", 0), out
    assert "-v" in status["command"]


def test_a_failing_run_keeps_pytests_exit_code(tmp_path: Path) -> None:
    code, status, _ = _watch(tmp_path, "def test_bad():\n    assert False\n")
    assert (code, status["state"]) == (1, "failed")


@pytest.mark.skipif(sys.platform == "win32", reason="the grandchild probe uses os.kill(pid, 0), which is TerminateProcess on Windows")
def test_a_hang_is_killed_with_its_tree_and_the_test_is_named(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    t0 = time.monotonic()
    code, status, out = _watch(tmp_path, _HANG, "--stall", "3", "--faulthandler-timeout", "1",
                               env={"GRANDCHILD_PID_FILE": str(pid_file)})
    assert time.monotonic() - t0 < 30, "the watchdog must fire in seconds, not wait for the test"
    assert (code, status["state"]) == (wp.EXIT_STALLED, "stalled"), out
    assert status["current_test"] == "test_case.py::test_hangs"
    assert "test_case.py::test_hangs" in out
    log = (tmp_path / "run.log").read_text()
    assert "Timeout (0:00:01)!" in log, "faulthandler must dump the stacks before the kill"
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), "the kill must take the whole process tree"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX arm: a raw SIGINT death stands in for the Windows console break")
def test_a_run_killed_by_a_console_ctrl_event_is_named_as_that(tmp_path: Path) -> None:
    body = ("import os, signal\ndef test_breaks():\n"
            "    signal.signal(signal.SIGINT, signal.SIG_DFL)\n    os.kill(os.getpid(), signal.SIGINT)\n")
    code, status, out = _watch(tmp_path, body)
    assert (code, status["state"]) == (wp.EXIT_CONSOLE_BREAK, "console_break"), out
    assert status["current_test"] == "test_case.py::test_breaks"


@pytest.mark.parametrize("code", [0xC000013A, 0xC000013A - (1 << 32)])
def test_windows_status_control_c_exit_is_a_console_break(code: int) -> None:
    assert wp.classify(code, stalled=False, over_total=False) == ("console_break", wp.EXIT_CONSOLE_BREAK)


def test_max_total_caps_a_run_that_keeps_talking(tmp_path: Path) -> None:
    body = "import time\ndef test_chatty():\n    for _ in range(600):\n        print('x', flush=True)\n        time.sleep(0.1)\n"
    code, status, _ = _watch(tmp_path, body, "--max-total", "3", "--stall", "30")
    assert (code, status["state"]) == (wp.EXIT_MAX_TOTAL, "timeout")


def test_quiet_output_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="-q"):
        wp.build_command(["pytest", "-q"], 10)


def test_the_watcher_ignores_console_ctrl_only_on_windows() -> None:
    """On Windows the watcher must outlive the break that killed the run, or nobody
    reports it. On POSIX it must not ignore SIGINT: the child would inherit that."""
    before = signal.getsignal(signal.SIGINT)
    try:
        wp._ignore_console_ctrl()
        ignored = signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        assert ignored is (sys.platform == "win32")
    finally:
        signal.signal(signal.SIGINT, before)


def test_the_hung_test_is_tracked_from_v_lines() -> None:

    watch = wp._Watch(io.StringIO())
    watch.feed("t.py::test_a PASSED                                     [ 50%]\n")
    assert watch.current_test is None
    watch.feed("t.py::test_b \n")
    assert watch.current_test == "t.py::test_b"
    watch.feed("t.py::test_b FAILED                                     [100%]\n")
    assert watch.current_test is None
    # pytest leaves the line open while the test runs: the hang sits there.
    watch.feed("t.py::test_c ")
    assert watch.current_test == "t.py::test_c"


def _status(state: str) -> "wp.Status":
    return wp.Status(state=state, command=["pytest"], started_at=0.0, elapsed_s=12.0,
                     exit_code=124, current_test="t.py::test_hangs", detail="no output for 180s",
                     tail=["a", "b", "t.py::test_hangs "])


def test_an_ntfy_url_publishes_text_with_ntfy_headers_over_https() -> None:
    req = wp.notify_request("ntfy://ntfy.example/topic-x?title=Beszel&click=http://hub:8090",
                            _status("stalled"), "qwentescence")
    assert req.full_url == "https://ntfy.example/topic-x"
    assert req.get_method() == "POST"
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["title"] == "Beszel: qwentescence: pytest stalled"
    assert headers["priority"] == "5" and "rotating_light" in headers["tags"]
    assert headers["click"] == "http://hub:8090"
    body = req.data.decode()
    assert "t.py::test_hangs" in body and "no output for 180s" in body
    assert "\na\n" not in f"\n{body}\n", "a named test needs no stack tail in the push"


def test_a_pass_is_low_priority_and_carries_no_tail() -> None:
    req = wp.notify_request("ntfy://ntfy.example/t", _status("passed"), "h")
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["priority"] == "2" and "click" not in headers
    assert "t.py::test_hangs " not in req.data.decode().splitlines()


def test_any_other_url_gets_json() -> None:
    req = wp.notify_request("https://hooks.example/x", _status("failed"), "h")
    payload = json.loads(req.data)
    assert payload["state"] == "failed" and payload["title"] == "h: pytest failed"


def test_the_notify_url_resolves_flag_then_env_then_host_file(tmp_path: Path) -> None:
    config = tmp_path / "notify-url"
    assert wp.resolve_notify_url(None, env={}, config=config) is None
    config.write_text("ntfy://from-file/t\n")
    assert wp.resolve_notify_url(None, env={}, config=config) == "ntfy://from-file/t"
    env = {"WATCHED_PYTEST_NOTIFY_URL": "ntfy://from-env/t"}
    assert wp.resolve_notify_url(None, env=env, config=config) == "ntfy://from-env/t"
    assert wp.resolve_notify_url("ntfy://flag/t", env=env, config=config) == "ntfy://flag/t"


def test_a_failed_notify_never_echoes_the_private_topic() -> None:
    problem = wp._notify("ntfy://127.0.0.1:1/secret-topic", _status("stalled"))
    assert problem is not None and "secret-topic" not in problem
