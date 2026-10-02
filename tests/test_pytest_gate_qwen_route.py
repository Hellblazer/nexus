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
import shutil
import subprocess
import tempfile
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
    root = tmp_path / "lease"
    root.mkdir()
    os.chmod(root, 0o555)
    try:
        proc, _ = _lease(tmp_path, root)
    finally:
        os.chmod(root, 0o755)
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere, so the refusal cannot be observed")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "cannot create entries" in out
    import getpass

    me = getpass.getuser()
    assert not (len(me) >= 4 and me in out.replace(str(tmp_path), "<tmp>")), "the account name must not be printed"
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
    assert "Probe run record: none yet" in agents, "the run id is recorded here when the probe has run green"
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
    r"""^\s*flock\s+(?P<opts>.*?)\s+"\$lock"\s+bash -c '(?P<inner>[^']*)'(?:\s*\|\|\s*rc=\$\?)?\s*$""", re.MULTILINE)


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
    for tool in ("bash", "tee"):
        real = shutil.which(tool)
        assert real
        (bin_dir / tool).symlink_to(real)
    log = tmp_path / "calls.log"
    jar = work / "scripts" / "build-gate-jar.sh"
    jar.write_text(f'#!{bash}\necho jar >> "$FAKE_LOG"\nexit {jar_rc}\n')
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
ecode="$4"; shift 5
if [ -n "${{FAKE_TIMEOUT:-}}" ]; then exit "$ecode"; fi
exec "$@"
''')
        fake.chmod(0o755)
    elif flock == "real":
        real = shutil.which("flock")
        assert real
        (bin_dir / "flock").symlink_to(real)
    env = {"PATH": str(bin_dir), "FAKE_LOG": str(log), "QWEN_SUITE_LEASE_ROOT": str(tmp_path / "lease"),
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


def test_the_docs_state_n8_and_the_box_lock_hand_run_convention_and_no_stale_n12() -> None:
    hand_run = "flock /var/lib/nx-suite-lease/box.lock bash -c 'uv sync -q && scripts/build-gate-jar.sh && uv run pytest -n 8 -q'"
    for name in ("AGENTS.md", "docs/contributing.md"):
        text = (_ROOT / name).read_text()
        assert hand_run in text, name
        assert "box.lock" in text and "hellmini" in text and "-n 8" in text, name
        for stale in ("one `-n 12` job", "xdist -n 12", "pytest tests/ -n 12", "under `-n 12`"):
            assert stale not in text, (name, stale)
