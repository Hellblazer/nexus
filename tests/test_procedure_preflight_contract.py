# SPDX-License-Identifier: AGPL-3.0-or-later
"""The result contract of every pytest command preflight emits, and the runs of the emitted
commands with real tools (nexus-qo7ie.36).

A step that names test files must collect a test from each under its own argv; a pytest
command passes only on a clean summary line (R44, R55); and the full-suite row, the
changed-tests step and the failing-diff cases run with the real ``scripts/watched_pytest.py``
and pytest, not stubs alone. The rules are cited by the numbers of the census in T2
``nexus/qo7ie.36-census-feedback_test_runs_before_push``. The harness is
tests/_preflight_harness.py.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._preflight_harness import (
    _SHELLS,
    BASH,
    EVERYTHING,
    REPO_ROOT,
    SOURCE,
    _change,
    _command,
    _env,
    _git,
    _inner,
    _Run,
    _run,
    repo,  # noqa: F401  (fixture)
    stubs,  # noqa: F401  (fixture)
)



# ---- a step that names files must collect them under its own argv ------------------------

_PYTEST_AT = re.compile(r"uv run pytest (.*?)(?: \)$|; fi; \}|'$|$)", re.S)
_NAMED = re.compile(r"tests/[A-Za-z0-9_./*?\[\]-]+\.py")
_PRINTF = re.compile(r"printf '%s\\0' ([^;]*);")


def _pytest_step(command: str) -> tuple[str, list[str], list[str]] | None:
    """(the pytest argv a step runs, the test files it names, the files a changed-tests step
    appends through ``printf '%s\\0' <files>``); None when it names no file. Files a selector or
    the diff supplies at run time are not named."""
    inner = _inner(command) if command.startswith("log=$(mktemp ") else command
    m = _PYTEST_AT.search(inner)
    if m is None:
        return None
    argv = m.group(1)
    appended = [w for p in _PRINTF.findall(inner) for w in p.split()]
    named = _NAMED.findall(argv) + appended
    return (argv, named, appended) if named else None


def _collected(argv: str, appended: list[str]) -> list[str]:
    """The node ids ``pytest --collect-only -q`` finds over a step's own argv (its -m, -o and
    -k included), run in a shell so a glob expands as the step's shell would."""
    argv = re.sub(r"(^|\s)--(\s|$)", " ", argv)  # a bare `--` is xargs's; the files follow it
    argv = re.sub(r"(^|\s)-q(\s|$)", " ", argv)  # one -q here: two print counts, not node ids
    proc = subprocess.run(
        ["bash", "-c", f"{sys.executable} -m pytest --collect-only -q -p no:cacheprovider {argv} {' '.join(appended)}"],
        cwd=REPO_ROOT,
        env={**_env(), "NX_TEST_T2_SUBSTRATE": "none"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return [line for line in proc.stdout.splitlines() if "::" in line]


def _steps_naming_files(out: dict) -> list[dict]:
    return [c for c in out["checks"] if _pytest_step(c["command"]) is not None]


@pytest.mark.parametrize(
    ("bad", "good", "file"),
    [
        # marlowe's procedure row as first written: default addopts deselect the lint file.
        (
            "-q -rf -p no:randomly tests/test_shipped_procedures_lint.py",
            "-q -rf -p no:randomly -m 'not integration and not slow' tests/test_shipped_procedures_lint.py",
            "tests/test_shipped_procedures_lint.py",
        ),
        # wren's schema step as first written: -m lint over a file that carries no lint mark.
        (
            "-q -rf -p no:randomly -m lint tests/test_changelog_validate_precondition_lint.py",
            "-q -rf -p no:randomly tests/test_changelog_validate_precondition_lint.py",
            "tests/test_changelog_validate_precondition_lint.py",
        ),
    ],
)
def test_the_collect_only_pin_goes_red_on_the_two_commands_it_was_written_for(
    bad: str, good: str, file: str
) -> None:
    """The measurement itself, so the pin below cannot pass vacuously: the first argv names a
    file and collects nothing from it, the second (the fix) collects it."""
    assert not any(i.startswith(file + "::") for i in _collected(bad, []))
    assert any(i.startswith(file + "::") for i in _collected(good, []))


def test_every_step_that_names_files_collects_a_test_from_each_under_its_own_argv(
    repo: Path,
) -> None:
    """Default addopts is -m 'not integration and not slow and not lint', so a step that names a
    lint-marked file collects nothing from it (pytest prints "no tests collected (10 deselected)"
    and a bare string check passes). And a step that adds -m lint over a file that is not
    lint-marked deselects all of that file. Neither is visible in the command text: this runs
    pytest --collect-only over each step's own argv and wants at least one test from every file
    it names (marlowe's procedure row, wren's schema step)."""
    _change(
        repo,
        {
            **EVERYTHING,
            "src/nexus/procedures/x.lua": "x\n",
            "src/nexus/cli.py": "x = 1\n",
            "tests/e2e/hook-cli-skew/run.sh": "x\n",
            "service/src/main/java/dev/nexus/service/db/CatalogRepository.java": "x\n",
            "src/nexus/catalog/recovery_bundle.py": "x = 1\n",
        },
    )
    out = _Run(repo)()
    steps = _steps_naming_files(out)
    bad: list[str] = []
    for c in steps:
        argv, named, appended = _pytest_step(c["command"])  # type: ignore[misc]
        ids = _collected(argv, appended)
        for pattern in named:
            files = sorted(p.relative_to(REPO_ROOT).as_posix() for p in REPO_ROOT.glob(pattern))
            if not files:
                bad.append(f"{c['check']}: {pattern} matches no file")
            for f in files:
                if not any(i.startswith(f + "::") for i in ids):
                    bad.append(f"{c['check']}: no test collected from {f}; argv: {argv}")
    assert not bad, "\n".join(bad)
    assert len(steps) >= 12, [c["check"] for c in steps]  # non-vacuity: the rows fired


def test_every_file_a_row_names_exists() -> None:
    """R48 for the rows themselves: a guessed file name makes pytest collect nothing."""
    src = SOURCE.decode("ascii")
    named = set(re.findall(r"\b((?:tests|scripts)/[A-Za-z0-9_./-]*\.(?:py|sh|txt))\b", src))
    assert len(named) >= 14, named  # non-vacuity: the rows' files were found
    assert {p for p in named if not (REPO_ROOT / p).exists()} == set()


# ---- the result contract on every pytest command (R44, R55) ---------------------------

#: (the summary pytest prints, its exit code, whether the command must pass)
_SUMMARIES = [
    ("964 passed in 3.21s", 0, True),
    ("964 passed, 3 skipped, 2 deselected, 1 warning in 3.21s (0:00:03)", 0, True),
    ("2 passed, 1 xfailed, 1 xpassed in 0.5s", 0, True),
    ("=========== 964 passed in 3.21s ===========", 0, True),
    ("964 passed in 3.21s\nwatched_pytest: passed (exit 0) after 4s", 0, True),
    # R44: `grep -q passed` reads these as green.
    ("1 failed, 964 passed in 3.21s", 1, False),
    ("1 failed, 964 passed in 3.21s", 0, False),
    ("964 passed, 1 error in 3.21s", 1, False),
    ("964 passed, 2 errors in 3.21s", 0, False),
    ("964 passed in 3.21s", 1, False),
    ("no tests ran in 0.01s", 5, False),
    ("no tests ran in 0.01s", 0, False),
    ("3 deselected in 0.01s", 5, False),
    # R55: a run that printed nothing is not a pass.
    ("", 0, False),
    # The summary is the LAST line that ends in a duration: a decoy before it (a test's own
    # output, an earlier chunk) decides nothing, in either direction.
    ("1 failed in 0.2s\n5 passed in 3s", 0, True),
    ("5 passed in 3s\n1 failed in 0.2s", 0, False),
]


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("summary", "code", "passes"), _SUMMARIES)
def test_r44_r55_a_pytest_command_passes_only_on_a_clean_summary_line(
    repo: Path, stubs: Path, shell: str, summary: str, code: int, passes: bool
) -> None:
    _change(repo, {"tests/sub/test_x.py": "def test(): pass\n"})
    cmd = _command(_Run(repo)(), "the changed test files")
    rc, lines = _run(repo, stubs, cmd, shell, {"STUB_SUMMARY": summary, "STUB_RC": str(code)})
    assert (rc == 0) is passes, (rc, lines)
    if passes:
        assert summary.splitlines()[0] in lines
    else:
        assert any("full log:" in line for line in lines), lines


@pytest.mark.parametrize("shell", _SHELLS)
def test_r44_nothing_selected_passes_only_when_the_command_says_so(
    repo: Path, stubs: Path, shell: str
) -> None:
    """The integration selection can be empty: that passes, said out loud. A selection
    that ran something and printed no summary does not."""
    _change(repo, {"src/x.py": "x = 1\n"})
    cmd = _command(_Run(repo)(), "the integration tests the change affects")
    rc, lines = _run(repo, stubs, cmd, shell, {"STUB_SELECT": ""})
    assert rc == 0 and "nothing selected" in lines, lines
    rc, lines = _run(repo, stubs, cmd, shell, {"STUB_SUMMARY": ""})
    assert rc != 0, lines


@pytest.mark.parametrize("shell", _SHELLS)
def test_r44_r55_every_pytest_command_is_gated_and_runs_under_the_shell(
    repo: Path, stubs: Path, shell: str
) -> None:
    _change(repo, EVERYTHING)
    out = _Run(repo)()
    runs = [c for c in out["checks"] if "pytest" in c["command"]]
    assert len(runs) >= 14, [c["check"] for c in runs]  # non-vacuity: the rows fired
    for c in runs:
        assert c["command"].startswith("log=$(mktemp "), c["check"]
        assert " -rf " in _inner(c["command"]), c["check"]
        assert "grep -q passed" not in c["command"], c["check"]
        rc, lines = _run(repo, stubs, c["command"], shell)
        assert rc == 0, (c["check"], lines)
        assert "1 passed in 0.01s" in lines, c["check"]


# ---- the emitted commands under the real tools (not stubs alone) ----------------------------

#: What a nested pytest must not inherit from the run that runs this test.
_UNSET_PYTEST = {k: None for k in os.environ if k.startswith("PYTEST_")}


def _real_tools(root: Path) -> Path:
    """A PATH directory whose ``uv run pytest`` is this interpreter's pytest and whose
    ``lab-remote`` runs what follows its ``--`` here, so an emitted command runs for real."""
    d = root / "realtools"
    d.mkdir()
    (d / "uv").write_text(
        f'#!/bin/sh\nshift\n[ "$1" = pytest ] && shift\nexec {sys.executable} -m pytest "$@"\n',
        encoding="utf-8",
    )
    (d / "lab-remote").write_text(
        '#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n', encoding="utf-8"
    )
    for tool in d.iterdir():
        tool.chmod(0o755)
    return d


def _project(root: Path, test: str) -> Path:
    """A directory shaped as the full-suite row expects: the real scripts/watched_pytest.py, a
    gate-jar script that succeeds, and one test file."""
    p = root / "proj"
    (p / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "watched_pytest.py", p / "scripts" / "watched_pytest.py")
    jar = p / "scripts" / "build-gate-jar.sh"
    jar.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    jar.chmod(0o755)
    (p / "test_trivial.py").write_text(test, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    ("test", "passes"),
    [
        ("def test_ok():\n    assert True\n", True),
        ("def test_bad():\n    assert False\n", False),
    ],
)
def test_the_full_suite_row_runs_under_the_real_watched_pytest(
    repo: Path, tmp_path: Path, test: str, passes: bool
) -> None:
    """The row's own argv, through the gated wrapper, with the real scripts/watched_pytest.py
    (stdlib only) and pytest. watched_pytest refuses -q and tees pytest's output only to its own
    --log, so a stub lab-remote that prints a summary for anything containing "pytest" cannot cover
    this: the row must put the log's tail where the wrapper reads it and exit with pytest's code."""
    _change(repo, {"src/nexus/x.py": "x = 1\n"})
    row = next(c for c in _Run(repo)()["checks"] if c["check"].startswith("the full Python suite"))
    assert " -q " not in _inner(row["command"])
    proj = _project(tmp_path, test)
    env = {**_UNSET_PYTEST, "HOME": str(tmp_path), "WATCHED_PYTEST_NOTIFY_URL": None}
    rc, lines = _run(proj, _real_tools(tmp_path), row["command"], BASH, env)
    if passes:
        assert rc == 0, lines
        assert any(re.search(r"\b1 passed in [0-9.]+s", line) for line in lines), lines
    else:
        assert rc != 0, lines
        assert any("full log:" in line for line in lines), lines


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    "step",
    ["ruff check", "actionlint", "the changed test files", "the integration tests the change affects"],
)
def test_a_diff_that_cannot_be_read_fails_the_step_instead_of_running_nothing(
    repo: Path, stubs: Path, shell: str, step: str
) -> None:
    """The diff is piped into xargs -r, so without pipefail a base ref that is absent where the
    command runs (a copy without the remote-tracking ref) read as an empty list and exit 0."""
    _change(
        repo,
        {
            "src/nexus/x.py": "x = 1\n",
            "tests/test_x.py": "def test(): pass\n",
            ".github/workflows/a.yml": "on: push\n",
        },
    )
    cmd = _command(_Run(repo)(), step)
    assert _run(repo, stubs, cmd, shell)[0] == 0  # the control: the base is there
    _git(repo, "tag", "-d", "base")
    rc, lines = _run(repo, stubs, cmd, shell)
    assert rc != 0, lines


def test_a_changed_lint_marked_test_file_is_collected_by_the_changed_tests_step(
    repo: Path, tmp_path: Path
) -> None:
    """The changed-tests step runs under the repo's default addopts, which deselect lint, slow and
    integration. A changed lint-marked file must still run there (the summary accepts "N
    deselected", so deselected tests read green); integration-marked ones run in the integration
    row and slow-marked ones in the nightly gate."""
    cfg = (
        "[tool.pytest.ini_options]\n"
        "addopts = \"-m 'not integration and not slow and not lint'\"\n"
        'markers = ["lint: x", "slow: x", "integration: x"]\n'
    )
    guard = "tests/test_mode_declarations_are_explicit.py"
    _change(repo, {"pyproject.toml": cfg, guard: "def test_guard():\n    pass\n"})
    _git(repo, "tag", "-f", "base")
    marked = {
        "lint": "tests/test_l.py",
        "slow": "tests/test_s.py",
        "integration": "tests/test_i.py",
    }
    _change(
        repo,
        {
            path: f"import pytest\npytestmark = pytest.mark.{mark}\n\ndef test_{mark}():\n    pass\n"
            for mark, path in marked.items()
        },
    )
    cmd = _command(_Run(repo)(), "the changed test files")
    rc, lines = _run(repo, _real_tools(tmp_path), cmd, BASH, _UNSET_PYTEST)
    assert rc == 0, lines
    # the guard and the lint file run; the slow and the integration file do not
    assert any(re.search(r"\b2 passed, 2 deselected in ", line) for line in lines), lines
    assert "integration row" in _command_text(_Run(repo)(), "the changed test files")


def _command_text(out: dict, prefix: str) -> str:
    [text] = [c["check"] for c in out["checks"] if c["check"].startswith(prefix)]
    return text
