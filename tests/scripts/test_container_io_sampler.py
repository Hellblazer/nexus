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
# `docker logs` output in $FAKE_DIR/logs-<id>.
case "$1" in
    ps) echo "$*" >> "$FAKE_DIR/ps-args"; [[ -e "$FAKE_DIR/docker-down" ]] && exit 1; cat "$FAKE_DIR/ids" 2>/dev/null; exit 0 ;;
    inspect) id="${@: -1}"; cat "$FAKE_DIR/info-$id" ;;
    logs) cat "$FAKE_DIR/logs-$2" ;;
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
    return {"tmp": tmp_path, "fake": fake, "bin": bin_dir, "root": root, "proc": proc}


def _run(world: dict[str, Path], command: str, *flags: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{world['bin']}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(world["fake"]),
        "NX_IO_SAMPLER_PROC": str(world["proc"]),
        "TMPDIR": str(world["tmp"]),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "-i", "0.1", "-r", str(world["root"]), *flags, "--", "bash", "-c", command],
        capture_output=True, text=True, timeout=60, env=env,
    )


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
