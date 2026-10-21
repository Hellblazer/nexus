# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/diag/container_io_sampler.sh -- the chas diagnostic that finds who writes to the host disk
while a command runs (nexus-3cwsw).

The parsing helpers are sourced and run against fixture files; one end-to-end run drives ``main``
with a fake ``docker``/``pgrep`` on PATH and a fixture cgroup and /proc tree, so the max-per-container
rule, the daemon deltas, the json-log sizing (stat and the ``docker logs`` fallback), the report and
the exit status are all pinned. Nothing here needs Docker, root or Linux.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
import time
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "diag" / "container_io_sampler.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "io_sampler"


def _call(function: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one helper of the sourced script and return its result."""
    quoted = " ".join(f"'{a}'" for a in args)
    return subprocess.run(
        ["bash", "-c", f"source '{SCRIPT}'; {function} {quoted}"], capture_output=True, text=True, timeout=30,
    )


# ---- helpers against fixture files -------------------------------------------------------------


def test_sum_wbytes_adds_every_device_row() -> None:
    result = _call("sum_wbytes", str(FIXTURES / "io.stat.two-devices"))
    assert result.returncode == 0 and result.stdout.strip() == str(2097152 + 1000), result.stderr


def test_sum_wbytes_of_an_empty_io_stat_is_zero_and_of_a_missing_file_fails_silently() -> None:
    empty = _call("sum_wbytes", str(FIXTURES / "io.stat.empty"))
    assert (empty.returncode, empty.stdout.strip()) == (0, "0")
    missing = _call("sum_wbytes", str(FIXTURES / "no-such-file"))
    assert missing.returncode != 0 and missing.stdout == ""


def test_cgroup_path_comes_from_the_v2_line_and_v1_has_none() -> None:
    v2 = _call("cgroup_path_from_proc", str(FIXTURES / "proc-cgroup-v2"))
    assert v2.returncode == 0
    assert v2.stdout.strip() == "system.slice/docker-" + "0123456789abcdef" * 4 + ".scope"
    assert _call("cgroup_path_from_proc", str(FIXTURES / "proc-cgroup-v1")).returncode != 0
    assert _call("cgroup_path_from_proc", str(FIXTURES / "no-such-file")).returncode != 0


def test_record_max_only_grows_and_refuses_a_non_number(tmp_path: Path) -> None:
    f = tmp_path / "wmax"
    for value in ("100", "5000", "10"):
        assert _call("record_max", str(f), value).returncode == 0
    assert f.read_text().strip() == "5000"
    assert _call("record_max", str(f), "12x").returncode != 0
    assert f.read_text().strip() == "5000"


def test_fmt_bytes() -> None:
    assert _call("fmt_bytes", "999").stdout == "999 B"
    assert _call("fmt_bytes", "1048576").stdout == "1048576 (1.00 MiB)"


# ---- the report from a hand-built state directory ----------------------------------------------


def _container(state: Path, cid: str, *, caller: str, wmax: int | None, logmax: int = 0, logmode: str = "stat") -> None:
    d = state / "c" / cid
    d.mkdir(parents=True)
    (d / "caller").write_text(caller + "\n")
    if wmax is not None:
        (d / "wmax").write_text(f"{wmax}\n")
    (d / "logmax").write_text(f"{logmax}\n")
    (d / "logmode").write_text(logmode + "\n")


def test_report_totals_ranks_and_aggregates_by_caller(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _container(state, "a" * 64, caller="BigTest", wmax=9000, logmax=300)
    _container(state, "b" * 64, caller="BigTest", wmax=1000, logmax=200, logmode="docker")
    _container(state, "c" * 64, caller="SmallTest", wmax=4000, logmode="none")
    _container(state, "d" * 64, caller="-", wmax=None, logmode="none")  # never resolved
    (state / "d").mkdir()
    (state / "d" / "dockerd.cg").write_text("system.slice/docker.service\n")
    (state / "d" / "dockerd.base").write_text("1000\n")
    (state / "d" / "dockerd.end").write_text("501000\n")
    (state / "d" / "systemd-journal.skip").write_text("no process named systemd-journal\n")
    (state / "meta").write_text("meta_exit=0\nmeta_secs=12\nmeta_samples=6\n")
    result = _call("report", str(state))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "command exit: 0   duration: 12s   samples: 6   containers seen: 4" in out
    assert "unreadable or never resolved: 1" in out
    assert "sum of per-container wbytes (max per container, lower bound): 14000 (13.67 KiB)" in out
    assert "cgroup wbytes delta, dockerd (/system.slice/docker.service): 500000 (488.28 KiB)" in out
    assert "cgroup wbytes delta, systemd-journal: skipped (no process named systemd-journal)" in out
    assert "json-log bytes total: 500 B  (1 sized by stat of docker's LogPath, 1 by 'docker logs | wc -c'" in out
    containers = out.split("top 10 containers by wbytes:")[1].split("top 10 callers")[0].strip().splitlines()
    assert [line.split()[0] for line in containers] == ["9000", "4000", "1000", "0"]
    assert containers[0].split()[2] == "BigTest" and containers[1].split()[2] == "SmallTest"
    callers = out.split("top 10 callers (dev.nexus.test.caller) by summed wbytes:")[1].strip().splitlines()
    assert callers[0].split() == ["10000", "2", "containers", "BigTest"]
    assert callers[1].split() == ["4000", "1", "containers", "SmallTest"]


# ---- the whole script, with a fake docker -------------------------------------------------------

_FAKE_DOCKER = """\
#!/usr/bin/env bash
# Fake docker: ids in $FAKE_DIR/ids, one "<pid>|<logpath>|<caller>" line per id in $FAKE_DIR/info-<id>,
# `docker logs` output in $FAKE_DIR/logs-<id> (a logs-<id>.gone file: the container died, docker prints an
# error and fails). $FAKE_STALL lists subcommands that never answer; `ps` answers its first call (the
# usability probe) and stalls after it. The stall is the process itself (exec), like a hung daemon socket.
stall() { exec sleep "${FAKE_SLEEP:-300}"; }
case "$1" in
    ps)
        echo "$*" >> "$FAKE_DIR/ps-args"
        [[ -e "$FAKE_DIR/docker-down" ]] && exit 1
        [[ -e "$FAKE_DIR/docker-hang" ]] && stall
        if [[ -e "$FAKE_DIR/probed" ]]; then [[ " $FAKE_STALL " == *" ps "* ]] && stall; fi
        touch "$FAKE_DIR/probed"
        cat "$FAKE_DIR/ids" 2>/dev/null; exit 0 ;;
    inspect) [[ " $FAKE_STALL " == *" inspect "* ]] && stall; id="${@: -1}"; cat "$FAKE_DIR/info-$id" ;;
    logs)
        if [[ -e "$FAKE_DIR/logs-$2.gone" ]]; then echo "Error response from daemon: No such container: $2" >&2; exit 1; fi
        cat "$FAKE_DIR/logs-$2" ;;
    info) [[ " $FAKE_STALL " == *" info "* ]] && stall; echo "overlay2 json-file false 2" ;;
    *) exit 1 ;;
esac
"""

_FAKE_PGREP = """\
#!/usr/bin/env bash
# Fake pgrep -x NAME: prints $FAKE_DIR/pgrep/NAME if it exists.
f="$FAKE_DIR/pgrep/$2"
[[ -r "$f" ]] && cat "$f" && exit 0
exit 1
"""

CID_A = "a" * 64
CID_B = "b" * 64


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    fake = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    root = tmp_path / "cgroup"
    proc = tmp_path / "proc"
    for d in (fake / "pgrep", bin_dir, root, proc):
        d.mkdir(parents=True)
    for name, body in (("docker", _FAKE_DOCKER), ("pgrep", _FAKE_PGREP)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    # container A (pid 101) has a readable json log, container B (pid 102) an unreadable one
    (fake / "ids").write_text(f"{CID_A}\n{CID_B}\n")
    (tmp_path / "a-json.log").write_text("x" * 123)
    (fake / f"info-{CID_A}").write_text(f"101|{tmp_path}/a-json.log|ClassA\n")
    (fake / f"info-{CID_B}").write_text("102|/var/lib/docker/containers/none/none-json.log|ClassB\n")
    (fake / f"logs-{CID_B}").write_text("y" * 77)
    for pid, cg in (("101", "system.slice/docker-a.scope"), ("102", "system.slice/docker-b.scope"),
                    ("200", "system.slice/docker.service"), ("self", "user.slice/own.scope")):
        (proc / pid).mkdir()
        (proc / pid / "cgroup").write_text(f"0::/{cg}\n")
        (root / cg).mkdir(parents=True, exist_ok=True)
        (root / cg / "io.stat").write_text("259:0 rbytes=0 wbytes=0 rios=0 wios=0 dbytes=0 dios=0\n")
    (fake / "pgrep" / "dockerd").write_text("200\n")
    # one block device, 259:0 (the id the io.stat fixtures name), 8000 sectors written so far
    (proc / "diskstats").write_text("259 0 nvme0n1 100 0 800 10 200 0 8000 20 0 30 40\n")
    return {"tmp": tmp_path, "fake": fake, "bin": bin_dir, "root": root, "proc": proc}


def _env(world: dict[str, Path], extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{world['bin']}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(world["fake"]),
        "FAKE_STALL": "",
        # a sleep length unique to this test dir, so a stalled call that outlives the run can be found
        "FAKE_SLEEP": f"{300 + (zlib.crc32(str(world['tmp']).encode()) % 100000) / 1e6:.6f}",
        "NX_IO_SAMPLER_PROC": str(world["proc"]),
        "NX_IO_SAMPLER_STATE_BASE": str(world["tmp"]),
        "TMPDIR": str(world["tmp"]),
        **(extra or {}),
    }


def _run(
    world: dict[str, Path], command: str, *flags: str, extra_env: dict[str, str] | None = None, interval: str = "0.1",
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    # Output goes to files, not pipes: a process left behind by the script would hold a pipe open and make
    # this call wait for it, which hides exactly the leak the hang tests look for.
    out, err = world["tmp"] / "run.out", world["tmp"] / "run.err"
    with out.open("w") as o, err.open("w") as e:
        done = subprocess.run(
            ["bash", str(SCRIPT), "-i", interval, "-r", str(world["root"]), *flags, "--", "bash", "-c", command],
            stdout=o, stderr=e, text=True, timeout=timeout, env=_env(world, extra_env),
        )
    return subprocess.CompletedProcess(done.args, done.returncode, out.read_text(), err.read_text())


def _set_wbytes(world: dict[str, Path], cgroup: str, value: int) -> str:
    path = world["root"] / cgroup / "io.stat"
    return f"printf '259:0 rbytes=0 wbytes={value} rios=0 wios=0 dbytes=0 dios=0\\n' > '{path}'"


def test_end_to_end_keeps_the_max_per_container_and_the_daemon_delta(world: dict[str, Path]) -> None:
    a, b, d = "system.slice/docker-a.scope", "system.slice/docker-b.scope", "system.slice/docker.service"
    (world["root"] / d / "io.stat").write_text("259:0 rbytes=0 wbytes=1000 rios=0 wios=0 dbytes=0 dios=0\n")  # the baseline
    command = "; ".join([
        _set_wbytes(world, a, 100), _set_wbytes(world, b, 40), "sleep 0.5",
        _set_wbytes(world, a, 5000), _set_wbytes(world, d, 3000), "sleep 0.5",
        # container A's cgroup disappears with its container; its last reading must survive
        f"rm -f '{world['root'] / a / 'io.stat'}'", "sleep 0.3", "exit 7",
    ])
    result = _run(world, command)
    assert result.returncode == 7, "the command's exit status is the script's"
    err = result.stderr
    assert "command exit: 7" in err and "containers seen: 2" in err
    assert "sum of per-container wbytes (max per container, lower bound): 5040 (4.92 KiB)" in err
    assert "cgroup wbytes delta, dockerd (/system.slice/docker.service): 2000 (1.95 KiB)" in err
    assert "cgroup wbytes delta, containerd: skipped (no process named containerd)" in err
    assert "json-log bytes total: 200 B  (1 sized by stat of docker's LogPath, 1 by 'docker logs | wc -c'" in err
    top = err.split("top 10 containers by wbytes:")[1].splitlines()[1:3]
    assert top[0].split() == ["5000", "a" * 12, "ClassA"] and top[1].split() == ["40", "b" * 12, "ClassB"]


def test_end_to_end_says_what_it_could_not_read(world: dict[str, Path]) -> None:
    (world["root"] / "system.slice/docker-b.scope/io.stat").unlink()
    result = _run(world, "sleep 0.3")
    assert result.returncode == 0
    assert "unreadable or never resolved: 1" in result.stderr
    assert "cgroup wbytes delta, systemd-journal: skipped" in result.stderr


def test_the_container_filter_reaches_docker_ps(world: dict[str, Path]) -> None:
    result = _run(world, "sleep 0.3", "-f", "label=dev.nexus.test.run=r1", "-f", "name=pg")
    assert result.returncode == 0, result.stderr
    calls = (world["fake"] / "ps-args").read_text().splitlines()
    assert calls[0] == "ps -q", "the usability probe has no filter"
    sampled = calls[1:]
    assert sampled and all(
        line == "ps -q --no-trunc --filter label=dev.nexus.test.run=r1 --filter name=pg" for line in sampled
    ), sampled


def test_it_refuses_to_run_the_command_when_docker_is_unusable(world: dict[str, Path]) -> None:
    (world["fake"] / "docker-down").write_text("")
    marker = world["tmp"] / "ran"
    result = _run(world, f"touch '{marker}'")
    assert result.returncode == 2 and not marker.exists()
    assert "docker is not usable" in result.stderr


def test_it_needs_a_command() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2


def test_shellcheck_is_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        if os.environ.get("CI"):
            pytest.fail("shellcheck is missing on CI; install it so this gate is not vacuous")
        pytest.skip("shellcheck is not installed")
    result = subprocess.run([shellcheck, str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_script_is_executable_and_bash_3_safe() -> None:
    assert os.access(SCRIPT, os.X_OK)
    text = SCRIPT.read_text()
    # macOS ships bash 3.2: no associative arrays, mapfile or ${var^^} in a diagnostic people may run there.
    for banned in ("declare -A", "mapfile", "readarray", "${@: -1}"):
        assert banned not in textwrap.dedent(text), banned


# ---- nexus-3cwsw fix round: bounded docker calls, the device reconcile, -f with no match, hardening ------

_BOUNDS = {
    "NX_IO_SAMPLER_DOCKER_TIMEOUT": "1",
    "NX_IO_SAMPLER_GRACE": "1",
    "NX_IO_SAMPLER_FINAL": "2",
}
# A hung docker must cost seconds, not the 300 s the fake sleeps: DOCKER_TIMEOUT 1 s (to 2 s granularity),
# GRACE 1 s, FINAL 2 s, plus the report. 30 s is generous for that and far under the fake's stall.
_HANG_BOUND_SECONDS = 30


def _kept_state(stderr: str) -> Path:
    line = [ln for ln in stderr.splitlines() if ln.startswith("(raw samples kept in ")][0]
    return Path(line[len("(raw samples kept in "):-1])


def _assert_no_stalled_calls_left(world: dict[str, Path]) -> None:
    """The fake's stalled docker calls sit under command and process substitutions under the sampler loop: a
    kill that stops only the loop's own pid orphans them (they would sleep out their 300 s)."""
    sleep_arg = _env(world)["FAKE_SLEEP"]
    leaked: list[str] = []
    for _ in range(3):  # a killed process can take a moment to leave the table
        leaked = subprocess.run(["pgrep", "-f", f"sleep {sleep_arg}"], capture_output=True, text=True).stdout.split()
        if not leaked:
            return
        time.sleep(0.2)
    subprocess.run(["kill", *leaked], check=False)  # do not leave them behind
    raise AssertionError(f"stalled docker calls outlived the run: {leaked}")


def test_a_docker_that_stalls_after_the_probe_cannot_hang_the_sampler(world: dict[str, Path]) -> None:
    started = time.monotonic()
    result = _run(world, "sleep 0.5", extra_env={**_BOUNDS, "FAKE_STALL": "ps"}, timeout=_HANG_BOUND_SECONDS)
    assert time.monotonic() - started < _HANG_BOUND_SECONDS
    assert result.returncode == 0, result.stderr
    assert "BOUNDS THAT EXPIRED" in result.stderr, "the skip is reported, not silent"
    assert "docker ps did not answer in 1s" in result.stderr


def test_a_docker_that_stalls_on_inspect_and_info_cannot_hang_the_sampler(world: dict[str, Path]) -> None:
    started = time.monotonic()
    result = _run(world, "sleep 0.5; exit 5", extra_env={**_BOUNDS, "FAKE_STALL": "inspect info"}, timeout=_HANG_BOUND_SECONDS)
    assert time.monotonic() - started < _HANG_BOUND_SECONDS
    assert result.returncode == 5, "the command's status survives the stalled daemon"
    assert "BOUNDS THAT EXPIRED" in result.stderr
    assert "docker inspect did not answer in 1s" in result.stderr
    assert "docker (driver, logging driver, debug, cgroup version): unavailable" in result.stderr
    _assert_no_stalled_calls_left(world)


def test_a_sampler_loop_that_outlives_its_grace_is_killed_and_reported(world: dict[str, Path]) -> None:
    # The loop is inside a docker call that outlasts the grace (DOCKER_TIMEOUT 20 s > GRACE 1 s).
    started = time.monotonic()
    result = _run(
        world, "sleep 0.5",
        extra_env={**_BOUNDS, "NX_IO_SAMPLER_DOCKER_TIMEOUT": "20", "NX_IO_SAMPLER_FINAL": "1", "FAKE_STALL": "ps"},
        timeout=_HANG_BOUND_SECONDS,
    )
    assert time.monotonic() - started < _HANG_BOUND_SECONDS
    assert result.returncode == 0, result.stderr
    assert "sampler loop still running 1s after the command ended; killed" in result.stderr
    _assert_no_stalled_calls_left(world)


def test_it_does_not_run_the_command_when_the_probe_itself_hangs(world: dict[str, Path]) -> None:
    (world["fake"] / "docker-hang").write_text("")
    marker = world["tmp"] / "ran"
    started = time.monotonic()
    result = _run(world, f"touch '{marker}'", extra_env=_BOUNDS, timeout=_HANG_BOUND_SECONDS)
    assert time.monotonic() - started < _HANG_BOUND_SECONDS
    assert result.returncode == 2 and not marker.exists()
    assert "did not answer in 1s" in result.stderr


def test_the_report_gives_the_docker_driver_and_reconciles_with_the_device(world: dict[str, Path]) -> None:
    a = "system.slice/docker-a.scope"
    diskstats = world["proc"] / "diskstats"
    command = "; ".join([
        _set_wbytes(world, a, 4096),
        "sleep 0.5",
        # 80 more sectors written to the device = 40960 bytes
        f"printf '259 0 nvme0n1 100 0 800 10 200 0 8080 20 0 30 40\\n' > '{diskstats}'",
        "sleep 0.3",
    ])
    result = _run(world, command)
    assert result.returncode == 0, result.stderr
    err = result.stderr
    assert "docker (driver, logging driver, debug, cgroup version): overlay2 json-file false 2" in err
    assert "device wbytes (/proc/diskstats), nvme0n1 (259:0): 40960 (40.00 KiB)" in err
    assert "attributed to sampled cgroups (containers + daemons + the command's cgroup): 4096 (4.00 KiB)" in err
    assert "unattributed = device - attributed: 36864 (36.00 KiB)" in err


def test_the_report_says_when_there_is_no_device_to_reconcile(world: dict[str, Path]) -> None:
    (world["proc"] / "diskstats").unlink()
    result = _run(world, "sleep 0.3")
    assert result.returncode == 0, result.stderr
    assert "device writes (/proc/diskstats): none to reconcile" in result.stderr
    assert "unattributed" not in result.stderr


def test_diskstats_delta_reads_sectors_written_for_the_named_devices_only(tmp_path: Path) -> None:
    base, end, devs = tmp_path / "base", tmp_path / "end", tmp_path / "devs"
    base.write_text("259 0 nvme0n1 1 0 1 1 1 0 1000 1 0 1 1\n8 0 sda 1 0 1 1 1 0 5 1 0 1 1\n")
    end.write_text("259 0 nvme0n1 1 0 1 1 1 0 1008 1 0 1 1\n8 0 sda 1 0 1 1 1 0 99 1 0 1 1\n")
    devs.write_text("259:0\n")
    result = _call("diskstats_delta", str(base), str(end), str(devs))
    assert result.returncode == 0 and result.stdout.split() == ["nvme0n1", "259:0", "4096"], result.stdout


def test_a_filter_that_matched_no_container_is_not_a_success(world: dict[str, Path]) -> None:
    (world["fake"] / "ids").write_text("")
    result = _run(world, "sleep 0.3", "-f", "label=dev.nexus.test.run=nobody")
    assert result.returncode == 3, result.stderr
    assert "WARNING: -f was given and NO container was seen" in result.stderr
    failing = _run(world, "sleep 0.3; exit 9", "-f", "label=dev.nexus.test.run=nobody")
    assert failing.returncode == 9, "the command's own failure is not masked by the filter warning"
    assert "WARNING: -f was given and NO container was seen" in failing.stderr
    # controls: the same empty listing without -f is fine, and a matching filter exits 0
    unfiltered = _run(world, "sleep 0.3")
    assert unfiltered.returncode == 0 and "WARNING" not in unfiltered.stderr
    (world["fake"] / "ids").write_text(f"{CID_A}\n")
    matched = _run(world, "sleep 0.3", "-f", "label=dev.nexus.test.run=r1")
    assert matched.returncode == 0 and "containers seen: 1" in matched.stderr


def test_a_filter_with_an_empty_value_is_refused(world: dict[str, Path]) -> None:
    marker = world["tmp"] / "ran"
    result = _run(world, f"touch '{marker}'", "-f", "label=dev.nexus.test.run=")
    assert result.returncode == 2 and not marker.exists()
    assert "empty value" in result.stderr and "NX_TEST_RUN_ID" in result.stderr


@pytest.mark.parametrize("bad", ["abc", "0", "0.0", "-1", "1e3", "", "2s"])
def test_a_bad_interval_is_refused_before_the_command_runs(world: dict[str, Path], bad: str) -> None:
    marker = world["tmp"] / "ran"
    result = _run(world, f"touch '{marker}'", interval=bad)
    assert result.returncode == 2 and not marker.exists(), result.stderr
    assert "-i must be a positive number" in result.stderr or "-i needs seconds" in result.stderr


def test_a_process_name_is_one_path_component_and_never_starts_with_a_dash(world: dict[str, Path]) -> None:
    result = _run(world, "sleep 0.3", "-x", "../../escaped name")
    assert result.returncode == 0, result.stderr
    state = _kept_state(result.stderr)
    assert (state / "d" / "_.._.._escaped_name.skip").exists(), sorted(p.name for p in state.rglob("*"))
    outside = [p for p in world["tmp"].rglob("*escaped*") if state / "d" not in p.parents]
    assert outside == [], "a -x name wrote outside the sampler's d/ directory"
    assert "cgroup wbytes delta, _.._.._escaped_name: skipped (no process named ../../escaped name)" in result.stderr
    marker = world["tmp"] / "ran"
    dash = _run(world, f"touch '{marker}'", "-x", "-rf")
    assert dash.returncode == 2 and not marker.exists()


def test_a_container_that_died_mid_tick_adds_no_log_bytes(world: dict[str, Path]) -> None:
    # B's log is sized by `docker logs` (its json file is unreadable). When docker then fails with its error
    # text on stdout/stderr, that text is not B's log: only A's stat of 123 B is counted.
    (world["fake"] / f"logs-{CID_B}.gone").write_text("")
    result = _run(world, "sleep 0.5")
    assert result.returncode == 0, result.stderr
    assert "json-log bytes total: 123 B  (1 sized by stat of docker's LogPath, 0 by 'docker logs | wc -c'" in result.stderr


def test_the_sampler_loop_ends_when_the_script_is_killed(world: dict[str, Path]) -> None:
    # The command SIGKILLs its parent (the script), which cannot clean up after itself. The orphaned loop must
    # notice and stop on its own: its tick counter freezes. Without the liveness check it ticks forever.
    # The command waits for the loop's first tick before the kill (up to 30 s): a fixed 0.6 s sleep lost that
    # race under a loaded -n auto run (nexus-ys8a4).
    first_tick = (
        f"for _ in $(seq 300); do for f in '{world['tmp']}'/nx-io-sampler.*/samples; do [ -s \"$f\" ] && break 2; done; "
        "sleep 0.1; done; kill -9 $PPID; sleep 0.2"
    )
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "-i", "0.1", "-r", str(world["root"]), "--", "bash", "-c", first_tick],
        env=_env(world), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    states: list[Path] = []
    try:
        proc.wait(timeout=30)
        states = list(world["tmp"].glob("nx-io-sampler.*"))
        assert len(states) == 1, states
        samples = states[0] / "samples"
        assert int(samples.read_text()) >= 1, "the loop was running before the kill"
        time.sleep(1.0)  # an orphan alive at the kill finishes its tick and sees the dead parent
        first = samples.read_text()
        time.sleep(1.5)
        assert samples.read_text() == first, "the sampler loop is still ticking after its script was killed"
    finally:
        for state in states:
            (state / "stop").touch()  # never leave a loop behind if this test fails
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def _pick_state_base(**env: str) -> str:
    result = subprocess.run(
        ["bash", "-c", f"source '{SCRIPT}'; pick_state_base"], capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ["PATH"], **env},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_state_directory_goes_on_shared_memory_when_it_is_writable(tmp_path: Path) -> None:
    shm = tmp_path / "shm"
    shm.mkdir()
    assert _pick_state_base(NX_IO_SAMPLER_SHM=str(shm), TMPDIR="/somewhere/else") == str(shm)
    assert _pick_state_base(NX_IO_SAMPLER_SHM=str(tmp_path / "absent"), TMPDIR="/somewhere/else") == "/somewhere/else"
    assert _pick_state_base(NX_IO_SAMPLER_SHM=str(tmp_path / "absent")) == "/tmp"
    override = tmp_path / "chosen"
    assert _pick_state_base(NX_IO_SAMPLER_SHM=str(shm), NX_IO_SAMPLER_STATE_BASE=str(override)) == str(override)


def test_the_final_sample_has_a_bound_of_its_own(world: dict[str, Path]) -> None:
    # Each stalled inspect would cost DOCKER_TIMEOUT (10 s), two containers over: the final sample's own
    # bound (1 s) is what ends it, and the report says so.
    started = time.monotonic()
    result = _run(
        world, "sleep 0.5",
        extra_env={**_BOUNDS, "NX_IO_SAMPLER_DOCKER_TIMEOUT": "10", "NX_IO_SAMPLER_FINAL": "1", "FAKE_STALL": "inspect"},
        timeout=_HANG_BOUND_SECONDS,
    )
    assert time.monotonic() - started < _HANG_BOUND_SECONDS
    assert result.returncode == 0, result.stderr
    assert "final sample did not finish in 1s" in result.stderr
    _assert_no_stalled_calls_left(world)
