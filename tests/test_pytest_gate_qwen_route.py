# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner pushes to develop run the whole pytest suite on `qwen-linux`; everything else keeps the hosted shards.

ci.yml's `changes` job decides the route once (`route` step, output `ci_runner`)
and `service-jar`, `test`, `test-qwen` and `pytest-gate` all read that output.
The workflow cannot run in CI-of-CI, so these tests do the next best thing:

* evaluate the routing expression for every trigger shape (the expression is
  plain `&&` / `||` / `==` over string operands, which Python evaluates with the
  same operand-returning semantics);
* execute the REAL `pytest-gate` shell for every combination of route and job
  results, and prove a qwen job that did not run, did not finish or did not
  report cannot satisfy it (the non-vacuity the route exists to keep).

The Service CI routing it mirrors is pinned by its own comments; the two
differ on purpose in where the label is chosen (here a literal `runs-on`, with
the decision in a step output, because the six hosted shards must run in the
cases the qwen job does not).
"""
from __future__ import annotations

import itertools
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
QWEN_LABEL = "qwen-linux"
_GH_EXPR_RE = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")
OWNER = "Hellblazer"
OWNER_ID = "1111"


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _route_step() -> dict:
    steps = _doc()["jobs"]["changes"]["steps"]
    found = [s for s in steps if s.get("id") == "route"]
    assert len(found) == 1, "the `changes` job must carry exactly one `route` step"
    return found[0]


def _routing_expression() -> str:
    expr = _route_step()["env"]["CI_RUNNER"].strip()
    m = _GH_EXPR_RE.fullmatch(expr)
    assert m, f"CI_RUNNER must be a single ${{{{ }}}} expression, got {expr!r}"
    return m.group(1)


def _route(*, event: str = "push", actor_id: str = OWNER_ID, triggering_actor: str = OWNER,
           variable: str = "") -> str:
    """Evaluate the routing expression the way GitHub does for these contexts."""
    py = _routing_expression().replace("&&", " and ").replace("||", " or ")
    contexts = {
        "github.event_name": event,
        "github.actor_id": actor_id,
        "github.repository_owner_id": OWNER_ID,
        "github.triggering_actor": triggering_actor,
        "github.repository_owner": OWNER,
        "vars.QWEN_CI_PUSH_RUNNER": variable,
    }
    # longest first so `github.repository_owner_id` is not eaten by `github.repository_owner`
    for name in sorted(contexts, key=len, reverse=True):
        py = py.replace(name, repr(contexts[name]))
    assert "github." not in py and "vars." not in py, f"unevaluated context left in {py!r}"
    return eval(py, {"__builtins__": {}}, {})  # noqa: S307 - the expression is this repo's own YAML


# ── the routing expression ────────────────────────────────────────────────


@pytest.mark.parametrize("variable", ["", "qwen-linux", "hellmini", "hellmini-ci", "qwen", "Ubuntu-Latest", "ubuntu", "true"])
def test_an_owner_push_goes_to_qwen_unless_the_variable_is_exactly_ubuntu_latest(variable: str) -> None:
    assert _route(variable=variable) == QWEN_LABEL


def test_the_variable_set_to_exactly_ubuntu_latest_sends_an_owner_push_to_the_hosted_shards() -> None:
    assert _route(variable="ubuntu-latest") == "ubuntu-latest"


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"event": "pull_request"}, "a pull_request, even the owner's"),
        ({"event": "workflow_dispatch"}, "any other event"),
        ({"actor_id": "2222", "triggering_actor": "someone-else"}, "a push by another account"),
        ({"actor_id": "2222"}, "a renamed login that took the owner's name (id differs)"),
        ({"triggering_actor": "someone-else"}, "a re-run of the owner's push by another account"),
    ],
)
@pytest.mark.parametrize("variable", ["", "ubuntu-latest", "hellmini-ci"])
def test_everything_but_an_owner_push_stays_on_the_hosted_shards(kw: dict, why: str, variable: str) -> None:
    assert _route(variable=variable, **kw) == "ubuntu-latest", why


def test_no_input_can_produce_a_label_other_than_the_two_routes() -> None:
    """A typo or a hostile variable value can never name hellmini, hellmini-ci or any other label."""
    variables = ["", "ubuntu-latest", "hellmini", "hellmini-ci", "self-hosted", "qwen-linux ", "x" * 40]
    events = ["push", "pull_request", "workflow_dispatch", "schedule"]
    seen = {
        _route(event=e, actor_id=a, triggering_actor=t, variable=v)
        for e, a, t, v in itertools.product(events, [OWNER_ID, "2222"], [OWNER, "x"], variables)
    }
    assert seen == {QWEN_LABEL, "ubuntu-latest"}


def test_the_route_step_refuses_an_unrecognised_value_and_publishes_the_output() -> None:
    run = _route_step()["run"]
    assert "set -euo pipefail" in run
    assert 'echo "ci_runner=$CI_RUNNER" >> "$GITHUB_OUTPUT"' in run
    for bad in ("hellmini-ci", "", "qwen"):
        proc = subprocess.run(
            ["bash", "-c", run], capture_output=True, text=True,
            env={"CI_RUNNER": bad, "GITHUB_OUTPUT": "/dev/null", "GITHUB_EVENT_NAME": "push", "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 1, (bad, proc.stdout, proc.stderr)


# ── the qwen job ──────────────────────────────────────────────────────────


def test_the_qwen_job_runs_on_a_literal_label_set_that_only_the_qwen_runner_carries() -> None:
    job = _doc()["jobs"]["test-qwen"]
    runs_on = job["runs-on"]
    assert runs_on == ["self-hosted", "Linux", "X64", QWEN_LABEL], runs_on
    assert "${{" not in str(runs_on)
    assert not {"hellmini", "hellmini-ci", "gtr-windows", "ubuntu-latest"} & set(runs_on)


def test_the_qwen_job_is_gated_on_the_route_and_needs_only_changes() -> None:
    job = _doc()["jobs"]["test-qwen"]
    assert job["needs"] == ["changes"]
    assert job["if"].strip() == (
        "success() && needs.changes.outputs.code == 'true' && needs.changes.outputs.ci_runner == 'qwen-linux'")
    assert job["timeout-minutes"] >= 30


def test_the_qwen_job_name_survives_the_board_adapter_unmangled() -> None:
    """The CI board adapter replaces odd characters in job names with '?' (seen: a comma, `$`, braces).

    The name is also the key scripts/ci_status.py EXPECTED_JOBS looks for, so it must stay
    in the plain set the board keeps.
    """
    name = _doc()["jobs"]["test-qwen"]["name"]
    assert re.fullmatch(r"[A-Za-z0-9 ()/_.+-]+", name), name


def test_the_qwen_job_is_armed_the_way_the_shards_are() -> None:
    job = _doc()["jobs"]["test-qwen"]
    assert job["env"]["NX_T2_SUBSTRATE_EXPECTED"] == "1"
    steps = job["steps"]
    runs = [(i, str(s.get("run", ""))) for i, s in enumerate(steps)]
    jar = next(i for i, r in runs if "scripts/build-gate-jar.sh" in r)
    suite = next(i for i, r in runs if "pytest tests/" in r)
    floor = next(i for i, r in runs if "check_lint_leg_non_vacuity.py" in r)
    assert jar < suite < floor, "build the jar, run the suite, then the executed-count floor"
    suite_run = runs[suite][1]
    assert "-n 12" in suite_run and "--splits" not in suite_run
    assert "set -euo pipefail" in suite_run and "| tee suite-output.txt" in suite_run
    assert "suite-output.txt" in runs[floor][1] and re.search(r"--floor\s+\d{5}", runs[floor][1])
    # the consistency check reads the same ids the route does
    first = steps[0]["run"]
    for token in ("GITHUB_EVENT_NAME", "ACTOR_ID", "OWNER_ID", "TRIGGERING_ACTOR"):
        assert token in first


def test_the_hosted_path_still_shards_by_the_committed_durations() -> None:
    """`.test_durations.json` balancing is untouched: the shards keep --splits/--group."""
    run = next(s["run"] for s in _doc()["jobs"]["test"]["steps"] if "--splits" in str(s.get("run", "")))
    assert "--splits 6 --group ${{ matrix.shard }}" in run
    assert "--durations-path=.test_durations.json" in run


# ── pytest-gate, executed ─────────────────────────────────────────────────


def _gate_script(**values: str) -> str:
    job = _doc()["jobs"]["pytest-gate"]
    assert "test-qwen" in job["needs"]
    step = next(s for s in job["steps"] if s.get("name", "").startswith("Verify the pytest suite"))
    base = {
        "needs.changes.result": "success",
        "needs.changes.outputs.code": "true",
        "needs.changes.outputs.ci_runner": "ubuntu-latest",
        "needs.test.result": "success",
        "needs.test-qwen.result": "skipped",
        "needs.test-lint.result": "success",
        "needs.test-mode-census.result": "success",
        "needs.release-ledger-gate.result": "skipped",
        "github.event_name": "push",
        "github.base_ref": "",
    }
    base.update(values)

    def repl(m: re.Match[str]) -> str:
        return base[m.group(1).strip()]

    return _GH_EXPR_RE.sub(repl, step["run"])


def _gate(**values: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", _gate_script(**values)], capture_output=True, text=True)


def _qwen(test_qwen: str, test: str = "skipped", **extra: str) -> subprocess.CompletedProcess[str]:
    return _gate(**{"needs.changes.outputs.ci_runner": QWEN_LABEL, "needs.test-qwen.result": test_qwen,
                    "needs.test.result": test, **extra})


def test_the_gate_passes_on_the_qwen_route_only_when_the_qwen_job_succeeded_and_the_shards_were_skipped() -> None:
    proc = _qwen("success")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "pytest-gate: PASS" in proc.stdout


@pytest.mark.parametrize("qwen_result", ["skipped", "failure", "cancelled", "timed_out", ""])
def test_a_qwen_job_that_did_not_run_finish_or_report_never_satisfies_the_gate(qwen_result: str) -> None:
    proc = _qwen(qwen_result)
    assert proc.returncode == 1, (qwen_result, proc.stdout, proc.stderr)
    assert "test-qwen" in proc.stdout + proc.stderr


def test_green_shards_cannot_stand_in_for_the_qwen_job_on_the_qwen_route() -> None:
    proc = _qwen("skipped", test="success")
    assert proc.returncode == 1
    proc = _qwen("success", test="success")  # both ran: the route was not honoured
    assert proc.returncode == 1
    assert "routing inconsistency" in proc.stdout + proc.stderr


def test_the_hosted_route_needs_the_shards_and_a_skipped_qwen_job() -> None:
    assert _gate().returncode == 0
    for test_result in ("skipped", "failure", "cancelled", ""):
        assert _gate(**{"needs.test.result": test_result}).returncode == 1, test_result
    for qwen_result in ("success", "failure", ""):
        assert _gate(**{"needs.test-qwen.result": qwen_result}).returncode == 1, qwen_result


@pytest.mark.parametrize("ci_runner", ["", "hellmini-ci", "qwen", "self-hosted"])
def test_an_unrecognised_route_fails_loud_even_if_a_suite_path_succeeded(ci_runner: str) -> None:
    proc = _gate(**{"needs.changes.outputs.ci_runner": ci_runner, "needs.test.result": "success",
                    "needs.test-qwen.result": "success"})
    assert proc.returncode == 1
    assert "ci_runner" in proc.stdout + proc.stderr


@pytest.mark.parametrize("ci_runner", [QWEN_LABEL, "ubuntu-latest"])
def test_a_doc_only_diff_skips_both_suite_paths_but_not_a_failure_in_either(ci_runner: str) -> None:
    doc_only = {"needs.changes.outputs.code": "false", "needs.changes.outputs.ci_runner": ci_runner,
                "needs.test-lint.result": "skipped", "needs.test-mode-census.result": "skipped"}
    assert _gate(**{**doc_only, "needs.test.result": "skipped", "needs.test-qwen.result": "skipped"}).returncode == 0
    for which in ("needs.test.result", "needs.test-qwen.result"):
        for bad in ("failure", "cancelled"):
            other = {"needs.test.result": "skipped", "needs.test-qwen.result": "skipped"}
            other[which] = bad
            assert _gate(**{**doc_only, **other}).returncode == 1, (which, bad)


def test_a_failed_changes_job_fails_the_gate_whatever_the_route() -> None:
    assert _qwen("success", **{"needs.changes.result": "failure"}).returncode == 1


def test_the_other_legs_still_gate_on_the_qwen_route() -> None:
    assert _qwen("success", **{"needs.test-lint.result": "failure"}).returncode == 1
    assert _qwen("success", **{"needs.test-mode-census.result": "skipped"}).returncode == 1


def test_every_route_by_result_combination_has_exactly_the_documented_outcome() -> None:
    """The whole table, so a new branch in the gate cannot change an unlisted cell."""
    results = ["success", "skipped", "failure"]
    for code, ci_runner, test, qwen in itertools.product(["true", "false"], [QWEN_LABEL, "ubuntu-latest"], results, results):
        proc = _gate(**{"needs.changes.outputs.code": code, "needs.changes.outputs.ci_runner": ci_runner,
                        "needs.test.result": test, "needs.test-qwen.result": qwen,
                        "needs.test-lint.result": "success" if code == "true" else "skipped",
                        "needs.test-mode-census.result": "success" if code == "true" else "skipped"})
        if code == "true":
            chosen, other = (qwen, test) if ci_runner == QWEN_LABEL else (test, qwen)
            expected = chosen == "success" and other == "skipped"
        else:
            expected = test in ("success", "skipped") and qwen in ("success", "skipped")
        assert (proc.returncode == 0) == expected, (code, ci_runner, test, qwen, proc.stdout)
