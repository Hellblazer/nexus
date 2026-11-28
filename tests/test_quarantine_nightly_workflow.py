# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): the nightly quarantine workflow keeps the CI cost rules and its own rules.

``.github/workflows/quarantine-nightly.yml`` runs only the registry's tests, several passes a night. Checked here,
from the YAML: schedule and dispatch only (a nightly never re-tests a tree CI tested), a concurrency group; the
tests run on the lab pool with the fallback the repo's other pool jobs use; an empty registry builds nothing and
starts no pytest (exit 5); every pass's exit code goes through ``classify`` and nothing masks it; the report job
demands that every listed test was examined.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.census

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quarantine-nightly.yml"

_POOL_PYTEST = "${{ vars.NX_CI_POOL == 'off' && 'ubuntu-latest' || 'lab-6c18g' }}"
_POOL_JAVA = "${{ vars.NX_CI_POOL == 'off' && 'ubuntu-latest' || 'lab-16c16g' }}"


@pytest.fixture(scope="module")
def doc() -> dict:
    loaded = yaml.safe_load(_WORKFLOW.read_text())
    assert isinstance(loaded, dict), "workflow did not parse"
    return loaded


def _on(doc: dict) -> dict:
    triggers = doc.get("on", doc.get(True))  # PyYAML reads the bare key `on` as True
    assert isinstance(triggers, dict), "the workflow has no `on` mapping"
    return triggers


def _steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def _run_text(doc: dict, job: str) -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps(doc, job))


def _step(doc: dict, job: str, needle: str) -> dict:
    (found,) = [s for s in _steps(doc, job) if needle in str(s.get("run", ""))]
    return found


def _uses(doc: dict, job: str, prefix: str) -> list[dict]:
    return [s for s in _steps(doc, job) if str(s.get("uses", "")).startswith(prefix)]


def test_triggers_are_schedule_and_dispatch_only(doc: dict) -> None:
    assert set(_on(doc)) == {"schedule", "workflow_dispatch"}
    assert len(_on(doc)["schedule"]) == 1  # once a day


def test_has_a_concurrency_group_and_read_only_permissions(doc: dict) -> None:
    assert doc["concurrency"]["group"]
    assert doc["permissions"] == {"contents": "read"}


def test_the_jobs_are_the_four_the_design_names(doc: dict) -> None:
    assert set(doc["jobs"]) == {"prepare", "pytest", "java", "report"}


def test_pytest_runs_on_the_pytest_pool_class_with_the_pool_off_fallback(doc: dict) -> None:
    assert doc["jobs"]["pytest"]["runs-on"] == _POOL_PYTEST


def test_java_runs_on_the_service_ci_pool_class_with_the_pool_off_fallback(doc: dict) -> None:
    assert doc["jobs"]["java"]["runs-on"] == _POOL_JAVA


def test_the_light_jobs_are_hosted_and_no_job_names_a_billed_premium_runner(doc: dict) -> None:
    assert doc["jobs"]["prepare"]["runs-on"] == "ubuntu-latest"
    assert doc["jobs"]["report"]["runs-on"] == "ubuntu-latest"
    for job in doc["jobs"].values():
        assert "macos" not in str(job["runs-on"]) and "windows" not in str(job["runs-on"])


def test_each_pool_job_probes_its_host_binaries_before_anything_else(doc: dict) -> None:
    for name, must_have in (("pytest", "rg"), ("java", "docker")):
        first = _steps(doc, name)[0]
        assert first["if"] == "runner.environment == 'self-hosted'"
        assert must_have in first["env"]["NX_CI_HOST_BINARIES"].split()
    assert "docker info" in _run_text(doc, "java")


def test_pool_jobs_skip_the_actions_cache_for_home_paths_and_prime_with_persistent_home(doc: dict) -> None:
    home_caches = 0
    primes = 0
    for name in ("pytest", "java"):
        for s in _uses(doc, name, "actions/cache"):
            if str(s["with"]["path"]).startswith("~"):
                home_caches += 1
                assert s["if"] == "runner.environment == 'github-hosted'"
        for s in _uses(doc, name, "./.github/actions/prime-"):
            primes += 1
            assert s["with"]["persistent-home"] == "${{ runner.environment == 'self-hosted' }}"
    assert home_caches >= 2 and primes >= 3  # the census saw what it checks


def test_a_dispatch_checks_out_the_branch_it_was_dispatched_on_and_later_jobs_the_same_revision(doc: dict) -> None:
    ref = _uses(doc, "prepare", "actions/checkout")[0]["with"]["ref"]
    assert "workflow_dispatch" in ref and "github.ref_name" in ref
    for name in ("pytest", "java", "report"):
        (checkout,) = _uses(doc, name, "actions/checkout")
        assert checkout["with"]["ref"] == "${{ needs.prepare.outputs.revision }}"


def test_an_empty_registry_builds_nothing_and_starts_no_job_after_the_first(doc: dict) -> None:
    assert "0 quarantined" in _run_text(doc, "prepare")
    assert set(doc["jobs"]["prepare"]["outputs"]) == {"pytest", "java", "revision"}
    built = [s for s in _steps(doc, "prepare") if s.get("name", "").startswith(("Set up GraalVM", "Restore the Maven", "Build shaded jar", "Upload jar"))]
    assert len(built) == 4
    assert all(s["if"] == "steps.select.outputs.pytest == 'true'" for s in built)  # the jar is only for a pytest entry
    assert doc["jobs"]["pytest"]["if"] == "${{ needs.prepare.outputs.pytest == 'true' }}"
    assert doc["jobs"]["java"]["if"] == "${{ needs.prepare.outputs.java == 'true' }}"
    assert "uv run pytest" not in _run_text(doc, "prepare")  # the first job never runs pytest


def test_the_pass_count_is_one_env_var_above_one(doc: dict) -> None:
    assert int(doc["env"]["NX_QUARANTINE_PASSES"]) > 1
    for job in ("pytest", "java"):
        assert 'seq 1 "$NX_QUARANTINE_PASSES"' in _run_text(doc, job)


def test_every_pytest_pass_goes_through_classify_and_nothing_masks_its_exit(doc: dict) -> None:
    step = _step(doc, "pytest", "uv run pytest")
    run = step["run"]
    assert "-m quarantine" in run and '-o addopts=""' in run
    assert "NX_QUARANTINE_STRICT=1" in run
    assert "--junitxml" in run and "|| rc=$?" in run
    assert 'classify --rc "$rc"' in run and "|| bad=1" in run and 'exit "$bad"' in run
    assert "|| true" not in run and "set +e" not in run
    assert "continue-on-error" not in step


def test_every_java_pass_goes_through_classify_and_nothing_masks_its_exit(doc: dict) -> None:
    step = _step(doc, "java", "./mvnw")
    run = step["run"]
    assert '"-Dtest=$selector"' in run and "-Dtest.excluded.groups=" in run
    assert "|| rc=$?" in run and 'classify --rc "$rc"' in run and "|| bad=1" in run and 'exit "$bad"' in run
    assert "|| true" not in run and "set +e" not in run
    assert "surefire-reports" in run
    assert "continue-on-error" not in step


def test_the_java_job_does_not_save_a_partial_maven_cache(doc: dict) -> None:
    (step,) = _uses(doc, "java", "./.github/actions/maven-cache")
    assert step["with"] == {"job-class": "test", "save": "false"}


def test_the_report_runs_whatever_the_passes_did_and_demands_every_test_was_examined(doc: dict) -> None:
    report = doc["jobs"]["report"]
    assert set(report["needs"]) == {"prepare", "pytest", "java"}
    assert "!cancelled()" in report["if"] and "needs.prepare.result == 'success'" in report["if"]
    run = _step(doc, "report", "quarantine_report.py")["run"]
    assert "--expect-all" in run and "--junit quarantine-results" in run and 'exit "$rc"' in run
    assert "|| true" not in run


def test_the_passes_upload_their_junit_for_the_report(doc: dict) -> None:
    for job, name in (("pytest", "quarantine-junit-pytest"), ("java", "quarantine-junit-java")):
        (up,) = _uses(doc, job, "actions/upload-artifact")
        assert up["with"]["name"] == name and up["if"] == "${{ always() }}"
    (down,) = _uses(doc, "report", "actions/download-artifact")
    assert down["with"]["pattern"] == "quarantine-junit-*"


def test_the_pytest_job_arms_the_engine_substrate(doc: dict) -> None:
    assert doc["jobs"]["pytest"]["env"]["NX_T2_SUBSTRATE_EXPECTED"] == "1"  # a missing jar fails loudly, never skips


def test_the_workflow_never_changes_the_repo() -> None:
    text = _WORKFLOW.read_text()
    for forbidden in ("git commit", "git push", "gh pr"):
        assert forbidden not in text
