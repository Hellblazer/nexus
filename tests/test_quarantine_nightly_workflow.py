# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): the nightly quarantine workflow keeps the CI cost rules and the empty-registry rule.

``.github/workflows/quarantine-nightly.yml`` runs only ``-m quarantine`` tests once a day. Checked here, from the
YAML: it is schedule and dispatch only (never push or pull request: a nightly never re-tests a tree CI tested),
it has a concurrency group, it runs on the hosted runner the sibling nightly uses, and with an empty registry it
reports "0 quarantined" and never starts pytest (pytest exits 5 on an empty selection). With entries, exit 5 is a
failure, a failing quarantined test is not (the report shows it), and any other exit code fails the job.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.census

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quarantine-nightly.yml"


@pytest.fixture(scope="module")
def doc() -> dict:
    loaded = yaml.safe_load(_WORKFLOW.read_text())
    assert isinstance(loaded, dict), "workflow did not parse"
    return loaded


def _on(doc: dict) -> dict:
    return doc.get("on") or doc.get(True)  # PyYAML reads the bare key `on` as True


def _steps(job: dict) -> list[dict]:
    return job["steps"]


def _pytest_steps(doc: dict) -> list[dict]:
    return [s for j in doc["jobs"].values() for s in _steps(j) if "pytest" in str(s.get("run", ""))]


def test_triggers_are_schedule_and_dispatch_only(doc: dict) -> None:
    assert set(_on(doc)) == {"schedule", "workflow_dispatch"}
    assert len(_on(doc)["schedule"]) == 1  # once a day


def test_has_a_concurrency_group_and_read_only_permissions(doc: dict) -> None:
    assert doc["concurrency"]["group"]
    assert doc["permissions"] == {"contents": "read"}


def test_every_job_runs_on_the_hosted_runner_the_sibling_nightly_uses(doc: dict) -> None:
    sibling = yaml.safe_load((_WORKFLOW.with_name("local-service-gate-nightly.yml")).read_text())
    assert {j["runs-on"] for j in sibling["jobs"].values()} == {"ubuntu-latest"}
    assert {j["runs-on"] for j in doc["jobs"].values()} == {"ubuntu-latest"}


def test_a_dispatch_checks_out_the_branch_it_was_dispatched_on(doc: dict) -> None:
    for job in doc["jobs"].values():
        ref = _steps(job)[0]["with"]["ref"]
        assert "workflow_dispatch" in ref and "github.ref_name" in ref


def test_the_registry_job_counts_before_anything_heavy(doc: dict) -> None:
    jobs = doc["jobs"]
    assert "scripts/quarantine_report.py count" in "\n".join(str(s.get("run", "")) for s in _steps(jobs["registry"]))
    assert jobs["registry"]["outputs"]["count"]
    run = jobs["quarantine"]
    assert run["needs"] == "registry" or "registry" in run["needs"]
    assert "needs.registry.outputs.count" in run["if"] and "'0'" in run["if"]


def test_an_empty_registry_reports_zero_quarantined(doc: dict) -> None:
    text = "\n".join(str(s.get("run", "")) for s in _steps(doc["jobs"]["registry"]))
    assert "0 quarantined" in text


def test_pytest_is_started_only_in_the_job_the_count_gates(doc: dict) -> None:
    assert "pytest" not in "\n".join(str(s.get("run", "")) for s in _steps(doc["jobs"]["registry"]))
    assert len(_pytest_steps(doc)) == 1
    assert "-m quarantine" in _pytest_steps(doc)[0]["run"]


def test_pytest_exit_codes_are_handled_one_by_one(doc: dict) -> None:
    run = _pytest_steps(doc)[0]["run"]
    assert "set +e" in run and "rc=$?" in run
    assert "0|1)" in run  # passed, or a quarantined test failed: the report shows which
    assert "5)" in run  # nothing selected although the registry lists tests: a failure, never a pass
    assert "*)" in run  # any other exit code fails the job
    assert "|| true" not in run and "continue-on-error" not in str(_pytest_steps(doc)[0])


def test_the_run_arms_the_engine_and_records_results(doc: dict) -> None:
    job = doc["jobs"]["quarantine"]
    assert job["env"]["NX_T2_SUBSTRATE_EXPECTED"] == "1"  # a missing jar fails loudly instead of skipping
    assert "NX_QUARANTINE_RESULTS" in str(_pytest_steps(doc)[0])


def test_the_report_runs_even_after_a_failed_pytest_and_demands_a_result_per_entry(doc: dict) -> None:
    (step,) = [s for s in _steps(doc["jobs"]["quarantine"]) if "quarantine_report.py" in str(s.get("run", ""))]
    assert "always()" in step["if"]
    assert "--expect-all" in step["run"] and "--results" in step["run"]


def test_the_job_never_releases_a_passing_test(doc: dict) -> None:
    text = _WORKFLOW.read_text()
    assert "release by hand" in text or "by hand" in text
    for forbidden in ("git commit", "git push", "gh pr"):
        assert forbidden not in text
