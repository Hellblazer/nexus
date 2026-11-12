# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every pool-hosted CI job checks its host binaries first (labrun-owz).

A pool host that lost a binary (uuidgen, from uuid-runtime) showed up as a red
deep inside a test run. Each job that runs on the owned lab pool now opens with a
``Host binaries present`` step that runs ``command -v`` over a list kept in the
step and fails with ``host missing: X``. These tests keep that step first in
every pool job, its list non-empty, and the Linux shard job's list holding
uuidgen."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
STEP_NAME = "Host binaries present (labrun-owz)"
#: A runner label that is the owned lab pool: a lab-* scale set or the hellmini host.
POOL_MARKERS = ("lab-", "hellmini")


def _can_land_on_the_pool(job: dict) -> bool:
    """runs-on, or any string a matrix can feed it (``${{ matrix.target.runner }}``), names the pool."""
    text = json.dumps(job.get("runs-on", "")) + json.dumps((job.get("strategy") or {}).get("matrix", ""))
    return any(m in text for m in POOL_MARKERS)


def _pool_jobs() -> list[tuple[str, str, dict]]:
    found = []
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in (doc.get("jobs") or {}).items():
            if _can_land_on_the_pool(job):
                found.append((path.name, name, job))
    return found


def _probe(job: dict) -> dict:
    return job["steps"][0]


def test_the_pool_jobs_are_found() -> None:
    names = {(wf, name) for wf, name, _ in _pool_jobs()}
    assert {("ci.yml", "test"), ("ci.yml", "ca3-pgvector-bundle-macos"),
            ("service-ci.yml", "java-test-and-drift-guard"),
            ("engine-service-release.yml", "build-publish"), ("engine-service-release.yml", "smoke-mac-arm64"),
            ("engine-service-release.yml", "build-publish-pg-bundle"),
            ("mac-signing-rehearsal.yml", "rehearse")} <= names, names


@pytest.mark.parametrize(("wf", "name", "job"), _pool_jobs(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_pool_job_opens_with_a_non_empty_host_probe(wf: str, name: str, job: dict) -> None:
    step = _probe(job)
    assert step.get("name") == STEP_NAME, f"{wf}:{name} does not open with the host probe"
    assert step.get("if") == "runner.environment == 'self-hosted'", f"{wf}:{name}: the probe must run on the pool only"
    binaries = str(step.get("env", {}).get("NX_CI_HOST_BINARIES", "")).split()
    assert binaries, f"{wf}:{name}: the host probe lists no binaries"
    assert "host missing: $b" in step["run"], f"{wf}:{name}: the probe does not name the missing binary"


def _listed(wf: str, name: str) -> list[str]:
    job = next(j for w, n, j in _pool_jobs() if (w, n) == (wf, name))
    return _probe(job)["env"]["NX_CI_HOST_BINARIES"].split()


def test_the_linux_shard_job_checks_uuidgen_python3_and_rg() -> None:
    assert {"uuidgen", "python3", "rg"} <= set(_listed("ci.yml", "test"))


def test_the_java_job_checks_python3_and_a_running_docker_daemon() -> None:
    assert "python3" in _listed("service-ci.yml", "java-test-and-drift-guard")
    job = next(j for w, n, j in _pool_jobs() if (w, n) == ("service-ci.yml", "java-test-and-drift-guard"))
    assert "docker info" in _probe(job)["run"]


@pytest.mark.parametrize(("wf", "name", "job"), _pool_jobs(), ids=lambda v: v if isinstance(v, str) else "")
def test_the_probe_names_a_missing_binary_and_fails(wf: str, name: str, job: dict) -> None:
    step = _probe(job)
    env = {"PATH": "/usr/bin:/bin", "NX_CI_HOST_BINARIES": "bash nx-no-such-binary-labrun-owz"}
    out = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, check=False)
    assert out.returncode != 0
    assert "host missing: nx-no-such-binary-labrun-owz" in out.stdout
    assert "host missing: bash" not in out.stdout


@pytest.mark.parametrize(("wf", "name", "job"), _pool_jobs(), ids=lambda v: v if isinstance(v, str) else "")
def test_the_probe_refuses_an_empty_list(wf: str, name: str, job: dict) -> None:
    out = subprocess.run(["bash", "-c", _probe(job)["run"]], env={"PATH": "/usr/bin:/bin", "NX_CI_HOST_BINARIES": " "},
                         capture_output=True, text=True, check=False)
    assert out.returncode != 0 and "empty binary list" in out.stdout
