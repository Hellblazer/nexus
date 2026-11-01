# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-qrldo: scripts/testcontainers_sweep.py removes orphaned Testcontainers
containers before a Java build.

``~/.testcontainers.properties`` disables Ryuk on the dev boxes, so a JVM that
exits without stopping its containers leaves them running; on 2026-11-01 29 of
them filled the laptop's colima disk. The sweep removes containers labelled
``org.testcontainers=true`` older than the age bound, with their volumes, and
never fails the build. Driven against a fake ``docker`` on PATH that records its
argv and answers ``ps`` from a fixture, with a fixed clock (``--now``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "testcontainers_sweep.py"
NOW = 1793532400  # 2026-11-01T11:26:40Z

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_DOCKER_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_DOCKER_MODE", "")
if mode == "fail":
    sys.stderr.write("Cannot connect to the Docker daemon\n")
    sys.exit(1)
if sys.argv[1:2] == ["ps"]:
    sys.stdout.write(open(os.environ["FAKE_DOCKER_PS"]).read())
'''


def _ps_line(cid: str, created: str, name: str) -> str:
    return f"{cid}\t{created}\t{name}\n"


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    ps = tmp_path / "ps.txt"
    ps.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}/usr/bin:/bin",
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


def test_an_old_testcontainers_container_is_removed_with_its_volumes(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(
        _ps_line("aaa111", "2026-10-30 12:00:00 -0700 PDT", "elegant_shirley")  # 39 h old
        + _ps_line("bbb222", "2026-11-01 03:00:00 -0800 PST", "young_one")  # 26 min old
    )
    r = _run(env)
    assert r.returncode == 0, r.stderr
    calls = _calls(log)
    ps_call = calls[0]
    assert ps_call[0] == "ps" and "-a" in ps_call
    assert "label=org.testcontainers=true" in ps_call
    assert ["rm", "-f", "-v", "aaa111"] in calls
    assert not any("bbb222" in c for c in calls)
    assert "removed 1" in r.stderr


def test_the_age_bound_is_configurable(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(_ps_line("bbb222", "2026-11-01 03:00:00 -0800 PST", "young_one"))
    r = _run({**env, "NX_TC_SWEEP_MAX_AGE_HOURS": "0.25"})
    assert r.returncode == 0, r.stderr
    assert ["rm", "-f", "-v", "bbb222"] in _calls(log)


def test_nothing_to_sweep_writes_nothing(fake_docker) -> None:
    env, log, _ps = fake_docker
    r = _run(env)
    assert r.returncode == 0
    assert [c for c in _calls(log) if c[0] == "rm"] == []
    assert r.stderr == ""


def test_the_opt_out_runs_no_docker_at_all(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(_ps_line("aaa111", "2026-10-30 12:00:00 -0700 PDT", "old"))
    r = _run({**env, "NX_TC_SWEEP": "0"})
    assert r.returncode == 0
    assert _calls(log) == []


def test_no_docker_on_path_is_a_quiet_no_op(tmp_path) -> None:
    r = _run({"PATH": str(tmp_path), "HOME": str(tmp_path)})
    assert r.returncode == 0
    assert r.stderr == ""


def test_a_failing_docker_never_fails_the_build(fake_docker) -> None:
    env, _log, _ps = fake_docker
    r = _run({**env, "FAKE_DOCKER_MODE": "fail"})
    assert r.returncode == 0
    assert "skipped" in r.stderr


def test_an_unparseable_created_time_is_kept(fake_docker) -> None:
    env, log, ps = fake_docker
    ps.write_text(_ps_line("ccc333", "yesterday-ish", "weird"))
    r = _run(env)
    assert r.returncode == 0
    assert not any("ccc333" in c for c in _calls(log))


def test_the_shared_build_runner_calls_the_sweep() -> None:
    text = (REPO / "scripts" / "lib" / "leased-build.sh").read_text()
    body = text[text.index("leased_build_run() {"):]
    assert "testcontainers_sweep.py" in body.split("set -m", 1)[0]
