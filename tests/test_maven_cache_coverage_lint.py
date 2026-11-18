# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every workflow job that can run Maven on a GitHub-hosted runner restores the Maven cache first.

``.github/actions/maven-cache`` restores a per-class ``~/.m2`` (repository and wrapper) and sets the resolver's
Central retries. Before it, every hosted Maven job used ``setup-graalvm``'s ``cache: maven``: one key per pom hash,
the first job to save defined the entry and an exact hit never saved again, so the native trip-wire fetched its own
plugins from Maven Central on every run (develop run 44099236667 failed reading asm-commons 9.7). These checks keep
a new hosted Maven job, or a returning ``cache: maven``, from bringing that back without a red test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_ACTION = _ROOT / ".github" / "actions" / "maven-cache" / "action.yml"
_USES = "./.github/actions/maven-cache"

#: A run line that starts Maven, directly or through a script that does. A script counts only as a command at the
#: start of a line: ci.yml's carve-out step reads constants out of local-service-gate.sh with sed and runs no Maven.
_MAVEN_RUN = re.compile(
    r"mvnw|\bmvn\b|^\s*(?:bash\s+)?(?:\./)?(?:scripts/build-gate-jar|tests/e2e/local-service-gate)\.sh\b",
    re.MULTILINE,
)
#: A GitHub-hosted runner label, wherever it appears in a job's runs-on or matrix (a pool fallback counts).
_HOSTED = re.compile(r"\b(?:ubuntu|macos|windows)-[\w.-]+")
#: The fewest hosted Maven jobs the census must find (11 when this test was written); fewer means it went blind.
_MIN_JOBS = 10


def _action_classes() -> set[str]:
    text = _ACTION.read_text()
    match = re.search(r"^\s+([a-z|]+)\) ;;$", text, re.MULTILINE)
    assert match, (
        "maven-cache action: the job-class check line (`a|b|c) ;;`) is missing"
    )
    return set(match.group(1).split("|"))


def _hosted_maven_jobs() -> list[tuple[str, str, list[dict]]]:
    jobs = []
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for name, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            if not any(_MAVEN_RUN.search(str(s.get("run", ""))) for s in steps):
                continue
            where = yaml.safe_dump(
                {"runs-on": job.get("runs-on"), "strategy": job.get("strategy")}
            )
            if _HOSTED.search(where):
                jobs.append((path.name, name, steps))
    return jobs


def test_census_is_not_vacuous() -> None:
    jobs = _hosted_maven_jobs()
    assert len(jobs) >= _MIN_JOBS, [f"{f}:{j}" for f, j, _ in jobs]


@pytest.mark.parametrize(
    ("workflow", "job", "steps"),
    _hosted_maven_jobs(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_hosted_maven_job_restores_the_cache_before_maven(
    workflow: str, job: str, steps: list[dict]
) -> None:
    first_maven = next(
        i for i, s in enumerate(steps) if _MAVEN_RUN.search(str(s.get("run", "")))
    )
    cache_steps = [i for i, s in enumerate(steps) if s.get("uses") == _USES]
    assert cache_steps and cache_steps[0] < first_maven, (
        f"{workflow} job {job}: step {first_maven} runs Maven on a hosted runner with no {_USES} step before it"
    )
    classes = _action_classes()
    for i in cache_steps:
        job_class = (steps[i].get("with") or {}).get("job-class")
        assert job_class in classes or str(job_class).startswith("${{"), (
            f"{workflow} job {job}: job-class {job_class!r} is not one of {sorted(classes)}"
        )


def test_no_setup_step_brings_back_the_shared_maven_cache() -> None:
    found = []
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        for name, job in (
            (yaml.safe_load(path.read_text()) or {}).get("jobs") or {}
        ).items():
            for step in job.get("steps") or []:
                uses = str(step.get("uses", ""))
                if ("setup-java" in uses or "setup-graalvm" in uses) and "maven" in str(
                    (step.get("with") or {}).get("cache", "")
                ):
                    found.append(f"{path.name}:{name}")
    assert not found, (
        f"setup-java/setup-graalvm `cache: maven` is back (use {_USES} instead): {found}"
    )
