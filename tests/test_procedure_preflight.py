# SPDX-License-Identifier: AGPL-3.0-or-later
"""The preflight procedure (RDR-240 Technical Design 8, nexus-qo7ie.32).

Most of this file runs the script in a test-local harness (class ``_Run``, in tests/_preflight_harness.py)
shaped like RDR-242 Technical Design 7's environment: the static check's own
allowlists (the base functions; string without dump, its metatable's __index
the reduced table; table, math, utf8), the two granted verbs and nothing else,
loaded in mode "t" with the arguments as the main chunk's vararg (contract
v6), the pattern of tests/test_procedure_research_add.py. A verb failure
reaches the script as a Lua error, as through the dispatcher. The verbs are
the real ones over a temp git repository. The header is read by
``header.parse`` and the static check passes the file with those grants. The
harness is the independent oracle: it shares the script's source with the real
run but none of the runtime (worker, frames, limits, result walk).

The last section, ``through the real run path``, runs the real
``run_procedure`` (library lookup, worker, dispatcher) over the same temp
repo. There the real result is pinned equal to the harness's for a code, a
docs-only and an empty diff; the two front doors (the MCP tool and ``nx run``)
are tested to return the same JSON; and the refusals with the repo root off
are tested through all three entry points.

The emitted commands are also run: in bash, in the temp repository, with the
tools they name replaced by stubs that print their arguments, so quoting,
the deleted-path filter and the ``--`` are tested as a shell reads them. Two
mirrors are pinned against their sources: the docs rows against CI's
doc-only case statement, run through bash, and the plugin rows against the
drift-ledger workflow's watched paths.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import lupa.lua54 as lua54
import pytest
import yaml
from click.testing import CliRunner

from nexus import bounded_subprocess
from nexus.cli import main
from nexus.lua_runtime import header as hd
from nexus.lua_runtime import library
from nexus.lua_runtime import run as run_mod
from nexus.lua_runtime import static_check as sc
from nexus.lua_runtime import supervisor
from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.runner import run_procedure
from nexus.mcp import core
from tests._module_seam import setattr_in
from tests._preflight_harness import (
    _SHELLS,
    BASH,
    BEAD_MANIFEST,
    CI,
    DRIFT,
    EVERYTHING,
    FOLD_CHECK,
    MARK,
    REPO_ROOT,
    SCRIPT,
    SOURCE,
    UNTRACKED,
    _args,
    _bare,
    _change,
    _ci_ruff,
    _class_names,
    _command,
    _commands,
    _git,
    _inner,
    _operands,
    _rows,
    _Run,
    _shell,
    repo,  # noqa: F401  (fixture)
    stubs,  # noqa: F401  (fixture)
)



# ---- the header and the static check -------------------------------------------


def test_the_header_declares_base_and_the_two_git_grants() -> None:
    h = hd.parse(SOURCE, name="preflight")
    assert h.args == (hd.ArgSpec("base", "string", False),)
    assert h.grants == frozenset({"git.diff_names", "git.diff_stat"})
    assert sc.check(SOURCE, h.grants) == h.grants


@pytest.mark.parametrize("dropped", ["git.diff_names", "git.diff_stat"])
def test_dropping_a_grant_is_refused_by_verb_ungranted(dropped: str) -> None:
    grants = frozenset({"git.diff_names", "git.diff_stat"}) - {dropped}
    with pytest.raises(sc.StaticCheckRefusal) as refused:
        sc.check(SOURCE, grants)
    assert refused.value.rule == "verb-ungranted"


def test_the_script_lives_in_the_repo_root_and_ships_in_no_package() -> None:
    assert SCRIPT.relative_to(REPO_ROOT).as_posix() == ".nexus/procedures/preflight.lua"
    assert not (
        Path(hd.__file__).parent.parent / "procedures" / "preflight.lua"
    ).exists()


# ---- each change class --------------------------------------------------------


def test_a_python_change_gets_ruff_its_tests_and_the_standing_checks(
    repo: Path,
) -> None:
    _change(
        repo, {"src/nexus/x.py": "x = 1\n", "tests/sub/test_x.py": "def test(): pass\n"}
    )
    out = _Run(repo)()
    assert out["code"] is True
    assert _class_names(out) == ["python"]
    assert _bare(_commands(out)["python"]) == [
        # pipefail, so a diff git cannot read fails the step instead of running nothing.
        "( set -o pipefail; git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
        "':(glob)src/**/*.py' | xargs -0 -r uvx ruff@0.15.18 check --force-exclude -- )",
        # R13: the default-loop guard that judges only what its invocation collected
        # rides in the same invocation as the changed tests; NOT_SLOW so a changed lint- or
        # slow-marked file is collected, not deselected.
        "( set -o pipefail; { git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
        "':(glob)tests/**/test_*.py' && printf '%s\\0' "
        "tests/test_mode_declarations_are_explicit.py; } | "
        "xargs -0 -r uv run pytest -q -rf -p no:randomly -m 'not integration and not slow' -- )",
    ]
    assert _bare(_commands(out)["code"]) == [
        "scripts/build-gate-jar.sh",
        UNTRACKED,
        "lab-remote -- bash -c 'scripts/build-gate-jar.sh && uv run pytest -q -rf -p no:randomly -m lint -n auto'",
        "uv run pytest -q -rf -p no:randomly tests/test_plugin_release_drift_ledger.py tests/daemon/test_lifecycle_gate.py",
        "uv run scripts/tenant_delta.py --strict --base=base",
        "uv run scripts/windows_hand_test.py --check",
        "f=$(set -o pipefail; git diff --no-renames --name-only base...HEAD | uv run python "
        "scripts/select_affected_integration_tests.py --repo .) && "
        '{ if [ -z "$f" ]; then echo \'nothing selected\'; else '
        "printf '%s\\n' \"$f\" | xargs lab-remote -- uv run pytest -q -rf -p no:randomly"
        f" -o addopts= -m {MARK}; fi; }}",
    ]


def test_the_gate_jar_comes_before_every_pytest(repo: Path) -> None:
    _change(repo, {"src/nexus/x.py": "x = 1\n", "service/A.java": "x\n"})
    out = _Run(repo)()
    commands = [c["command"] for c in out["checks"]]
    assert commands[0] == "scripts/build-gate-jar.sh"
    assert all("pytest" not in c for c in commands[:1])


def test_ruff_reads_src_only(repo: Path) -> None:
    _change(repo, {"tests/test_x.py": "x\n", "scripts/s.py": "x\n"})
    out = _Run(repo)()
    assert not any("ruff" in c for c in _commands(out).get("python", []))


def test_a_dependency_change_is_python_but_gets_no_file_checks(repo: Path) -> None:
    _change(repo, {"pyproject.toml": "[project]\n", "uv.lock": "x\n"})
    out = _Run(repo)()
    assert _class_names(out) == ["python"]
    assert "python" not in _commands(out)
    assert "code" in _commands(out)


def test_an_engine_change_gets_the_leased_main_suite_and_integration_group(
    repo: Path,
) -> None:
    _change(repo, {"service/src/main/java/A.java": "class A {}\n"})
    out = _Run(repo)()
    assert _commands(out)["engine"] == [
        "scripts/mvnw-leased.sh --lab-run lab-m -q test -Dnexus.ddl.suite=main",
        "scripts/mvnw-leased.sh --lab-run lab-m -q test -Dnexus.ddl.suite=integration"
        " -Dtest.excluded.groups= -Dgroups=integration",
    ]
    assert all(not c.startswith("lab-remote") for c in _commands(out)["engine"])
    assert "schema" not in _commands(out)
    assert out["code"] is True


def test_a_changeset_is_engine_and_schema(repo: Path) -> None:
    _change(repo, {"service/src/main/resources/db/changelog/x-001.xml": "<x/>\n"})
    out = _Run(repo)()
    assert _class_names(out) == ["engine", "schema"]
    # The validation test is not lint-marked: a `-m lint` here deselected all 32 of its tests and
    # exited 0 (wren). The parity test reads the changelogs against COLLECTION_SCOPED_TABLES
    # through a live engine substrate, so it is heavy and comes after the gate jar.
    assert _bare(_commands(out)["schema"]) == [
        "uv run pytest -q -rf -p no:randomly tests/test_changelog_validate_precondition_lint.py",
        "uv run pytest -q -rf -p no:randomly tests/catalog/test_collection_scoped_tables_schema_parity.py",
    ]
    parity = _rows(out, "the collection-scoped tables schema parity test")[0]
    assert parity["heavy"] is True
    names = [c["check"] for c in out["checks"]]
    assert names[0].startswith("build the gate jar") and names.index(parity["check"]) > 0


def test_a_hook_change_gets_the_hook_tests_and_other_plugin_paths_add_none(
    repo: Path,
) -> None:
    _change(repo, {"conexus/hooks/x.py": "x\n"})
    assert _bare(_commands(_Run(repo)())["plugin"]) == [
        "uv run pytest -q -rf -p no:randomly tests/hooks"
    ]
    _change(repo, {"conexus/skills/x/SKILL.md": "x\n"})
    _git(repo, "tag", "after-hook")
    _change(repo, {"conexus/skills/x/SKILL.md": "y\n"})
    out = _Run(repo)("after-hook")
    assert _class_names(out) == ["plugin"]
    assert "plugin" not in _commands(out)
    assert out["code"] is True  # the drift ledger is a standing check


def test_a_workflow_change_gets_actionlint(repo: Path) -> None:
    _change(
        repo,
        {".github/workflows/ci.yml": "on: push\n", ".github/workflows/b.yaml": "x\n"},
    )
    out = _Run(repo)()
    assert _class_names(out) == ["workflow"]
    assert _commands(out)["workflow"] == [
        "( set -o pipefail; git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
        "':(glob).github/workflows/*.yml' ':(glob).github/workflows/*.yaml'"
        " | xargs -0 -r actionlint -- )"
    ]


def test_a_docs_only_change_gets_the_docs_readers_and_no_code_checks(
    repo: Path,
) -> None:
    _change(
        repo, {"docs/a.md": "a\n", "web/index.html": "<p/>\n", "CHANGELOG.md": "c\n"}
    )
    out = _Run(repo)()
    assert out["code"] is False
    assert _bare(_commands(out)["code"]) == ["scripts/build-gate-jar.sh"]
    assert _bare(_commands(out)["docs"]) == [
        "f=$(uv run python scripts/select_docs_reader_tests.py --repo .)"
        # CI's docs lane: -o addopts= so lint-marked docs scans run too, integration and slow stay out.
        " && printf '%s\\n' \"$f\" | xargs uv run pytest -q -rf -p no:randomly"
        " -o addopts= -m 'not integration and not slow'",
        BEAD_MANIFEST,
        FOLD_CHECK,  # CHANGELOG.md changed
    ]
    assert set(_commands(out)) == {"code", "docs"}


def test_docs_beside_code_still_get_the_docs_readers(repo: Path) -> None:
    _change(repo, {"docs/a.md": "a\n", "src/nexus/x.py": "x = 1\n"})
    out = _Run(repo)()
    assert _class_names(out) == ["python", "docs"]
    assert "docs" in _commands(out)
    assert "code" in _commands(out)


def test_an_unclassified_path_is_listed_and_still_code(repo: Path) -> None:
    _change(repo, {"data/blob.bin": "b\n"})
    out = _Run(repo)()
    assert out["unclassified"] == ["data/blob.bin"]
    assert out["code"] is True
    assert set(_commands(out)) == {"code"}


def test_counts_come_from_diff_stat(repo: Path) -> None:
    _change(repo, {"src/nexus/x.py": "a\nb\nc\n"})
    out = _Run(repo)()
    assert (out["files"], out["added"], out["deleted"], out["empty"]) == (
        1,
        3,
        0,
        False,
    )
    assert out["base"] == "base"


def test_an_empty_diff_says_so(repo: Path) -> None:
    out = _Run(repo)()
    assert (out["files"], out["empty"], out["code"]) == (0, True, False)
    assert not out["checks"]


# ---- the mirrors: CI's docs predicate and the drift-ledger paths --------------------

_CORPUS = [
    "docs/a.md",
    "docs/x.py",
    "docs/CHANGELOG.md",
    "web/index.html",
    "web/install.sh",
    "web/install.d/a",
    "web/sub/install.sh",
    "conexus/CHANGELOG.md",
    "README.md",
    "README.md.d/x.py",
    "CHANGELOG.md",
    "CHANGELOG.d/evil.py",
    "LICENSE",
    "LICENSE-vendor/code.py",
    "LICENSE.d/x",
    "AGENTS.md",
    "sub/README.md",
    "src/web/x.py",
    "src/nexus/x.py",
    "data/x",
    "tests/AGENTS.md",
    # Changelog fragments (nexus-3ceda.2): prose one level deep, code below that.
    "changelog/unreleased/nexus-x.md",
    "changelog/unreleased/README.md",
    "changelog/unreleased/sub/nexus-x.md",
    "changelog/unreleased/nexus-x.txt",
    "changelog/other.md",
]


def _ci_case() -> str:
    """The case statement of ci.yml's doc-only predicate, verbatim."""
    text = CI.read_text(encoding="utf-8")
    m = re.search(r'^(\s*)case "\$f" in\n.*?^\1esac\n', text, re.S | re.M)
    assert m is not None, "ci.yml's doc-only case statement moved"
    return m.group(0)


@pytest.mark.skipif(BASH is None, reason="needs bash")
@pytest.mark.parametrize("path", _CORPUS)
def test_the_docs_rows_agree_with_ci_on_every_path(repo: Path, path: str) -> None:
    script = f'docs_only=true\nf={path!r}\n{_ci_case()}echo "$docs_only"\n'
    ci_docs = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    _change(repo, {path: "x\n"})
    out = _Run(repo)()
    assert out["code"] is (ci_docs == "false"), path


def _drift_paths() -> list[str]:
    on = yaml.safe_load(DRIFT.read_text(encoding="utf-8"))
    triggers = on.get("on", on.get(True))
    return list(triggers["push"]["paths"])


@pytest.mark.parametrize("watched", _drift_paths())
def test_every_path_the_drift_ledger_watches_is_plugin(
    repo: Path, watched: str
) -> None:
    path = watched.replace("**", "x/y.md")
    _change(repo, {path: "x\n"})
    assert "plugin" in _class_names(_Run(repo)()), watched


@pytest.mark.parametrize("near", ["conexus/README.md", "sn/x.md", "conexus/hooksx/a"])
def test_a_near_miss_is_not_plugin(repo: Path, near: str) -> None:
    _change(repo, {near: "x\n"})
    assert "plugin" not in _class_names(_Run(repo)())


# ---- the commands as a shell runs them ------------------------------------------


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_ruff_gets_exactly_the_live_src_files_after_its_dashes(
    repo: Path, stubs: Path
) -> None:
    _change(repo, {"src/gone.py": "x\n"})
    _git(repo, "tag", "-f", "base")
    odd = [
        "src/-h.py",
        "src/a b.py",
        "src/it's.py",
        "src/$(touch pwned).py",
        "src/semi;colon.py",
    ]
    _change(
        repo, {"src/gone.py": None, **{p: "x = 1\n" for p in odd}, "src/keep.py": "x\n"}
    )
    lines = _shell(repo, stubs, _command(_Run(repo)(), "ruff check"))
    assert sorted(_operands(lines)) == sorted([*odd, "src/keep.py"])
    assert not (repo / "pwned").exists()


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_a_deleted_test_file_never_reaches_pytest(repo: Path, stubs: Path) -> None:
    _change(repo, {"tests/test_gone.py": "x\n"})
    _git(repo, "tag", "-f", "base")
    _change(repo, {"tests/test_gone.py": None, "tests/deep/test_new.py": "x\n"})
    lines = _shell(repo, stubs, _command(_Run(repo)(), "the changed test files"))
    # R13: the default-loop guard rides along in the same invocation.
    assert _operands(lines) == [
        "tests/deep/test_new.py",
        "tests/test_mode_declarations_are_explicit.py",
    ]


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_only_deletions_run_no_tool(repo: Path, stubs: Path) -> None:
    _change(repo, {"src/gone.py": "x\n"})
    _git(repo, "tag", "-f", "base")
    _change(repo, {"src/gone.py": None})
    assert _shell(repo, stubs, _command(_Run(repo)(), "ruff check")) == []


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_actionlint_gets_the_live_workflows(repo: Path, stubs: Path) -> None:
    _change(repo, {".github/workflows/a.yml": "x\n", ".github/workflows/b.yaml": "x\n"})
    lines = _shell(repo, stubs, _command(_Run(repo)(), "actionlint"))
    assert lines[0] == "actionlint"
    assert sorted(_operands(lines)) == [
        ".github/workflows/a.yml",
        ".github/workflows/b.yaml",
    ]


@pytest.mark.skipif(BASH is None, reason="needs bash")
@pytest.mark.parametrize(
    "base", ["v$(touch${IFS}pwned)", "it's", "a&b", "semi;x", "back`tick`", "x|y>z"]
)
def test_a_base_with_shell_characters_reaches_the_tools_intact(
    repo: Path, stubs: Path, base: str
) -> None:
    _git(repo, "tag", base, "base")
    _change(repo, {"src/x.py": "x = 1\n"})
    out = _Run(repo)(base)
    tenant = _shell(repo, stubs, _command(out, "tests that mint a tenant"))
    assert f"<--base={base}>" in tenant
    assert _operands(_shell(repo, stubs, _command(out, "ruff check"))) == ["src/x.py"]
    assert not (repo / "pwned").exists()


def test_a_bad_base_fails_the_run_with_the_verbs_error(repo: Path) -> None:
    with pytest.raises(lua54.LuaError, match="does not name a commit"):
        _Run(repo)("no-such-ref")


def test_it_runs_nothing_but_the_two_git_verbs(repo: Path) -> None:
    _change(repo, {"src/nexus/x.py": "x = 1\n", "service/A.java": "x\n"})
    run = _Run(repo)
    run()
    assert run.calls == ["git.diff_names", "git.diff_stat"]


@pytest.mark.parametrize(
    ("path", "cls"),
    [
        ("uv.lock", "python"),
        ("pyproject.toml", "python"),
        (".github/workflows/only.yaml", "workflow"),
        (".github/workflows/only.yml", "workflow"),
    ],
)
def test_each_single_file_row_names_its_class(repo: Path, path: str, cls: str) -> None:
    _change(repo, {path: "x\n"})
    assert _class_names(_Run(repo)()) == [cls]


@pytest.mark.parametrize("shell", _SHELLS)
def test_the_docs_selection_reaches_pytest_as_separate_files(
    repo: Path, stubs: Path, shell: str
) -> None:
    _change(repo, {"docs/a.md": "x\n"})
    lines = _shell(
        repo, stubs, _command(_Run(repo)(), "the tests that read docs/"), shell
    )
    # xargs splits on blanks, so "b c.py" arrives as two words: the selectors
    # print repo paths, none of which has a blank.
    assert _args(lines)[:2] == ["run", "pytest"]
    assert _args(lines)[-3:] == ["tests/a.py", "tests/b", "c.py"]


@pytest.mark.parametrize("shell", _SHELLS)
def test_the_integration_selection_reaches_pytest_as_separate_files(
    repo: Path, stubs: Path, shell: str
) -> None:
    _change(repo, {"src/x.py": "x = 1\n"})
    command = _command(_Run(repo)(), "the integration tests the change affects")
    lines = _shell(repo, stubs, command, shell)
    args = _args(lines)
    assert lines[0] == "lab-remote"
    assert args[-3:] == ["tests/a.py", "tests/b", "c.py"]
    assert MARK.strip("'") in args


def test_ci_and_preflight_name_the_same_integration_mark() -> None:
    m = re.search(r'INTEGRATION_MARK_EXPR: "([^"]+)"', CI.read_text(encoding="utf-8"))
    assert m is not None
    assert MARK == f"'{m.group(1)}'"


@pytest.mark.parametrize(
    "script",
    [
        "scripts/select_affected_integration_tests.py",
        "scripts/assert_integration_affected_ran.py",
    ],
)
def test_a_change_to_the_selector_selects_every_integration_file(
    repo: Path, script: str
) -> None:
    _change(repo, {script: "x\n"})
    cmd = _inner(_command(_Run(repo)(), "the integration tests the change affects"))
    assert cmd.startswith(
        "f=$(set -o pipefail; uv run python scripts/select_affected_integration_tests.py --repo . --all) && "
    )


def test_ruff_is_the_version_ci_pins() -> None:
    assert f'local RUFF = "{_ci_ruff()}"' in SOURCE.decode("ascii")


def test_the_real_run_equals_the_harness_for_every_row(
    root_on: Path, workers: list[str]
) -> None:
    _change(root_on, EVERYTHING)
    real = run_procedure("preflight", {"base": "base"})
    assert real == _Run(root_on)()
    assert isinstance(real, dict)
    assert len(real["checks"]) >= 20

# ---- through the real run path (run_script, the worker, the library root) -----------
#
# How the run finds its repo: run_script probes ``os.getcwd()`` (``header.probe_repo``), so
# the tests chdir into the temp repo; the probe's top is the verbs' ``repo_root`` and the
# top's ``.nexus/procedures`` is the repo root of the library, which runs only when
# NX_PROCEDURES_REPO_ROOT is exactly "1". The copy of preflight.lua in the temp repo is
# untracked, so it is never part of the diff the run reads.
#
# The root-off tests pin RDR-242 S5's default (off unless exactly "1"). The nexus project
# settings now export NX_PROCEDURES_REPO_ROOT=1, so every fixture sets or clears the
# variable itself and no test depends on the ambient value.

CASES: dict[str, dict[str, str | None]] = {
    "code": {"src/nexus/x.py": "x = 1\n", "tests/sub/test_x.py": "def test(): pass\n"},
    "docs-only": {"docs/a.md": "# a\n", "README.md": "changed\n"},
    "empty": {},
}


CLASSES: dict[str, list[str]] = {"code": ["python"], "docs-only": ["docs"], "empty": []}

RUFF_ON_SRC = (
    "( set -o pipefail; git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
    f"':(glob)src/**/*.py' | xargs -0 -r uvx {_ci_ruff()} check --force-exclude -- )"
)

#: Every git a preflight run spawns, in order: the repo probe, then git.diff_names and
#: git.diff_stat (each resolves the base, then diffs). All read-only.
PREFLIGHT_GIT = ["rev-parse", "rev-parse", "diff", "rev-parse", "diff"]
READ_ONLY_GIT = {"rev-parse", "merge-base", "diff", "log", "show"}


@pytest.fixture
def procedure_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The temp repo holding the real script under .nexus/procedures, as the cwd, root OFF."""
    dest = repo / ".nexus" / "procedures"
    dest.mkdir(parents=True)
    shutil.copy(SCRIPT, dest / "preflight.lua")
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def root_on(procedure_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    return procedure_repo


@pytest.fixture
def workers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of the workers run_script started (a spy on the supervisor)."""
    started: list[str] = []
    real = run_mod.supervisor.supervise

    def spy(request, **kw):  # type: ignore[no-untyped-def]
        started.append(request["name"])
        return real(request, **kw)

    monkeypatch.setattr(run_mod.supervisor, "supervise", spy)
    return started


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The argv of every process the run starts: the worker (``supervisor.start_worker``) and
    the git calls of the repo probe and the verbs (``bounded_subprocess.run_bounded``). The spy
    is installed as those two modules' own ``subprocess``, never on the stdlib module
    (tests/test_module_seam.py)."""
    seen: list[list[str]] = []

    class Spy(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, args, *a, **kw):  # type: ignore[no-untyped-def]
            seen.append([str(x) for x in args])
            super().__init__(args, *a, **kw)

    setattr_in(monkeypatch, [bounded_subprocess, supervisor], "subprocess.Popen", Spy)
    return seen


def _git_subcommand(argv: list[str]) -> str:
    """The subcommand of ``git [-c k=v | -C dir]... <sub> ...``."""
    rest = argv[1:]
    while rest and rest[0] in ("-c", "-C"):
        rest = rest[2:]
    return rest[0]


def _apply(repo: Path, case: str) -> None:
    if CASES[case]:
        _change(repo, CASES[case])


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_real_run_equals_the_harness_field_for_field(
    root_on: Path, workers: list[str], case: str
) -> None:
    _apply(root_on, case)
    real = run_procedure("preflight", {"base": "base"})
    assert workers == ["preflight"], "the worker did not run"
    assert real == _Run(root_on)()
    assert isinstance(real, dict)
    assert real["empty"] is (case == "empty")
    # The harness and the real run read the same script, so equality cannot catch a broken
    # script by itself; these pin the real result to what the class table must say.
    assert bool(real["checks"]) is (case != "empty")  # not an empty-equals-empty pass
    assert real["code"] is (case == "code")
    assert _class_names(real) == CLASSES[case]
    if case == "code":
        assert _commands(real)["python"][0] == RUFF_ON_SRC
    if case == "empty":
        # An empty list comes back from the worker as {} (src/nexus/lua_runtime/runtime.py,
        # module docstring near line 56: "an empty list or object is {}"), and the harness
        # reads it the same way. Pinned here until the result walk says otherwise.
        assert real["checks"] == {}
        assert real["classes"] == {}
        assert real["unclassified"] == {}


def test_the_real_run_starts_the_worker_and_runs_nothing_but_read_only_git(
    root_on: Path, spawns: list[list[str]]
) -> None:
    """RDR-240 Test Plan, Preflight: the runner executes none of the commands it returns."""
    _apply(root_on, "code")
    spawns.clear()  # the fixture's own git add and commit are not the run's
    out = run_procedure("preflight", {"base": "base"})
    assert isinstance(out, dict) and out["checks"]
    others = [a for a in spawns if a[0] != "git"]
    assert len(others) == 1 and others[0][0] == sys.executable, others
    git = [a for a in spawns if a[0] == "git"]
    # The repo probe (rev-parse), then each of the two granted verbs: it resolves the
    # base (rev-parse) and reads the diff (diff).
    assert [_git_subcommand(a) for a in git] == PREFLIGHT_GIT, git
    assert set(PREFLIGHT_GIT) <= READ_ONLY_GIT


def test_both_front_doors_return_identical_json(root_on: Path) -> None:
    _apply(root_on, "code")
    args = {"base": "base"}
    via_mcp = core.procedure_run(name="preflight", args=args)
    cli = CliRunner().invoke(main, ["run", "preflight", "--args-file", "-"], input=json.dumps(args))
    assert cli.exit_code == 0, cli.output
    assert json.loads(str(via_mcp)) == json.loads(cli.stdout)
    assert json.loads(cli.stdout) == _Run(root_on)()
    assert json.loads(cli.stdout)["checks"]


@pytest.mark.parametrize("value", [None, "true"])
def test_with_the_root_off_the_run_is_refused_through_every_door(
    procedure_repo: Path,
    workers: list[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is not None:
        monkeypatch.setenv(library.REPO_ROOT_ENV, value)
    _apply(procedure_repo, "code")
    prefix = "name: repo_root_off: "

    with pytest.raises(ScriptError) as refused:
        run_procedure("preflight", {"base": "base"})
    assert refused.value.kind == "name"
    assert refused.value.detail.startswith("repo_root_off: ")
    assert library.REPO_ROOT_ENV in refused.value.detail

    via_mcp = str(core.procedure_run(name="preflight", args={"base": "base"}))
    assert via_mcp.startswith(prefix) and library.REPO_ROOT_ENV in via_mcp

    cli = CliRunner().invoke(main, ["run", "preflight", "--args-file", "-"], input='{"base": "base"}')
    assert cli.exit_code == 1
    assert cli.stderr.strip() == via_mcp == str(refused.value)
    assert cli.stdout == ""
    assert workers == [], "a refused run started a worker"


def test_the_nexus_project_settings_turn_on_the_switch_the_library_reads() -> None:
    """Sam's RDR-242 S5 answer (ask baton/s5-repo-root, option a): the root is off by
    default and the nexus repo turns it on through its project settings env. A typo in
    either name would leave preflight off in this repo with every other test green."""
    assert library.REPO_ROOT_ENV == "NX_PROCEDURES_REPO_ROOT"
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"][library.REPO_ROOT_ENV] == "1"


def test_bash_is_present_so_the_shell_legs_do_not_all_skip() -> None:
    """Max-skip guard: the stub-tool tests above skip when there is no bash, which would
    leave quoting and the deleted-path filter untested. bash is required, so its absence
    fails here. zsh is only an extra parametrized leg of the docs and integration selection
    tests (the commands are written for bash; zsh adds a second reading of the quoting), so
    a host without zsh may skip that leg."""
    assert BASH is not None
