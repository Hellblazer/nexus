# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m643n: every lab-pool pytest job keeps its bytecode on ``/dev/shm``.

A pool shard's collection writes ~116 MB of ``.pyc`` to the host's NVMe; with
``PYTHONPYCACHEPREFIX`` on a per-job ``/dev/shm`` directory the disk write is ~23 MB
(T2 ``nexus/analysis-ci-startup-bytes-2026-10-19``). The pin: each workflow job that
runs ``pytest`` on a ``lab-*`` runner has (1) a pool-only step, before the first step
that runs anything Python, that creates a per-job directory under ``/dev/shm`` and
exports it as ``PYTHONPYCACHEPREFIX`` through ``GITHUB_ENV``, and (2) a LAST step
guarded by ``always()`` that removes it. The job count is asserted non-zero: a scan
that found no pool pytest job passed nothing.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_LAB_RUNNER = re.compile(r"['\"]lab-[a-z0-9]+")
_PYTHON_USERS = re.compile(r"\b(uv|pytest|python3?)\b")


def pool_pytest_jobs(docs: dict[str, dict]) -> list[tuple[str, str, dict]]:
    """(file, job id, job spec) for every job that runs pytest on a ``lab-*`` runner."""
    found = []
    for fname, doc in docs.items():
        for jid, spec in (doc.get("jobs") or {}).items():
            if not _LAB_RUNNER.search(str(spec.get("runs-on", ""))):
                continue
            if any("pytest" in str(st.get("run", "")) for st in spec.get("steps", [])):
                found.append((fname, jid, spec))
    return found


def prefix_problems(spec: dict) -> list[str]:
    """Every way a pool pytest job's bytecode-prefix wiring is wrong; empty means wired."""
    steps = spec.get("steps", [])
    problems: list[str] = []
    setup = [
        i for i, st in enumerate(steps)
        if "PYTHONPYCACHEPREFIX=" in str(st.get("run", "")) and "GITHUB_ENV" in str(st.get("run", ""))
    ]
    if len(setup) != 1:
        return [f"expected exactly one step exporting PYTHONPYCACHEPREFIX via GITHUB_ENV, found {len(setup)}"]
    i = setup[0]
    st = steps[i]
    run = str(st["run"])
    cond = str(st.get("if", ""))
    if "self-hosted" not in cond or "Linux" not in cond:
        problems.append(f"setup step must be pool-only (self-hosted and Linux), if: {cond!r}")
    dirs = str(st.get("env", {}).get("NX_PYC_DIR", ""))
    if not dirs.startswith("/dev/shm/"):
        problems.append(f"the prefix directory must sit under /dev/shm/, got {dirs!r}")
    for part in ("github.run_id", "github.run_attempt", "matrix.python-version", "matrix.shard"):
        if part not in dirs:
            problems.append(f"the prefix directory is not unique per job: {part} missing from {dirs!r}")
    if "mkdir -m 0700" not in run:
        problems.append("the prefix directory must be created 0700 (private to the job user)")
    if "PYTHONPYCACHEPREFIX=$NX_PYC_DIR" not in run:
        problems.append("the exported prefix is not the per-job NX_PYC_DIR")
    early = [
        j for j, other in enumerate(steps[:i])
        if _PYTHON_USERS.search(str(other.get("run", ""))) or "setup-uv" in str(other.get("uses", ""))
    ]
    if early:
        problems.append(f"steps {early} run Python or uv before the prefix is exported")
    last = steps[-1]
    lcond = str(last.get("if", ""))
    lrun = str(last.get("run", ""))
    if "always()" not in lcond:
        problems.append(f"the last step must be guarded by always(), if: {lcond!r}")
    if "rm -rf" not in lrun or "PYTHONPYCACHEPREFIX" not in lrun or "/dev/shm/nx-pyc-" not in lrun:
        problems.append("the last step does not remove the nx-pyc-* prefix directory")
    return problems


def _load() -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(WORKFLOWS.glob("*.yml"))}


def test_every_lab_pool_pytest_job_keeps_bytecode_on_dev_shm() -> None:
    jobs = pool_pytest_jobs(_load())
    # non-vacuity: today exactly the ci.yml `test` job; a scan that found none passed nothing
    assert [(f, j) for f, j, _ in jobs] == [("ci.yml", "test")], (
        f"pool pytest jobs found: {[(f, j) for f, j, _ in jobs]}; a new one needs the same wiring and a line here"
    )
    for fname, jid, spec in jobs:
        assert prefix_problems(spec) == [], f"{fname}:{jid}"


def _ci_test_job() -> dict:
    return copy.deepcopy(_load()["ci.yml"]["jobs"]["test"])


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda s: [st.pop("env") for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})], "under /dev/shm"),
        (lambda s: [st.pop("if") for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})], "pool-only"),
        (lambda s: s["steps"][-1].pop("if"), "always()"),
        (lambda s: s["steps"].pop(-1), "always()"),
        (
            lambda s: [st.update(env={"NX_PYC_DIR": "/dev/shm/nx-pyc-fixed"}) for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})],
            "not unique per job",
        ),
        (lambda s: s["steps"].insert(0, {"run": "uv sync"}), "before the prefix is exported"),
        (
            lambda s: [st["env"].update(NX_PYC_DIR=st["env"]["NX_PYC_DIR"].replace("-py${{ matrix.python-version }}", "")) for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})],
            "matrix.python-version missing",
        ),
        (
            lambda s: [st.update(run=st["run"].replace("mkdir -m 0700", "mkdir")) for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})],
            "created 0700",
        ),
    ],
    ids=["no-dir", "not-pool-only", "cleanup-not-always", "cleanup-missing", "not-unique", "uv-before-export", "no-python-version", "not-0700"],
)
def test_each_wiring_break_is_caught(mutate, expect) -> None:  # noqa: ANN001
    spec = _ci_test_job()
    mutate(spec)
    problems = prefix_problems(spec)
    assert any(expect in p for p in problems), problems


def test_a_job_without_the_export_is_caught() -> None:
    spec = _ci_test_job()
    spec["steps"] = [st for st in spec["steps"] if "PYTHONPYCACHEPREFIX=" not in str(st.get("run", ""))]
    assert "found 0" in prefix_problems(spec)[0]
