# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner pushes to develop run the whole pytest suite on `qwen-linux`; everything else keeps the hosted shards.

ci.yml's `changes` job decides the route once (`route` step, output `ci_runner`)
and `service-jar`, `test`, `test-qwen` and `pytest-gate` all read that output.
The workflow cannot run in CI-of-CI, so these tests do the next best thing:

* evaluate the routing expression for every trigger shape (the expression is
  plain `&&` / `||` / `==` over string operands, which Python evaluates with the
  same operand-returning semantics, once `==` is made case-insensitive the way
  GitHub's is);
* EXECUTE the two small shell steps that guard the route (`route`, and the
  qwen job's consistency check) with owner and non-owner environments, and
  prove the table has the power to catch the obvious mutants of each;
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
import os
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


class _GhStr(str):
    """A string whose `==` ignores case, as every GitHub Actions `==` on strings does."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and self.casefold() == other.casefold()

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = str.__hash__


def _route(*, event: str = "push", actor_id: str = OWNER_ID, triggering_actor: str = OWNER,
           variable: str = "") -> str:
    """Evaluate the routing expression the way GitHub does for these contexts."""
    py = _routing_expression().replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"'[^']*'", lambda m: f"_GhStr({m.group(0)})", py)  # literals compare case-insensitively
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
        py = py.replace(name, f"_GhStr({contexts[name]!r})")
    assert "github." not in py and "vars." not in py, f"unevaluated context left in {py!r}"
    return str(eval(py, {"__builtins__": {}, "_GhStr": _GhStr}, {}))  # noqa: S307 - the expression is this repo's own YAML


# ── the routing expression ────────────────────────────────────────────────


@pytest.mark.parametrize("variable", ["", "qwen-linux", "hellmini", "hellmini-ci", "qwen", "ubuntu", "ubuntu-latest ", "true"])
def test_an_owner_push_goes_to_qwen_unless_the_variable_is_ubuntu_latest(variable: str) -> None:
    assert _route(variable=variable) == QWEN_LABEL


@pytest.mark.parametrize("variable", ["ubuntu-latest", "Ubuntu-Latest", "UBUNTU-LATEST"])
def test_the_variable_set_to_ubuntu_latest_in_any_case_sends_an_owner_push_to_the_hosted_shards(variable: str) -> None:
    """GitHub's `==` on strings ignores case, so `Ubuntu-Latest` toggles the route too."""
    assert _route(variable=variable) == "ubuntu-latest"


def test_the_login_comparison_ignores_case_like_githubs() -> None:
    assert _route(triggering_actor=OWNER.lower()) == QWEN_LABEL


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


def _run_step(step: dict, tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Execute a step's `run` script the way the runner does (bash -eo pipefail), with its own env only."""
    out, genv = tmp_path / "github_output", tmp_path / "github_env"
    out.write_text("")
    genv.write_text("")
    full = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(genv), **env}
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=full)
    return proc, out, genv


def test_the_route_step_publishes_the_output_for_each_of_the_two_routes(tmp_path: Path) -> None:
    """The positive half: a step that always exited 1 would pass the refusal test alone."""
    for good in (QWEN_LABEL, "ubuntu-latest"):
        proc, out, _ = _run_step(_route_step(), tmp_path, CI_RUNNER=good, GITHUB_EVENT_NAME="push")
        assert proc.returncode == 0, (good, proc.stdout, proc.stderr)
        assert out.read_text() == f"ci_runner={good}\n"
        assert good in proc.stdout


def test_the_route_step_refuses_an_unrecognised_value_and_publishes_nothing(tmp_path: Path) -> None:
    assert "set -euo pipefail" in _route_step()["run"]
    for bad in ("hellmini-ci", "hellmini", "", "qwen", "self-hosted", "QWEN-LINUX"):
        proc, out, _ = _run_step(_route_step(), tmp_path, CI_RUNNER=bad, GITHUB_EVENT_NAME="push")
        assert proc.returncode == 1, (bad, proc.stdout, proc.stderr)
        assert out.read_text() == "", f"a refused value must not reach GITHUB_OUTPUT: {bad!r}"


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


def _qwen_step(prefix: str) -> dict:
    steps = [s for s in _doc()["jobs"]["test-qwen"]["steps"] if str(s.get("name", "")).startswith(prefix)]
    assert len(steps) == 1, (prefix, [s.get("name") for s in _doc()["jobs"]["test-qwen"]["steps"]])
    return steps[0]


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


def test_the_guard_steps_run_before_the_checkout_and_in_a_fixed_order() -> None:
    names = [str(s.get("name", s.get("uses", ""))) for s in _doc()["jobs"]["test-qwen"]["steps"]]
    order = [next(i for i, n in enumerate(names) if n.startswith(p)) for p in (
        "Self-hosted runner consistency check", "Shared suite lease directory", "Toolchain preflight", "actions/checkout")]
    assert order == sorted(order) and order[0] == 0, names


# The consistency check: executed, not read. Rows are (event, actor_id, triggering_actor, expected rc).
_OWNER_PUSH = ("push", OWNER_ID, OWNER)
_CONSISTENCY_TABLE = [
    (*_OWNER_PUSH, 0),
    ("pull_request", OWNER_ID, OWNER, 1),
    ("workflow_dispatch", OWNER_ID, OWNER, 1),
    ("schedule", OWNER_ID, OWNER, 1),
    ("push", "2222", OWNER, 1),         # another account under the owner's name
    ("push", OWNER_ID, "someone-else", 1),  # a re-run of the owner's push by another account
    ("push", "2222", "someone-else", 1),
    ("pull_request", "2222", "someone-else", 1),
]


def _consistency_rc(run: str, tmp_path: Path, event: str, actor_id: str, triggering_actor: str) -> int:
    step = {"run": run}
    proc, _, _ = _run_step(step, tmp_path, GITHUB_EVENT_NAME=event, ACTOR_ID=actor_id, OWNER_ID=OWNER_ID,
                           TRIGGERING_ACTOR=triggering_actor, OWNER=OWNER)
    return proc.returncode


@pytest.mark.parametrize(("event", "actor_id", "triggering_actor", "expected"), _CONSISTENCY_TABLE)
def test_the_consistency_check_passes_an_owner_push_and_refuses_every_other_shape(
        event: str, actor_id: str, triggering_actor: str, expected: int, tmp_path: Path) -> None:
    step = _qwen_step("Self-hosted runner consistency check")
    proc, _, _ = _run_step(step, tmp_path, GITHUB_EVENT_NAME=event, ACTOR_ID=actor_id, OWNER_ID=OWNER_ID,
                           TRIGGERING_ACTOR=triggering_actor, OWNER=OWNER)
    assert proc.returncode == expected, (proc.stdout, proc.stderr)
    if expected:
        assert "routing inconsistency" in proc.stdout + proc.stderr


def test_the_consistency_check_reads_the_ids_the_route_does() -> None:
    step = _qwen_step("Self-hosted runner consistency check")
    env = step["env"]
    assert env["ACTOR_ID"] == "${{ github.actor_id }}" and env["OWNER_ID"] == "${{ github.repository_owner_id }}"
    assert env["TRIGGERING_ACTOR"] == "${{ github.triggering_actor }}" and env["OWNER"] == "${{ github.repository_owner }}"
    assert "GITHUB_EVENT_NAME" in step["run"]


@pytest.mark.parametrize(("old", "new", "label"), [
    ('"$GITHUB_EVENT_NAME" != "push"', '"$GITHUB_EVENT_NAME" == "push"', "event comparison flipped"),
    ("] || [", "] && [", "|| turned into &&"),
    ('"$ACTOR_ID" != "$OWNER_ID"', '"$ACTOR_ID" == "$OWNER_ID"', "actor id comparison flipped"),
    ('"$TRIGGERING_ACTOR" != "$OWNER"', '"$TRIGGERING_ACTOR" == "$OWNER"', "triggering actor comparison flipped"),
    ("exit 1", "exit 0", "refusal that exits 0"),
])
def test_the_consistency_table_kills_each_obvious_mutant_of_the_check(old: str, new: str, label: str, tmp_path: Path) -> None:
    """The table must have power: each mutant has to disagree with it on at least one row."""
    run = _qwen_step("Self-hosted runner consistency check")["run"]
    assert old in run, f"mutation site {old!r} no longer in the step: update this test with the step"
    mutant = run.replace(old, new)
    assert any(_consistency_rc(mutant, tmp_path, *row[:3]) != row[3] for row in _CONSISTENCY_TABLE), label
    # and the unmutated step agrees with every row (so the mutant disagreement is the mutation's doing)
    assert all(_consistency_rc(run, tmp_path, *row[:3]) == row[3] for row in _CONSISTENCY_TABLE)


# The shared suite lease directory step, executed against real directories.


def _lease(tmp_path: Path, root: Path | str, **env: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    proc, _, genv = _run_step(_qwen_step("Shared suite lease directory"), tmp_path, QWEN_SUITE_LEASE_ROOT=str(root), **env)
    return proc, genv


def test_the_lease_root_defaults_to_a_host_path_and_can_be_overridden_by_a_repository_variable() -> None:
    expr = _doc()["jobs"]["test-qwen"]["env"]["QWEN_SUITE_LEASE_ROOT"]
    assert "vars.QWEN_SUITE_LEASE_ROOT" in expr and "'/var/lib/nx-suite-lease'" in expr


def test_a_usable_lease_root_is_exported_for_the_build_and_suite_leases(tmp_path: Path) -> None:
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o2775)
    proc, genv = _lease(tmp_path, root)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert genv.read_text().splitlines() == [f"NX_BUILD_LEASE_ROOT={root}", "NX_SUITE_LEASE_WAIT=1"]
    assert list(root.iterdir()) == [], "the write probe must clean up after itself"


def test_a_missing_lease_root_fails_loud_with_the_host_setup_and_exports_nothing(tmp_path: Path) -> None:
    proc, genv = _lease(tmp_path, tmp_path / "does-not-exist")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert genv.read_text() == "", "a job that cannot take the lease must not export a root it cannot use"
    for needle in ("groupadd", "usermod -aG nx-suite ghci", "usermod -aG nx-suite nxtest", "install -d",
                   "-m 2775", "NX_BUILD_LEASE_ROOT=", "NX_SUITE_LEASE_WAIT=1", str(tmp_path / "does-not-exist")):
        assert needle in out, needle


def test_a_symlinked_lease_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    proc, _ = _lease(tmp_path, link)
    assert proc.returncode == 1, proc.stdout + proc.stderr


def test_a_sticky_lease_root_is_refused_because_a_dead_holders_lease_could_never_be_reclaimed(tmp_path: Path) -> None:
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o1777)
    proc, genv = _lease(tmp_path, root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "sticky" in proc.stdout + proc.stderr
    assert genv.read_text() == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere, so the permission refusal cannot be observed")
def test_an_unwritable_lease_root_is_refused_not_silently_unguarded(tmp_path: Path) -> None:
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o555)
    try:
        proc, genv = _lease(tmp_path, root)
    finally:
        os.chmod(root, 0o755)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert genv.read_text() == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere, so the unwritable root cannot be built")
def test_the_premise_the_step_exists_for_an_unwritable_root_makes_the_suite_lease_run_unguarded(tmp_path: Path) -> None:
    """tests/_suite_lease.py swallows an unwritable root and hands back a no-op release, saying nothing.

    If this stops being true the step's message ("would run unguarded") is stale and the step may be loosened.
    """
    from tests import _suite_lease

    root = tmp_path / "ro"
    root.mkdir()
    os.chmod(root, 0o555)
    try:
        release = _suite_lease.acquire("probe", lease_root=root / "nested")
        assert release is not None, "an unwritable root must read as 'no lease taken', not as 'held by someone'"
        assert not (root / "nested").exists()
        assert _suite_lease.holder(root / "nested") is None
        release()
    finally:
        os.chmod(root, 0o755)
        os.environ.pop(_suite_lease.HELD_BY_ENV, None)


def test_the_two_users_share_one_lease_root_so_the_second_run_sees_the_first(tmp_path: Path) -> None:
    """With the root fixed by the environment (not the checkout's git dir), a second acquirer is refused."""
    from tests import _suite_lease

    root = tmp_path / "shared"
    release = _suite_lease.acquire("ci job", lease_root=root)
    try:
        assert release is not None
        assert _suite_lease.acquire("hand run", lease_root=root, wait_seconds=0) is None
        assert "ci job" in (_suite_lease.holder(root) or "")
    finally:
        if release:
            release()
        os.environ.pop(_suite_lease.HELD_BY_ENV, None)


# The toolchain preflight: TMPDIR must be a private persistent directory.


@pytest.mark.parametrize("tmpdir", ["", "/tmp", "/tmp/ghci", "/var/tmp", "/var/tmp/x"])
def test_the_preflight_refuses_a_tmpdir_shared_with_the_other_users_of_the_distro(tmpdir: str, tmp_path: Path) -> None:
    proc, _, _ = _run_step(_qwen_step("Toolchain preflight"), tmp_path, TMPDIR=tmpdir)
    assert proc.returncode == 1
    assert "TMPDIR" in proc.stdout + proc.stderr


def test_the_preflight_accepts_a_private_tmpdir_and_goes_on_to_the_tool_check(tmp_path: Path) -> None:
    private = tmp_path / "ghci-tmp"
    if str(private).startswith(("/tmp", "/var/tmp")):
        pytest.skip("pytest's tmp_path is itself under a shared temp directory here")
    proc, _, _ = _run_step(_qwen_step("Toolchain preflight"), tmp_path, TMPDIR=str(private))
    out = proc.stdout + proc.stderr
    assert "TMPDIR is" not in out and private.is_dir()
    # on this PATH the host tools are absent, which is the NEXT check speaking
    assert proc.returncode == 1 and "missing host tools" in out


def test_the_tmpdir_stays_persistent_on_purpose_and_the_reason_is_in_the_job() -> None:
    """Not RUNNER_TEMP: the orphan sweep finds a killed run's cluster through sidecars under TMPDIR."""
    job = _doc()["jobs"]["test-qwen"]
    assert "TMPDIR" not in job.get("env", {}), "service-ci's RUNNER_TEMP pin must not be copied here"
    text = WORKFLOW.read_text()
    assert "sidecar" in text and "tests/_engine_substrate.py" in text


def test_the_sweep_the_persistent_tmpdir_exists_for_really_scans_the_tempdir() -> None:
    from tests import _engine_substrate

    src = Path(_engine_substrate.__file__).read_text()
    assert "Path(tempfile.gettempdir())" in src and 'nexus_t2_substrate_pg_*' in src


def test_the_checkout_does_not_persist_the_job_token() -> None:
    co = next(s for s in _doc()["jobs"]["test-qwen"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert co["with"]["persist-credentials"] is False


def test_the_suite_logs_skip_reasons_and_the_floor_carries_its_reconciliation_todo() -> None:
    job = _doc()["jobs"]["test-qwen"]
    suite = next(str(s["run"]) for s in job["steps"] if "pytest tests/" in str(s.get("run", "")))
    assert re.search(r"pytest tests/ .*-rs\b", suite), "-rs puts skip reasons in the log for the hosted-run diff"
    floor = next(s for s in job["steps"] if "check_lint_leg_non_vacuity.py" in str(s.get("run", "")))
    assert "--floor 20000" in floor["run"], "the floor stays loose until the reconciliation has been done"
    assert "TODO(qwen-floor)" in WORKFLOW.read_text()
    contributing = (Path(__file__).parent.parent / "docs" / "contributing.md").read_text()
    assert "First run of the qwen-linux route" in contributing
    for item in ("skip reasons", "minus 2%", "second warm run", "cancel", "overlap", "QWEN_CI_PUSH_RUNNER"):
        assert item in contributing, item


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
