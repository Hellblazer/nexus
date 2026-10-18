# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Decision-logic tests for infra/hellmini/watchdog/actions-runner-watchdog (nexus-ey1me).

The script runs as root on hellmini and restarts the runner when its listener is
alive but stalled. Here it runs for real (bash) against a fake runner directory,
a tmp state directory, and stand-in executables for pgrep, ps, lsof, launchctl
and date found first on WD_PATH. The clock is fixed through the date stand-in, so
every threshold is exercised exactly at, and one second either side of, its
boundary. Log mtimes are real (os.utime). One test drives the real lsof against
a real socket to pin the output parsing; the real-pgrep tests spawn processes
with forged argv (bash exec -a) and run the REAL pgrep, since a stub that ignores
its flags would let `-f` or `-u` be deleted unnoticed.

Set WD_TEST_SCRIPT to point the suite at a mutated copy of the script.

macOS only, by design: the script uses BSD stat, ps, lsof and pgrep and runs only
on hellmini. The module skips everywhere else (CI is Linux), so these tests run
on a Mac dev box or on hellmini itself, not in a CI lane. A skip is therefore the
normal result off-Mac; `pytest -rs` names it.
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS-only by design: the watchdog uses BSD stat/ps/lsof/pgrep and runs only on hellmini; no CI lane runs it",
)

REPO_SCRIPT = Path(__file__).resolve().parent.parent / "infra" / "hellmini" / "watchdog" / "actions-runner-watchdog"
SCRIPT = Path(os.environ.get("WD_TEST_SCRIPT") or REPO_SCRIPT)

NOW = 1_800_000_000
LISTENER_PID = 4141
TARGET = "system/test.runner"

STUBS = {
    "pgrep": r"""#!/bin/sh
for a; do pat="$a"; done
echo "$*" >> "$CTL/pgrep.calls"
case "$pat" in
  *Runner.Listener*)
    echo "$pat" >> "$CTL/pgrep.patterns"
    [ -f "$CTL/listener_rc" ] && exit "$(cat "$CTL/listener_rc")"
    n=$(cat "$CTL/listener_calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$CTL/listener_calls"
    if [ "$n" -ge 2 ] && [ -f "$CTL/listener_pid_after_first" ]; then cat "$CTL/listener_pid_after_first"; exit 0; fi
    [ -f "$CTL/listener_pid" ] || exit 1
    cat "$CTL/listener_pid"; exit 0 ;;
  *Runner.Worker*)
    echo "$pat" >> "$CTL/pgrep.patterns"
    [ -f "$CTL/worker_rc" ] && exit "$(cat "$CTL/worker_rc")"
    n=$(cat "$CTL/worker_calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$CTL/worker_calls"
    if [ -f "$CTL/touch_log_on_call" ] && [ "$n" -eq "$(cat "$CTL/touch_log_on_call")" ]; then
      touch "$(cat "$CTL/log_path")"
    fi
    if [ -f "$CTL/worker_from_call" ] && [ "$n" -ge "$(cat "$CTL/worker_from_call")" ]; then echo 4242; exit 0; fi
    exit 1 ;;
esac
exit 1
""",
    "ps": '#!/bin/sh\necho "$*" >> "$CTL/ps.calls"\necho "  $(cat "$CTL/etime")"\n',
    "date": '#!/bin/sh\nif [ "$1" = "+%s" ]; then cat "$CTL/now"; else exec /bin/date "$@"; fi\n',
    "lsof": r"""#!/bin/sh
echo "$@" >> "$CTL/lsof.calls"
case "$(cat "$CTL/lsof_mode")" in
  fail) echo "lsof: WARNING: cannot do the thing" >&2; exit 1 ;;
  empty) exit 0 ;;
  hang) exec sleep 30 ;;
esac
cat "$CTL/lsof.out"
""",
    "launchctl": r"""#!/bin/sh
echo "$@" >> "$CTL/launchctl.calls"
[ -f "$CTL/kickstart_hang" ] && exec sleep 30
exit "$(cat "$CTL/kickstart_rc" 2>/dev/null || echo 0)"
""",
}


class Wd:
    def __init__(self, tmp: Path, real_lsof: bool = False, real_pgrep: bool = False) -> None:
        self.tmp = tmp
        self.runner = tmp / "runner"
        (self.runner / "bin").mkdir(parents=True)
        (self.runner / "_diag").mkdir()
        self.ctl = tmp / "ctl"
        self.ctl.mkdir()
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        self.state = tmp / "state"
        self.state.mkdir()
        self.state.chmod(0o700)
        for name, body in STUBS.items():
            if (name == "lsof" and real_lsof) or (name == "pgrep" and real_pgrep):
                continue
            p = self.stubs / name
            p.write_text(body)
            p.chmod(0o755)
        self.lpid = LISTENER_PID
        self.log = self.runner / "_diag" / "Runner_20261017-010101-utc.log"
        self.log.write_text("log\n")
        (self.ctl / "log_path").write_text(str(self.log))
        self.set_now(NOW)
        self.set_log_age(3000)
        self.set("listener_pid", str(LISTENER_PID))
        self.set("etime", "02:00:00")
        self.set("kickstart_rc", "0")
        self.lsof(socket_up=True)
        self.extra_env: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        (self.ctl / name).write_text(value + "\n")

    def set_now(self, t: int) -> None:
        self.set("now", str(t))

    def set_log_age(self, age: int, now: int = NOW) -> None:
        os.utime(self.log, (now - age, now - age))

    def lsof(self, socket_up: bool = True, mode: str = "ok", port: int = 443) -> None:
        self.set("lsof_mode", mode)
        out = [
            f"p{self.lpid}",
            "fcwd", "tDIR", f"n{self.runner}",
            "f4", "tREG", f"n{self.log}",
        ]
        if socket_up:
            out += ["f7", "tIPv4", f"n10.0.0.2:51000->20.1.1.1:{port}", "TST=ESTABLISHED", "TQR=0", "TQS=0"]
        else:
            # A listening socket, a connection to another port, and a half-dead
            # connection to the right port must not count.
            out += ["f8", "tIPv4", "n*:8080", "TST=LISTEN",
                    "f9", "tIPv4", "n10.0.0.2:51001->20.1.1.1:80", "TST=ESTABLISHED",
                    "f10", "tIPv4", f"n10.0.0.2:51002->20.1.1.1:{port}", "TST=CLOSE_WAIT"]
        (self.ctl / "lsof.out").write_text("\n".join(out) + "\n")

    def use_pid(self, pid: int) -> None:
        """Make the lsof stand-in report `pid` (for the real-pgrep tests)."""
        self.lpid = pid
        self.set("listener_pid", str(pid))
        self.lsof()

    def run(self, *args: str, timeout: int = 25, **env: str) -> subprocess.CompletedProcess:
        e = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.tmp),
            "CTL": str(self.ctl),
            "WD_PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "WD_RUNNER_DIR": str(self.runner),
            "WD_STATE_DIR": str(self.state),
            "WD_TARGET": TARGET,
            "WD_RUNNER_USER": "ghrunner",
            **self.extra_env,
            **env,
        }
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=timeout
        )

    def tick(self, now: int, *args: str, **env: str) -> subprocess.CompletedProcess:
        self.set_now(now)
        return self.run(*args, **env)

    def kicks(self) -> list[str]:
        f = self.ctl / "launchctl.calls"
        return f.read_text().splitlines() if f.exists() else []

    def wlog(self) -> str:
        f = self.state / "watchdog.log"
        return f.read_text() if f.exists() else ""

    def state_file(self, name: str) -> str | None:
        f = self.state / name
        return f.read_text().strip() if f.exists() else None


@pytest.fixture
def wd(tmp_path: Path) -> Wd:
    return Wd(tmp_path)


def test_healthy_idle_with_a_socket_takes_no_action(wd: Wd) -> None:
    # 3000 s is the longest silence an idle healthy listener shows (max 3058 s observed).
    wd.set_log_age(3000)
    r = wd.run("--verbose")
    assert r.returncode == 0, r.stderr
    assert wd.kicks() == []
    assert "healthy; no action" in r.stdout
    assert "RESTART" not in wd.wlog()
    assert wd.state_file("nosock_since") is None
    assert wd.state_file("last_restart") is None
    # Worker and listener are matched on the runner dir's own binaries, anchored.
    pats = (wd.ctl / "pgrep.patterns").read_text().splitlines()
    assert f"^{wd.runner}/bin/Runner.Listener run" in pats
    assert f"^{wd.runner}/bin/Runner.Worker" in pats
    # ...and on the CI user's own processes only (the real-pgrep tests below pin the flags' effect).
    calls = (wd.ctl / "pgrep.calls").read_text().splitlines()
    assert calls and all(c.startswith("-u ghrunner -f ^") for c in calls), calls
    # etime is read for the listener's own pid.
    assert (wd.ctl / "ps.calls").read_text().splitlines() == [f"-o etime= -p {LISTENER_PID}"]
    assert not [p for p in os.listdir(wd.state) if p.startswith(".tmp.")]


def test_no_listener_means_no_action_and_clears_the_socket_clock(wd: Wd) -> None:
    (wd.ctl / "listener_pid").unlink()
    (wd.state / "nosock_since").write_text(f"{NOW - 999}\n")
    wd.set_log_age(9999)
    r = wd.run("--verbose")
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "no Runner.Listener running" in r.stdout
    assert wd.state_file("nosock_since") is None


def test_worker_present_blocks_restart_even_when_stall_eligible(wd: Wd) -> None:
    wd.set_log_age(5000)  # A-eligible
    wd.lsof(socket_up=False)  # B-eligible
    (wd.state / "nosock_since").write_text(f"{NOW - 999}\n")
    wd.set("worker_from_call", "1")
    r = wd.run("--verbose")
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "Runner.Worker running" in r.stdout
    assert wd.state_file("nosock_since") is None


@pytest.mark.parametrize("age,fires", [(3899, False), (3900, True), (3901, True)])
def test_condition_a_fires_at_the_threshold_and_not_below(wd: Wd, age: int, fires: bool) -> None:
    wd.set_log_age(age)  # socket stays up, so B cannot fire
    r = wd.run()
    assert r.returncode == 0, r.stderr
    assert wd.kicks() == ([f"kickstart -k {TARGET}"] if fires else [])
    if fires:
        assert f"listener log silent {age}s >= 3900s" in wd.wlog()
        assert wd.state_file("last_restart") == str(NOW)


def test_condition_b_needs_consecutive_socketless_ticks(wd: Wd) -> None:
    wd.lsof(socket_up=False)
    wd.set_log_age(400, now=NOW)  # silent >= 300 and stays put as the clock advances
    r = wd.tick(NOW)
    assert r.returncode == 0 and wd.kicks() == []
    assert wd.state_file("nosock_since") == str(NOW)
    assert "INFO listener pid=4141 has no established :443 connection" in wd.wlog()
    assert wd.tick(NOW + 299).returncode == 0
    assert wd.kicks() == [], "299 s without a socket is below the 300 s threshold"
    assert wd.state_file("nosock_since") == str(NOW), "the clock is not restarted by later socketless ticks"
    assert wd.tick(NOW + 300).returncode == 0
    assert wd.kicks() == [f"kickstart -k {TARGET}"]
    assert "no established :443 connection for 300s" in wd.wlog()
    assert wd.state_file("nosock_since") is None, "a restart clears the socket clock"
    assert not [p for p in os.listdir(wd.state) if p.startswith(".tmp.")]


def test_a_socket_resets_condition_b(wd: Wd) -> None:
    wd.set_log_age(400)
    wd.lsof(socket_up=False)
    wd.tick(NOW)
    assert wd.state_file("nosock_since") == str(NOW)
    wd.lsof(socket_up=True)
    wd.tick(NOW + 200)
    assert wd.state_file("nosock_since") is None
    assert "connection is back after 200s" in wd.wlog()
    wd.lsof(socket_up=False)
    wd.tick(NOW + 400)
    assert wd.kicks() == [], "the gap before the socket returned must not count"
    assert wd.state_file("nosock_since") == str(NOW + 400)


def test_condition_b_needs_the_log_to_be_silent_too(wd: Wd) -> None:
    wd.lsof(socket_up=False)
    wd.set_log_age(299)
    (wd.state / "nosock_since").write_text(f"{NOW - 5000}\n")
    assert wd.run().returncode == 0
    assert wd.kicks() == []
    wd.set_log_age(300)
    assert wd.run().returncode == 0
    assert wd.kicks() == [f"kickstart -k {TARGET}"]


@pytest.mark.parametrize("since,fires", [(899, False), (900, True)])
def test_cooldown_blocks_a_second_restart(wd: Wd, since: int, fires: bool) -> None:
    wd.set_log_age(5000)
    (wd.state / "last_restart").write_text(f"{NOW - since}\n")
    r = wd.run("--verbose")
    assert r.returncode == 0
    assert (wd.kicks() != []) is fires
    if not fires:
        assert f"last restart was {since}s ago < cooldown 900s" in r.stdout


@pytest.mark.parametrize(
    "etime,fires",
    [("04:59", False), ("05:00", True), ("00:10", False), ("1-00:00:10", True), ("02:00:00", True)],
)
def test_startup_grace_blocks_a_new_listener(wd: Wd, etime: str, fires: bool) -> None:
    wd.set_log_age(5000)
    wd.set("etime", etime)
    r = wd.run("--verbose")
    assert r.returncode == 0
    assert (wd.kicks() != []) is fires
    if not fires:
        assert "< grace 300s; waiting" in r.stdout


def test_a_worker_appearing_before_the_kickstart_aborts_it(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.set("worker_from_call", "2")  # absent at the first check, present at the re-check
    r = wd.run()
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "ABORT restart" in wd.wlog() and "Runner.Worker appeared" in wd.wlog()
    assert wd.state_file("last_restart") is None
    assert "RESTART pid" not in wd.wlog()


def test_a_listener_log_that_moved_before_the_kickstart_aborts_it(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.set("touch_log_on_call", "2")  # the log gets written between the checks
    r = wd.run()
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "ABORT restart" in wd.wlog() and "changed before the kickstart" in wd.wlog()


def test_a_listener_pid_that_changed_before_the_kickstart_aborts_it(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.set("listener_pid_after_first", "5151")
    r = wd.run()
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "listener pid changed" in wd.wlog()


def test_lsof_failure_skips_b_and_logs_an_error(wd: Wd) -> None:
    wd.set_log_age(400)
    wd.lsof(mode="fail")
    (wd.state / "nosock_since").write_text(f"{NOW - 5000}\n")  # B would fire on "no socket"
    r = wd.run("--verbose")
    assert r.returncode == 0
    assert wd.kicks() == [], "an lsof failure is unknown, not 'no connection'"
    assert "ERROR lsof -p 4141 failed rc=1" in wd.wlog()
    assert "lsof_ok=0" in r.stdout
    assert wd.state_file("nosock_since") == str(NOW - 5000), "an unknown tick leaves the socket clock alone"


def test_condition_a_still_works_when_lsof_fails(wd: Wd) -> None:
    wd.lsof(mode="fail")
    wd.set_log_age(5000)
    assert wd.run().returncode == 0
    assert wd.kicks() == [f"kickstart -k {TARGET}"]


def test_a_hung_lsof_is_killed_and_treated_as_unknown(wd: Wd) -> None:
    wd.set_log_age(400)
    wd.lsof(mode="hang")
    (wd.state / "nosock_since").write_text(f"{NOW - 5000}\n")
    t0 = time.monotonic()
    r = wd.run(timeout=20, WD_LSOF_TIMEOUT="1")
    assert time.monotonic() - t0 < 15
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "rc=124" in wd.wlog()


def test_a_failed_kickstart_is_an_error_and_records_no_restart(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.set("kickstart_rc", "5")
    r = wd.run()
    assert r.returncode == 1
    assert "ERROR kickstart -k" in wd.wlog() and "rc=5" in wd.wlog()
    assert wd.state_file("last_restart") is None


def test_a_hung_kickstart_is_killed(wd: Wd) -> None:
    wd.set_log_age(5000)
    (wd.ctl / "kickstart_hang").write_text("1")
    t0 = time.monotonic()
    r = wd.run(timeout=20, WD_LAUNCHCTL_TIMEOUT="1")
    assert time.monotonic() - t0 < 15
    assert r.returncode == 1
    assert "rc=124" in wd.wlog()
    assert wd.state_file("last_restart") is None


def test_the_log_the_listener_holds_open_wins_over_a_newer_stray_log(wd: Wd) -> None:
    wd.set_log_age(5000)
    stray = wd.runner / "_diag" / "Runner_20261017-020202-utc.log"  # a `--version` run
    stray.write_text("x\n")
    os.utime(stray, (NOW - 10, NOW - 10))
    assert wd.run().returncode == 0
    assert wd.kicks() == [f"kickstart -k {TARGET}"]
    assert "log=Runner_20261017-010101-utc.log" in wd.wlog()


def test_dry_run_never_restarts_but_still_writes_state_and_info(wd: Wd) -> None:
    wd.lsof(socket_up=False)
    wd.set_log_age(400)
    (wd.state / "nosock_since").write_text(f"{NOW - 5000}\n")
    r = wd.run("--dry-run")
    assert r.returncode == 0
    assert wd.kicks() == []
    assert f"DRY-RUN would run: launchctl kickstart -k {TARGET}" in r.stdout
    assert wd.state_file("last_restart") is None
    # Documented: a dry run is not read-only for state and the INFO log.
    wd.lsof(socket_up=True)
    wd.run("--dry-run")
    assert "connection is back" in wd.wlog()
    assert wd.state_file("nosock_since") is None


def test_unmounted_volume_exits_quietly_and_creates_nothing(wd: Wd) -> None:
    shutil.rmtree(wd.runner / "bin")
    missing = wd.tmp / "never" / "created"
    r = wd.run("--verbose", WD_STATE_DIR=str(missing))
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""
    assert not (wd.tmp / "never").exists()
    assert wd.kicks() == []


def test_a_symlinked_state_dir_is_refused(wd: Wd) -> None:
    link = wd.tmp / "statelink"
    link.symlink_to(wd.state)
    r = wd.run(WD_STATE_DIR=str(link))
    assert r.returncode == 1 and "symlink" in r.stderr
    assert wd.kicks() == []


def test_a_writable_state_dir_is_refused(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.state.chmod(0o770)
    r = wd.run()
    assert r.returncode == 1 and "not group/other-writable" in r.stderr
    assert wd.kicks() == []


@pytest.mark.parametrize(
    "content",
    ["", "abc", "12 34", "-5", "1e3", "99999999999999999999", str(NOW + 10), "a[$(touch {pwn})]", "$(touch {pwn})", "0x10"],
)
def test_a_bad_last_restart_file_is_treated_as_absent(wd: Wd, content: str) -> None:
    pwn = wd.tmp / "PWNED"
    wd.set_log_age(5000)
    (wd.state / "last_restart").write_text(content.format(pwn=pwn) + "\n")
    r = wd.run()
    assert r.returncode == 0, r.stderr
    assert not pwn.exists(), "a state file's content must never be evaluated"
    assert wd.kicks() == [f"kickstart -k {TARGET}"], "absent last_restart means no cooldown"
    assert "WARN ignored and removed invalid state file last_restart" in wd.wlog()
    assert wd.state_file("last_restart") == str(NOW), "rewritten with a clean value after the restart"


@pytest.mark.parametrize(
    "content",
    ["", "abc", "12 34", "-5", "99999999999999999999", str(NOW + 10), "a[$(touch {pwn})]", "$(touch {pwn})"],
)
def test_a_bad_nosock_since_file_is_treated_as_absent(wd: Wd, content: str) -> None:
    pwn = wd.tmp / "PWNED"
    wd.lsof(socket_up=False)
    wd.set_log_age(400)
    (wd.state / "nosock_since").write_text(content.format(pwn=pwn) + "\n")
    r = wd.run()
    assert r.returncode == 0, r.stderr
    assert not pwn.exists(), "a state file's content must never be evaluated"
    assert wd.kicks() == [], "an absent clock restarts at zero, it does not fire"
    assert wd.state_file("nosock_since") == str(NOW)
    assert "WARN ignored and removed invalid state file nosock_since" in wd.wlog()


def test_log_rotates_at_a_mebibyte(wd: Wd) -> None:
    (wd.state / "watchdog.log").write_text("x" * (1024 * 1024))
    wd.lsof(socket_up=False)
    wd.run()
    assert (wd.state / "watchdog.log.1").stat().st_size == 1024 * 1024
    assert "INFO listener pid=4141 has no established" in wd.wlog()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="no lsof on this host")
def test_real_lsof_output_is_parsed(tmp_path: Path) -> None:
    """Pin the -F ftnT parsing against the real lsof: a live connection counts, another port does not."""
    wd = Wd(tmp_path, real_lsof=True)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    child = subprocess.Popen(
        [
            sys.executable, "-I", "-c",
            "import socket,sys,time\n"
            "c=socket.create_connection(('127.0.0.1',int(sys.argv[1])))\n"
            "f=open(sys.argv[2])\n"
            "print('ready',flush=True)\n"
            "time.sleep(60)\n",
            str(port), str(wd.log),
        ],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        conn, _ = srv.accept()
        wd.set("listener_pid", str(child.pid))
        wd.set_log_age(100)
        up = wd.run("--verbose", WD_SOCK_PORT=str(port))
        assert up.returncode == 0, up.stderr
        assert f"sockets:{port}=1 lsof_ok=1" in up.stdout, up.stdout
        assert f"log={wd.log.name}" in up.stdout
        other = wd.run("--verbose", WD_SOCK_PORT=str(port + 1))
        assert f"sockets:{port + 1}=0 lsof_ok=1" in other.stdout, other.stdout
        conn.close()
    finally:
        child.kill()
        child.wait()
        srv.close()


# --- state, symlinks, owner ---------------------------------------------------


def test_a_state_dir_not_owned_by_the_running_uid_is_refused(wd: Wd) -> None:
    # `id -u` says 0 (the root the daemon runs as); the tmp dir belongs to this user.
    stub = wd.stubs / "id"
    stub.write_text("#!/bin/sh\necho 0\n")
    stub.chmod(0o755)
    wd.set_log_age(5000)
    r = wd.run()
    assert r.returncode == 1 and "must be owned by uid 0" in r.stderr
    assert wd.kicks() == []


@pytest.mark.parametrize("dangling", [False, True])
def test_a_symlinked_state_file_is_treated_as_absent_not_followed(wd: Wd, dangling: bool) -> None:
    target = wd.tmp / "target"
    if not dangling:
        target.write_text(f"{NOW - 100}\n")  # followed, this would put the restart in cooldown
    (wd.state / "last_restart").symlink_to(target)
    wd.set_log_age(5000)
    r = wd.run()
    assert r.returncode == 0, r.stderr
    assert wd.kicks() == [f"kickstart -k {TARGET}"]
    assert "WARN ignored and removed invalid state file last_restart" in wd.wlog()
    if not dangling:
        assert target.read_text() == f"{NOW - 100}\n", "the link's target is never written"
    assert not (wd.state / "last_restart").is_symlink()
    assert wd.state_file("last_restart") == str(NOW)


# --- last_tick ------------------------------------------------------------------


def _fields(text: str | None) -> dict[str, str]:
    assert text is not None
    return dict(kv.split("=", 1) for kv in text.split())


def test_last_tick_is_rewritten_by_every_tick_that_reaches_the_decision(wd: Wd) -> None:
    assert wd.run().returncode == 0
    assert _fields(wd.state_file("last_tick")) == {
        "epoch": str(NOW), "pid": str(LISTENER_PID), "uptime": "7200", "silent": "3000",
        "sockets": "1", "lsof_ok": "1", "logsrc": "lsof", "halted": "0",
    }
    assert wd.tick(NOW + 60).returncode == 0
    assert _fields(wd.state_file("last_tick"))["epoch"] == str(NOW + 60), "overwritten, not appended"
    assert len((wd.state / "last_tick").read_text().splitlines()) == 1
    assert oct((wd.state / "last_tick").stat().st_mode & 0o777) == "0o600"
    assert not [p for p in os.listdir(wd.state) if p.startswith(".tmp.")]


def test_last_tick_reports_a_failed_lsof_and_the_fallback_log(wd: Wd) -> None:
    wd.lsof(mode="fail")
    r = wd.run("--verbose")
    assert "logsrc=newest" in r.stdout
    f = _fields(wd.state_file("last_tick"))
    assert (f["lsof_ok"], f["logsrc"], f["sockets"]) == ("0", "newest", "0")
    wd.lsof()
    assert "logsrc=lsof" in wd.run("--verbose").stdout


def test_last_tick_is_not_written_when_no_decision_is_reached(wd: Wd) -> None:
    wd.set("worker_from_call", "1")
    assert wd.run().returncode == 0
    assert wd.state_file("last_tick") is None
    wd.run()  # still a job
    (wd.ctl / "worker_from_call").unlink()
    (wd.ctl / "listener_pid").unlink()
    assert wd.run().returncode == 0
    assert wd.state_file("last_tick") is None


# --- fail-closed edges ----------------------------------------------------------


def test_a_failing_worker_pgrep_means_a_job_is_assumed(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.set("worker_rc", "3")
    r = wd.run()
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "ERROR pgrep for Runner.Worker failed rc=3" in wd.wlog()


def test_a_failing_listener_pgrep_is_an_error_not_no_listener(wd: Wd) -> None:
    wd.set_log_age(5000)
    (wd.state / "nosock_since").write_text(f"{NOW - 50}\n")
    wd.set("listener_rc", "3")
    r = wd.run()
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "ERROR pgrep for Runner.Listener failed rc=3" in wd.wlog()
    assert wd.state_file("nosock_since") == str(NOW - 50), "an unknown tick leaves the socket clock alone"


def test_lsof_that_exits_zero_with_no_record_is_unknown_not_zero_sockets(wd: Wd) -> None:
    wd.set_log_age(400)
    wd.lsof(mode="empty")
    (wd.state / "nosock_since").write_text(f"{NOW - 5000}\n")  # B would fire on "no socket"
    r = wd.run("--verbose")
    assert r.returncode == 0
    assert wd.kicks() == []
    assert "ERROR lsof -p 4141 exited 0 but listed no p4141 record" in wd.wlog()
    assert "lsof_ok=0" in r.stdout and "logsrc=newest" in r.stdout
    assert wd.state_file("nosock_since") == str(NOW - 5000)


# --- repeat guard ---------------------------------------------------------------


def _halt_errors(wd: Wd) -> int:
    return wd.wlog().count("did not cure the listener")


def test_a_repeat_after_our_own_restart_halts_once_and_removing_the_flag_resumes(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.set("etime", "10:00")  # the listener our restart started 600 s ago
    (wd.state / "last_restart").write_text(f"{NOW - 600}\n")
    r = wd.run("--verbose")
    assert r.returncode == 0
    assert wd.kicks() == []
    assert (wd.state / "halted").exists()
    assert wd.state_file("last_restart") is None, "the restart record goes with the halt"
    assert _halt_errors(wd) == 1
    assert "ERROR restart 600s ago did not cure the listener (pid=4141;" in wd.wlog()
    # Later ticks stay quiet and do nothing.
    r2 = wd.tick(NOW + 60, "--verbose")
    assert wd.kicks() == [] and _halt_errors(wd) == 1
    assert "auto-restart is halted" in r2.stdout
    assert _fields(wd.state_file("last_tick"))["halted"] == "1"
    # A human removes the flag: the next eligible tick restarts.
    (wd.state / "halted").unlink()
    assert wd.tick(NOW + 120).returncode == 0
    assert wd.kicks() == [f"kickstart -k {TARGET}"]
    assert wd.state_file("last_restart") == str(NOW + 120)


@pytest.mark.parametrize(
    "since,etime,halts",
    [
        (600, "10:05", True),    # uptime 605 == since + 5: ours
        (600, "10:06", False),   # uptime 606: not the listener we started
        (1800, "30:00", True),   # the window's last second
        (1801, "30:01", False),  # outside the window; a plain restart
    ],
)
def test_the_repeat_guard_boundaries(wd: Wd, since: int, etime: str, halts: bool) -> None:
    wd.set_log_age(5000)
    wd.set("etime", etime)
    (wd.state / "last_restart").write_text(f"{NOW - since}\n")
    assert wd.run().returncode == 0
    assert (wd.state / "halted").exists() is halts
    assert _halt_errors(wd) == (1 if halts else 0)
    if halts:
        assert wd.kicks() == []
    elif since >= 900:
        assert wd.kicks() == [f"kickstart -k {TARGET}"]
    else:
        assert wd.kicks() == [], "inside the cooldown, and not halted"


def test_a_halted_watchdog_does_not_restart_even_when_eligible(wd: Wd) -> None:
    wd.set_log_age(5000)
    (wd.state / "halted").write_text("1\n")
    r = wd.run("--verbose")
    assert r.returncode == 0 and wd.kicks() == []
    assert "auto-restart is halted; remove" in r.stdout


def test_dry_run_reports_the_halt_and_writes_nothing(wd: Wd) -> None:
    wd.set_log_age(5000)
    wd.set("etime", "10:00")
    (wd.state / "last_restart").write_text(f"{NOW - 600}\n")
    r = wd.run("--dry-run")
    assert r.returncode == 0 and "DRY-RUN would halt auto-restart" in r.stdout
    assert not (wd.state / "halted").exists()
    assert wd.state_file("last_restart") == str(NOW - 600)
    assert wd.kicks() == []


# --- the REAL pgrep -------------------------------------------------------------

REAL_PGREP = pytest.mark.skipif(not Path("/usr/bin/pgrep").exists(), reason="no /usr/bin/pgrep")
USER = getpass.getuser()


@pytest.fixture
def forge() -> Iterator[Callable[[Path, str], int]]:
    """Spawn `sleep` whose argv[0] reads "<exe> <args>" (bash exec -a); killed at teardown."""
    procs: list[subprocess.Popen] = []

    def spawn(exe: Path, args: str = "") -> int:
        argv0 = f"{exe} {args}".rstrip()
        p = subprocess.Popen(
            ["/bin/bash", "-c", 'exec -a "$0" sleep 120', argv0],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            seen = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(p.pid)], capture_output=True, text=True)
            if seen.stdout.startswith(argv0):
                return p.pid
            time.sleep(0.05)
        raise AssertionError(f"forged process {argv0!r} never showed its argv")

    yield spawn
    for p in procs:
        p.kill()
        p.wait()


@REAL_PGREP
def test_real_pgrep_finds_the_listener_of_the_right_dir_only(tmp_path: Path, forge: Callable[[Path, str], int]) -> None:
    wd = Wd(tmp_path, real_pgrep=True)
    wd.set_log_age(5000)
    # Stand-ins for a second runner (/Users/ghconexus-cred/actions-runner) and a
    # non-"run" invocation: neither is the listener of wd.runner.
    forge(tmp_path / "other" / "bin" / "Runner.Listener", "run")
    forge(wd.runner / "bin" / "Runner.Listener", "--version")
    r = wd.run("--verbose", WD_RUNNER_USER=USER)
    assert r.returncode == 0, r.stderr
    assert "no Runner.Listener running" in r.stdout and wd.kicks() == []
    pid = forge(wd.runner / "bin" / "Runner.Listener", "run --startuptype service")
    wd.use_pid(pid)
    r = wd.run("--verbose", WD_RUNNER_USER=USER)
    assert r.returncode == 0, r.stderr
    assert f"listener pid={pid} " in r.stdout
    assert wd.kicks() == [f"kickstart -k {TARGET}"], "found, and the pre-kickstart recheck found it again"


@REAL_PGREP
def test_real_pgrep_is_restricted_to_the_runner_user(tmp_path: Path, forge: Callable[[Path, str], int]) -> None:
    wd = Wd(tmp_path, real_pgrep=True)
    wd.set_log_age(5000)
    pid = forge(wd.runner / "bin" / "Runner.Listener", "run")
    wd.use_pid(pid)
    r = wd.run("--verbose", WD_RUNNER_USER="nobody")
    assert "no Runner.Listener running" in r.stdout and wd.kicks() == []
    assert f"listener pid={pid} " in wd.run("--verbose", WD_RUNNER_USER=USER).stdout


@REAL_PGREP
def test_real_pgrep_worker_of_this_runner_blocks_a_restart_and_a_sibling_runners_does_not(
    tmp_path: Path, forge: Callable[[Path, str], int]
) -> None:
    wd = Wd(tmp_path, real_pgrep=True)
    wd.set_log_age(5000)
    wd.use_pid(forge(wd.runner / "bin" / "Runner.Listener", "run"))
    forge(tmp_path / "other" / "bin" / "Runner.Worker", "")
    assert wd.run(WD_RUNNER_USER=USER).returncode == 0
    assert wd.kicks() == [f"kickstart -k {TARGET}"], "another runner's job must not block this one"
    (wd.ctl / "launchctl.calls").unlink()
    (wd.state / "last_restart").unlink()
    forge(wd.runner / "bin" / "Runner.Worker", "")
    r = wd.run("--verbose", WD_RUNNER_USER=USER)
    assert r.returncode == 0
    assert "Runner.Worker running" in r.stdout and wd.kicks() == []


@REAL_PGREP
def test_real_pgrep_two_listeners_are_ambiguous_and_nothing_is_restarted(
    tmp_path: Path, forge: Callable[[Path, str], int]
) -> None:
    wd = Wd(tmp_path, real_pgrep=True)
    wd.set_log_age(5000)
    a = forge(wd.runner / "bin" / "Runner.Listener", "run")
    b = forge(wd.runner / "bin" / "Runner.Listener", "run")
    wd.use_pid(a)
    r = wd.run(WD_RUNNER_USER=USER)
    assert r.returncode == 0
    assert wd.kicks() == []
    assert f"ERROR more than one Runner.Listener under {wd.runner} ({min(a, b)} {max(a, b)})" in wd.wlog()


@REAL_PGREP
def test_real_pgrep_worker_search_is_restricted_to_the_runner_user_too(
    tmp_path: Path, forge: Callable[[Path, str], int]
) -> None:
    wd = Wd(tmp_path)
    # The listener is stubbed (so the user can differ); the Worker search is the real pgrep.
    stub = wd.stubs / "pgrep"
    stub.write_text(
        '#!/bin/sh\nfor a; do pat="$a"; done\n'
        'case "$pat" in *Runner.Listener*) cat "$CTL/listener_pid"; exit 0 ;; esac\n'
        'exec /usr/bin/pgrep "$@"\n'
    )
    wd.set_log_age(5000)
    forge(wd.runner / "bin" / "Runner.Worker", "")  # ours, and this user is not "nobody"
    r = wd.run("--verbose", WD_RUNNER_USER=USER)
    assert "Runner.Worker running" in r.stdout and wd.kicks() == []
    assert wd.run(WD_RUNNER_USER="nobody").returncode == 0
    assert wd.kicks() == [f"kickstart -k {TARGET}"], "a process of another user is not this runner's job"
