# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service CI's Java job defaults to GitHub-hosted and opts into `hellmini-ci` only on an exact variable (nexus-xyrtc).

service-ci.yml's `changes` job decides the route once (`route` step, output
`java_runner`) and the Java job's `runs-on` reads it. The workflow cannot run in
CI-of-CI, so this does the next best thing, the same way
tests/test_pytest_gate_qwen_route.py does for the pytest route: evaluate the
owner-push expression with GitHub's case-insensitive `==`, then EXECUTE the real
`route` step under `bash -eo pipefail` with the variable passed raw.
"""
from __future__ import annotations

import itertools
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "service-ci.yml"
JAVA_JOB = "java-test-and-drift-guard"
HOSTED = "ubuntu-latest"
SELF_HOSTED = "hellmini-ci"
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


def _owner_push_expression() -> str:
    expr = _route_step()["env"]["OWNER_PUSH"].strip()
    m = _GH_EXPR_RE.fullmatch(expr)
    assert m, f"OWNER_PUSH must be a single ${{{{ }}}} expression, got {expr!r}"
    return m.group(1)


class _GhStr(str):
    """A string whose `==` ignores case, as every GitHub Actions `==` on strings does."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and self.casefold() == other.casefold()

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = str.__hash__


def _owner_push(*, event: str, actor_id: str, triggering_actor: str) -> str:
    py = _owner_push_expression().replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"'[^']*'", lambda m: f"_GhStr({m.group(0)})", py)
    contexts = {
        "github.event_name": event,
        "github.actor_id": actor_id,
        "github.repository_owner_id": OWNER_ID,
        "github.triggering_actor": triggering_actor,
        "github.repository_owner": OWNER,
    }
    for name in sorted(contexts, key=len, reverse=True):  # longest first
        py = py.replace(name, f"_GhStr({contexts[name]!r})")
    assert "github." not in py and "vars." not in py, f"unevaluated context left in {py!r}"
    return "true" if eval(py, {"__builtins__": {}, "_GhStr": _GhStr}, {}) is True else "false"  # noqa: S307 - this repo's own YAML


def _run(script: str, *, event: str, variable: str, owner_push: str) -> tuple[subprocess.CompletedProcess[str], str]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "github_output"
        out.write_text("")
        env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), "GITHUB_EVENT_NAME": event,
               "OWNER_PUSH": owner_push, "SERVICE_CI_PUSH_RUNNER": variable}
        proc = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True, env=env)
        return proc, out.read_text()


def _route(*, event: str = "push", actor_id: str = OWNER_ID, triggering_actor: str = OWNER,
           variable: str = "") -> str:
    step = _route_step()
    assert step["env"]["SERVICE_CI_PUSH_RUNNER"] == "${{ vars.SERVICE_CI_PUSH_RUNNER }}", "the variable must travel raw"
    owner_push = _owner_push(event=event, actor_id=actor_id, triggering_actor=triggering_actor)
    proc, published = _run(step["run"], event=event, variable=variable, owner_push=owner_push)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert re.fullmatch(r"java_runner=(hellmini-ci|ubuntu-latest)\n", published), published
    return published.removeprefix("java_runner=").strip()


# ── the routing: hosted by default, opt-in on the exact value ─────────────


def test_an_owner_push_runs_hosted_while_the_variable_is_unset() -> None:
    assert _route(variable="") == HOSTED


def test_only_the_exact_value_hellmini_ci_opts_an_owner_push_in() -> None:
    assert _route(variable="hellmini-ci") == SELF_HOSTED


@pytest.mark.parametrize("variable", [
    "", "ubuntu-latest", "hellmini", "Hellmini-CI", "HELLMINI-CI", " hellmini-ci", "hellmini-ci ", "hellmini-ci\n",
    "hellmini-ci,ubuntu-latest", "self-hosted", "hellmini_ci", "true", "1", "garbage", "x" * 40,
])
def test_every_other_variable_value_runs_hosted(variable: str) -> None:
    """Empty, another label (the release runner `hellmini` included), a typo, a stray space, a different case."""
    assert _route(variable=variable) == HOSTED


def test_the_login_comparison_ignores_case_like_githubs() -> None:
    assert _route(triggering_actor=OWNER.lower(), variable="hellmini-ci") == SELF_HOSTED


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
@pytest.mark.parametrize("variable", ["", "hellmini-ci", "ubuntu-latest", "hellmini"])
def test_everything_but_an_owner_push_runs_hosted_even_with_the_variable_on(kw: dict, why: str, variable: str) -> None:
    assert _route(variable=variable, **kw) == HOSTED, why


def test_no_input_can_produce_a_label_other_than_the_two_routes() -> None:
    """The variable can never name `hellmini` (the release runner) or any other label."""
    variables = ["", "hellmini-ci", "hellmini", "self-hosted", "Hellmini-CI", "hellmini-ci ", "x" * 40]
    events = ["push", "pull_request", "workflow_dispatch", "schedule"]
    seen = {
        _route(event=e, actor_id=a, triggering_actor=t, variable=v)
        for e, a, t, v in itertools.product(events, [OWNER_ID, "2222"], [OWNER, "x"], variables)
    }
    assert seen == {SELF_HOSTED, HOSTED}


def test_exactly_one_combination_reaches_the_self_hosted_runner() -> None:
    variables = ["", "hellmini-ci", "Hellmini-CI", "hellmini-ci ", "ubuntu-latest"]
    reached = [
        (e, a, t, v)
        for e, a, t, v in itertools.product(["push", "pull_request"], [OWNER_ID, "2222"], [OWNER, "x"], variables)
        if _route(event=e, actor_id=a, triggering_actor=t, variable=v) == SELF_HOSTED
    ]
    assert reached == [("push", OWNER_ID, OWNER, "hellmini-ci")]


# ── structure: the decision is in bash and the Java job only reads it ─────


def test_the_variable_is_compared_in_bash_because_a_github_expression_cannot_compare_case_sensitively() -> None:
    step = _route_step()
    assert "SERVICE_CI_PUSH_RUNNER" not in step["env"]["OWNER_PUSH"], "the variable must not be compared in the expression"
    assert '"$SERVICE_CI_PUSH_RUNNER" = "hellmini-ci"' in step["run"]
    assert "vars." not in _owner_push_expression()
    assert "set -euo pipefail" in step["run"]


def test_the_java_job_runs_on_the_published_output_and_no_expression_names_the_variable_or_a_self_hosted_label() -> None:
    doc = _doc()
    assert doc["jobs"]["changes"]["outputs"]["java_runner"] == "${{ steps.route.outputs.java_runner }}"
    job = doc["jobs"][JAVA_JOB]
    assert "changes" in (job["needs"] if isinstance(job["needs"], list) else [job["needs"]])
    runs_on = str(job["runs-on"])
    assert "needs.changes.outputs.java_runner" in runs_on
    assert "vars." not in runs_on and "hellmini" not in runs_on, runs_on
    # the `changes` job itself, which decides the route, never leaves the hosted runner
    assert doc["jobs"]["changes"]["runs-on"] == HOSTED


@pytest.mark.parametrize(("old", "new", "label"), [
    ('[ "$OWNER_PUSH" = "true" ] &&', '[ "$OWNER_PUSH" = "true" ] ||', "&& turned into || (the variable alone opts in)"),
    ('"$SERVICE_CI_PUSH_RUNNER" = "hellmini-ci"', '"$SERVICE_CI_PUSH_RUNNER" != "hellmini-ci"', "variable comparison inverted"),
    ("JAVA_RUNNER=ubuntu-latest\n", "JAVA_RUNNER=hellmini-ci\n", "default flipped back to hellmini-ci"),
    ('"$OWNER_PUSH" = "true"', '"$OWNER_PUSH" = "false"', "owner rule inverted"),
])
def test_the_route_table_kills_each_obvious_mutant_of_the_step(old: str, new: str, label: str) -> None:
    """The table must have power: each mutant disagrees with it on at least one row."""
    step = _route_step()
    assert old in step["run"], f"mutation site {old!r} no longer in the step: update this test with the step"
    rows = [(e, v) for e in ("push", "pull_request") for v in ("", "hellmini-ci", "Hellmini-CI", "ubuntu-latest")]
    want = {(e, v): (SELF_HOSTED if (e, v) == ("push", "hellmini-ci") else HOSTED) for e, v in rows}

    def run(script: str, event: str, variable: str) -> str:
        owner_push = _owner_push(event=event, actor_id=OWNER_ID, triggering_actor=OWNER)
        _, published = _run(script, event=event, variable=variable, owner_push=owner_push)
        return published.removeprefix("java_runner=").strip()

    assert all(run(step["run"], e, v) == want[(e, v)] for e, v in rows)
    assert any(run(step["run"].replace(old, new), e, v) != want[(e, v)] for e, v in rows), label


def test_the_route_step_publishes_nothing_on_a_shell_error() -> None:
    """An unset OWNER_PUSH is a wiring defect: the step must not guess a route."""
    step = _route_step()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "o"
        out.write_text("")
        env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), "GITHUB_EVENT_NAME": "push",
               "SERVICE_CI_PUSH_RUNNER": "hellmini-ci"}
        proc = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=env)
        assert proc.returncode != 0 and out.read_text() == ""
