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

import contextlib
import itertools
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
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
    """Evaluate the OWNER_PUSH expression the way GitHub does, rendered as GitHub renders a boolean into env."""
    py = _owner_push_expression().replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"'[^']*'", lambda m: f"_GhStr({m.group(0)})", py)  # literals compare case-insensitively
    contexts = {
        "github.event_name": event,
        "github.actor_id": actor_id,
        "github.repository_owner_id": OWNER_ID,
        "github.triggering_actor": triggering_actor,
        "github.repository_owner": OWNER,
    }
    # longest first so `github.repository_owner_id` is not eaten by `github.repository_owner`
    for name in sorted(contexts, key=len, reverse=True):
        py = py.replace(name, f"_GhStr({contexts[name]!r})")
    assert "github." not in py and "vars." not in py, f"unevaluated context left in {py!r}"
    return "true" if eval(py, {"__builtins__": {}, "_GhStr": _GhStr}, {}) is True else "false"  # noqa: S307 - the expression is this repo's own YAML


def _route(*, event: str = "push", actor_id: str = OWNER_ID, triggering_actor: str = OWNER,
           variable: str = "") -> str:
    """The route a push would get: the expression feeds OWNER_PUSH, the step's bash decides, the output is read back.

    The variable reaches the step raw, exactly as `${{ vars.QWEN_CI_PUSH_RUNNER }}` renders it (an unset one as ''),
    and the real `route` step runs under the runner's `bash -eo pipefail`.
    """
    step = _route_step()
    assert step["env"]["QWEN_CI_PUSH_RUNNER"] == "${{ vars.QWEN_CI_PUSH_RUNNER }}", "the variable must travel raw"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "github_output"
        out.write_text("")
        env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), "GITHUB_EVENT_NAME": event,
               "OWNER_PUSH": _owner_push(event=event, actor_id=actor_id, triggering_actor=triggering_actor),
               "QWEN_CI_PUSH_RUNNER": variable}
        proc = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        published = out.read_text()
    assert re.fullmatch(r"ci_runner=(qwen-linux|ubuntu-latest)\n", published), published
    return published.removeprefix("ci_runner=").strip()


# ── the routing: OPT-IN (Sam, 2026-10-01) ─────────────────────────────────


def test_an_owner_push_stays_on_the_hosted_shards_while_the_variable_is_unset() -> None:
    """The default. Landing the route changes nothing until someone sets the variable."""
    assert _route(variable="") == "ubuntu-latest"


def test_only_the_exact_value_qwen_linux_opts_an_owner_push_in() -> None:
    assert _route(variable="qwen-linux") == QWEN_LABEL


@pytest.mark.parametrize("variable", [
    "", "ubuntu-latest", "qwen-linux ", " qwen-linux", "Qwen-Linux", "QWEN-LINUX", "qwen", "qwen-linux\n", "hellmini",
    "hellmini-ci", "self-hosted", "true", "1", "garbage", "qwen-linux,ubuntu-latest", "x" * 40,
])
def test_every_other_variable_value_keeps_the_hosted_shards(variable: str) -> None:
    """Empty, another label, a typo, a stray space, a different case, garbage: none of them opts in."""
    assert _route(variable=variable) == "ubuntu-latest"


def test_the_login_comparison_ignores_case_like_githubs() -> None:
    assert _route(triggering_actor=OWNER.lower(), variable="qwen-linux") == QWEN_LABEL


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
@pytest.mark.parametrize("variable", ["", "qwen-linux", "ubuntu-latest", "hellmini-ci"])
def test_everything_but_an_owner_push_stays_on_the_hosted_shards_even_with_the_variable_on(
        kw: dict, why: str, variable: str) -> None:
    assert _route(variable=variable, **kw) == "ubuntu-latest", why


def test_no_input_can_produce_a_label_other_than_the_two_routes() -> None:
    """A typo or a hostile variable value can never name hellmini, hellmini-ci or any other label."""
    variables = ["", "qwen-linux", "hellmini", "hellmini-ci", "self-hosted", "qwen-linux ", "x" * 40]
    events = ["push", "pull_request", "workflow_dispatch", "schedule"]
    seen = {
        _route(event=e, actor_id=a, triggering_actor=t, variable=v)
        for e, a, t, v in itertools.product(events, [OWNER_ID, "2222"], [OWNER, "x"], variables)
    }
    assert seen == {QWEN_LABEL, "ubuntu-latest"}


def test_the_route_step_cannot_be_flipped_by_anything_but_an_owner_push_plus_the_exact_variable() -> None:
    """Of the whole table, exactly ONE combination reaches qwen-linux."""
    variables = ["", "qwen-linux", "Qwen-Linux", "qwen-linux ", "ubuntu-latest"]
    reached = [
        (e, a, t, v)
        for e, a, t, v in itertools.product(["push", "pull_request"], [OWNER_ID, "2222"], [OWNER, "x"], variables)
        if _route(event=e, actor_id=a, triggering_actor=t, variable=v) == QWEN_LABEL
    ]
    assert reached == [("push", OWNER_ID, OWNER, "qwen-linux")]


def test_the_opt_in_is_decided_in_bash_because_a_github_expression_cannot_compare_case_sensitively() -> None:
    """Pins WHY the variable is not compared in the expression: `==` there ignores case, so `Qwen-Linux` would opt in."""
    step = _route_step()
    assert "QWEN_CI_PUSH_RUNNER" not in step["env"]["OWNER_PUSH"], "the variable must not be compared in the expression"
    assert '"$QWEN_CI_PUSH_RUNNER" = "qwen-linux"' in step["run"]
    assert "vars." not in _owner_push_expression()


@pytest.mark.parametrize(("old", "new", "label"), [
    ('[ "$OWNER_PUSH" = "true" ] &&', '[ "$OWNER_PUSH" = "true" ] ||', "&& turned into || (the variable alone opts in)"),
    ('"$QWEN_CI_PUSH_RUNNER" = "qwen-linux"', '"$QWEN_CI_PUSH_RUNNER" != "qwen-linux"', "variable comparison inverted"),
    ("CI_RUNNER=ubuntu-latest\n", "CI_RUNNER=qwen-linux\n", "default flipped to qwen-linux"),
    ('"$OWNER_PUSH" = "true"', '"$OWNER_PUSH" = "false"', "owner rule inverted"),
])
def test_the_route_table_kills_each_obvious_mutant_of_the_step(old: str, new: str, label: str) -> None:
    """The table must have power: each mutant disagrees with it on at least one row."""
    step = _route_step()
    assert old in step["run"], f"mutation site {old!r} no longer in the step: update this test with the step"
    rows = [(e, v) for e in ("push", "pull_request") for v in ("", "qwen-linux", "Qwen-Linux", "ubuntu-latest")]
    want = {(e, v): ("qwen-linux" if (e, v) == ("push", "qwen-linux") else "ubuntu-latest") for e, v in rows}

    def run(script: str, event: str, variable: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o"
            out.write_text("")
            env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), "GITHUB_EVENT_NAME": event,
                   "OWNER_PUSH": _owner_push(event=event, actor_id=OWNER_ID, triggering_actor=OWNER),
                   "QWEN_CI_PUSH_RUNNER": variable}
            subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True, env=env)
            return out.read_text().removeprefix("ci_runner=").strip()

    assert all(run(step["run"], e, v) == want[(e, v)] for e, v in rows)
    assert any(run(step["run"].replace(old, new), e, v) != want[(e, v)] for e, v in rows), label


def _run_step(step: dict, tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Execute a step's `run` script the way the runner does (bash -eo pipefail), with its own env only."""
    out, genv = tmp_path / "github_output", tmp_path / "github_env"
    out.write_text("")
    genv.write_text("")
    full = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(genv), **env}
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=full)
    return proc, out, genv


def test_the_route_step_publishes_one_of_two_outputs_and_nothing_on_a_shell_error(tmp_path: Path) -> None:
    assert "set -euo pipefail" in _route_step()["run"]
    # an unset OWNER_PUSH is a defect in the wiring: the step must not guess a route
    proc, out, _ = _run_step(_route_step(), tmp_path, GITHUB_EVENT_NAME="push", QWEN_CI_PUSH_RUNNER="qwen-linux")
    assert proc.returncode != 0 and out.read_text() == ""


# ── the qwen job ──────────────────────────────────────────────────────────


def test_the_qwen_job_runs_on_the_custom_label_only_that_only_the_qwen_runner_carries() -> None:
    """Sam, 2026-10-02: the runner is re-registered with --no-default-labels, so no generic label may be named."""
    job = _doc()["jobs"]["test-qwen"]
    runs_on = job["runs-on"]
    assert runs_on == QWEN_LABEL, runs_on
    assert "${{" not in str(runs_on)
    assert runs_on not in {"hellmini", "hellmini-ci", "gtr-windows", "ubuntu-latest", "self-hosted"}


def test_the_qwen_job_is_gated_on_the_route_and_needs_only_changes() -> None:
    job = _doc()["jobs"]["test-qwen"]
    assert job["needs"] == ["changes"]
    assert job["if"].strip() == (
        "success() && needs.changes.outputs.code == 'true' && needs.changes.outputs.ci_runner == 'qwen-linux'")
    assert job["timeout-minutes"] >= 30


def test_the_qwen_job_name_survives_the_board_adapter_unmangled() -> None:
    """The CI board adapter replaces odd characters in job names with '?' (seen: a comma, `$`, braces).

    The name must stay in the plain set the board keeps.
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
    # the jar build and the suite share ONE step (the box lock cannot span steps, see the box-lock tests below)
    assert jar == suite < floor, "build the jar and run the suite in one locked step, then the executed-count floor"
    suite_run = runs[suite][1]
    assert "-n 8" in suite_run and "-n 12" not in suite_run and "--splits" not in suite_run
    assert "set -euo pipefail" in suite_run and "| tee suite-output.txt" in suite_run
    assert "suite-output.txt" in runs[floor][1] and re.search(r"--floor\s+\d{5}", runs[floor][1])


def test_the_guard_steps_run_before_the_checkout_and_in_a_fixed_order() -> None:
    names = [str(s.get("name", s.get("uses", ""))) for s in _doc()["jobs"]["test-qwen"]["steps"]]
    order = [next(i for i, n in enumerate(names) if n.startswith(p)) for p in (
        "Windows-side preflight", "Self-hosted runner consistency check", "Shared suite lease directory",
        "Toolchain preflight", "actions/checkout")]
    assert order == sorted(order) and order[0] == 0, names


_IDENTITY_READS = (r"\bwhoami\b", r"\blogname\b", r"\bgetent\b", r"\$\{?USER\}?", r"\$\{?LOGNAME\}?",
                   r"\bid\s+-\w*[nG]\w*", r"\bgroups\b", r"\bstat\s+-c", r"\bls\s+-\w*l\b")


def test_the_qwen_job_prints_no_account_name_group_list_mode_or_owner() -> None:
    """Public log, public repo: pass or fail and counts only (the round-3 review's L2, extended to every step)."""
    for step in _doc()["jobs"]["test-qwen"]["steps"]:
        run = str(step.get("run", ""))
        for pattern in _IDENTITY_READS:
            assert not re.search(pattern, run), (step.get("name"), pattern)


def test_the_lease_step_failure_message_names_no_user_mode_owner_or_group(tmp_path: Path) -> None:
    """Hermetic: the account name is INJECTED (a stub `id`/`whoami`/`logname` on PATH plus USER/LOGNAME).

    The step's fixed wording says "the runner user"; asserting on the host's real account name breaks when that
    account is itself named `runner` (GitHub's hosted runner), so the name here is one no fixed text can contain.
    """
    acct = "zz-probe-acct-7"
    bin_dir = tmp_path / "idbin"
    bin_dir.mkdir()
    for tool in ("id", "whoami", "logname"):
        stub = bin_dir / tool
        stub.write_text(f"#!/bin/sh\necho {acct}\n")
        stub.chmod(0o755)
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o555)
    try:
        proc, _ = _lease(tmp_path, root, PATH=f"{bin_dir}:/usr/bin:/bin", USER=acct, LOGNAME=acct)
    finally:
        os.chmod(root, 0o755)
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere, so the refusal cannot be observed")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "cannot create entries" in out
    assert acct not in out, "the account name must not be printed"
    assert "mode " not in out and "owner" not in out and "groups of" not in out


def test_the_lease_step_success_line_prints_no_mode_or_owner(tmp_path: Path) -> None:
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o2775)
    proc, _ = _lease(tmp_path, root)
    assert proc.returncode == 0 and "suite lease root:" in proc.stdout
    assert "mode " not in proc.stdout and "2775" not in proc.stdout


# The Windows-side preflight: FIRST step, before the checkout, fail closed, executed against a fake tree.

_WSL_VERSION = "Linux version 6.6.87.2-microsoft-standard-WSL2 (root@build) (gcc 11.2.0)\n"
_PRE_FIX = "/etc/wsl.conf"


def _windows_preflight(tmp_path: Path, *, version: str | None = _WSL_VERSION, binfmt_status: bool = True,
                       handlers: tuple[str, ...] = (), cmd_exe: str | None = None,
                       drives: dict[str, list[str]] | None = None, mounts: str = "",
                       path: str = "/usr/bin:/bin") -> subprocess.CompletedProcess[str]:
    """Run the REAL first step of test-qwen with its four roots pointed at a fake tree."""
    run = _qwen_step("Windows-side preflight")["run"]
    roots = {"proc_version": tmp_path / "proc_version", "binfmt_dir": tmp_path / "binfmt",
             "mounts_file": tmp_path / "mounts", "mnt_root": tmp_path / "mnt"}
    for name, target in roots.items():
        line = {"proc_version": "proc_version=/proc/version\n", "binfmt_dir": "binfmt_dir=/proc/sys/fs/binfmt_misc\n",
                "mounts_file": "mounts_file=/proc/mounts\n", "mnt_root": "mnt_root=/mnt\n"}[name]
        assert line in run, f"{line!r} must stay a plain assignment so the tests can repoint it"
        run = run.replace(line, f"{name}={target}\n")
    if version is not None:
        roots["proc_version"].write_text(version)
    roots["binfmt_dir"].mkdir(exist_ok=True)
    if binfmt_status:
        (roots["binfmt_dir"] / "status").write_text("enabled\n")
    for h in handlers:
        (roots["binfmt_dir"] / h).write_text("enabled\ninterpreter /init\n")
    roots["mounts_file"].write_text(mounts)
    roots["mnt_root"].mkdir(exist_ok=True)
    if cmd_exe is not None:
        exe = roots["mnt_root"] / cmd_exe / "Windows" / "System32" / "cmd.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("MZ")  # not executable: the check is presence, it never starts Windows code
    for letter, entries in (drives or {}).items():
        (roots["mnt_root"] / letter).mkdir(exist_ok=True)
        for e in entries:
            (roots["mnt_root"] / letter / e).write_text("x")
    return subprocess.run(["bash", "-eo", "pipefail", "-c", run], capture_output=True, text=True,
                          env={"PATH": path})


def test_the_windows_preflight_is_the_first_step_and_runs_before_any_repo_code() -> None:
    steps = _doc()["jobs"]["test-qwen"]["steps"]
    assert str(steps[0]["name"]).startswith("Windows-side preflight")
    assert steps[0].get("shell") == "bash" and "if" not in steps[0], "no condition may skip it"
    assert "uses" not in steps[0], "the first step is a script, not an action (no checkout has happened)"


def test_a_wsl_host_with_the_fix_applied_passes_and_prints_counts_only(tmp_path: Path) -> None:
    proc = _windows_preflight(tmp_path, drives={"c": []})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "WSLInterop handlers=0 cmd.exe=0 non-empty-drives=0 drive-mounts=0 path-entries=0" in proc.stdout
    assert "::error::" not in proc.stdout


@pytest.mark.parametrize("version", ["Linux version 6.8.0-generic (buildd@lcy02) (gcc 13.2.0)\n", "", "Darwin\n"])
def test_a_kernel_that_is_not_wsl_fails_closed_instead_of_passing_vacuously(tmp_path: Path, version: str) -> None:
    """POSITIVE CONTROL: every check is trivially true off WSL, so being off WSL must be a failure."""
    proc = _windows_preflight(tmp_path, version=version)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "not a WSL kernel" in proc.stdout and "WSLInterop handlers=" not in proc.stdout


def test_a_missing_proc_version_fails_closed(tmp_path: Path) -> None:
    proc = _windows_preflight(tmp_path, version=None)
    assert proc.returncode == 1 and "not a WSL kernel" in proc.stdout


def test_an_unmounted_binfmt_misc_fails_closed_because_a_handler_would_be_invisible(tmp_path: Path) -> None:
    proc = _windows_preflight(tmp_path, binfmt_status=False)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "binfmt_misc is not mounted" in proc.stdout


@pytest.mark.parametrize("handler", ["WSLInterop", "WSLInterop-late"])
def test_a_wslinterop_handler_fails_the_preflight(tmp_path: Path, handler: str) -> None:
    proc = _windows_preflight(tmp_path, handlers=(handler,))
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "WSLInterop handlers=1" in proc.stdout and _PRE_FIX in proc.stdout and "[interop] enabled=false" in proc.stdout


def test_a_windows_cmd_exe_reachable_under_any_mnt_entry_fails_the_preflight_without_naming_it(tmp_path: Path) -> None:
    proc = _windows_preflight(tmp_path, cmd_exe="a-person-name")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "cmd.exe=1" in proc.stdout and "a-person-name" not in proc.stdout + proc.stderr


def test_a_non_empty_mnt_drive_fails_and_an_empty_one_does_not(tmp_path: Path) -> None:
    ok = _windows_preflight(tmp_path, drives={"c": []})
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    (tmp_path / "bad").mkdir()
    bad = _windows_preflight(tmp_path / "bad", drives={"d": ["Users", "id_ed25519"]})
    assert bad.returncode == 1, (bad.stdout, bad.stderr)
    assert "non-empty-drives=1" in bad.stdout and "id_ed25519" not in bad.stdout + bad.stderr


def test_a_non_letter_mnt_directory_is_not_a_drive_for_the_preflight(tmp_path: Path) -> None:
    proc = _windows_preflight(tmp_path, drives={"wsl": ["x"], "cc": ["y"]})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_a_drvfs_or_9p_mount_at_a_drive_letter_fails_even_when_it_lists_empty(tmp_path: Path) -> None:
    for fs in ("drvfs", "9p"):
        sub = tmp_path / fs
        sub.mkdir()
        proc = _windows_preflight(sub, mounts=f"C: {sub / 'mnt' / 'c'} {fs} rw 0 0\n", drives={"c": []})
        assert proc.returncode == 1 and "drive-mounts=1" in proc.stdout, (fs, proc.stdout, proc.stderr)


def test_wsl_helper_mounts_that_are_not_a_drive_letter_do_not_fail_the_preflight(tmp_path: Path) -> None:
    """The host really has a 9p mount at /usr/lib/wsl/drivers; a drive mount is only /mnt/<one letter>."""
    mounts = (f"drivers /usr/lib/wsl/drivers 9p ro 0 0\n"
              f"x {tmp_path / 'mnt' / 'cc'} 9p rw 0 0\n"
              f"y {tmp_path / 'mnt' / 'c'} ext4 rw 0 0\n")
    proc = _windows_preflight(tmp_path, mounts=mounts, drives={"c": []})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


@pytest.mark.parametrize("entry", ["/mnt/c/Windows/System32", "/mnt/wsl", "/mnt"])
def test_any_mnt_entry_on_path_fails_the_preflight_without_printing_it(tmp_path: Path, entry: str) -> None:
    # the step reads its own mnt_root, which these tests repoint, so build the PATH entry from it
    run_root = tmp_path / "mnt"
    proc = _windows_preflight(tmp_path, path=f"/usr/bin:/bin:{run_root}{entry.removeprefix('/mnt')}")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "path-entries=1" in proc.stdout and "System32" not in proc.stdout + proc.stderr


def test_a_path_entry_that_only_resembles_mnt_is_not_flagged(tmp_path: Path) -> None:
    proc = _windows_preflight(tmp_path, path=f"/usr/bin:/bin:{tmp_path}/mntx:{tmp_path}/opt/mnt")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_the_preflight_failure_names_the_host_fix_and_never_a_path(tmp_path: Path) -> None:
    proc = _windows_preflight(tmp_path, handlers=("WSLInterop",), cmd_exe="c", drives={"c": ["Users"]})
    assert proc.returncode == 1
    assert "[automount] enabled=false" in proc.stdout and "appendWindowsPath=false" in proc.stdout
    assert str(tmp_path) not in proc.stdout + proc.stderr


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


@pytest.mark.parametrize("value", ["1", "true", "0", " ", "off"])
def test_a_job_whose_environment_carries_the_suite_lease_opt_out_is_refused_before_anything_is_exported(
        tmp_path: Path, value: str) -> None:
    """Round-4 review L5: a host-side runner .env could set it, and no lint of the repo can see that.

    ANY non-empty value refuses (the opt-out itself means exactly "1", but a typo here is still a sign the
    host sets it), and only the fact is printed, never the value.
    """
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o2775)
    proc, genv = _lease(tmp_path, root, NX_SUITE_LEASE_UNGUARDED=value)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "opt-out is set" in out and "::error::" in out
    assert genv.read_text() == "", "a refused job must not export a lease root"
    assert f"={value}" not in out and "NX_SUITE_LEASE_UNGUARDED" not in out, "the log names the fact, not the variable's value"


def test_an_empty_suite_lease_opt_out_in_the_environment_is_not_a_refusal(tmp_path: Path) -> None:
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o2775)
    proc, genv = _lease(tmp_path, root, NX_SUITE_LEASE_UNGUARDED="")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert genv.read_text().splitlines() == [f"NX_BUILD_LEASE_ROOT={root}", "NX_SUITE_LEASE_WAIT=1"]


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


_ROOT = Path(__file__).parent.parent


def test_the_docs_describe_an_opt_in_route_and_carry_no_stale_default_on_claim() -> None:
    """Sam, 2026-10-01: unset means the hosted shards. The earlier text said the opposite in five places."""
    agents = (_ROOT / "AGENTS.md").read_text()
    contributing = (_ROOT / "docs" / "contributing.md").read_text()
    ci = WORKFLOW.read_text()
    for name, text in (("AGENTS.md", agents), ("docs/contributing.md", contributing), ("ci.yml", ci)):
        for stale in ("unset means `qwen-linux`", "Unset `QWEN_CI_PUSH_RUNNER` means `qwen-linux`", "ON by default",
                      "route is on by default", "must be `ubuntu-latest`", "QWEN_CI_PUSH_RUNNER=ubuntu-latest sends",
                      "delete the variable to return to qwen-linux"):
            assert stale not in text, (name, stale)
    assert "OPT-IN" in agents and "opt-in" in contributing.lower()
    assert "exactly `qwen-linux`" in agents and "exactly\n`qwen-linux`" in contributing


def test_the_docs_give_the_enable_sequence_in_order_and_the_rerun_caveat() -> None:
    contributing = (_ROOT / "docs" / "contributing.md").read_text()
    agents = (_ROOT / "AGENTS.md").read_text()
    order = [contributing.index(m) for m in (
        "**Land the change.**", "**Host steps, once.**", "**Run the probe as `ghci` and read it green.**",
        "**Record the green run id**", "**Set the variable:**", "**Push once and walk the checklist below.**")]
    assert order == sorted(order)
    for text in (contributing, agents):
        assert "gh variable set QWEN_CI_PUSH_RUNNER --body qwen-linux" in text
        assert "Re-run failed jobs" in text
    assert "runner-probe/qwen-<date>" in contributing and "default branch" in contributing
    # the record carries a numeric run id and the NOT-CHECKED caveat (a run that never read the credentials is no pass)
    record = re.search(r"\*\*Probe run record: run (\d{8,}), green", agents)
    assert record, "the green probe run id is recorded here"
    flat = " ".join(agents.split())
    assert record.group(1) == "36980355165", "the current record is the run of the current probe that logged the closed-home pass"
    # run 36956876942 was recorded as CHECKED but logged NOT CHECKED: the correction stays in the record
    assert "Correction to the earlier record" in flat and "36956876942" in flat and "was not" in flat
    # the rule: two passing states, one failing state, and every other NOT CHECKED is still not a pass
    assert "passes in two states and fails in one" in flat
    assert "not traversable by the runner user" in flat and "any file under the config directory is readable" in flat
    assert "A `NOT CHECKED` for any other cause" in flat and "is not a pass" in flat
    assert "`NOT CHECKED` on the credential line is not a pass" not in flat, "the unqualified rule is the one this replaced"
    assert "EVERY owner push to develop whose diff is not doc-only" in agents, "the scope claim is not 'merges touching ci.yml'"


def test_the_docs_say_docker_is_root_and_the_probe_does_not_bound_it() -> None:
    agents = (_ROOT / "AGENTS.md").read_text()
    assert "root on the distro" in agents and "rewrite `/etc/wsl.conf`" in agents
    assert "does not bound docker" in agents and "not a bound on docker" in agents
    assert "per-run Windows-side preflight" in agents


def test_the_suite_lease_refusal_text_lives_in_the_general_lease_section() -> None:
    agents = (_ROOT / "AGENTS.md").read_text()
    build = agents.index("The Python suite reads the same lease at session start (nexus-pv93h)")
    suite = agents.index("**It fails closed on contention**")
    assert 0 < suite - build < 2500, "the suite-lease text must sit beside the build lease's exit-75 text"
    section = agents[suite:suite + 1800]
    assert "unwritable lease root still runs UNGUARDED" in section
    assert 'exactly `1`' in section and "test_suite_lease_unguarded_lint.py" in section


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


# ── the box lock: ONE flock across the jar build and the suite (host owner, 2026-10-01) ─────────────
#
# The WSL VM wedged twice when a suite overlapped a Maven gate-jar build, which holds a different lease.
# The host convention is `flock <lease root>/box.lock bash -c '<jar build> && <pytest -n 8>'`; the job
# does the same in ONE step, because a lock cannot span steps.

_BOX_STEP = "Build the stamped service jar and run the full suite"
_HEAVY = re.compile(r"build-gate-jar\.sh|\bpytest\b|\bmvnw?\b|mvnw-leased")
_FLOCK_CALL = re.compile(
    r"""^\s*flock\s+(?P<opts>.*?)\s+"\$lock"\s+bash -c '(?P<inner>[^']*)'\s+8>&-\s*&\s*$""", re.MULTILINE)


def _code(run: str) -> str:
    """The commands of a step: comment lines and echoed messages (which may NAME a script) are dropped."""
    return "\n".join(ln for ln in str(run).splitlines() if not ln.strip().startswith(("#", "echo ")))


def _box_run() -> str:
    return str(_qwen_step(_BOX_STEP)["run"])


def _flock_call(run: str) -> re.Match[str]:
    found = list(_FLOCK_CALL.finditer(run))
    assert len(found) == 1, "the step must take the box lock with exactly one flock invocation"
    return found[0]


def test_the_box_lock_is_one_bounded_flock_on_box_lock_under_the_lease_root() -> None:
    run, job = _box_run(), _doc()["jobs"]["test-qwen"]
    assert 'lock="$QWEN_SUITE_LEASE_ROOT/box.lock"' in run, "derived from the same root the suite lease uses"
    opts = _flock_call(run).group("opts")
    assert re.search(r'(^|\s)-w\s+"\$wait_s"', opts), f"a bounded wait (flock -w), got {opts!r}"
    assert 'wait_s="$QWEN_BOX_LOCK_WAIT_SECONDS"' in run
    wait = int(job["env"]["QWEN_BOX_LOCK_WAIT_SECONDS"])
    # the loud failure must come before GitHub's silent timeout, with room left for a cold run
    assert 0 < wait and wait + 20 * 60 <= job["timeout-minutes"] * 60, (wait, job["timeout-minutes"])


def test_the_jar_build_and_the_suite_run_inside_that_one_lock_in_that_order_at_n8() -> None:
    run = _box_run()
    m = _flock_call(run)
    inner = m.group("inner")
    assert inner.index("scripts/build-gate-jar.sh") < inner.index("uv run pytest tests/")
    assert "set -euo pipefail" in inner and "| tee suite-output.txt" in inner
    assert "-n 8" in inner and "-n 12" not in inner
    outside = _code(run[:m.start("inner")] + run[m.end("inner"):])
    assert not _HEAVY.search(outside), "nothing heavy may sit in the step outside the locked bash -c"


def test_no_heavy_step_runs_outside_the_locked_step() -> None:
    steps = _doc()["jobs"]["test-qwen"]["steps"]
    box = _qwen_step(_BOX_STEP)
    for step in steps:
        if step.get("name") == box["name"]:
            continue
        assert not _HEAVY.search(_code(step.get("run", ""))), (step.get("name"), "heavy work outside the box lock")
    assert sum(1 for s in steps if "scripts/build-gate-jar.sh" in _code(s.get("run", ""))) == 1


def test_no_qwen_step_pins_twelve_workers() -> None:
    for step in _doc()["jobs"]["test-qwen"]["steps"]:
        assert not re.search(r"-n\s*12\b", str(step.get("run", ""))), step.get("name")
        assert "-n 12" not in str(step.get("name", "")), step.get("name")


def test_the_toolchain_preflight_requires_flock() -> None:
    run = str(_qwen_step("Toolchain preflight")["run"])
    assert re.search(r"for tool in [^;]*\bflock\b", run)


def _box_env(tmp_path: Path, *, flock: str | None, uv_rc: int = 0, jar_rc: int = 0) -> tuple[Path, dict[str, str]]:
    """A work dir with a fake jar script and a PATH holding only bash, tee, a fake uv and (optionally) a flock."""
    work, bin_dir = tmp_path / "work", tmp_path / "bin"
    (work / "scripts").mkdir(parents=True)
    bin_dir.mkdir()
    bash = shutil.which("bash")
    assert bash
    for tool in ("bash", "tee", "rm", "sleep"):
        real = shutil.which(tool)
        assert real
        (bin_dir / tool).symlink_to(real)
    # Records which CI-priority markers sit in the lease root when a stub runs (a no-op unless FAKE_MARKERS names a
    # log). Bash builtins only: this PATH has no ls or grep.
    logmarkers = bin_dir / "logmarkers"
    logmarkers.write_text(f'''#!{bash}
[ -n "${{FAKE_MARKERS:-}}" ] || exit 0
m=""
for f in "$QWEN_SUITE_LEASE_ROOT"/ci-waiting.*; do [ -e "$f" ] && m="$m ${{f##*/}}"; done
echo "$1:$m" >> "$FAKE_MARKERS"
''')
    logmarkers.chmod(0o755)
    log = tmp_path / "calls.log"
    jar = work / "scripts" / "build-gate-jar.sh"
    # fd 8 is the CI step's marker lock; it must not reach the jar build or the suite (nexus-0wp30 round 2).
    # `{ : >&8; }` succeeds only when fd 8 is open, in any bash.
    jar.write_text(f'#!{bash}\necho jar >> "$FAKE_LOG"\nlogmarkers jar\n'
                   f'if {{ : >&8; }} 2>/dev/null; then echo open >> "$FAKE_FD8"; else echo closed >> "$FAKE_FD8"; fi\n'
                   f'exit {jar_rc}\n')
    uv = bin_dir / "uv"
    uv.write_text(f'#!{bash}\necho "uv $*" >> "$FAKE_LOG"\necho "1 passed"\nexit {uv_rc}\n')
    jar.chmod(0o755)
    uv.chmod(0o755)
    if flock == "fake":
        # mirrors util-linux: `flock -n LOCK CMD` probes, `flock -w N -E C LOCK CMD` waits and exits C on timeout
        fake = bin_dir / "flock"
        fake.write_text(f'''#!{bash}
echo "flock $*" >> "$FAKE_LOG"
if [ "$1" = "-n" ]; then [ -z "${{FAKE_HELD:-}}" ]; exit; fi
if [ "$1" = "-w" ] && [ "$3" = "-x" ]; then exit "${{FAKE_MARKER_LOCK_RC:-0}}"; fi   # the marker's own kernel lock
logmarkers flock
if [ -n "${{FAKE_BLOCK:-}}" ]; then exec sleep 60; fi
ecode="$4"; shift 5
if [ -n "${{FAKE_TIMEOUT:-}}" ]; then exit "$ecode"; fi
exec "$@"
''')
        fake.chmod(0o755)
    elif flock == "real":
        real = shutil.which("flock")
        assert real
        (bin_dir / "flock").symlink_to(real)
    env = {"PATH": str(bin_dir), "FAKE_LOG": str(log), "FAKE_FD8": str(tmp_path / "fd8.log"),
           "QWEN_SUITE_LEASE_ROOT": str(tmp_path / "lease"),
           # the job env derives it from the run id and attempt (see the marker-name test below)
           "CI_WAIT_MARKER": str(tmp_path / "lease" / _MARKER),
           "QWEN_BOX_LOCK_WAIT_SECONDS": _doc()["jobs"]["test-qwen"]["env"]["QWEN_BOX_LOCK_WAIT_SECONDS"]}
    return work, env


def _run_box(work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash
    return subprocess.run([bash, "-eo", "pipefail", "-c", _box_run()], cwd=work, env=env, capture_output=True, text=True)


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def test_a_free_box_lock_runs_the_jar_build_then_the_suite_at_n8_inside_one_flock(tmp_path: Path) -> None:
    work, env = _box_env(tmp_path, flock="fake")
    proc = _run_box(work, env)
    lock = f"{tmp_path / 'lease'}/box.lock"
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert f"box lock {lock} is free" in proc.stdout
    calls = _calls(env)
    wait = env["QWEN_BOX_LOCK_WAIT_SECONDS"]
    assert calls[0] == f"flock -n {lock} true"
    assert calls[1].startswith(f"flock -w {wait} -E 200 {lock} bash -c ")
    assert calls[2:] == ["jar", "uv run pytest tests/ -q -rs -n 8 --durations=25"], calls
    assert (work / "suite-output.txt").read_text() == "1 passed\n"


def test_a_held_box_lock_says_it_is_waiting_and_for_how_long(tmp_path: Path) -> None:
    work, env = _box_env(tmp_path, flock="fake")
    proc = _run_box(work, {**env, "FAKE_HELD": "1"})
    wait = env["QWEN_BOX_LOCK_WAIT_SECONDS"]
    assert proc.returncode == 0
    assert f"box lock {tmp_path / 'lease'}/box.lock is held" in proc.stdout and f"waiting up to {wait}s" in proc.stdout


def test_a_box_lock_wait_that_runs_out_fails_loud_naming_the_lock_and_runs_nothing(tmp_path: Path) -> None:
    work, env = _box_env(tmp_path, flock="fake")
    proc = _run_box(work, {**env, "FAKE_HELD": "1", "FAKE_TIMEOUT": "1"})
    out = proc.stdout + proc.stderr
    wait = env["QWEN_BOX_LOCK_WAIT_SECONDS"]
    assert proc.returncode == 1 and "::error::" in out
    assert f"{tmp_path / 'lease'}/box.lock" in out.split("::error::", 1)[1] and f"within {wait}s" in out
    assert not any(c == "jar" or c.startswith("uv ") for c in _calls(env)), "nothing may run without the lock"


def test_a_missing_flock_fails_closed_before_anything_runs(tmp_path: Path) -> None:
    work, env = _box_env(tmp_path, flock=None)
    proc = _run_box(work, env)
    assert proc.returncode == 1 and "flock is not installed" in proc.stdout
    assert _calls(env) == []


def test_a_red_suite_or_a_failed_jar_build_inside_the_lock_fails_the_step(tmp_path: Path) -> None:
    work, env = _box_env(tmp_path / "suite", flock="fake", uv_rc=3)
    assert _run_box(work, env).returncode == 3, "tee must not mask the suite's status"
    work, env = _box_env(tmp_path / "jar", flock="fake", jar_rc=1)
    proc = _run_box(work, env)
    assert proc.returncode == 1 and not any(c.startswith("uv ") for c in _calls(env)), "no suite after a failed jar build"


@pytest.mark.skipif(shutil.which("flock") is None,
                    reason="util-linux flock is not installed here (macOS); the runner and Linux CI have it")
def test_the_real_flock_times_out_with_exit_200_on_a_lock_another_process_holds(tmp_path: Path) -> None:
    import fcntl

    work, env = _box_env(tmp_path, flock="real")
    lease = tmp_path / "lease"
    lease.mkdir()
    with open(lease / "box.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = _run_box(work, {**env, "QWEN_BOX_LOCK_WAIT_SECONDS": "1"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "::error::" in out and "within 1s" in out and "is held" in out
    assert _calls(env) == [], "the jar build and the suite must not start without the lock"


_POLICY = ("Policy (Sam, 2026-10-02): agents' full suites use hellmini. Item 10 of "
           "`docs/contributing.md` § First run of the qwen-linux route (the overlap check) was run green on "
           "the host on 2026-10-02, so a hand run there is supported only through `scripts/qwen-hand-run.sh`, and only "
           "for a case that needs Linux.")


def test_the_docs_state_n8_and_the_box_lock_hand_run_convention_and_no_stale_n12() -> None:
    # nexus-0wp30: a hand run goes ONLY through the wrapper, which yields to CI; the raw flock form is retired
    # from the docs because it takes the lock with no regard for a queued CI job.
    raw_hand_run = "flock /var/lib/nx-suite-lease/box.lock env NX_BUILD_LEASE_ROOT"
    for name in ("AGENTS.md", "docs/contributing.md"):
        text = (_ROOT / name).read_text()
        flat = " ".join(text.split())
        assert "scripts/qwen-hand-run.sh" in text, name
        assert raw_hand_run not in text, (name, "the unconditional raw flock form must not be documented")
        assert "strict priority" in flat and "ci-waiting" in text, name
        assert "box.lock" in text and "hellmini" in text and "-n 8" in text, name
        assert "agents' full suites" in flat, (name, "the interim policy: agents use hellmini")
        # the hand run PASSES the two variables; nxtest's .bashrc must not export them (the export leaked into tests)
        assert "no longer exports them" in flat or "Do not export them from `nxtest`'s `.bashrc`" in flat, name
        for stale in ("one `-n 12` job", "xdist -n 12", "pytest tests/ -n 12", "under `-n 12`"):
            assert stale not in text, (name, stale)


def test_one_policy_statement_for_who_may_run_what_on_qwentescence_in_both_docs() -> None:
    """Round 2 (critique S4): the policy was worded three ways. One sentence, verbatim, in both files."""
    for name in ("AGENTS.md", "docs/contributing.md"):
        flat = " ".join((_ROOT / name).read_text().split())
        assert _POLICY in flat, (name, "the one policy statement is missing or reworded")


def test_no_doc_blesses_a_hand_run_that_skips_the_box_lock() -> None:
    """Round 2 (critique S3): a pytest-only hand run on the suite lease skips the box lock and the marker AND holds
    the suite lease CI's pytest needs after it takes the box lock, a second starvation channel."""
    for name in ("AGENTS.md", "docs/contributing.md", ".github/workflows/ci.yml"):
        flat = " ".join((_ROOT / name).read_text().split())
        assert "for pytest-only hand runs" not in flat and "serializes a pytest-only hand run" not in flat, name
    for name in ("AGENTS.md", "docs/contributing.md"):
        flat = " ".join((_ROOT / name).read_text().split())
        assert "is not a supported hand-run form" in flat, (name, "say what is NOT supported: a raw flock, a bare pytest")


def test_no_unmeasured_duration_is_stated_as_a_fact() -> None:
    """Round 2 (critique S2): '11 minutes warm, 20 cold' was never measured at -n 8 (T2 howto: not yet timed)."""
    for name in ("AGENTS.md", "docs/contributing.md", ".github/workflows/ci.yml", "scripts/qwen-hand-run.sh"):
        flat = " ".join((_ROOT / name).read_text().split())
        assert not re.search(r"about (11|20) min|11 minutes|20 cold|11 min warm", flat), name


# ── CI priority on the box lock (nexus-0wp30, Sam 2026-10-02) ───────────────────────────────────────
#
# flock does not order its waiters, test-qwen gives up after 1800 s and a hand run takes about 11 minutes, so a
# queue of hand runs starved CI for about 24 minutes. The remedy has two halves that these tests pin together:
#   * test-qwen posts `ci-waiting.<run>.<attempt>` in the lease root BEFORE it waits on the box lock, HOLDS A KERNEL
#     LOCK ON IT for as long as it is queued (liveness is the lock, not a clock: the kernel drops it when the
#     process dies, SIGKILL included), drops the marker the moment it holds the box lock, and on every other way out
#     of the step (success, timeout, failure, signal, an always() cleanup step);
#   * scripts/qwen-hand-run.sh, the only documented hand-run entry, treats a marker as live only while it cannot take
#     a shared lock on it, re-checks after it takes the box lock and yields if CI arrived meanwhile, and caps its own
#     hold time below CI's wait. mtime is the fallback only where the lock cannot be tested.

_WRAPPER = Path(__file__).parent.parent / "scripts" / "qwen-hand-run.sh"
_RUN_ID, _ATTEMPT = "4242", "2"
_MARKER = f"ci-waiting.{_RUN_ID}.{_ATTEMPT}"


def _prio_env(tmp_path: Path, flock: str = "fake", **kw: int) -> tuple[Path, dict[str, str]]:
    """_box_env plus an existing lease directory and a marker log."""
    work, env = _box_env(tmp_path, flock=flock, **kw)
    (tmp_path / "lease").mkdir()
    env.update(FAKE_MARKERS=str(tmp_path / "markers.log"))
    return work, env


def _seen_markers(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_MARKERS"])
    return [" ".join(ln.split()) for ln in log.read_text().splitlines()] if log.exists() else []


def _left_in_lease(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "lease").iterdir())


def test_the_marker_name_is_defined_once_in_the_job_env_from_the_run_and_attempt() -> None:
    """The step and the always() cleanup both read $CI_WAIT_MARKER, so they cannot disagree about the name."""
    env = _doc()["jobs"]["test-qwen"]["env"]
    expr = env["CI_WAIT_MARKER"]
    assert "github.run_id" in expr and "github.run_attempt" in expr and "/ci-waiting." in expr, expr
    assert env["QWEN_SUITE_LEASE_ROOT"].strip() in expr, "the marker lives in the same lease root as the box lock"


def test_the_step_locks_its_marker_before_waiting_closes_it_for_the_subtree_and_waits_on_the_flock_as_a_job() -> None:
    run = _box_run()
    assert 'marker="$CI_WAIT_MARKER"' in run
    assert re.search(r"umask\s+022", run), "the marker must be readable by the other user: its lock is tested read-only"
    assert re.search(r"exec 8>", run) and re.search(r'flock\s+-w\s+\d+\s+-x\s+8\b', run), "a kernel lock on fd 8"
    assert run.index("exec 8>") < run.index('flock -w "$wait_s"'), "locked before the wait"
    assert run.index("umask 022") < run.index("exec 8>"), "created under the explicit umask"
    m = _flock_call(run)  # a background job with the marker fd closed: `... 8>&- &`
    assert re.search(r'\bwait\b\s+"\$', run[m.end():]), "waited on, so a trapped signal is not deferred behind a foreground child"
    assert re.search(r"trap\s+\S+.*\bEXIT\b", run), "an EXIT trap removes the marker on every way out"
    assert re.search(r"trap\s+.*\bTERM\b", run) and re.search(r"trap\s+.*\bINT\b", run), "cancellation reaches the trap"
    inner = m.group("inner")
    assert inner.index('rm -f "$CI_WAIT_MARKER"') < inner.index("scripts/build-gate-jar.sh"), "dropped the moment the lock is held"


def test_the_ci_step_posts_its_marker_before_waiting_and_drops_it_once_it_holds_the_lock(tmp_path: Path) -> None:
    work, env = _prio_env(tmp_path)
    proc = _run_box(work, env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # present when flock is called (CI is QUEUED), gone when the jar build starts (CI is RUNNING)
    assert _seen_markers(env) == [f"flock: {_MARKER}", "jar:"]
    assert _left_in_lease(tmp_path) == []
    assert "flock -w 10 -x 8" in _calls(env), "non-vacuity: the marker's kernel lock was taken (a blocking, short wait)"


def test_the_marker_lock_does_not_reach_the_jar_build_or_the_suite(tmp_path: Path) -> None:
    """fd 8 must be closed in the locked subtree, or an orphan there would keep the marker 'live' after CI left."""
    work, env = _prio_env(tmp_path)
    assert _run_box(work, env).returncode == 0
    assert "flock -w 10 -x 8" in _calls(env), "non-vacuity: fd 8 existed to leak"
    assert Path(env["FAKE_FD8"]).read_text().split() == ["closed"]


def test_a_marker_that_cannot_be_locked_is_removed_and_warned_about_and_the_run_goes_on(tmp_path: Path) -> None:
    """An unlocked marker is dead to the wrapper, so leaving it would only look like priority. Say so instead."""
    work, env = _prio_env(tmp_path)
    proc = _run_box(work, {**env, "FAKE_MARKER_LOCK_RC": "1"})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "::warning::" in proc.stdout and "ci-waiting" in proc.stdout
    assert _seen_markers(env) == ["flock:", "jar:"], "no marker may be left for the wait"


def test_the_ci_marker_is_dropped_when_the_lock_wait_times_out(tmp_path: Path) -> None:
    work, env = _prio_env(tmp_path)
    proc = _run_box(work, {**env, "FAKE_HELD": "1", "FAKE_TIMEOUT": "1"})
    assert proc.returncode == 1 and "::error::" in proc.stdout + proc.stderr
    assert _seen_markers(env) == [f"flock: {_MARKER}"]
    assert _left_in_lease(tmp_path) == [], "a timed-out job must not leave CI looking queued"


@pytest.mark.parametrize("kw,rc", [({"uv_rc": 3}, 3), ({"jar_rc": 1}, 1)])
def test_the_ci_marker_is_dropped_when_the_suite_or_the_jar_build_fails(tmp_path: Path, kw: dict[str, int], rc: int) -> None:
    work, env = _prio_env(tmp_path, **kw)
    assert _run_box(work, env).returncode == rc
    assert _seen_markers(env)[0] == f"flock: {_MARKER}", "non-vacuity: the marker was posted before it was dropped"
    assert _left_in_lease(tmp_path) == []


@contextlib.contextmanager
def _sigint_default() -> Iterator[None]:
    """Spawn children with SIGINT at its default action. A pytest launched from a backgrounded non-interactive shell
    (nohup, `cmd &`, some harnesses) has SIGINT IGNORED, a child inherits that, and bash cannot trap a signal that was
    ignored on entry, so a SIGINT test would read a harness difference as a trap regression (round 2 verify, S1)."""
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_DFL)
    except ValueError:  # not the main thread: nothing to reset
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _start_blocked_step(tmp_path: Path, *, flock: str, umask: int | None = None) -> tuple[subprocess.Popen[str], Path]:
    """The CI step, blocked in its lock wait (fake flock sleeps; or real flock against a lock the test holds)."""
    work, env = _prio_env(tmp_path, flock=flock)
    bash = shutil.which("bash")
    assert bash
    env = {**env, "FAKE_BLOCK": "1"}
    out = open(tmp_path / "step.out", "w")
    # umask is inherited, so set it around the spawn (no preexec_fn: that forces a fork instead of posix_spawn)
    previous = os.umask(umask) if umask is not None else None
    try:
        with _sigint_default():
            proc = subprocess.Popen([bash, "-eo", "pipefail", "-c", _box_run()], cwd=work, env=env, stdout=out, stderr=out,
                                    text=True, start_new_session=True)
    finally:
        if previous is not None:
            os.umask(previous)
    marker = tmp_path / "lease" / _MARKER
    return proc, marker


def _wait_for(pred, what: str, seconds: float = 15.0) -> None:  # type: ignore[no-untyped-def]

    deadline = time.monotonic() + seconds
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pred(), what


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _reap(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=20)


@pytest.mark.parametrize("sig_name,rc", [("SIGTERM", 143), ("SIGINT", 130)])
def test_a_cancelled_ci_step_drops_its_marker_when_only_the_step_shell_is_signalled(tmp_path: Path, sig_name: str, rc: int) -> None:
    """Round 2 (both reviews): a trapped signal is deferred while bash waits on a FOREGROUND child, and the step sat in
    `flock -w 1800` in the foreground. The old test signalled the whole process group, which kills that child too and
    so could not see it. Signal ONLY the shell: the cleanup must run at once, not when the lock wait ends."""

    proc, marker = _start_blocked_step(tmp_path, flock="fake")
    try:
        _wait_for(marker.exists, "the marker must be posted while the step waits for the lock")
        os.kill(proc.pid, getattr(signal, sig_name))  # the shell only, not the group
        assert proc.wait(timeout=10) == rc, "the step must leave at once on a cancel, not when the 60 s wait ends"
        assert not marker.exists(), "a cancelled job must not leave CI looking queued"
    finally:
        _reap(proc)


def test_the_marker_is_readable_by_the_other_user_whatever_the_runner_umask(tmp_path: Path) -> None:
    """nxtest tests the marker's lock read-only, so it needs read permission; ghci's umask is not ours to assume."""
    proc, marker = _start_blocked_step(tmp_path, flock="fake", umask=0o077)
    try:
        _wait_for(marker.exists, "the marker must be posted")
        assert marker.stat().st_mode & 0o044 == 0o044, oct(marker.stat().st_mode)
    finally:
        _reap(proc)


def _marker_is_locked(path: Path) -> bool:
    """True when a shared lock on the file cannot be taken: what the hand-run wrapper's test amounts to."""
    import fcntl

    with open(path) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock is not installed here (macOS)")
def test_the_real_flock_the_marker_is_locked_while_queued_and_unlocked_the_moment_the_step_is_killed(tmp_path: Path) -> None:
    """The kernel lock IS the liveness signal: held while CI is queued, gone when the process dies (SIGKILL
    included, which no trap survives), and not inherited by the flock child that is still waiting."""
    import fcntl

    proc, marker = _start_blocked_step(tmp_path, flock="real")
    lease = tmp_path / "lease"
    try:
        with open(lease / "box.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            _wait_for(lambda: marker.exists() and _marker_is_locked(marker), "the queued step must hold a lock on its marker")
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=20)
            assert marker.exists(), "SIGKILL runs no trap, so the file stays; its LOCK must not"
            assert not _marker_is_locked(marker), "a killed CI step must not look queued: the waiting flock child must not hold it"
    finally:
        _reap(proc)


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock is not installed here (macOS)")
def test_the_real_flock_a_cancelled_queued_step_is_gone_at_once_and_leaves_no_waiter_holding_the_marker(tmp_path: Path) -> None:
    import fcntl

    proc, marker = _start_blocked_step(tmp_path, flock="real")
    try:
        with open(tmp_path / "lease" / "box.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            _wait_for(lambda: marker.exists() and _marker_is_locked(marker), "queued")
            os.kill(proc.pid, signal.SIGTERM)
            assert proc.wait(timeout=10) == 143
            assert not marker.exists()
            _wait_for(lambda: not _group_alive(proc.pid), "the waiting flock child must be killed with the step")
    finally:
        _reap(proc)


def test_an_unwritable_lease_root_warns_loudly_but_does_not_fail_the_step(tmp_path: Path) -> None:
    """Priority is a courtesy to CI; losing it must not cost the run. The lease step already fails a bad root."""
    work, env = _box_env(tmp_path, flock="fake")  # no lease directory at all
    proc = _run_box(work, env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "::warning::" in proc.stdout and "ci-waiting" in proc.stdout


def _always_step() -> dict:
    steps = [s for s in _doc()["jobs"]["test-qwen"]["steps"] if str(s.get("if", "")).strip() == "always()"
             and "CI_WAIT_MARKER" in str(s.get("run", ""))]
    assert len(steps) == 1, "exactly one always() step must drop the CI priority marker"
    return steps[0]


def test_an_always_step_after_the_locked_step_drops_the_marker_even_if_the_step_shell_was_killed(tmp_path: Path) -> None:
    """Round 2: a second cleanup path. The step's own trap cannot run on SIGKILL; this step is the runner's."""
    steps = _doc()["jobs"]["test-qwen"]["steps"]
    names = [s.get("name", "") for s in steps]
    always = _always_step()
    assert names.index(always["name"]) > names.index(_qwen_step(_BOX_STEP)["name"])
    (tmp_path / "lease").mkdir()
    marker = tmp_path / "lease" / _MARKER
    marker.write_text("run=1\n")
    bash = shutil.which("bash")
    assert bash
    proc = subprocess.run([bash, "-eo", "pipefail", "-c", str(always["run"])], env={"PATH": os.environ["PATH"], "CI_WAIT_MARKER": str(marker)},
                          capture_output=True, text=True)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert not marker.exists()
    proc = subprocess.run([bash, "-eo", "pipefail", "-c", str(always["run"])], env={"PATH": os.environ["PATH"], "CI_WAIT_MARKER": str(marker)},
                          capture_output=True, text=True)
    assert proc.returncode == 0, "removing an already-removed marker must not fail the step"


# ── scripts/qwen-hand-run.sh ─────────────────────────────────────────────────────────────────────────


def _hand_env(tmp_path: Path, *, flock: str | None = "fake", timeout: str | None = "fake", uv_rc: int = 0, jar_rc: int = 0,
              lease: bool = True) -> tuple[Path, dict[str, str]]:
    """A work tree holding the real wrapper, stubs for uv and the jar build, and a PATH of only what it may use."""
    work, bin_dir, lease_dir = tmp_path / "work", tmp_path / "bin", tmp_path / "lease"
    (work / "scripts").mkdir(parents=True)
    bin_dir.mkdir()
    for tool in ("bash", "date", "dirname", "rm"):
        real = shutil.which(tool)
        assert real, tool
        (bin_dir / tool).symlink_to(real)
    bash, real_sleep, real_stat = shutil.which("bash"), shutil.which("sleep"), shutil.which("stat")
    assert bash and real_sleep and real_stat
    log = tmp_path / "calls.log"
    shutil.copy(_WRAPPER, work / "scripts" / "qwen-hand-run.sh")
    (work / "scripts" / "qwen-hand-run.sh").chmod(0o755)
    jar = work / "scripts" / "build-gate-jar.sh"
    jar.write_text(f'#!{bash}\necho jar >> "$FAKE_LOG"\nexit {jar_rc}\n')
    jar.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(f'''#!{bash}
echo "uv $*" >> "$FAKE_LOG"
if [ "$1" = "run" ]; then
  echo "env NX_BUILD_LEASE_ROOT=${{NX_BUILD_LEASE_ROOT:-}} NX_SUITE_LEASE_WAIT=${{NX_SUITE_LEASE_WAIT:-}}" >> "$FAKE_LOG"
  if {{ : >&9; }} 2>/dev/null; then echo "fd9=open" >> "$FAKE_LOG"; else echo "fd9=closed" >> "$FAKE_LOG"; fi
  if [ -n "${{FAKE_LOCK_PROBE:-}}" ]; then flock -n "$FAKE_LOCK_PROBE" true; echo "lock-probe rc=$?" >> "$FAKE_LOG"; fi
  if [ -n "${{FAKE_ORPHAN:-}}" ]; then {real_sleep} "$FAKE_ORPHAN" >/dev/null 2>&1 & fi
  if [ -n "${{FAKE_UV_EVAL:-}}" ]; then eval "$FAKE_UV_EVAL"; fi
  if [ -n "${{FAKE_UV_SLEEP:-}}" ]; then sleep "$FAKE_UV_SLEEP"; fi
  exit {uv_rc}
fi
exit 0
''')
    uv.chmod(0o755)
    # a sleep that really sleeps but can drop the CI markers on its Nth call, to model CI taking its turn
    sleep = bin_dir / "sleep"
    sleep.write_text(f'''#!{bash}
echo "sleep $1" >> "$FAKE_LOG"
n=0; [ -f "$FAKE_SLEEP_CNT" ] && read -r n < "$FAKE_SLEEP_CNT"; n=$((n+1)); echo "$n" > "$FAKE_SLEEP_CNT"
if [ "$n" = "${{FAKE_CLEAR_ON_SLEEP:-0}}" ]; then rm -f "$QWEN_SUITE_LEASE_ROOT"/ci-waiting.*; fi
exec {real_sleep} "$1"
''')
    sleep.chmod(0o755)
    # stat that can fail or make the file vanish mid-call (CI removes its marker the moment it takes the lock)
    stat = bin_dir / "stat"
    stat.write_text(f'''#!{bash}
case "${{FAKE_STAT:-}}" in
  vanish) for last; do :; done; rm -f "$last"; exit 1 ;;
  fail) exit 1 ;;
esac
exec {real_stat} "$@"
''')
    stat.chmod(0o755)
    if flock == "fake":
        # `flock -w SLICE -E CODE 9`: succeeds, or exits CODE when held; call N can make CI arrive.
        # `flock -n -s -E 200 7`: the marker probe; it reads the marker through fd 7, "locked" = CI still holds it.
        fake = bin_dir / "flock"
        fake.write_text(f'''#!{bash}
echo "flock $*" >> "$FAKE_LOG"
if [ "$1" = "-n" ]; then
  [ -n "${{FAKE_MARKER_FLOCK_FAIL:-}}" ] && exit 1
  IFS= read -r first <&7
  [ "$first" = locked ] && exit 200
  exit 0
fi
n=0; [ -f "$FAKE_FLOCK_CNT" ] && read -r n < "$FAKE_FLOCK_CNT"; n=$((n+1)); echo "$n" > "$FAKE_FLOCK_CNT"
if [ -n "${{FAKE_HELD:-}}" ]; then exit "$4"; fi
if [ "$n" = "${{FAKE_MARKER_ON_CALL:-0}}" ]; then echo locked > "$QWEN_SUITE_LEASE_ROOT/ci-waiting.9.1"; fi
exit 0
''')
        fake.chmod(0o755)
    elif flock == "real":
        real = shutil.which("flock")
        assert real
        (bin_dir / "flock").symlink_to(real)
    if timeout == "fake":
        # `timeout -k GRACE SECONDS cmd...`: runs cmd, or reports an expiry (124) when FAKE_TIMEOUT_EXPIRE is set
        fake_t = bin_dir / "timeout"
        fake_t.write_text(f'''#!{bash}
echo "timeout $1 $2 $3" >> "$FAKE_LOG"
if [ -n "${{FAKE_TIMEOUT_EXPIRE:-}}" ]; then exit "$FAKE_TIMEOUT_EXPIRE"; fi
shift 3
exec "$@"
''')
        fake_t.chmod(0o755)
    elif timeout == "real":
        real_t = shutil.which("timeout")
        assert real_t
        (bin_dir / "timeout").symlink_to(real_t)
    if lease:
        lease_dir.mkdir()
    env = {"PATH": str(bin_dir), "REAL_SLEEP": real_sleep, "FAKE_LOG": str(log), "FAKE_FLOCK_CNT": str(tmp_path / "flock.cnt"),
           "FAKE_SLEEP_CNT": str(tmp_path / "sleep.cnt"), "QWEN_SUITE_LEASE_ROOT": str(lease_dir),
           "QWEN_HAND_RUN_SLICE_SECONDS": "1", "QWEN_HAND_RUN_BACKOFF_SECONDS": "1", "QWEN_HAND_RUN_KILL_SECONDS": "2",
           "QWEN_HAND_RUN_MAX_WAIT_SECONDS": "3"}
    return work, env


def _post_marker(tmp_path: Path, name: str = "ci-waiting.7.1", age: float = 0.0, live: bool = True) -> Path:
    """A marker file. With the fake flock a marker is LIVE when its first line says `locked` (the stand-in for CI
    holding the kernel lock); the real-flock tests hold a genuine lock instead."""

    p = tmp_path / "lease" / name
    p.write_text("locked\n" if live else "free\n")
    os.utime(p, (time.time() - age, time.time() - age))
    return p


def _hand(work: Path, env: dict[str, str], *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash
    return subprocess.run([bash, str(work / "scripts" / "qwen-hand-run.sh"), *args], cwd=cwd or work, env=env,
                          capture_output=True, text=True, timeout=60)


def _hand_calls(env: dict[str, str]) -> list[str]:
    return [c for c in _calls(env) if not c.startswith("sleep ")]


def _acquires(env: dict[str, str]) -> list[str]:
    """The wrapper's attempts to take the box lock (not the marker probes)."""
    return [c for c in _calls(env) if c.startswith("flock -w")]


def _script_default(name: str) -> int:
    m = re.search(rf"{name}:-(\d+)", _WRAPPER.read_text())
    assert m, name
    return int(m.group(1))


def test_a_hand_run_refuses_to_start_while_ci_is_queued(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    _post_marker(tmp_path)
    proc = _hand(work, env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 76, (proc.returncode, out)
    assert "ci-waiting.7.1" in out and "strict priority" in out and "retry later" in out
    assert "not a test failure" in out, "an agent reading only the exit code must be able to tell this from a red suite"
    assert _acquires(env) == [] and "uv sync -q" not in _calls(env), "a refused hand run takes no lock and builds nothing"


def test_a_marker_nobody_holds_locked_is_dead_whatever_its_age_and_is_not_removed_while_young(tmp_path: Path) -> None:
    """Liveness is the kernel lock. A fresh file nobody locks is the corpse of a killed job (or the instant between
    its create and its lock, hence the grace before anyone deletes it)."""
    work, env = _hand_env(tmp_path)
    marker = _post_marker(tmp_path, live=False)  # mtime: now
    proc = _hand(work, env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert marker.exists(), "within the grace window an unlocked marker may be one CI is about to lock: leave it"


def test_a_dead_marker_past_the_grace_window_is_removed_by_the_wrapper(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    marker = _post_marker(tmp_path, live=False, age=600)
    proc = _hand(work, env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert not marker.exists(), "SIGKILL leaves the file; the next hand run clears it"


def test_a_live_marker_is_never_removed_however_old(tmp_path: Path) -> None:
    """CI waits at most 1800 s, but the clock is not the signal: while the lock is held CI is queued."""
    work, env = _hand_env(tmp_path)
    marker = _post_marker(tmp_path, age=5000)
    assert _hand(work, env).returncode == 76
    assert marker.exists()


@pytest.mark.parametrize("delta,rc", [(-2, 76), (1, 0)])
def test_where_the_lock_cannot_be_tested_the_mtime_bound_is_the_fallback_and_is_pinned_tight(tmp_path: Path, delta: int, rc: int) -> None:
    """Secondary signal only. The bound is read from the script, and the pins sit within 2 s of it (the old pin
    ages 2000 and 2160 admitted any bound in between)."""
    stale = _script_default("QWEN_CI_MARKER_STALE_SECONDS")
    assert int(_doc()["jobs"]["test-qwen"]["env"]["QWEN_BOX_LOCK_WAIT_SECONDS"]) < stale
    work, env = _hand_env(tmp_path)
    _post_marker(tmp_path, age=stale + delta)
    assert _hand(work, {**env, "FAKE_MARKER_FLOCK_FAIL": "1"}).returncode == rc


def test_a_marker_that_vanishes_during_the_age_check_is_not_read_as_fresh(tmp_path: Path) -> None:
    """Round 2 (code S1): CI removes its marker the instant it takes the lock, which is when this check matters."""
    work, env = _hand_env(tmp_path)
    marker = _post_marker(tmp_path)
    proc = _hand(work, {**env, "FAKE_MARKER_FLOCK_FAIL": "1", "FAKE_STAT": "vanish"})
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert not marker.exists()


def test_a_marker_whose_age_cannot_be_read_does_not_count_as_fresh_forever(tmp_path: Path) -> None:
    """Round 2 (critique O4): lock untestable AND mtime unreadable: ignore it rather than wait out the whole bound."""
    work, env = _hand_env(tmp_path)
    _post_marker(tmp_path)
    proc = _hand(work, {**env, "FAKE_MARKER_FLOCK_FAIL": "1", "FAKE_STAT": "fail"})
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_marker_the_wrapper_cannot_open_falls_back_to_its_mtime(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    marker = _post_marker(tmp_path)
    marker.chmod(0)
    try:
        assert _hand(work, env).returncode == 76, "unreadable but young: err toward CI"
    finally:
        marker.chmod(0o644)


def test_a_relative_lease_root_still_finds_the_marker_after_the_wrapper_changes_directory(tmp_path: Path) -> None:
    """Round 2 (critique O4): the wrapper cd's to the checkout, so a relative root silently pointed somewhere else."""
    work, env = _hand_env(tmp_path)
    _post_marker(tmp_path)
    proc = _hand(work, {**env, "QWEN_SUITE_LEASE_ROOT": "lease"}, cwd=tmp_path)
    assert proc.returncode == 76, (proc.returncode, proc.stdout, proc.stderr)


def test_a_marker_name_with_control_characters_is_not_echoed_raw(tmp_path: Path) -> None:
    """Round 2 (code S3): the lease root is writable by ghci; its file names reach nxtest's terminal."""
    work, env = _hand_env(tmp_path)
    _post_marker(tmp_path, "ci-waiting.\x1b[31mred")
    proc = _hand(work, env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 76 and "ci-waiting." in out
    assert "\x1b" not in out


def test_a_hand_run_with_no_marker_runs_the_documented_command_under_the_lease_variables_and_a_hold_cap(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    proc = _hand(work, env, "tests/test_x.py", "-k", "foo")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    lease = env["QWEN_SUITE_LEASE_ROOT"]
    assert _hand_calls(env) == [
        "flock -w 1 -E 200 9", "timeout -k 2 1500", "uv sync -q", "jar", "uv run pytest -n 8 -q tests/test_x.py -k foo",
        f"env NX_BUILD_LEASE_ROOT={lease} NX_SUITE_LEASE_WAIT=1", "fd9=closed"]


def test_the_suite_does_not_inherit_the_box_lock_fd_so_a_leaked_orphan_cannot_hold_it(tmp_path: Path) -> None:
    """Round 2 (code S2, critique O5): decided. The wrapper holds fd 9 for its own lifetime, which covers the run; the
    suite gets it closed, because a daemonized Postgres or JVM that outlives the run would otherwise hold the box lock
    until it dies, and CI (which has priority) would fail its wait naming a hand run that ended long ago."""
    work, env = _hand_env(tmp_path)
    assert _hand(work, env).returncode == 0
    assert "fd9=closed" in _calls(env)


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock is not installed here (macOS)")
def test_the_real_flock_an_orphan_the_suite_leaves_behind_does_not_keep_the_box_lock(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path, flock="real")
    proc = _hand(work, {**env, "FAKE_ORPHAN": "5"})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    after = subprocess.run([shutil.which("flock") or "flock", "-n", str(tmp_path / "lease" / "box.lock"), "true"])
    assert after.returncode == 0, "the orphan is still alive, and must not be holding the lock"


def test_the_hold_cap_defaults_to_less_than_ci_waits_even_with_the_kill_grace() -> None:
    hold, wait = _script_default("QWEN_HAND_RUN_HOLD_SECONDS"), int(_doc()["jobs"]["test-qwen"]["env"]["QWEN_BOX_LOCK_WAIT_SECONDS"])
    grace = _script_default("QWEN_HAND_RUN_KILL_SECONDS")
    # timeout's own -k grace, then the wrapper's group sweep waits the same grace again before its SIGKILL
    assert 0 < hold and hold + 2 * grace < wait, (hold, grace, wait)


def test_a_hand_run_that_hits_its_hold_cap_exits_with_its_own_code_and_says_so(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    proc = _hand(work, {**env, "FAKE_TIMEOUT_EXPIRE": "124", "QWEN_HAND_RUN_HOLD_SECONDS": "77"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 77, (proc.returncode, out)
    assert "77s" in out and "QWEN_HAND_RUN_HOLD_SECONDS" in out and "CI" in out
    assert "timeout -k 2 77" in _calls(env)


@pytest.mark.skipif(shutil.which("flock") is None or shutil.which("timeout") is None,
                    reason="needs util-linux flock and GNU timeout (not on macOS by default)")
def test_the_real_timeout_stops_a_suite_that_outlives_the_cap_and_releases_the_lock(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path, flock="real", timeout="real")
    t0 = time.monotonic()
    proc = _hand(work, {**env, "FAKE_UV_SLEEP": "40", "QWEN_HAND_RUN_HOLD_SECONDS": "2"})
    assert proc.returncode == 77, (proc.returncode, proc.stdout, proc.stderr)
    assert time.monotonic() - t0 < 30, "the cap must cut the run, not wait for the suite"
    after = subprocess.run([shutil.which("flock") or "flock", "-n", str(tmp_path / "lease" / "box.lock"), "true"])
    assert after.returncode == 0


def _pid_alive(pid: int) -> bool:
    """Alive means running: a zombie nobody has reaped yet (a container whose PID 1 does not reap) is already dead."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _box_lock_is_free(lock: Path) -> bool:
    return subprocess.run([shutil.which("flock") or "flock", "-n", str(lock), "true"]).returncode == 0


def _spawn_hand(tmp_path: Path, work: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    """The wrapper as a user's shell would run it: its own session, so a signal sent to it is the ONLY one it gets."""
    bash = shutil.which("bash")
    assert bash
    with _sigint_default(), open(tmp_path / "hand.out", "w") as out:
        return subprocess.Popen([bash, str(work / "scripts" / "qwen-hand-run.sh")], cwd=work, env=env, stdout=out,
                                stderr=out, text=True, start_new_session=True)


def _pids_from(path: Path, count: int) -> list[int]:
    _wait_for(lambda: path.exists() and len(path.read_text().split()) >= count, f"the fake suite must record {count} pids")
    return [int(x) for x in path.read_text().split()]


def _kill_all(pids: list[int]) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


# the fake suite records its own pid and a child's, then (see each test) waits; a trapped signal interrupts `wait`
_SUITE_PIDS = 'echo $$ >> "$FAKE_PIDS"; "$REAL_SLEEP" 60 & echo $! >> "$FAKE_PIDS"; '

_NEEDS_REAL_FLOCK_AND_TIMEOUT = pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("timeout") is None,
    reason="needs util-linux flock and GNU timeout (not on macOS by default)")


@_NEEDS_REAL_FLOCK_AND_TIMEOUT
@pytest.mark.parametrize("sig,rc", [(signal.SIGHUP, 129), (signal.SIGTERM, 143)])
def test_a_hangup_or_term_to_the_wrapper_stops_the_whole_suite_and_the_box_lock_outlives_it(tmp_path: Path, sig: signal.Signals, rc: int) -> None:
    """Round 3 (round 2 verify I1): `timeout` gives the suite its own process group and the suite does not hold the
    lock, so a hangup (an ssh drop) that killed only the wrapper released the box lock under a LIVE suite, and CI
    could take it and overlap. The wrapper must stop the suite's whole group, and release the lock only after."""
    work, env = _hand_env(tmp_path, flock="real", timeout="real")
    pids_file, lock = tmp_path / "pids", tmp_path / "lease" / "box.lock"
    # a slow, orderly exit on the signal, so there is a window in which the lock could be wrongly free
    suite = _SUITE_PIDS + 'trap \'"$REAL_SLEEP" 1; exit 0\' TERM HUP; wait'
    proc = _spawn_hand(tmp_path, work, {**env, "FAKE_PIDS": str(pids_file), "FAKE_UV_EVAL": suite})
    pids: list[int] = []
    try:
        pids = _pids_from(pids_file, 2)
        assert not _box_lock_is_free(lock), "the wrapper must hold the box lock while the suite runs"
        os.kill(proc.pid, sig)  # the wrapper only, as an ssh drop does
        held_with_suite_alive, violation = 0, ""
        deadline = time.monotonic() + 30
        while proc.poll() is None and time.monotonic() < deadline:
            free = _box_lock_is_free(lock)  # sample the lock BEFORE the suite: a pid alive now was alive then
            alive = [p for p in pids if _pid_alive(p)]
            if free and alive:
                violation = f"box.lock was free while suite processes {alive} were still running"
            if not free and alive:
                held_with_suite_alive += 1
            time.sleep(0.02)
        assert not violation, violation
        assert proc.poll() == rc, ("the wrapper must exit 128+signal", proc.poll(), (tmp_path / "hand.out").read_text())
        assert held_with_suite_alive > 0, "non-vacuity: the window with the suite still stopping was never observed"
        assert not [p for p in pids if _pid_alive(p)], "the suite and its child must be gone"
        assert _box_lock_is_free(lock)
    finally:
        _kill_all(pids)
        _reap(proc)


@_NEEDS_REAL_FLOCK_AND_TIMEOUT
def test_a_term_with_stderr_a_dead_pipe_still_stops_the_suite_before_the_box_lock_goes(tmp_path: Path) -> None:
    """Round 3 verify S-B: the trap logged before it signalled, so with stderr a pipe whose reader had gone the log
    line raised SIGPIPE, the wrapper died inside its own trap, the box lock went free and the suite ran on. The
    signal must go out first, and the wrapper must ignore SIGPIPE once the suite is launched."""
    work, env = _hand_env(tmp_path, flock="real", timeout="real")
    pids_file, lock = tmp_path / "pids", tmp_path / "lease" / "box.lock"
    suite = _SUITE_PIDS + 'trap \'"$REAL_SLEEP" 1; exit 0\' TERM HUP; wait'
    bash = shutil.which("bash")
    assert bash
    with _sigint_default(), open(tmp_path / "hand.out", "w") as out:
        proc = subprocess.Popen([bash, str(work / "scripts" / "qwen-hand-run.sh")], cwd=work,
                                env={**env, "FAKE_PIDS": str(pids_file), "FAKE_UV_EVAL": suite}, stdout=out,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
    pids: list[int] = []
    try:
        pids = _pids_from(pids_file, 2)
        assert proc.stderr is not None
        proc.stderr.close()  # the reader goes away after the launch: the next log line meets a dead pipe
        os.kill(proc.pid, signal.SIGTERM)
        violation = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and [p for p in pids if _pid_alive(p)]:
            if _box_lock_is_free(lock) and (alive := [p for p in pids if _pid_alive(p)]):
                violation = f"box.lock was free while suite processes {alive} were still running"
                break
            time.sleep(0.02)
        assert not violation, violation
        assert not [p for p in pids if _pid_alive(p)], "the TERM must have reached the suite's group"
        assert proc.wait(timeout=20) == 143, "the wrapper must survive its own log line and exit 128+TERM"
    finally:
        _kill_all(pids)
        _reap(proc)


def test_ctrl_c_reaches_the_suite_and_the_wrapper_leaves_with_130(tmp_path: Path) -> None:
    """Round 3 (I1): the suite is in `timeout`'s process group, not the foreground one, so the terminal's ^C never
    reached it and bash did not leave until it ended. The wrapper's own SIGINT trap must forward it."""
    work, env = _hand_env(tmp_path, flock="fake", timeout="real" if shutil.which("timeout") else "fake")
    pids_file, mark = tmp_path / "pids", tmp_path / "int.mark"
    suite = _SUITE_PIDS + 'trap \'echo int >> "$FAKE_MARK"; exit 0\' INT; wait'
    proc = _spawn_hand(tmp_path, work, {**env, "FAKE_PIDS": str(pids_file), "FAKE_MARK": str(mark), "FAKE_UV_EVAL": suite})
    pids: list[int] = []
    try:
        pids = _pids_from(pids_file, 2)
        os.kill(proc.pid, signal.SIGINT)  # the wrapper only: the terminal would not signal the suite's group
        try:
            rc: int | None = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            rc = None
        assert rc == 130, ("^C must end the run at once, not when the suite does", rc)
        assert mark.exists() and "int" in mark.read_text(), "the suite must have received the SIGINT"
        assert not [p for p in pids if _pid_alive(p)]
    finally:
        _kill_all(pids)
        _reap(proc)


@_NEEDS_REAL_FLOCK_AND_TIMEOUT
def test_a_descendant_that_ignores_term_is_killed_at_the_cap_plus_the_grace(tmp_path: Path) -> None:
    """Round 3 (round 2 verify S2): `timeout` leaves once its direct child is gone, so its -k grace never reaches a
    descendant that ignores SIGTERM; the run ended 77 with that process still running and the lock released."""
    work, env = _hand_env(tmp_path, flock="real", timeout="real")
    pids_file, lock = tmp_path / "pids", tmp_path / "lease" / "box.lock"
    suite = 'echo $$ >> "$FAKE_PIDS"; ( trap \'\' TERM; echo $BASHPID >> "$FAKE_PIDS"; exec "$REAL_SLEEP" 60 ) & wait'
    pids: list[int] = []
    try:
        t0 = time.monotonic()
        proc = _hand(work, {**env, "FAKE_PIDS": str(pids_file), "FAKE_UV_EVAL": suite,
                            "QWEN_HAND_RUN_HOLD_SECONDS": "2", "QWEN_HAND_RUN_KILL_SECONDS": "2"})
        elapsed = time.monotonic() - t0
        pids = [int(x) for x in pids_file.read_text().split()]
        assert len(pids) == 2
        assert proc.returncode == 77, (proc.returncode, proc.stdout, proc.stderr)
        assert not [p for p in pids if _pid_alive(p)], "a descendant that ignores TERM must be killed, not left running"
        assert elapsed < 20, f"the run must end at cap + grace, not when the ignorer does ({elapsed:.1f}s)"
        assert _box_lock_is_free(lock)
    finally:
        _kill_all(pids)


def test_a_clean_run_leaves_nothing_of_the_suite_running_in_its_group(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path, flock="fake", timeout="real" if shutil.which("timeout") else "fake")
    pids_file = tmp_path / "pids"
    pids: list[int] = []
    try:
        proc = _hand(work, {**env, "FAKE_PIDS": str(pids_file), "FAKE_UV_EVAL": _SUITE_PIDS + "exit 0"})
        pids = [int(x) for x in pids_file.read_text().split()]
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert not [p for p in pids if _pid_alive(p)], "a straggler of the suite must be gone before the lock is released"
    finally:
        _kill_all(pids)


def test_a_hand_run_yields_the_lock_when_ci_arrives_while_it_waited(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    proc = _hand(work, {**env, "FAKE_MARKER_ON_CALL": "1"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 76 and "yield" in out and "retry later" in out, (proc.returncode, out)
    assert not any(c == "jar" or c.startswith("uv ") for c in _calls(env)), "it must not build or run with CI queued"
    assert any(c.startswith("sleep ") for c in _calls(env)), "it backs off rather than spinning on the lock"


def test_a_hand_run_that_yielded_goes_ahead_once_ci_has_taken_its_turn(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    proc = _hand(work, {**env, "FAKE_MARKER_ON_CALL": "1", "FAKE_CLEAR_ON_SLEEP": "1"})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _acquires(env) == ["flock -w 1 -E 200 9", "flock -w 1 -E 200 9"], "yield, back off, take the lock again"
    assert "uv run pytest -n 8 -q" in _calls(env)


def test_a_lock_that_stays_held_ends_in_the_retry_later_code_after_the_bounded_wait(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path)
    proc = _hand(work, {**env, "FAKE_HELD": "1"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 76 and "held" in out and "retry later" in out, (proc.returncode, out)
    assert not any(c == "jar" or c.startswith("uv ") for c in _calls(env))


@pytest.mark.parametrize("kw,rc", [({"uv_rc": 3}, 3), ({"jar_rc": 1}, 1)])
def test_a_hand_run_exits_with_the_suites_or_the_jar_builds_status(tmp_path: Path, kw: dict[str, int], rc: int) -> None:
    work, env = _hand_env(tmp_path, **kw)
    proc = _hand(work, env)
    assert proc.returncode == rc, (proc.stdout, proc.stderr)
    if "jar_rc" in kw:
        assert not any(c.startswith("uv run") for c in _calls(env)), "no suite after a failed jar build"


def test_a_host_without_flock_timeout_or_the_lease_directory_fails_closed_with_its_own_code(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path / "noflock", flock=None)
    proc = _hand(work, env)
    assert proc.returncode == 69 and "flock" in proc.stdout + proc.stderr and "not a test failure" in proc.stdout + proc.stderr
    assert _calls(env) == []
    work, env = _hand_env(tmp_path / "notimeout", timeout=None)
    proc = _hand(work, env)
    assert proc.returncode == 69 and "timeout" in proc.stdout + proc.stderr
    assert _calls(env) == []
    work, env = _hand_env(tmp_path / "nolease", lease=False)
    proc = _hand(work, env)
    assert proc.returncode == 69 and str(tmp_path / "nolease" / "lease") in proc.stdout + proc.stderr
    assert _calls(env) == []


def test_the_hand_run_exit_codes_do_not_collide_with_the_suites_own_75() -> None:
    """tests/_suite_lease.py exits 75 on a held suite lease; 'CI has priority' must be tellable apart from it."""
    code = re.sub(r"(?m)^\s*#.*$", "", _WRAPPER.read_text())
    assert "exit 76" in code or "return 76" in code or "refuse 76" in code
    assert not re.search(r"\b75\b", code), "75 is the suite lease's code, not this wrapper's"


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock is not installed here (macOS)")
def test_the_real_flock_the_box_lock_is_held_while_the_suite_runs_and_released_after(tmp_path: Path) -> None:
    work, env = _hand_env(tmp_path, flock="real")
    probe = tmp_path / "lease" / "box.lock"
    proc = _hand(work, {**env, "FAKE_LOCK_PROBE": str(probe)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "lock-probe rc=1" in _calls(env), "a second taker must be refused while the suite runs"
    after = subprocess.run([shutil.which("flock") or "flock", "-n", str(probe), "true"])  # released at exit
    assert after.returncode == 0


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock is not installed here (macOS)")
def test_the_real_flock_a_marker_is_live_only_while_its_holder_has_it_locked(tmp_path: Path) -> None:
    """The point of round 2: with a genuine kernel lock, a held marker blocks a start and an abandoned one does not."""
    import fcntl

    work, env = _hand_env(tmp_path, flock="real")
    marker = tmp_path / "lease" / "ci-waiting.8.1"
    marker.write_text("run=8\n")
    with open(marker) as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert _hand(work, env).returncode == 76, "CI holds its marker: queued"
        assert _calls(env) == [], "and nothing was taken or built"
    proc = _hand(work, env)  # the holder is gone (a killed CI step), the file is fresh and unlocked
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock is not installed here (macOS)")
@pytest.mark.parametrize("ci_arrives", [False, True])
def test_the_real_flock_a_hand_run_waits_for_a_holder_and_yields_if_ci_queued_meanwhile(tmp_path: Path, ci_arrives: bool) -> None:
    import fcntl

    work, env = _hand_env(tmp_path, flock="real")
    lock = tmp_path / "lease" / "box.lock"
    marker = tmp_path / "lease" / "ci-waiting.8.1"
    bash = shutil.which("bash")
    assert bash
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.Popen([bash, str(work / "scripts" / "qwen-hand-run.sh")], cwd=work,
                                env={**env, "QWEN_HAND_RUN_MAX_WAIT_SECONDS": "6"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        assert proc.poll() is None, "the hand run waits for the holder"
        marker_fd = None
        if ci_arrives:
            marker.write_text("run=8\n")
            marker_fd = open(marker)
            fcntl.flock(marker_fd, fcntl.LOCK_EX)
        fcntl.flock(held, fcntl.LOCK_UN)
        try:
            out, err = proc.communicate(timeout=60)
        finally:
            if marker_fd is not None:
                marker_fd.close()
    if ci_arrives:
        assert proc.returncode == 76, (out, err)
        assert not any(c == "jar" or c.startswith("uv ") for c in _calls(env))
    else:
        assert proc.returncode == 0, (out, err)
        assert "uv run pytest -n 8 -q" in _calls(env)
