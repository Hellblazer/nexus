# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behaviour and shape pins for the macOS test guest on hellmini (nexus-ppuwc).

``tests/e2e/macos-guest/launch.sh`` (laptop) and ``host.sh`` (hellmini) run RDR-228
MVV row 1 in a throwaway tart VM. No tart, hellmini or Claude credential exists in
CI, so both scripts run for real against stand-ins on ``PATH``: ``ssh`` and ``scp``
(which execute the "remote" command locally, or map a guest path into a directory),
``sudo`` and ``launchctl`` (which only strip their own arguments), ``pgrep`` (which
only sees processes whose command line names this test's directory) and a ``tart``
that keeps its VMs as files and its ``tart run`` as a real process. The host-side
overrides are honoured only because the harness sets ``NX_MAC_GUEST_TEST=1``.

Nothing here reaches a host, a keychain or a credential; the harness asserts that
the stand-ins are what the scripts find before it runs them.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import _suite_lease
from tests._child_process import kill_group, signal_group, terminal_signals_default

REPO: Path = Path(__file__).resolve().parent.parent
GUEST_DIR: Path = REPO / "tests" / "e2e" / "macos-guest"
HOST_SH: Path = GUEST_DIR / "host.sh"
LAUNCH_SH: Path = GUEST_DIR / "launch.sh"
ROW1_SH: Path = GUEST_DIR / "row1.sh"
PROVISION_SH: Path = GUEST_DIR / "provision.sh"
BUILD_SH: Path = GUEST_DIR / "build-image.sh"
LEASE_LIB: Path = REPO / "scripts" / "lib" / "build-lease.sh"
PYTHON_SH: Path = REPO / "tests" / "e2e" / "lib" / "python.sh"

PRE_CHECKS = (
    "clean_start", "setup_done", "setup_local_autostart", "launchagent_plist",
    "launchd_job_running_pre", "doctor_pre", "note_stored_pre",
)
POST_CHECKS = (
    "login_session_back", "launchd_job_running_post", "doctor_post",
    "note_survives_reboot", "session_search_post",
)
ALL_CHECKS = PRE_CHECKS + POST_CHECKS

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="bash scripts and process signals")
_NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _code(p: Path) -> str:
    return "\n".join(ln for ln in _read(p).splitlines() if not ln.strip().startswith("#"))


# ---- stand-ins ---------------------------------------------------------------------

_FAKE_SSH = r"""#!/bin/bash
# stand-in for ssh: a host target runs the command locally; admin@<ip> is the guest.
# umask 022 is hellmini's. A real ssh passes no descriptor but 0-2 to the remote side: launch.sh's hold
# pipe is fd 8, and bash keeps a saved copy of it (fd 10 was seen), so a remote-side process that outlives
# the call (the deadman of a VM whose delete failed) held the pipe open and the hold never saw EOF; the
# test then waited out end_hold's 90 s cap (nexus-w6oif). Close them all, as the real hop does.
umask 022
for fd in $(seq 3 63); do [[ -e /dev/fd/$fd ]] && eval "exec $fd>&-"; done
dest=''; opts=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i|-p|-l|-F|-E) opts+=("$1" "$2"); shift 2 ;;
    -*) opts+=("$1"); shift ;;
    *) dest="$1"; shift; break ;;
  esac
done
cmd="$*"
echo "$dest :: $cmd" >> "$FAKE_ROOT/ssh.log"
case "$dest" in
  admin@*)
    echo "${opts[*]}" >> "$FAKE_ROOT/guest-ssh-opts.log"
    exec "$FAKE_ROOT/guest" "$cmd" ;;
  *) exec bash -c "$cmd" ;;
esac
"""

_FAKE_GUEST = r"""#!/bin/bash
# the guest: a directory tree under $FAKE_ROOT/guestfs, a boot session file, and a
# reboot that takes ssh down for FAKE_DOWN_POLLS polls.
cd "$FAKE_ROOT/guestfs/Users/admin" || exit 1
b="$FAKE_ROOT/boot"
if [[ -f "$b/down" ]]; then
  n=$(cat "$b/down")
  if (( n > 0 )); then echo $((n - 1)) > "$b/down"; exit 255; fi
  rm -f "$b/down"; echo aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee > "$b/uuid"
fi
case "$1" in
  'sysctl -n kern.bootsessionuuid') cat "$b/uuid" ;;
  'sudo -n shutdown -r now')
    echo shutdown >> "$FAKE_ROOT/guest-actions.log"
    if [[ -n "${FAKE_NO_REBOOT:-}" ]]; then :
    elif [[ -n "${FAKE_NO_DOWN:-}" ]]; then echo aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee > "$b/uuid"
    else echo "${FAKE_DOWN_POLLS:-1}" > "$b/down"; fi ;;
  *) bash -c "$1" ;;
esac
"""

_FAKE_SCP = r"""#!/bin/bash
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i|-P) shift 2 ;;
    -*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
n=${#args[@]}
map() { case "$1" in admin@*:*) echo "$FAKE_ROOT/guestfs${1#*:}" ;; fakehost:*) echo "${1#*:}" ;; *) echo "$1" ;; esac; }
echo "scp ${args[*]}" >> "$FAKE_ROOT/scp.log"
d="$(map "${args[$((n - 1))]}")"
for ((i = 0; i < n - 1; i++)); do
  s="$(map "${args[$i]}")"
  [[ -e "$s" ]] || exit 1
  mkdir -p "$(dirname "$d")"
  cp -R "$s" "$d" || exit 1
done
"""

_FAKE_SUDO = """#!/bin/bash
while [[ "${1:-}" == -* ]]; do case "$1" in -u) shift 2 ;; *) shift ;; esac; done
exec "$@"
"""

_FAKE_LAUNCHCTL = """#!/bin/bash
case "$1" in
  asuser) shift 2; exec "$@" ;;
  print) [[ -z "${FAKE_GUI_ABSENT:-}" ]] || exit 112; exit 0 ;;
  *) exit 64 ;;
esac
"""

# Only processes that name this test's directory are visible, so a parallel test's
# fake `tart run` is never this test's busy signal. The runner-user query is scripted.
_FAKE_PGREP = r"""#!/bin/bash
if [[ "$1" == -u ]]; then
  [[ -z "${FAKE_PGREP_FAIL:-}" ]] || exit 2
  [[ -z "${FAKE_RUNNER_BUSY:-}" ]] || exit 0
  exit 1
fi
if [[ "$1" == -f && $# -eq 2 ]]; then
  found=1
  for pid in $(/usr/bin/pgrep -f "$2"); do
    [[ "$(ps -o command= -p "$pid" 2>/dev/null)" == *"$FAKE_ROOT"* ]] && { echo "$pid"; found=0; }
  done
  exit $found
fi
exec /usr/bin/pgrep "$@"
"""

_FAKE_TART = r"""#!/bin/bash
# stand-in for tart: VMs are files under $FAKE_ROOT/vms; `run` is a real process.
v="$FAKE_ROOT/vms"
echo "$*" >> "$FAKE_ROOT/tart.log"
case "$1" in
  --version) echo 2.32.1 ;;
  get) [[ -e "$v/$2" ]] ;;
  list) ls "$v" | grep -v '\.' ;;
  clone) [[ -e "$v/$2" ]] && touch "$v/$3" ;;
  run)
    echo $$ > "$v/$3.runpid"; touch "$v/$3.run"
    trap 'echo "TERM $(date +%T)" >> "$FAKE_ROOT/run-exit.log"; rm -f "$v/$3.run" "$v/$3.runpid"; exit 0' TERM
    trap 'echo "EXIT $(date +%T)" >> "$FAKE_ROOT/run-exit.log"' EXIT
    while [[ -e "$v/$3.run" ]]; do sleep 0.2; done
    rm -f "$v/$3.runpid" ;;
  stop)
    [[ -z "${FAKE_STOP_HANGS:-}" ]] || exit 0
    [[ -z "${FAKE_STOP_SLOW:-}" ]] || sleep "$FAKE_STOP_SLOW"
    rm -f "$v/$2.run"
    for _ in $(seq 1 30); do [[ -e "$v/$2.runpid" ]] || break; sleep 0.1; done ;;
  delete)
    [[ -z "${FAKE_DELETE_SLOW:-}" ]] || sleep "$FAKE_DELETE_SLOW"
    if [[ -n "${FAKE_DELETE_FAIL_N:-}" ]]; then
      done_n=$(cat "$FAKE_ROOT/delete-failed" 2>/dev/null || echo 0)
      if (( done_n < FAKE_DELETE_FAIL_N )); then echo $((done_n + 1)) > "$FAKE_ROOT/delete-failed"; exit 1; fi
    fi
    rm -f "$v/$2" ;;
  ip) echo "${FAKE_IP:-192.168.64.5}" ;;
  exec) echo "${FAKE_HOSTKEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE root@guest}" ;;
  *) exit 64 ;;
esac
"""

_FAKE_CREDENTIALS = r"""import os, sys, time
from pathlib import Path

root = Path(os.environ["FAKE_ROOT"])
if sys.argv[1] == "status":
    sys.exit(1 if os.environ.get("FAKE_NO_TOKEN") else 0)
cmd = sys.argv[sys.argv.index("--") + 1:]
phase = cmd[1]
with open(root / "creds.log", "a") as f:
    f.write(" ".join(cmd) + "\n")
lease = Path(os.environ["NX_MAC_GUEST_LEASE_ROOT"]) / "nexus-build-lease" / "suite" / "pid"
try:
    pid = int(lease.read_text())
    os.kill(pid, 0)
    seen = f"held by live pid {pid}"
except Exception as exc:  # noqa: BLE001
    seen = f"NOT held ({type(exc).__name__})"
with open(root / "lease-seen.log", "a") as f:
    f.write(f"{phase}: {seen}\n")
out = root / "guestfs" / "Users" / "admin" / "nxmvv" / "out"
out.mkdir(parents=True, exist_ok=True)
mode = os.environ.get("FAKE_" + phase.upper(), "")
if mode == "sleep":
    (root / "phase.pid").write_text(str(os.getpid()))
    (root / "phase-started").touch()
    time.sleep(300)
names = {"pre": %(pre)r, "post": %(post)r}[phase]
lines = [f"CHECK PASS {n}: ok" for n in names]
if mode == "short":
    lines = lines[:-1]
if mode == "extra":
    lines.append("CHECK PASS surprise: ok")
if mode == "dup":
    lines.append(lines[0])
if mode == "fail":
    lines[-1] = f"CHECK FAIL {names[-1]}: broke"
if mode == "leak":
    (out / "10-setup.txt").write_text("the model printed " + "sk-ant" + "-api03-NOTREAL\n")
(out / f"checks-{phase}.txt").write_text("\n".join(lines) + "\n")
print(f"ROW1 {phase} done")
sys.exit(1 if mode == "fail" else 0)
""" % {"pre": PRE_CHECKS, "post": POST_CHECKS}


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _bash() -> str:
    found = shutil.which("bash")
    assert found, "bash is required"
    return found


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp.resolve()
        self.root = self.tmp / "fake"
        self.tree = self.tmp / "tree"
        self.bin = self.tmp / "bin"
        self.w = self.tmp / "w"
        self.base = self.w / "base"
        self.common = self.tmp / "common"
        self.out = self.tmp / "out"
        self.user = pwd.getpwuid(os.getuid()).pw_name
        gdir = self.tree / "tests" / "e2e" / "macos-guest"
        lib = self.tree / "tests" / "e2e" / "lib"
        gdir.mkdir(parents=True)
        lib.mkdir(parents=True)
        for name in ("host.sh", "launch.sh", "row1.sh", "provision.sh", "build-image.sh"):
            shutil.copy2(GUEST_DIR / name, gdir / name)
        shutil.copy2(PYTHON_SH, lib / "python.sh")
        (lib / "claude_credentials.py").write_text(_FAKE_CREDENTIALS, encoding="utf-8")
        (self.tree / "conexus" / ".claude-plugin").mkdir(parents=True)
        (self.tree / "conexus" / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        (self.tree / "scripts" / "lib").mkdir(parents=True)
        shutil.copy2(LEASE_LIB, self.tree / "scripts" / "lib" / "build-lease.sh")
        self.host = gdir / "host.sh"
        self.launch = gdir / "launch.sh"
        self.row1 = gdir / "row1.sh"
        self.bin.mkdir()
        for name, body in (
            ("ssh", _FAKE_SSH), ("scp", _FAKE_SCP), ("sudo", _FAKE_SUDO),
            ("launchctl", _FAKE_LAUNCHCTL), ("pgrep", _FAKE_PGREP),
        ):
            _exe(self.bin / name, body)
        self.root.mkdir()
        _exe(self.root / "guest", _FAKE_GUEST)
        _exe(self.root / "tart", _FAKE_TART)
        (self.root / "vms").mkdir()
        (self.root / "vms" / "nexus-mac-clean").touch()
        (self.root / "boot").mkdir()
        (self.root / "boot" / "uuid").write_text("11111111-2222-3333-4444-555555555555\n")
        (self.root / "guestfs" / "Users" / "admin").mkdir(parents=True)
        (self.tmp / "home").mkdir()
        (self.tmp / "tmp").mkdir()
        (self.tmp / "tarthome").mkdir()
        self.w.mkdir(mode=0o755)
        self.base.mkdir(mode=0o755)
        (self.base / "ssh").mkdir(mode=0o700)
        (self.base / "ssh" / "id_ed25519").write_text("not a key\n")
        (self.base / "ssh" / "id_ed25519").chmod(0o600)
        (self.base / "ssh" / "known_hosts").write_text("* ssh-ed25519 AAAAFAKE\n")
        (self.base / "ssh" / "known_hosts").chmod(0o644)
        (self.base / "runs").mkdir()
        (self.base / "logs").mkdir()
        self.common.mkdir()
        self.procs: list[subprocess.Popen[str]] = []

    # -- environment ---------------------------------------------------------------
    @property
    def lease_root(self) -> Path:
        return self.common / "nexus-build-lease"

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.tmp / "home"),
            "TMPDIR": str(self.tmp / "tmp"),
            "TART_HOME": str(self.tmp / "tarthome"),
            "FAKE_ROOT": str(self.root),
            "NX_MAC_GUEST_TEST": "1",
            "NX_MAC_GUEST_DIR": str(self.base),
            "NX_MAC_GUEST_TART": str(self.root / "tart"),
            "NX_MAC_GUEST_LEASE_ROOT": str(self.common),
            "NX_MAC_GUEST_LEASE_REPO": str(self.tree),
            "NX_MAC_GUEST_RUNNER_USERS": self.user,
            "NX_MAC_GUEST_RUN_UID": str(max(os.getuid(), 1)),
            "NX_MAC_GUEST_LIFETIME_SECS": "300",
            "NX_MAC_GUEST_STOP_WAIT": "2",
            "NX_MAC_GUEST_RETRY_SLEEP": "0",
            "NX_MAC_GUEST_TEST_ROOT": str(self.tmp),
            "NX_MAC_GUEST_HOST": "fakehost",
            "NX_E2E_PYTHON": sys.executable,
            **extra,
        }
        probe = subprocess.run(
            [_bash(), "-c", "command -v ssh scp sudo launchctl pgrep"],
            env=env, capture_output=True, text=True, check=True,
        )
        assert probe.stdout.split() == [str(self.bin / n) for n in ("ssh", "scp", "sudo", "launchctl", "pgrep")], (
            "the stand-ins must be what the scripts find, or this test would reach a real host"
        )
        return env

    # -- running -------------------------------------------------------------------
    def run_host(self, *args: str, stdin: str = "", **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.host), *args], env=self.env(**extra), capture_output=True,
            text=True, timeout=120, input=stdin, cwd=self.tmp,
        )

    def run_launch(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.launch), "--out", str(self.out), *args], env=self.env(**extra),
            capture_output=True, text=True, timeout=240, cwd=self.tmp, input="",
        )

    def start_launch(self, *args: str, **extra: str) -> subprocess.Popen[str]:
        with terminal_signals_default():
            proc = subprocess.Popen(
                [str(self.launch), "--out", str(self.out), *args], env=self.env(**extra),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=self.tmp, start_new_session=True,
            )
        self.procs.append(proc)
        return proc

    def spawn_tart_run(self, vm: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [_bash(), str(self.root / "tart"), "run", "--no-graphics", vm], env=self.env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
            start_new_session=True,
        )
        self.procs.append(proc)
        _wait_for(self.root / "vms" / f"{vm}.run")
        return proc

    def cleanup(self) -> None:
        """Nothing a test started may outlive it: deadmen sleep for the lifetime cap, a fake tart run loops."""
        pidfiles = list((self.base / "deadman").glob("*.pid")) + list((self.root / "vms").glob("*.runpid"))
        for pf in pidfiles:
            try:
                pid = int(pf.read_text())
            except (OSError, ValueError):
                continue
            subprocess.run(["pkill", "-KILL", "-P", str(pid)], check=False)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for proc in self.procs:
            if proc.poll() is None:
                kill_group(proc)  # every process started here has a session of its own
            try:
                proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                pass

    # -- state ---------------------------------------------------------------------
    def tart_calls(self, verb: str) -> list[str]:
        path = self.root / "tart.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [ln for ln in lines if ln.split(" ", 1)[0] == verb]

    def vms(self) -> list[str]:
        return sorted(p.name for p in (self.root / "vms").iterdir() if "." not in p.name and p.name != "nexus-mac-clean")

    def leftovers(self) -> dict[str, list[str]]:
        """What a finished run must not leave behind."""
        return {
            "vms": self.vms(),
            "run markers": sorted(p.name for p in (self.root / "vms").glob("*.run*")),
            "run dirs": sorted(p.name for p in (self.base / "runs").iterdir()),
            "deadman pids": sorted(p.name for p in (self.base / "deadman").glob("*")) if (self.base / "deadman").exists() else [],
            "lease": [p.name for p in self.lease_root.iterdir()] if self.lease_root.exists() else [],
            "run logs": sorted(p.name for p in (self.base / "logs").glob("nx-mac-run-*")),
        }

    def hold_lease(self, pid: int) -> Path:
        d = self.lease_root / "suite"
        d.mkdir(parents=True)
        (d / "pid").write_text(f"{pid}\n")
        return d


def _wait_for(path: Path, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


@pytest.fixture()
def h(tmp_path: Path):
    harness = Harness(tmp_path)
    yield harness
    harness.cleanup()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sleeper() -> subprocess.Popen[str]:
    return subprocess.Popen(["sleep", "120"])


# ---- the guard ---------------------------------------------------------------------


@_POSIX_ONLY
def test_the_guard_passes_when_nothing_is_running_and_says_what_it_checked(h: Harness) -> None:
    res = h.run_host("guard")
    assert res.returncode == 0, res.stderr
    assert "1 runner user(s) checked" in res.stdout


@_POSIX_ONLY
def test_a_live_lease_holder_makes_the_guard_exit_75(h: Harness) -> None:
    holder = _sleeper()
    try:
        h.hold_lease(holder.pid)
        res = h.run_host("guard")
    finally:
        holder.kill()
        holder.wait()
    assert res.returncode == 75, res.stderr
    assert f"pid {holder.pid} holds nexus-build-lease/suite" in res.stderr


@_POSIX_ONLY
def test_the_engine_build_lease_counts_too(h: Harness) -> None:
    holder = _sleeper()
    try:
        d = h.lease_root / "service"
        d.mkdir(parents=True)
        (d / "pid").write_text(f"{holder.pid}\n")
        res = h.run_host("guard")
    finally:
        holder.kill()
        holder.wait()
    assert res.returncode == 75
    assert "nexus-build-lease/service" in res.stderr


@_POSIX_ONLY
def test_a_dead_lease_holder_is_stale_and_the_guard_exits_0(h: Harness) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    h.hold_lease(gone.pid)
    res = h.run_host("guard")
    assert res.returncode == 0, res.stderr


@_POSIX_ONLY
def test_a_pid_file_that_cannot_be_read_counts_as_busy(h: Harness) -> None:
    d = h.lease_root / "suite"
    (d / "pid").mkdir(parents=True)  # cat fails on a directory, for root as well
    res = h.run_host("guard")
    assert res.returncode == 75, res.stderr
    assert "cannot be read" in res.stderr


@_POSIX_ONLY
@_NOT_ROOT
def test_a_lease_directory_the_user_cannot_enter_counts_as_busy(h: Harness) -> None:
    d = h.hold_lease(os.getpid())
    d.chmod(0)
    try:
        res = h.run_host("guard")
    finally:
        d.chmod(0o755)
    assert res.returncode == 75, res.stderr
    assert "cannot be read" in res.stderr


@_POSIX_ONLY
def test_a_fresh_pidless_lease_is_being_populated_and_an_old_one_is_a_corpse(h: Harness) -> None:
    d = h.lease_root / "suite"
    d.mkdir(parents=True)
    assert h.run_host("guard").returncode == 75
    old = time.time() - 3600
    os.utime(d, (old, old))
    assert h.run_host("guard").returncode == 0


@_POSIX_ONLY
def test_an_existing_run_vm_makes_the_guard_exit_75(h: Harness) -> None:
    (h.root / "vms" / "nx-mac-run-leftover").touch()
    res = h.run_host("guard")
    assert res.returncode == 75
    assert "nx-mac-run-leftover" in res.stderr


@_POSIX_ONLY
def test_a_tart_run_process_makes_the_guard_exit_75(h: Harness) -> None:
    h.spawn_tart_run("someone-elses-vm")
    res = h.run_host("guard")
    assert res.returncode == 75
    assert "a tart run process exists" in res.stderr


@_POSIX_ONLY
def test_a_running_ci_job_makes_the_guard_exit_75(h: Harness) -> None:
    res = h.run_host("guard", FAKE_RUNNER_BUSY="1")
    assert res.returncode == 75
    assert "Runner.Worker" in res.stderr


@_POSIX_ONLY
def test_a_pgrep_error_is_busy_not_free(h: Harness) -> None:
    res = h.run_host("guard", FAKE_PGREP_FAIL="1")
    assert res.returncode == 75
    assert "pgrep" in res.stderr and "failed" in res.stderr


@_POSIX_ONLY
def test_runner_users_that_do_not_exist_are_named_and_never_read_as_free(h: Harness) -> None:
    res = h.run_host("guard", NX_MAC_GUEST_RUNNER_USERS=f"nx-no-such-user-1 {h.user}")
    assert res.returncode == 0, res.stderr
    assert "runner user nx-no-such-user-1 does not exist; not checked" in res.stdout
    assert "1 runner user(s) checked" in res.stdout
    none = h.run_host("guard", NX_MAC_GUEST_RUNNER_USERS="nx-no-such-user-1 nx-no-such-user-2")
    assert none.returncode == 3, "a guard that checked no runner user must fail, not pass"
    assert "would pass without looking" in none.stderr


@_POSIX_ONLY
@pytest.mark.skipif(Path("/Volumes/Bulk/src/nexus").exists(), reason="the real hellmini clone exists here")
def test_the_overrides_are_ignored_without_the_test_flag(h: Harness) -> None:
    holder = _sleeper()
    try:
        h.hold_lease(holder.pid)
        env = h.env()
        env.pop("NX_MAC_GUEST_TEST")
        res = subprocess.run([str(h.host), "guard"], env=env, capture_output=True, text=True, timeout=60)
    finally:
        holder.kill()
        holder.wait()
    # The real clone path is used: there is none here, so the guard cannot look and says so.
    assert res.returncode == 3, res.stderr
    assert "git common dir of /Volumes/Bulk/src/nexus" in res.stderr


# ---- preflight ---------------------------------------------------------------------


@_POSIX_ONLY
def test_preflight_passes_on_a_complete_host(h: Harness) -> None:
    res = h.run_host("preflight")
    assert res.returncode == 0, res.stderr
    assert "owner-checked" in res.stdout


@_POSIX_ONLY
def test_a_signed_out_tart_account_exits_3_and_says_where_to_sign_in(h: Harness) -> None:
    res = h.run_host("preflight", FAKE_GUI_ABSENT="1")
    assert res.returncode == 3
    assert "is not signed in at the hellmini login window; sign it in once (Screen Sharing)" in res.stderr
    assert f"(uid {max(os.getuid(), 1)})" in res.stderr


@_POSIX_ONLY
@pytest.mark.parametrize("uid", ["0", "abc", "-5"])
def test_a_tart_account_uid_that_is_zero_or_not_a_number_is_refused(h: Harness, uid: str) -> None:
    res = h.run_host("preflight", NX_MAC_GUEST_RUN_UID=uid)
    assert res.returncode == 3
    assert "no usable uid" in res.stderr


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("rel", "mode"),
    [
        ("w", 0o775),
        ("w/base", 0o775),
        ("w/base/ssh", 0o770),
        ("w/base/ssh/id_ed25519", 0o660),
        ("w/base/ssh/known_hosts", 0o666),
        ("w/base/runs", 0o757),
    ],
)
def test_preflight_refuses_a_file_or_ancestor_writable_by_group_or_other(h: Harness, rel: str, mode: int) -> None:
    (h.tmp / rel).chmod(mode)
    res = h.run_host("preflight")
    assert res.returncode == 3, res.stdout
    assert "writable by group or other" in res.stderr
    assert rel.rsplit("/", 1)[-1] in res.stderr


@_POSIX_ONLY
def test_preflight_refuses_a_symlinked_key(h: Harness) -> None:
    key = h.base / "ssh" / "id_ed25519"
    real = h.tmp / "elsewhere-key"
    key.rename(real)
    key.symlink_to(real)
    res = h.run_host("preflight")
    assert res.returncode == 3
    assert "is a symlink" in res.stderr


@_POSIX_ONLY
def test_preflight_needs_the_pinned_guest_host_key(h: Harness) -> None:
    (h.base / "ssh" / "known_hosts").unlink()
    res = h.run_host("preflight")
    assert res.returncode == 3
    assert "no pinned guest host key" in res.stderr


# ---- the lease hold ----------------------------------------------------------------


def _start_hold(h: Harness, vm: str, *extra: str, **env: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [str(h.host), "hold", vm, *extra], env=h.env(**env), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=h.tmp, start_new_session=True,
    )
    h.procs.append(proc)
    return proc


@_POSIX_ONLY
def test_hold_takes_the_suite_lease_and_releases_it_after_the_vm_is_destroyed(h: Harness) -> None:
    vm = "nx-mac-run-t1"
    (h.root / "vms" / vm).touch()
    proc = _start_hold(h, vm)
    assert proc.stdout is not None and proc.stdin is not None
    assert proc.stdout.readline().startswith("HOLD: acquired the suite lease")
    lease = h.lease_root / "suite"
    assert (lease / "pid").read_text().strip() == str(proc.pid)
    # a suite starting now finds the lease held, which is how it fails closed
    assert _suite_lease.acquire("a suite", lease_root=h.lease_root) is None
    assert h.run_host("guard").returncode == 75
    proc.stdin.close()
    proc.stdin = None
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert not lease.exists(), "the lease must be released"
    assert h.tart_calls("delete") == [f"delete {vm}"], "the VM is destroyed first"
    assert out.index("destroyed") < out.index("released the suite lease")
    release = _suite_lease.acquire("after", lease_root=h.lease_root)
    assert release is not None
    release()


@_POSIX_ONLY
def test_hold_refuses_with_75_when_a_suite_holds_the_lease_and_destroys_nothing(h: Harness) -> None:
    vm = "nx-mac-run-t2"
    (h.root / "vms" / vm).touch()
    holder = _sleeper()
    try:
        h.hold_lease(holder.pid)
        proc = _start_hold(h, vm)
        out, err = proc.communicate(timeout=60)
    finally:
        holder.kill()
        holder.wait()
    assert proc.returncode == 75, err
    assert (h.root / "vms" / vm).exists(), "a refused hold must not touch the VM"
    assert h.tart_calls("delete") == []
    assert (h.lease_root / "suite" / "pid").read_text().strip() == str(holder.pid), "the holder's lease is left alone"


@_POSIX_ONLY
def test_a_terminated_hold_still_destroys_the_vm_and_releases(h: Harness) -> None:
    vm = "nx-mac-run-t3"
    (h.root / "vms" / vm).touch()
    proc = _start_hold(h, vm)
    assert proc.stdout is not None
    assert proc.stdout.readline().startswith("HOLD: acquired")
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=60)
    assert proc.returncode == 143
    assert h.leftovers()["lease"] == [] and not (h.root / "vms" / vm).exists()


@_POSIX_ONLY
def test_the_lifetime_cap_ends_a_hold_that_nobody_closes(h: Harness) -> None:
    vm = "nx-mac-run-t4"
    (h.root / "vms" / vm).touch()
    proc = _start_hold(h, vm, NX_MAC_GUEST_LIFETIME_SECS="3")
    proc.wait(timeout=60)  # stdin stays open, so only the cap can end it
    assert proc.returncode == 0
    assert h.leftovers()["lease"] == [] and not (h.root / "vms" / vm).exists()


@_POSIX_ONLY
def test_a_kept_hold_still_destroys_at_the_cap_after_its_run_directory_is_removed(h: Harness) -> None:
    # launch.sh starts the hold as $RD/host.sh and removes $RD when it ends; a --keep-guest
    # hold outlives that, and its destroy at the cap exited 127 (nexus-2fn57).
    vm = "nx-mac-run-t4k"
    (h.root / "vms" / vm).touch()
    rd = h.tmp / "rd"
    rd.mkdir()
    shutil.copy2(h.host, rd / "host.sh")
    proc = subprocess.Popen(
        [str(rd / "host.sh"), "hold", vm, "keep"], env=h.env(NX_MAC_GUEST_LIFETIME_SECS="6"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=h.tmp,
        start_new_session=True,
    )
    h.procs.append(proc)
    assert proc.stdout is not None and proc.stdin is not None
    assert proc.stdout.readline().startswith("HOLD: acquired the suite lease")
    proc.stdin.close()
    proc.stdin = None
    shutil.rmtree(rd)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert "was not destroyed" not in out, out
    assert h.tart_calls("delete") == [f"delete {vm}"]
    assert h.leftovers()["lease"] == [] and not (h.root / "vms" / vm).exists()
    assert list((h.tmp / "tmp").iterdir()) == [], "the hold's private copy is removed"


@_POSIX_ONLY
def test_a_refused_hold_leaves_no_private_copy(h: Harness) -> None:
    vm = "nx-mac-run-t4r"
    (h.root / "vms" / vm).touch()
    holder = _sleeper()
    try:
        h.hold_lease(holder.pid)
        proc = _start_hold(h, vm)
        proc.communicate(timeout=60)
    finally:
        holder.kill()
        holder.wait()
    assert proc.returncode == 75
    assert list((h.tmp / "tmp").iterdir()) == []


# ---- destroy -----------------------------------------------------------------------


@_POSIX_ONLY
def test_destroy_retries_a_failed_delete(h: Harness) -> None:
    vm = "nx-mac-run-t5"
    (h.root / "vms" / vm).touch()
    res = h.run_host("destroy", vm, FAKE_DELETE_FAIL_N="2")
    assert res.returncode == 0, res.stderr
    assert len(h.tart_calls("delete")) == 3, "two failures then the delete that worked"
    assert not (h.root / "vms" / vm).exists()


@_POSIX_ONLY
def test_destroy_gives_up_after_three_attempts_and_says_so(h: Harness) -> None:
    vm = "nx-mac-run-t6"
    (h.root / "vms" / vm).touch()
    res = h.run_host("destroy", vm, FAKE_DELETE_FAIL_N="99")
    assert res.returncode == 1
    assert len(h.tart_calls("delete")) == 3
    assert "cannot destroy" in res.stderr


@_POSIX_ONLY
def test_a_vm_that_ignores_tart_stop_has_its_tart_run_killed_then_is_deleted(h: Harness) -> None:
    vm = "nx-mac-run-t7"
    (h.root / "vms" / vm).touch()
    h.spawn_tart_run(vm)
    res = h.run_host("destroy", vm, FAKE_STOP_HANGS="1")
    assert res.returncode == 0, res.stderr
    assert "killing its tart run" in res.stderr
    assert not (h.root / "vms" / vm).exists()
    assert not (h.root / "vms" / f"{vm}.runpid").exists()


@_POSIX_ONLY
def test_destroy_closes_the_ssh_sessions_into_the_guest(h: Harness) -> None:
    vm = "nx-mac-run-t7b"
    (h.root / "vms" / vm).touch()
    h.spawn_tart_run(vm)
    argv0 = f"ssh -i {h.base}/ssh/id_ed25519 -o BatchMode=yes admin@192.168.64.5"
    session = subprocess.Popen(
        ["bash", "-c", f'exec -a "{argv0}" sleep 120'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    h.procs.append(session)
    time.sleep(0.5)
    other = subprocess.Popen(
        ["bash", "-c", 'exec -a "ssh -i /elsewhere/key admin@192.168.64.5" sleep 120'], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    h.procs.append(other)
    time.sleep(0.5)
    assert h.run_host("destroy", vm).returncode == 0
    assert session.wait(timeout=10) is not None, "the session into the destroyed guest must be closed"
    assert other.poll() is None, "only this host's own guest sessions are closed"


@_POSIX_ONLY
def test_destroy_only_touches_the_names_it_owns(h: Harness) -> None:
    res = h.run_host("destroy", "nexus-mac-clean")
    assert res.returncode == 2
    assert (h.root / "vms" / "nexus-mac-clean").exists()


# ---- the deadman -------------------------------------------------------------------


@_POSIX_ONLY
def test_the_host_side_deadman_deletes_a_vm_nobody_came_back_for(h: Harness) -> None:
    vm = "nx-mac-run-t8"
    res = h.run_host("start", vm, NX_MAC_GUEST_LIFETIME_SECS="2")
    assert res.returncode == 0, res.stderr
    assert (h.root / "vms" / vm).exists()
    deadline = time.monotonic() + 30
    while (h.root / "vms" / vm).exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not (h.root / "vms" / vm).exists(), "the deadman must delete the VM at its cap"
    assert "deadman: lifetime cap" in (h.base / "logs" / f"{vm}.deadman.log").read_text()


@_POSIX_ONLY
def test_destroying_a_vm_stops_its_deadman(h: Harness) -> None:
    vm = "nx-mac-run-t9"
    assert h.run_host("start", vm).returncode == 0
    pidfile = h.base / "deadman" / f"{vm}.pid"
    pid = int(pidfile.read_text())
    assert _alive(pid)
    kids = [int(k) for k in subprocess.run(["/usr/bin/pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()]
    assert kids, "the deadman's sleep must exist for this test to prove anything"
    assert h.run_host("destroy", vm).returncode == 0
    deadline = time.monotonic() + 10
    while (_alive(pid) or any(_alive(k) for k in kids)) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid) and not pidfile.exists()
    assert not any(_alive(k) for k in kids), "the deadman's sleep is killed too, not left orphaned"
    time.sleep(0.5)
    log = h.base / "logs" / f"{vm}.deadman.log"
    # killing the sleep first woke the deadman, which logged a false cap line (nexus-knyvj)
    assert "lifetime cap" not in (log.read_text() if log.exists() else "")


@_POSIX_ONLY
def test_stopping_a_deadman_never_wakes_it_even_when_its_sleep_dies_first(h: Harness) -> None:
    # nexus-knyvj, deterministic: a pkill that lingers after killing gives a sleep-first stop a full second
    # in which the woken deadman logs its cap line. The fixed stop kills the deadman first and never calls pkill.
    _exe(h.bin / "pkill", '#!/bin/bash\n/usr/bin/pkill "$@"; rc=$?; sleep 1; exit $rc\n')
    vm = "nx-mac-run-t9d"
    assert h.run_host("start", vm).returncode == 0
    pid = int((h.base / "deadman" / f"{vm}.pid").read_text())
    assert h.run_host("destroy", vm).returncode == 0
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)
    log = h.base / "logs" / f"{vm}.deadman.log"
    assert "lifetime cap" not in (log.read_text() if log.exists() else "")


@_POSIX_ONLY
def test_the_launcher_never_shares_an_ssh_control_master() -> None:
    body = _read(LAUNCH_SH)
    opts = next(ln for ln in body.splitlines() if ln.startswith("SSH_OPTS=("))
    assert "-o ControlPath=none" in opts


# ---- the guest hop -----------------------------------------------------------------


@_POSIX_ONLY
def test_an_address_outside_the_vmnet_range_is_never_connected_to(h: Harness) -> None:
    vm = "nx-mac-run-t10"
    h.spawn_tart_run(vm)
    res = h.run_host("waitssh", vm, "4", FAKE_IP="10.9.9.9")
    assert res.returncode == 1
    assert "outside tart's vmnet range" in res.stderr
    log = (h.root / "ssh.log")
    assert not log.exists() or "admin@" not in log.read_text(), "the key must not be offered to a stranger"


@_POSIX_ONLY
@pytest.mark.parametrize("ip", ["192.168.64.1", "192.168.64.255", "192.168.65.5", "192.168.64.5.6", "192.168.64.05x"])
def test_only_addresses_in_the_guest_range_are_accepted(h: Harness, ip: str) -> None:
    vm = "nx-mac-run-t11"
    h.spawn_tart_run(vm)
    res = h.run_host("waitssh", vm, "3", FAKE_IP=ip)
    assert res.returncode == 1


@_POSIX_ONLY
def test_the_guest_hop_pins_the_host_key_and_refuses_passwords(h: Harness) -> None:
    vm = "nx-mac-run-t12"
    h.spawn_tart_run(vm)
    res = h.run_host("waitssh", vm, "20")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "192.168.64.5"
    opts = (h.root / "guest-ssh-opts.log").read_text()
    assert "StrictHostKeyChecking=yes" in opts
    assert f"UserKnownHostsFile={h.base}/ssh/known_hosts" in opts
    assert "StrictHostKeyChecking=no" not in opts and "UserKnownHostsFile=/dev/null" not in opts
    assert "BatchMode=yes" in opts and "PasswordAuthentication=no" in opts and "ServerAliveInterval=" in opts


@_POSIX_ONLY
def test_hostkey_pins_the_ed25519_key_read_through_the_guest_agent(h: Harness) -> None:
    (h.base / "ssh" / "known_hosts").unlink()
    res = h.run_host("hostkey", "nexus-mac-build")
    assert res.returncode == 0, res.stderr
    pinned = (h.base / "ssh" / "known_hosts").read_text().splitlines()
    assert pinned == ["* ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"]
    assert h.tart_calls("exec") == ["exec nexus-mac-build /bin/cat /etc/ssh/ssh_host_ed25519_key.pub"]


@_POSIX_ONLY
def test_hostkey_refuses_anything_but_an_ed25519_key_and_leaves_the_old_pin(h: Harness) -> None:
    before = (h.base / "ssh" / "known_hosts").read_text()
    res = h.run_host("hostkey", "nexus-mac-build", FAKE_HOSTKEY="ssh-rsa AAAAB3NzaC1yc2E root@guest")
    assert res.returncode == 1
    assert (h.base / "ssh" / "known_hosts").read_text() == before
    assert h.run_host("hostkey", "nx-mac-run-x").returncode == 2


def test_provision_regenerates_host_keys_and_turns_off_password_ssh_after_the_key_is_checked() -> None:
    text = _read(PROVISION_SH)
    code = _code(PROVISION_SH)
    assert "100-macos.conf" in text, "the comment must say why the drop-in is numbered 050"
    assert re.search(r"tee /etc/ssh/sshd_config\.d/050-nexus-keyonly\.conf", code), "sshd takes the first value it sees: 050 sorts before 100-macos.conf"
    assert "PasswordAuthentication no" in code and "KbdInteractiveAuthentication no" in code
    assert code.index("authorized_keys") < code.index("tee /etc/ssh/sshd_config.d"), (
        "the key must be confirmed before password login is closed"
    )
    assert re.search(r"^sudo -n ssh-keygen -A\b", code, re.M) and re.search(r"^sudo -n rm -f /etc/ssh/ssh_host_\*", code, re.M)
    assert re.search(r'^eff="\$\(sudo -n /usr/sbin/sshd -T', code, re.M), "the effective config is read back, not assumed"
    assert re.search(r"grep -qix 'passwordauthentication no' <<<\"\$eff\"", code)
    assert re.search(r"grep -qix 'kbdinteractiveauthentication no' <<<\"\$eff\"", code)
    # the admin password must stay: kcpassword auto-login depends on it
    assert not re.search(r"\bpasswd\b|chpasswd|sysadminctl|dscl\b.*-passwd|resetPasswordFor", code)


def test_build_image_pins_before_the_first_ssh_and_again_after_provisioning() -> None:
    code = _code(BUILD_SH)
    first = code.index("host.sh hostkey nexus-mac-build")
    assert first < code.index("waitssh nexus-mac-build"), "the first ssh must already be strict"
    second = code.index("host.sh hostkey nexus-mac-build", first + 1)
    assert code.index("provision.sh failed") < second < code.index("shutdown -h now")
    assert "gssh nexus-mac-build true" in code[second:], "the new pin is proved against the live sshd"


def test_the_host_scripts_never_turn_host_key_checking_off() -> None:
    for p in (HOST_SH, LAUNCH_SH, BUILD_SH):
        code = _code(p)
        assert "StrictHostKeyChecking=no" not in code and "UserKnownHostsFile=/dev/null" not in code, p.name
    assert "StrictHostKeyChecking=yes" in _code(HOST_SH)
    for p in (HOST_SH, LAUNCH_SH, BUILD_SH):
        assert "BatchMode=yes" in _code(p) and "ServerAliveInterval" in _code(p), p.name


# ---- reboot ------------------------------------------------------------------------


@_POSIX_ONLY
def test_a_reboot_needs_a_new_boot_session_and_ssh_seen_down(h: Harness) -> None:
    vm = "nx-mac-run-t13"
    h.spawn_tart_run(vm)
    res = h.run_host("reboot", vm, "60")
    assert res.returncode == 0, res.stderr
    assert "boot session before: 11111111-2222-3333-4444-555555555555" in res.stdout
    assert "boot session after: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in res.stdout


@_POSIX_ONLY
def test_a_new_boot_session_without_ssh_ever_going_down_is_not_a_reboot(h: Harness) -> None:
    vm = "nx-mac-run-t14"
    h.spawn_tart_run(vm)
    res = h.run_host("reboot", vm, "30", FAKE_NO_DOWN="1")
    assert res.returncode == 1
    assert "never seen down" in res.stderr


@_POSIX_ONLY
def test_a_guest_that_does_not_reboot_fails_at_the_deadline(h: Harness) -> None:
    vm = "nx-mac-run-t15"
    h.spawn_tart_run(vm)
    res = h.run_host("reboot", vm, "5", FAKE_NO_REBOOT="1")
    assert res.returncode == 1
    assert "did not come back with a new boot session" in res.stderr


@_POSIX_ONLY
def test_a_vm_that_stopped_instead_of_rebooting_is_a_failure(h: Harness) -> None:
    res = h.run_host("reboot", "nx-mac-run-t16", "10")
    assert res.returncode == 1
    assert "stopped instead of rebooting" in res.stderr


# ---- a whole launch ----------------------------------------------------------------


def _line(res: subprocess.CompletedProcess[str], needle: str) -> bool:
    return needle in res.stdout


@_POSIX_ONLY
def test_a_clean_run_passes_holds_the_lease_throughout_and_leaves_nothing(h: Harness) -> None:
    res = h.run_launch()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "MACOS GUEST ROW 1 PASSED: rows run 1, checks passed 12, failed 0" in res.stdout
    seen = (h.root / "lease-seen.log").read_text().splitlines()
    assert [ln.split(":")[0] for ln in seen] == ["pre", "post"]
    assert all("held by live pid" in ln for ln in seen), seen
    assert h.leftovers() == {k: [] for k in h.leftovers()}, h.leftovers()
    assert len(h.tart_calls("delete")) == 1 and len(h.tart_calls("clone")) == 1
    assert "the hellmini suite lease is released" in _read(h.out / "launch.log")
    assert (h.out / "guest-out" / "checks-pre.txt").exists() and (h.out / "guest-out" / "checks-post.txt").exists()
    assert _read(h.root / "guest-actions.log").split() == ["shutdown"]


@_POSIX_ONLY
def test_a_suite_started_mid_run_fails_closed(h: Harness) -> None:
    proc = h.start_launch(FAKE_PRE="sleep")
    _wait_for(h.root / "phase-started", 120)
    try:
        assert _suite_lease.acquire("a suite", lease_root=h.lease_root) is None
        guard = h.run_host("guard")
        assert guard.returncode == 75 and "holds nexus-build-lease/suite" in guard.stderr
    finally:
        proc.terminate()
        proc.communicate(timeout=120)
    assert h.leftovers()["lease"] == []


@_POSIX_ONLY
def test_a_run_started_while_a_suite_holds_the_lease_exits_75_before_cloning(h: Harness) -> None:
    holder = _sleeper()
    try:
        h.hold_lease(holder.pid)
        res = h.run_launch()
    finally:
        holder.kill()
        holder.wait()
    assert res.returncode == 75, res.stdout
    assert h.tart_calls("clone") == [] and h.vms() == []
    assert (h.lease_root / "suite" / "pid").read_text().strip() == str(holder.pid)


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("what", "extra"),
    [("a leftover run vm", {}), ("a running ci job", {"FAKE_RUNNER_BUSY": "1"})],
    ids=["leftover-vm", "ci-job"],
)
def test_the_guard_stops_a_run_before_the_hold_and_the_clone(h: Harness, what: str, extra: dict[str, str]) -> None:
    if not extra:
        (h.root / "vms" / "nx-mac-run-leftover").touch()
    res = h.run_launch(**extra)
    assert res.returncode == 75, (what, res.stdout)
    assert "hellmini is busy" in res.stdout
    assert h.tart_calls("clone") == [] and not h.lease_root.exists() and h.leftovers()["run dirs"] == []


@_POSIX_ONLY
def test_a_signed_out_tart_account_stops_the_run_with_3_before_the_lease_or_a_clone(h: Harness) -> None:
    res = h.run_launch(FAKE_GUI_ABSENT="1")
    assert res.returncode == 3, res.stdout
    assert "is not signed in at the hellmini login window" in res.stdout
    assert h.tart_calls("clone") == [] and h.leftovers()["lease"] == [] and h.leftovers()["run dirs"] == []


@_POSIX_ONLY
def test_a_failed_pre_phase_destroys_the_vm_once_and_never_reboots(h: Harness) -> None:
    res = h.run_launch(FAKE_PRE="fail")
    assert res.returncode == 1, res.stdout
    assert "pre phase failed" in res.stdout and "MACOS GUEST ROW 1 FAILED" in res.stdout
    assert len(h.tart_calls("delete")) == 1, "destroy ran once, and the hold's own destroy found nothing left"
    assert not (h.root / "guest-actions.log").exists(), "no reboot after a failed pre phase"
    assert h.leftovers() == {k: [] for k in h.leftovers()}, h.leftovers()


@_POSIX_ONLY
def test_a_failed_delete_is_retried_and_the_run_still_cleans_up(h: Harness) -> None:
    res = h.run_launch(FAKE_PRE="fail", FAKE_DELETE_FAIL_N="1")
    assert res.returncode == 1
    assert len(h.tart_calls("delete")) == 2, "the failed delete is tried again"
    assert "WARNING" not in res.stdout, res.stdout
    assert h.leftovers() == {k: [] for k in h.leftovers()}, h.leftovers()


@_POSIX_ONLY
def test_a_delete_that_never_works_is_reported_not_hidden(h: Harness) -> None:
    res = h.run_launch(FAKE_PRE="fail", FAKE_DELETE_FAIL_N="99")
    assert res.returncode == 1
    assert "may still exist" in res.stdout
    assert h.vms() != [] and h.leftovers()["lease"] == [], "the lease is released; the deadman owns the leftover VM"
    # 3 attempts inside each destroy: the launcher's two tries, then the hold's own on its way out
    assert len(h.tart_calls("delete")) == 9, h.tart_calls("delete")


@_POSIX_ONLY
@pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)], ids=["TERM", "INT", "HUP"])
def test_a_signal_during_a_phase_kills_it_at_once_and_cleans_up(h: Harness, sig: signal.Signals, code: int) -> None:
    proc = h.start_launch(FAKE_PRE="sleep")
    _wait_for(h.root / "phase-started", 120)
    phase_pid = int((h.root / "phase.pid").read_text())
    t0 = time.monotonic()
    os.kill(proc.pid, sig)  # the script only: its children get nothing, so it must reap them itself
    proc.communicate(timeout=120)
    assert proc.returncode == code
    assert time.monotonic() - t0 < 60, "the 300 s phase was waited out"
    deadline = time.monotonic() + 10
    while _alive(phase_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(phase_pid), "the phase process survived the run"
    assert len(h.tart_calls("delete")) == 1
    assert h.leftovers() == {k: [] for k in h.leftovers()}, h.leftovers()


@_POSIX_ONLY
def test_a_second_signal_during_the_cleanup_does_not_interrupt_it(h: Harness) -> None:
    proc = h.start_launch(FAKE_PRE="sleep", FAKE_DELETE_SLOW="4")
    _wait_for(h.root / "phase-started", 120)
    signal_group(proc, signal.SIGTERM)  # the whole group, as a terminal does
    time.sleep(2)  # inside the slow `tart delete` of the launcher's own destroy
    assert proc.poll() is None, "the cleanup ended before the second signal; the test would prove nothing"
    signal_group(proc, signal.SIGTERM)
    proc.communicate(timeout=120)
    assert proc.returncode == 143
    assert h.vms() == [], "the VM must be gone though the cleanup was signalled again"
    assert h.leftovers()["lease"] == []
    log = _read(h.out / "launch.log")
    assert f"destroyed nx-mac-run-" in log, "the launcher's own destroy must have run to its end"
    assert "WARNING" not in log, log


def test_the_cleanup_ignores_signals_rather_than_handling_them() -> None:
    body = _read(LAUNCH_SH)
    finish = body[body.index("finish() {"):body.index("on_exit()")]
    assert "trap '' INT TERM HUP" in finish, "children inherit an ignored signal; they reset a handled one"
    assert "trap ':'" not in finish


@_POSIX_ONLY
def test_a_token_string_anywhere_in_the_output_fails_the_run(h: Harness) -> None:
    res = h.run_launch(FAKE_PRE="leak")
    assert res.returncode == 1, res.stdout
    assert "sk-ant-" in res.stdout and "10-setup.txt" in res.stdout
    assert "NOTREAL" not in res.stdout, "the run must name the file, never print the string"
    assert h.leftovers() == {k: [] for k in h.leftovers()}


@_POSIX_ONLY
@pytest.mark.parametrize("mode", ["short", "extra", "dup"])
def test_the_run_needs_exactly_the_twelve_check_names(h: Harness, mode: str) -> None:
    res = h.run_launch(FAKE_POST=mode)
    assert res.returncode == 1, res.stdout
    assert "are not the 12 expected ones" in res.stdout


@_POSIX_ONLY
def test_a_failing_check_fails_the_run_even_with_all_twelve_present(h: Harness) -> None:
    res = h.run_launch(FAKE_POST="fail")
    assert res.returncode == 1
    assert "checks passed 11, failed 1" in res.stdout


@_POSIX_ONLY
def test_a_missing_automation_token_stops_before_any_host_work(h: Harness) -> None:
    res = h.run_launch(FAKE_NO_TOKEN="1")
    assert res.returncode == 3
    assert not (h.root / "ssh.log").exists()


def test_the_expected_check_names_are_exactly_the_ones_row1_emits() -> None:
    emitted = set(re.findall(r'^\s*check (\w+) ', _read(ROW1_SH), re.M)) - {"deps"}
    assert emitted == set(ALL_CHECKS)
    declared = re.search(r'^EXPECTED_CHECKS="([^"]+)"', _read(LAUNCH_SH), re.M)
    assert declared is not None and set(declared.group(1).split()) == set(ALL_CHECKS)
    assert len(declared.group(1).split()) == 12 == int(re.search(r"^EXPECTED_COUNT=(\d+)", _read(LAUNCH_SH), re.M).group(1))


def test_the_phases_run_in_the_background_so_a_signal_is_acted_on() -> None:
    code = _code(LAUNCH_SH)
    for phase in ("bgwait phase pre", "bgwait phase post", "bgwait hv reboot", "bgwait hv waitssh", "bgwait deliver"):
        assert phase in code, phase
    assert "wait \"$BG_PID\"" in code and "kill_tree" in code


# ---- row1.sh inside the guest --------------------------------------------------------


def _row1_env(h: Harness, model: str, **extra: str) -> dict[str, str]:
    stub = h.tmp / "stubs"
    stub.mkdir(exist_ok=True)
    _exe(stub / "sysctl", f"#!/bin/bash\n[[ \"$*\" == *hw.model* ]] && {{ echo '{model}'; exit 0; }}\nexit 1\n")
    home = h.tmp / "guesthome"
    home.mkdir(exist_ok=True)
    return {
        "PATH": f"{stub}{os.pathsep}/usr/bin{os.pathsep}/bin{os.pathsep}/usr/sbin{os.pathsep}/sbin",
        "HOME": str(home),
        **extra,
    }


@_POSIX_ONLY
def test_row1_refuses_to_run_on_a_machine_that_is_not_a_tart_guest(h: Harness) -> None:
    env = _row1_env(h, "Mac17,16", **{"CLAUDE_CODE_OAUTH_TOKEN": "x"})
    res = subprocess.run([str(h.row1), "pre"], env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 2
    assert "refusing to run outside a tart guest" in res.stderr and "Mac17,16" in res.stderr
    assert not (Path(env["HOME"]) / "nxmvv").exists(), "nothing may be created before the refusal"


@_POSIX_ONLY
def test_row1_goes_on_in_a_virtualmac_guest(h: Harness) -> None:
    env = _row1_env(h, "VirtualMac2,1")
    res = subprocess.run([str(h.row1), "pre"], env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 1  # past the model gate, then the missing claude is the first failed check
    assert "CHECK FAIL deps: claude is not on PATH" in res.stdout


@_POSIX_ONLY
def test_the_automation_token_reaches_only_the_claude_calls(h: Harness) -> None:
    env = _row1_env(h, "VirtualMac2,1", **{"CLAUDE_CODE_OAUTH_TOKEN": "fake-token-for-the-test"})
    home = Path(env["HOME"])
    stub = home / ".local" / "bin"  # row1.sh resets PATH to this directory and the system's
    stub.mkdir(parents=True)
    (home / "nxmvv" / "plugin" / "conexus").mkdir(parents=True)
    seen = h.tmp / "token-seen.log"
    for name in ("claude", "nx", "uv"):
        _exe(
            stub / name,
            "#!/bin/bash\n"
            f"if [[ -n \"${{CLAUDE_CODE_OAUTH_TOKEN:-}}\" ]]; then t=set; else t=unset; fi\n"
            f"echo \"{name} $1: $t\" >> {seen}\n"
            "exit 0\n",
        )
    subprocess.run([str(h.row1), "pre"], env=env, capture_output=True, text=True, timeout=180)
    lines = sorted(set(seen.read_text().splitlines()))
    assert "claude -p: set" in lines, lines  # the turns that need the token get it
    assert not [ln for ln in lines if ln.startswith("claude -p") and ln.endswith("unset")], lines
    others = [ln for ln in lines if not ln.startswith("claude -p")]
    assert others and all(ln.endswith("unset") for ln in others), f"a command other than a claude turn saw the token: {others}"
    assert "fake-token-for-the-test" not in (home / "nxmvv" / "out" / "checks-pre.txt").read_text()


def test_row1_records_the_nx_version_and_requires_a_new_job_pid_after_the_reboot() -> None:
    code = _code(ROW1_SH)
    assert 'v="$(nx --version 2>&1)"' in code and code.count("$(nx_version)") >= 2
    assert '"$pid" != "$prev"' in code and '-n "$prev"' in code


def test_every_script_has_the_spdx_header_and_is_executable() -> None:
    for p in (HOST_SH, LAUNCH_SH, ROW1_SH, PROVISION_SH, BUILD_SH):
        assert _read(p).split("\n", 2)[1] == "# SPDX-License-Identifier: AGPL-3.0-or-later", p.name
        assert p.stat().st_mode & stat.S_IXUSR, p.name
