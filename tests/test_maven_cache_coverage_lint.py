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
    match = re.search(r"^\s+((?:[a-z]+\|)*package(?:\|[a-z]+)*)\) ;;$", text, re.MULTILINE)
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


_JOB_BUILDS_JAR = re.compile(r"^\s*(?:bash\s+)?(?:\./)?(?:scripts/build-gate-jar|tests/e2e/local-service-gate)\.sh\b", re.M)


def _maven_lines(steps: list[dict]) -> list[str]:
    """Each Maven command line of the steps, continuations joined, with comment lines and ``-D`` properties removed
    (so ``-Dmaven.test.skip`` cannot read as a ``test`` goal)."""
    out = []
    for s in steps:
        text = re.sub(r"\\\n\s*", " ", str(s.get("run", "")))
        for line in text.splitlines():
            if line.strip().startswith("#") or not re.search(r"mvnw|\bmvn\b", line):
                continue
            out.append(re.sub(r"\"?-D[^\s\"]*\"?", " ", line))
    return out


def _resolves_whole_class(job_class: str, steps: list[dict]) -> bool:
    """True when the job runs a build that resolves everything its class needs: for ``package`` a shaded-jar
    ``package`` (or a script that builds the jar), for ``native`` a ``-Pnative package`` with the jOOQ codegen
    plugins resolved (``generate-sources`` in the job, or no ``-Pprebuilt-jooq``, which turns them off), for
    ``test`` a ``test`` goal. Any other job resolves part of its class."""
    lines = _maven_lines(steps)
    goal = lambda g, line: re.search(rf"(?<![\w:-]){g}(?![\w:-])", line)  # noqa: E731
    if job_class == "package":
        return any(_JOB_BUILDS_JAR.search(str(s.get("run", ""))) for s in steps) or any(
            goal("package", ln) and "-Pnative" not in ln for ln in lines
        )
    if job_class == "native":
        codegen = any(goal("generate-sources", ln) for ln in lines)
        return any(
            goal("package", ln) and "-Pnative" in ln and (codegen or "-Pprebuilt-jooq" not in ln) for ln in lines
        )
    if job_class == "test":
        return any(goal("test", ln) for ln in lines)
    return False


def _cache_uses(workflow: str, job: str, steps: list[dict]) -> list[tuple[str, bool, list[dict]]]:
    """(class, saves, the steps that run for that class) for each maven-cache step; a ``${{ matrix.class }}`` class
    is expanded over the job's matrix, dropping steps whose ``if:`` names another class."""
    doc = yaml.safe_load((_WORKFLOWS / workflow).read_text()) or {}
    matrix = ((doc["jobs"][job].get("strategy") or {}).get("matrix") or {})
    out = []
    for s in steps:
        if s.get("uses") != _USES:
            continue
        w = s.get("with") or {}
        saves = str(w.get("save", "true")).lower() != "false"
        cls = str(w.get("job-class"))
        if cls == "${{ matrix.class }}":
            for value in matrix.get("class") or []:
                mine = [x for x in steps if not re.search(r"matrix\.class == '(?!%s')" % re.escape(value), str(x.get("if", "")))]
                out.append((value, saves, mine))
        else:
            out.append((cls, saves, steps))
    return out


@pytest.mark.parametrize(
    ("workflow", "job", "steps"),
    _hosted_maven_jobs(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_cache_step_saves_only_for_a_job_that_resolves_its_whole_class(
    workflow: str, job: str, steps: list[dict]
) -> None:
    """The first job to save a class's key defines it, an exact hit never saves again, and restore keys carry an
    entry forward to the next pom hash, so a job that resolves part of its class must pass ``save: false`` (develop
    run 44164358782: a cold jOOQ codegen job saved the package entry without maven-resources-plugin; restore keys
    carried it to the next hash, and run 44314623097's shaded-jar build fetched the plugin from Central and met a
    429)."""
    for job_class, saves, mine in _cache_uses(workflow, job, steps):
        if saves:
            assert _resolves_whole_class(job_class, mine), (
                f"{workflow} job {job}: saves the {job_class} class entry but runs no build that resolves all of "
                f"it; pass `save: false` to its {_USES} step"
            )


def test_the_class_census_is_not_vacuous() -> None:
    uses = [(f"{f}:{j}", c, sv) for f, j, s in _hosted_maven_jobs() for c, sv, _ in _cache_uses(f, j, s)]
    assert sum(not sv for _, _, sv in uses) >= 3, uses  # the two jOOQ codegen jobs and the release native leg
    assert {c for _, c, sv in uses if sv} == {"package", "native", "test"}, uses
    assert sum(sv for _, _, sv in uses) >= 9, uses  # 10 saving uses when this test was written


@pytest.mark.parametrize(
    ("job_class", "run", "whole"),
    [
        ("package", "./mvnw -q -DskipTests package", True),
        ("package", "./mvnw -q generate-sources", False),
        ("package", "./mvnw -q -Dmaven.test.skip=true generate-sources", False),
        ("package", "./mvnw dependency:go-offline", False),
        ("package", "./mvnw -q validate\n# then package it elsewhere", False),
        ("native", "./mvnw -B -Pnative -Pprebuilt-jooq -DskipTests package", False),
        ("native", "./mvnw -q generate-sources\n./mvnw -B -Pnative -Pprebuilt-jooq -DskipTests package", True),
        ("native", "./mvnw -B -Pnative -DskipTests package", True),
        ("test", "./mvnw -q -Dmaven.test.skip=true compile", False),
        ("test", "./mvnw -q test -Dgroups=integration", True),
    ],
)
def test_the_whole_class_reading_of_a_run(job_class: str, run: str, whole: bool) -> None:
    assert _resolves_whole_class(job_class, [{"run": line} for line in run.split("\n")]) is whole


def test_the_action_honours_save_and_keeps_one_key() -> None:
    """The callers' ``save: false`` only works while the action wires it: the saving step runs only for save
    'true', the restore-only step for save 'false' (or a tag), and both read one key and one restore-key list."""
    steps = (yaml.safe_load(_ACTION.read_text())["runs"])["steps"]
    cache = next(s for s in steps if str(s.get("uses", "")).startswith("actions/cache@"))
    restore = next(s for s in steps if str(s.get("uses", "")).startswith("actions/cache/restore@"))
    assert "inputs.save == 'true'" in str(cache.get("if")), cache.get("if")
    assert "inputs.save == 'false'" in str(restore.get("if")), restore.get("if")
    for field in ("key", "restore-keys", "path"):
        assert cache["with"][field] == restore["with"][field], field


@pytest.mark.parametrize(
    ("workflow", "job", "steps"),
    _hosted_maven_jobs(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_saving_cache_step_runs_only_when_maven_does(workflow: str, job: str, steps: list[dict]) -> None:
    """A job that succeeds saves what its cache step restored, whether Maven ran or not, so a Maven step that can
    be skipped (``if:``) leaves the restore-key entry saved under the new key with nothing built. A saving cache
    step must carry the condition of every Maven step after it, or pass ``save: false``. A Maven step skipped only
    on the cache step's own exact hit is exempt (maven-cache-seed.yml): an exact hit saves nothing."""
    for i, s in enumerate(steps):
        if s.get("uses") != _USES or str((s.get("with") or {}).get("save", "true")).lower() == "false":
            continue
        cache_if = str(s.get("if", "")).strip()
        own_hit = f"steps.{s['id']}.outputs.cache-hit != 'true'" if s.get("id") else None
        for later in steps[i + 1:]:
            later_if = str(later.get("if", "")).strip()
            if own_hit and later_if.startswith(own_hit):
                continue
            if _MAVEN_RUN.search(str(later.get("run", ""))) and later_if not in ("", cache_if):
                assert False, (
                    f"{workflow} job {job}: Maven step {later.get('name')!r} runs only if {later.get('if')!r}, "
                    f"but the saving {_USES} step runs if {cache_if or 'always'!r}"
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
