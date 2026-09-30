# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vyg07: the pytest shard count in ``ci.yml`` is one number kept in four places.

The ``test`` job's matrix (``shard: [1 .. N]``), its job name (``shard X/N``), the
step name (``Run tests (shard X/N)``) and ``pytest --splits N`` must agree.
``pytest-gate`` reads ``needs.test.result`` only, so if a later edit shrinks the
matrix and leaves ``--splits`` alone, the remaining shards pass and the dropped
group's tests never run, green. The per-shard executed-count floor cannot see it
(it is per shard). This lint parses the workflow and fails on any disagreement;
the mutation cases prove each disagreement is caught, not merely that today's file
is consistent (nexus-qzxzh, critique T2 ``nexus/critique-f5i1m-qzxzh-ci`` finding 6).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_SPLITS = re.compile(r"--splits\s+(\d+)")
_GROUP = re.compile(r"--group\s+\$\{\{\s*matrix\.shard\s*\}\}")
_NAME_SUFFIX = re.compile(r"shard\s+\$\{\{\s*matrix\.shard\s*\}\}\s*/\s*(\d+)")


def shard_problems(text: str, job: str = "test") -> list[str]:
    """Every way the shard count of *job* disagrees with itself; empty means consistent."""
    doc = yaml.safe_load(text)
    spec = doc["jobs"][job]
    problems: list[str] = []

    shards = spec["strategy"]["matrix"]["shard"]
    n = len(shards)
    if shards != list(range(1, n + 1)):
        problems.append(f"matrix shard list {shards!r} is not exactly 1..{n}")

    m = _NAME_SUFFIX.search(str(spec.get("name", "")))
    if not m:
        problems.append("job name carries no 'shard ${{ matrix.shard }}/N' suffix")
    elif int(m.group(1)) != n:
        problems.append(f"job name says /{m.group(1)} but the matrix has {n} shards")

    splits_seen = 0
    step_suffix_seen = 0
    for step in spec["steps"]:
        step_name = str(step.get("name", ""))
        if step_name.startswith("Run tests"):
            sm = _NAME_SUFFIX.search(step_name)
            step_suffix_seen += 1
            if not sm:
                problems.append(f"step {step_name!r} carries no 'shard ${{{{ matrix.shard }}}}/N' suffix")
            elif int(sm.group(1)) != n:
                problems.append(f"step {step_name!r} says /{sm.group(1)} but the matrix has {n} shards")
        run = str(step.get("run", ""))
        for sp in _SPLITS.findall(run):
            splits_seen += 1
            if int(sp) != n:
                problems.append(f"--splits {sp} in step {step_name!r} but the matrix has {n} shards")
            if not _GROUP.search(run):
                problems.append(f"--splits without '--group ${{{{ matrix.shard }}}}' in step {step_name!r}")

    # non-vacuity: a lint that found nothing to compare passed nothing
    if not splits_seen:
        problems.append("no `--splits N` found in any step of the job")
    if not step_suffix_seen:
        problems.append("no 'Run tests (shard X/N)' step found")
    return problems


def test_the_shard_count_agrees_everywhere_in_ci_yml() -> None:
    assert shard_problems(CI_YML.read_text(encoding="utf-8")) == []


def test_no_other_job_splits_with_a_different_count() -> None:
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    n = len(doc["jobs"]["test"]["strategy"]["matrix"]["shard"])
    counts = {
        (name, int(sp))
        for name, spec in doc["jobs"].items()
        for step in spec.get("steps", [])
        for sp in _SPLITS.findall(str(step.get("run", "")))
    }
    assert counts, "non-vacuity: no --splits found in any job"
    assert {c for _j, c in counts} == {n}, counts


def _n() -> int:
    return len(yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]["test"]["strategy"]["matrix"]["shard"])


def _mutations() -> dict[str, tuple[str, str]]:
    n = _n()
    full = ", ".join(str(i) for i in range(1, n + 1))
    return {
        "matrix shrunk, the rest untouched": (f"shard: [{full}]", f"shard: [{full.rsplit(', ', 1)[0]}]"),
        "matrix with a gap": (f"shard: [{full}]", f"shard: [{full.rsplit(', ', 1)[0]}, {n + 1}]"),
        "--splits drifts": (
            f"--splits {n} --group ${{{{ matrix.shard }}}}", f"--splits {n - 1} --group ${{{{ matrix.shard }}}}"),
        "job name suffix drifts": (
            f"shard ${{{{ matrix.shard }}}}/{n})\n    runs-on", f"shard ${{{{ matrix.shard }}}}/{n - 1})\n    runs-on"),
        "step name suffix drifts": (
            f"name: Run tests (shard ${{{{ matrix.shard }}}}/{n})", f"name: Run tests (shard ${{{{ matrix.shard }}}}/{n + 1})"),
        "--group dropped": ("--group ${{ matrix.shard }}", "--group 1"),
    }


@pytest.mark.parametrize("label", list(_mutations()))
def test_the_lint_is_red_on_a_mutated_copy(label: str) -> None:
    old, new = _mutations()[label]
    text = CI_YML.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation target {old!r} must occur exactly once (did ci.yml's shape change?)"
    assert shard_problems(text.replace(old, new)), f"mutation {label!r} went undetected"
