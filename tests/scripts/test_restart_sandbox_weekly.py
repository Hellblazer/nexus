# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/restart-sandbox-weekly.sh (Sam, 2026-11-18): the weekly sandbox run for
claude-restart-sessions, and the launchd job that schedules it.

The real harness drives Claude Code sessions for about 17 minutes, so these tests give the
wrapper a stand-in repo whose tests/e2e/restart-sessions/run.sh does what FAKE_HARNESS says
(record a pass, exit with a code, sleep, trap TERM), a scratch HOME, and a stand-in launchctl
on PATH. They check what the wrapper itself decides: when it runs, that it never runs twice at
once, what it records, how it fails, and the plist it installs.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "restart-sandbox-weekly.sh"

# The stand-in harness. FAKE_HARNESS_RECORD=1 writes a new pass record (as a full pass does);
# FAKE_HARNESS_SLEEP holds it running; a TERM is noted in $HOME/harness-term and ends it.
HARNESS = """#!/usr/bin/env bash
trap 'kill "$nap" 2>/dev/null; echo term >> "$HOME/harness-term"; exit 143' TERM
echo "ran in tempdir $TMPDIR with path $PATH" >> "$HOME/harness-calls"
if [ -n "${FAKE_HARNESS_SLEEP:-}" ]; then sleep "$FAKE_HARNESS_SLEEP" & nap=$!; wait "$nap"; fi
if [ "${FAKE_HARNESS_RECORD:-0}" = 1 ]; then
  mkdir -p "$HOME/.local/state/claude-restart-sessions"
  printf '{"passed_at": "%s"}\\n' "$(date -u +%Y-%m-%dT%H:%M:%S.$$Z)" > "$HOME/.local/state/claude-restart-sessions/sandbox-pass.json"
fi
exit "${FAKE_HARNESS_EXIT:-0}"
"""


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    harness = repo / "tests" / "e2e" / "restart-sessions" / "run.sh"
    harness.parent.mkdir(parents=True)
    harness.write_text(HARNESS)
    harness.chmod(0o755)
    (repo / "scripts").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / SCRIPT.name)
    tmp = tmp_path / "sanhell-tmp"
    tmp.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text('#!/usr/bin/env bash\necho "$*" >> "$HOME/launchctl-calls"\n'
                         'exit "${FAKE_LAUNCHCTL_EXIT:-0}"\n')
    launchctl.chmod(0o755)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n")
    return {"home": home, "repo": repo, "tmp": tmp, "bin": bin_dir, "agents": tmp_path / "LaunchAgents"}


def _env(box: dict[str, Path], **env: str) -> dict[str, str]:
    return {"HOME": str(box["home"]), "NX_REPO": str(box["repo"]), "NX_SANDBOX_TMPDIR": str(box["tmp"]),
            "NX_LAUNCH_AGENTS": str(box["agents"]), "NX_SANDBOX_VOLUME_WAIT_S": "0", "FAKE_HARNESS_RECORD": "1",
            "PATH": f"{box['bin']}:/usr/bin:/bin", **env}


def _run(box: dict[str, Path], *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], env=_env(box, **env), capture_output=True, text=True, timeout=120)


def _start(box: dict[str, Path], **env: str) -> subprocess.Popen[str]:
    return subprocess.Popen(["bash", str(SCRIPT)], env=_env(box, **env), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def _state(box: dict[str, Path]) -> Path:
    return box["home"] / ".local" / "state" / "claude-restart-sessions"


def _last(box: dict[str, Path]) -> dict:
    return json.loads((_state(box) / "sandbox-weekly-last.json").read_text())


def _skip(box: dict[str, Path]) -> dict:
    return json.loads((_state(box) / "sandbox-weekly-skip.json").read_text())


def _log(box: dict[str, Path]) -> str:
    return (_state(box) / "sandbox-weekly.log").read_text()


def _calls(box: dict[str, Path], name: str) -> list[str]:
    path = box["home"] / name
    return path.read_text().splitlines() if path.exists() else []


def _wait_for(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.05)


def _snapshot(root: Path) -> dict[str, tuple[int, float]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime) for p in root.rglob("*")}


# ── a run ─────────────────────────────────────────────────────────────────────────────

def test_a_passing_run_records_a_new_pass_under_the_sanhell_tmpdir(box) -> None:
    r = _run(box)
    assert r.returncode == 0, r.stderr
    (call,) = _calls(box, "harness-calls")
    assert f"tempdir {box['tmp']} " in call
    last = _last(box)
    assert last["status"] == "passed" and last["exit"] == 0 and last["recorded"] is True
    assert last["started"] <= last["ended"]
    assert "START:" in _log(box) and "END: exit 0" in _log(box)
    assert not os.path.lexists(_state(box) / "sandbox-weekly.lock")


def test_the_run_puts_claude_and_homebrew_on_path_for_launchd(box) -> None:
    """launchd starts a job with a bare PATH; claude, nx, uv and tmux are in these."""
    assert _run(box).returncode == 0
    (call,) = _calls(box, "harness-calls")
    path = call.split("with path ", 1)[1].split(":")
    assert f"{box['home']}/.local/bin" in path and "/opt/homebrew/bin" in path


def test_a_run_leaves_the_operators_claude_dir_untouched(box) -> None:
    claude_dir = box["home"] / ".claude"
    before = _snapshot(claude_dir)
    assert _run(box).returncode == 0
    assert _snapshot(claude_dir) == before


def test_a_failing_harness_is_recorded_reported_and_its_code_returned(box) -> None:
    r = _run(box, FAKE_HARNESS_EXIT="1")
    assert r.returncode == 1
    assert _last(box)["status"] == "failed" and _last(box)["recorded"] is False
    assert "the harness exited 1" in r.stderr and "END: exit 1" in _log(box)


def test_exit_zero_without_a_new_pass_is_not_a_pass(box) -> None:
    """The harness exits 0 after printing 'pass NOT recorded' (Claude Code updated mid-run)."""
    r = _run(box, FAKE_HARNESS_RECORD="0")
    assert r.returncode == 3
    assert _last(box)["status"] == "not-recorded" and "recorded no new pass" in r.stderr


def test_a_missing_tmpdir_fails_loudly_and_runs_nothing(box) -> None:
    r = _run(box, NX_SANDBOX_TMPDIR=str(box["tmp"] / "absent"))
    assert r.returncode == 2
    assert _calls(box, "harness-calls") == []
    assert "TMPDIR" in r.stderr and "is missing" in r.stderr
    assert _last(box)["status"] == "not-run" and "FAIL: TMPDIR" in _log(box)


def test_a_tmpdir_that_appears_while_waiting_is_used(box) -> None:
    late = box["tmp"] / "late"
    p = _start(box, NX_SANDBOX_TMPDIR=str(late), NX_SANDBOX_VOLUME_WAIT_S="30")
    time.sleep(1)
    late.mkdir()
    assert p.wait(timeout=60) == 0
    assert f"tempdir {late} " in _calls(box, "harness-calls")[0]


def test_a_harness_past_the_limit_is_stopped_and_reported(box) -> None:
    r = _run(box, FAKE_HARNESS_SLEEP="60", NX_SANDBOX_LIMIT_S="1")
    assert r.returncode == 124
    assert _calls(box, "harness-term") == ["term"]
    assert _last(box)["status"] == "timeout" and "ran past 1 s" in r.stderr
    assert not os.path.lexists(_state(box) / "sandbox-weekly.lock")


def test_a_term_to_the_wrapper_stops_the_harness_before_the_lock_goes(box) -> None:
    p = _start(box, FAKE_HARNESS_SLEEP="60")
    lock = _state(box) / "sandbox-weekly.lock"
    _wait_for(lambda: _calls(box, "harness-calls"))
    assert os.readlink(lock) == str(p.pid)
    p.terminate()
    assert p.wait(timeout=30) == 143
    assert _calls(box, "harness-term") == ["term"]
    assert not os.path.lexists(lock) and _last(box)["status"] == "failed"


# ── the lock ──────────────────────────────────────────────────────────────────────────

def test_two_runs_started_together_run_the_harness_once(box) -> None:
    runs = [_start(box, FAKE_HARNESS_SLEEP="3") for _ in range(2)]
    codes = sorted(p.wait(timeout=60) for p in runs)
    assert codes == [0, 0]
    assert len(_calls(box, "harness-calls")) == 1
    assert "SKIP:" in _log(box)


def _holder(argv0: str) -> subprocess.Popen[bytes]:
    """A live process whose command line is *argv0* (the lock checks the holder is this script)."""
    return subprocess.Popen(["bash", "-c", f"exec -a {argv0} sleep 60"])


def test_a_live_holder_skips_the_run_and_keeps_its_lock(box) -> None:
    holder = _holder("restart-sandbox-weekly.sh")
    try:
        lock = _state(box) / "sandbox-weekly.lock"
        lock.parent.mkdir(parents=True)
        os.symlink(str(holder.pid), lock)
        r = _run(box)
        assert r.returncode == 0 and _calls(box, "harness-calls") == []
        assert _skip(box)["status"] == "skipped" and "SKIP: another weekly run" in _log(box)
        assert os.readlink(lock) == str(holder.pid)  # never removed from under its holder
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("kind", ["dead-pid", "recycled-pid", "not-a-pid"])
def test_a_stale_lock_is_taken_over(box, kind) -> None:
    """A dead pid, a live pid that is not this script (recycled after a reboot), or junk."""
    lock = _state(box) / "sandbox-weekly.lock"
    lock.parent.mkdir(parents=True)
    other = None
    if kind == "dead-pid":
        gone = subprocess.Popen(["true"])
        gone.wait()
        target = str(gone.pid)
    elif kind == "recycled-pid":
        other = _holder("some-other-program")
        target = str(other.pid)
    else:
        target = "not-a-pid"
    os.symlink(target, lock)
    try:
        r = _run(box)
        assert r.returncode == 0 and len(_calls(box, "harness-calls")) == 1
        assert "taking it over" in _log(box)
        assert not os.path.lexists(lock)
        if other is not None:
            assert other.poll() is None  # a recycled pid is never signalled
    finally:
        if other is not None:
            other.kill()
            other.wait()


def test_a_lock_past_the_limit_is_taken_over_and_its_holder_stopped(box) -> None:
    holder = _holder("restart-sandbox-weekly.sh")
    try:
        lock = _state(box) / "sandbox-weekly.lock"
        lock.parent.mkdir(parents=True)
        os.symlink(str(holder.pid), lock)
        old = time.time() - 2 * 3600
        os.utime(lock, (old, old), follow_symlinks=False)
        r = _run(box, NX_SANDBOX_LIMIT_S="60")  # 60 s + 15 min, against a 2 h old lock
        assert r.returncode == 0 and len(_calls(box, "harness-calls")) == 1
        assert "past the limit; stopping it" in _log(box)
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


# ── when it does not run ──────────────────────────────────────────────────────────────

def test_a_recent_restart_skips_the_run(box) -> None:
    (box["home"] / ".claude" / "restart-sessions.log").write_text("12:00:00 RESTART\n")
    r = _run(box)
    assert r.returncode == 0 and _calls(box, "harness-calls") == []
    assert _skip(box)["status"] == "skipped" and "SKIP:" in _log(box)
    assert not os.path.lexists(_state(box) / "sandbox-weekly.lock")


def test_a_skip_never_overwrites_the_last_real_result(box) -> None:
    """The restart's warning reads sandbox-weekly-last.json; a skip must not hide a pass or a failure."""
    assert _run(box).returncode == 0
    passed = _last(box)
    assert passed["status"] == "passed"
    (box["home"] / ".claude" / "restart-sessions.log").write_text("12:00:00 RESTART\n")
    assert _run(box).returncode == 0
    assert _last(box) == passed and _skip(box)["status"] == "skipped"


def test_the_record_carries_the_claude_code_version(box) -> None:
    """The wrapper puts $HOME/.local/bin first on PATH, where Claude Code's native install lives."""
    claude = box["home"] / ".local" / "bin" / "claude"
    claude.parent.mkdir(parents=True)
    claude.write_text("#!/usr/bin/env bash\necho '2.1.399 (Claude Code)'\n")
    claude.chmod(0o755)
    assert _run(box).returncode == 0
    assert _last(box)["claude_version"] == "2.1.399"


def test_an_old_restart_log_does_not_skip_the_run(box) -> None:
    log = box["home"] / ".claude" / "restart-sessions.log"
    log.write_text("12:00:00 RESTART\n")
    old = time.time() - 3600
    os.utime(log, (old, old))
    assert _run(box).returncode == 0 and len(_calls(box, "harness-calls")) == 1


# ── the launchd job ───────────────────────────────────────────────────────────────────

def test_the_plist_is_valid_and_runs_the_primary_checkouts_copy_weekly(box) -> None:
    r = _run(box, "--print-plist")
    assert r.returncode == 0
    plist = plistlib.loads(r.stdout.encode())
    assert plist["Label"] == "com.nexus.restart-sandbox-weekly"
    assert plist["ProgramArguments"] == ["/bin/bash", str(box["repo"] / "scripts" / "restart-sandbox-weekly.sh")]
    assert plist["StartCalendarInterval"] == {"Weekday": 0, "Hour": 3, "Minute": 30}
    assert plist["RunAtLoad"] is False and plist["EnvironmentVariables"] == {"NX_REPO": str(box["repo"])}


def test_install_writes_the_plist_and_bootstraps_it(box) -> None:
    r = _run(box, "--install")
    assert r.returncode == 0, r.stderr
    path = box["agents"] / "com.nexus.restart-sandbox-weekly.plist"
    assert plistlib.loads(path.read_bytes())["Label"] == "com.nexus.restart-sandbox-weekly"
    uid = os.getuid()
    assert _calls(box, "launchctl-calls") == [f"bootout gui/{uid}/com.nexus.restart-sandbox-weekly",
                                              f"bootstrap gui/{uid} {path}"]
    assert "launchctl kickstart" in r.stdout
    assert _calls(box, "harness-calls") == []  # installing runs nothing


def test_install_retries_then_reports_a_failed_bootstrap(box) -> None:
    r = _run(box, "--install", FAKE_LAUNCHCTL_EXIT="5")
    assert r.returncode == 1 and "bootstrap failed" in r.stderr
    assert sum(c.startswith("bootstrap ") for c in _calls(box, "launchctl-calls")) == 2


def test_uninstall_unloads_and_removes_the_plist(box) -> None:
    assert _run(box, "--install").returncode == 0
    r = _run(box, "--uninstall")
    assert r.returncode == 0
    assert not (box["agents"] / "com.nexus.restart-sandbox-weekly.plist").exists()
    assert _calls(box, "launchctl-calls")[-1] == f"bootout gui/{os.getuid()}/com.nexus.restart-sandbox-weekly"


def test_an_unknown_argument_prints_the_usage_and_runs_nothing(box) -> None:
    r = _run(box, "--bogus")
    assert r.returncode == 2 and "--install" in r.stdout and "SPDX" not in r.stdout
    assert _calls(box, "harness-calls") == []
