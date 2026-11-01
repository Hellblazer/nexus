# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-qrldo: scripts/testcontainers_sweep.py removes this repo's orphaned
Testcontainers containers before a Java build.

The laptop's ``~/.testcontainers.properties`` disables Ryuk, so a killed JVM
with Ryuk off leaves its containers running; on 2026-11-01 29 of them filled the laptop's
colima disk. The sweep removes containers carrying both ``org.testcontainers=true``
and this repo's ``dev.nexus.test.run`` label, older than the age bound, and never
fails the build. Driven against a fake ``docker`` on PATH that records its argv
and applies the ``--filter label=...`` arguments to a fixture, with a fixed clock.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "testcontainers_sweep.py"
LIB = REPO / "scripts" / "lib" / "leased-build.sh"
NOW = 1793532400  # 2026-11-01T11:26:40Z
OLD = "2026-10-30 12:00:00 -0700 PDT"  # 40.4 h before NOW
YOUNG = "2026-11-01 03:00:00 -0800 PST"  # 26 min before NOW
NEXUS = {"org.testcontainers": "true", "dev.nexus.test.run": "mvnw-u-1-2"}

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys, time
log = os.environ["FAKE_DOCKER_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_DOCKER_MODE", "")
args = sys.argv[1:]
if mode == "fail":
    sys.stderr.write("Cannot connect to the Docker daemon\n")
    sys.exit(1)
if args[:1] == ["rm"] and mode == "slow-rm":
    time.sleep(float(os.environ.get("FAKE_DOCKER_DELAY", "5")))
if args[:1] == ["ps"] and mode == "slow-ps":
    time.sleep(float(os.environ.get("FAKE_DOCKER_DELAY", "5")))
if args[:1] == ["rm"] and mode == "rm-fails":
    sys.stderr.write("conflict\n")
    sys.exit(1)
if args[:1] == ["ps"]:
    wants = [a.split("=", 1)[1] for a in args if a.startswith("label=")]
    for row in json.load(open(os.environ["FAKE_DOCKER_PS"])):
        labels = row["labels"]
        def ok(w):
            k, _, v = w.partition("=")
            return k in labels and (not _ or labels[k] == v)
        if all(ok(w) for w in wants):
            sys.stdout.write("\t".join([row["id"], row["created"], row["name"], row["image"]]) + "\n")
'''


def _row(cid: str, created: str, name: str, labels: dict[str, str] | None = None,
         image: str = "pgvector/pgvector:pg17") -> dict:
    return {"id": cid, "created": created, "name": name, "image": image,
            "labels": NEXUS if labels is None else labels}


@pytest.fixture
def fake_docker(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    ps = tmp_path / "ps.json"
    ps.write_text("[]")
    py_dir = str(Path(sys.executable).parent)
    env = {
        "PATH": os.pathsep.join([str(bindir), py_dir, "/usr/bin", "/bin"]),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_PS": str(ps),
        "HOME": str(tmp_path),
    }
    return env, log, ps


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--now", str(NOW), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _removed(log: Path) -> list[str]:
    return [c[-1] for c in _calls(log) if c[:1] == ["rm"]]


def test_an_old_container_of_ours_is_removed_and_a_young_one_kept(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("aaa111", OLD, "elegant_shirley"), _row("bbb222", YOUNG, "young_one")]))
    r = _run(env)
    assert r.returncode == 0, r.stderr
    ps_call = _calls(log)[0]
    assert ps_call[0] == "ps" and "-a" in ps_call
    assert "label=org.testcontainers=true" in ps_call and "label=dev.nexus.test.run" in ps_call
    assert ["rm", "-f", "-v", "aaa111"] in _calls(log)
    assert _removed(log) == ["aaa111"]
    assert "removed elegant_shirley (aaa111, pgvector/pgvector:pg17, 40.4 h old)" in r.stderr
    assert "removed 1 of 1" in r.stderr


def test_another_projects_testcontainers_container_is_never_removed(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([
        _row("ddd444", OLD, "someones_dev_db", labels={"org.testcontainers": "true",
                                                       "org.testcontainers.hash": "abc"}),
        _row("eee555", OLD, "not_testcontainers", labels={"dev.nexus.test.run": "x"}),
    ]))
    r = _run(env)
    assert r.returncode == 0
    assert _removed(log) == []


@pytest.mark.parametrize(("hours", "removed"), [("6", []), ("0.4", None), ("0", None), ("-1", None)])
def test_the_default_bound_is_six_hours_and_tiny_bounds_are_refused(fake_docker, hours, removed) -> None:
    env, log, ps = fake_docker
    five_h = "2026-10-31 22:26:40 -0800 PST"  # 5 h before NOW (03:26:40 PST)
    seven_h = "2026-10-31 20:26:40 -0800 PST"  # 7 h before NOW
    ps.write_text(json.dumps([_row("f5", five_h, "five"), _row("s7", seven_h, "seven")]))
    r = _run({**env, "NX_TC_SWEEP_MAX_AGE_HOURS": hours})
    assert r.returncode == 0
    if removed is None:  # refused: no docker call at all, one note
        assert _calls(log) == []
        assert "is under 0.5 h" in r.stderr
    else:
        assert _removed(log) == ["s7"]


def test_the_default_applies_when_the_variable_is_unset(fake_docker) -> None:
    env, log, ps = fake_docker
    five_h = "2026-10-31 22:26:40 -0800 PST"
    seven_h = "2026-10-31 20:26:40 -0800 PST"
    ps.write_text(json.dumps([_row("f5", five_h, "five"), _row("s7", seven_h, "seven")]))
    _run(env)
    assert _removed(log) == ["s7"]


def test_the_age_bound_is_configurable(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("bbb222", YOUNG, "young_one")]))
    r = _run({**env, "NX_TC_SWEEP_MAX_AGE_HOURS": "0.25"})
    assert "is under 0.5 h" in r.stderr  # below the floor
    r = _run({**env, "NX_TC_SWEEP_MAX_AGE_HOURS": "0.5"})
    assert _removed(log) == []  # 26 min < 30 min
    ps.write_text(json.dumps([_row("ccc", "2026-11-01 02:00:00 -0800 PST", "hour_and_a_half")]))
    _run({**env, "NX_TC_SWEEP_MAX_AGE_HOURS": "1"})
    assert _removed(log) == ["ccc"]


def test_the_whole_sweep_stops_at_its_deadline(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row(f"c{i}", OLD, f"old{i}") for i in range(5)]))
    t0 = time.monotonic()
    r = _run({**env, "FAKE_DOCKER_MODE": "slow-rm", "FAKE_DOCKER_DELAY": "1"}, "--deadline", "2.5")
    assert time.monotonic() - t0 < 10
    assert r.returncode == 0
    assert len(_removed(log)) < 5
    assert "deadline" in r.stderr


def test_a_failed_rm_is_reported_and_the_rest_continue(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("a", OLD, "one"), _row("b", OLD, "two")]))
    r = _run({**env, "FAKE_DOCKER_MODE": "rm-fails"})
    assert r.returncode == 0
    assert _removed(log) == ["a", "b"]
    assert "could not remove one (a)" in r.stderr and "removed 0 of 2" in r.stderr


def test_nothing_to_sweep_writes_nothing(fake_docker) -> None:
    env, log, _ = fake_docker
    r = _run(env)
    assert r.returncode == 0
    assert _removed(log) == []
    assert r.stderr == ""


def test_the_opt_out_runs_no_docker_at_all(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("aaa111", OLD, "old")]))
    r = _run({**env, "NX_TC_SWEEP": "0"})
    assert r.returncode == 0
    assert _calls(log) == []


def test_no_docker_on_path_is_a_quiet_no_op(tmp_path) -> None:
    r = _run({"PATH": str(tmp_path), "HOME": str(tmp_path)})
    assert r.returncode == 0
    assert r.stderr == ""


def test_a_failing_docker_never_fails_the_build(fake_docker) -> None:
    env, _, _ = fake_docker
    r = _run({**env, "FAKE_DOCKER_MODE": "fail"})
    assert r.returncode == 0
    assert "skipped" in r.stderr


def test_unreadable_created_times_are_kept_and_said_so(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("ccc333", "yesterday-ish", "weird")]))
    r = _run(env)
    assert r.returncode == 0
    assert _removed(log) == []
    assert "could not read any created time" in r.stderr


# --- the shell wiring: leased_build_run's sweep step, run for real ---------------

def _sweep_step(env: dict[str, str], lib_dir: Path) -> subprocess.CompletedProcess[str]:
    """Source a copy of leased-build.sh and run its sweep step, then `echo after`."""
    # leased-build.sh refuses to load without build-lease.sh's two helpers; stub them.
    script = ('set -e; build_lease_track_pid() { :; }; build_lease_untrack_pid() { :; }; '
              f'source "{lib_dir}/leased-build.sh"; leased_build_sweep_testcontainers; echo after')
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def lib_copy(tmp_path):
    """A scripts/ tree holding leased-build.sh and the sweep, as the repo lays them out."""
    scripts = tmp_path / "repo" / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "lib" / "leased-build.sh").write_text(LIB.read_text())
    (scripts / "testcontainers_sweep.py").write_text(SCRIPT.read_text())
    return scripts


def test_the_build_runner_sweeps_for_real(fake_docker, lib_copy) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("aaa111", OLD, "old")]))
    r = _sweep_step(env, lib_copy / "lib")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "after"
    assert _removed(log) == ["aaa111"]


def test_the_build_runner_honours_the_opt_out(fake_docker, lib_copy) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("aaa111", OLD, "old")]))
    r = _sweep_step({**env, "NX_TC_SWEEP": "0"}, lib_copy / "lib")
    assert r.stdout.strip() == "after"
    assert _calls(log) == []


def test_a_crashing_sweep_never_stops_the_build(fake_docker, lib_copy) -> None:
    env, _, _ = fake_docker
    (lib_copy / "testcontainers_sweep.py").write_text("raise SystemExit(3)\n")
    r = _sweep_step(env, lib_copy / "lib")
    assert r.returncode == 0
    assert r.stdout.strip() == "after"


def test_leased_build_run_calls_the_sweep_step_first() -> None:
    text = LIB.read_text()
    body = text[text.index("leased_build_run() {"):]
    first = [ln.strip() for ln in body.splitlines()[1:6] if ln.strip() and not ln.strip().startswith("#")]
    assert "leased_build_sweep_testcontainers" in first


def test_a_hung_docker_ps_is_abandoned_at_the_deadline(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(json.dumps([_row("aaa111", OLD, "old")]))
    t0 = time.monotonic()
    r = _run({**env, "FAKE_DOCKER_MODE": "slow-ps", "FAKE_DOCKER_DELAY": "8"}, "--deadline", "1.5")
    assert time.monotonic() - t0 < 6
    assert r.returncode == 0
    assert _removed(log) == []
    assert "did not answer" in r.stderr


def test_the_sweep_filters_on_the_label_the_java_recipe_sets() -> None:
    """A rename on the Java side would make the sweep match nothing, silently."""
    import re  # noqa: PLC0415 — test-local
    recipe = (REPO / "service" / "src" / "build" / "java" / "dev" / "nexus" / "build" / "PgContainerRecipe.java").read_text()
    labels = re.findall(r'withLabel\("([^"]+)"', recipe)
    assert "dev.nexus.test.run" in labels
    assert "label=dev.nexus.test.run" in SCRIPT.read_text()
