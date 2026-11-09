# SPDX-License-Identifier: AGPL-3.0-or-later
"""The preflight procedure (RDR-240 Technical Design 8, nexus-qo7ie.32).

Most of this file runs the script in a test-local harness (class ``_Run``)
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
import os
import re
import shutil
import subprocess
import sys
import time
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
from nexus.lua_runtime import supervisor, verbs_git
from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.runner import run_procedure
from nexus.lua_runtime.verb_types import VerbContext
from nexus.mcp import core
from tests._module_seam import setattr_in

REPO_ROOT = Path(hd.__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".nexus" / "procedures" / "preflight.lua"
SOURCE = SCRIPT.read_bytes()
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DRIFT = REPO_ROOT / ".github" / "workflows" / "plugin-drift-ledger.yml"
BASH = shutil.which("bash")
ZSH = shutil.which("zsh")
MARK = "'integration and not lived_in and not keyed_voyage and not mandatory_regression_pin'"
#: R37: the bead-id manifest refresh, then no diff against HEAD (Addendum 2026-09-18).
BEAD_MANIFEST = (
    "uv run python scripts/refresh_bead_id_manifest.py && "
    "{ git diff --quiet HEAD -- tests/fixtures/bead_ids.txt || "
    "{ echo 'the manifest changed: commit tests/fixtures/bead_ids.txt with the doc'; false; }; }"
)
#: CI's docs lane runs this when a fragment or a changelog changed (nexus-3ceda.2).
FOLD_CHECK = "uv run python scripts/changelog_fold.py check"
#: R33: the precondition of the lint bucket (feedback_test_runs_before_push, Third recurrence).
UNTRACKED = (
    'u=$(git ls-files --others --exclude-standard) && [ -z "$u" ] || '
    "{ printf 'untracked files, so the repo-wide lints skip them (git add first):\\n%s\\n' \"$u\"; false; }"
)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=_env()
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("r\n", encoding="utf-8")
    _git(r, "add", "README.md")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    return r


def _change(repo: Path, files: dict[str, str | None]) -> None:
    """Write each file (None deletes it) and commit."""
    for name, text in files.items():
        path = repo / name
        if text is None:
            _git(repo, "rm", "-q", "--", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", "change")


def _py(value: object) -> object:
    """A lupa table as Python: a list when its keys are 1..n, else a dict."""
    if lua54.lua_type(value) != "table":
        return value
    items = dict(value.items())  # type: ignore[attr-defined]
    if (
        items
        and all(isinstance(k, int) for k in items)
        and sorted(items) == list(range(1, len(items) + 1))
    ):
        return [_py(items[i]) for i in range(1, len(items) + 1)]
    return {k: _py(v) for k, v in items.items()}


class _Run:
    """One run in a sandbox-shaped environment holding only the two git verbs."""

    def __init__(self, repo: Path, source: bytes = SOURCE) -> None:
        self.ctx = VerbContext(repo_root=repo)
        self.source = source
        self.calls: list[str] = []
        self.rt = lua54.LuaRuntime(
            register_eval=False, register_builtins=False, unpack_returned_tuples=False
        )

    def _verb(self, name: str):
        fn = {"diff_names": verbs_git.diff_names, "diff_stat": verbs_git.diff_stat}[
            name
        ]

        def call(base: str) -> object:
            # As the dispatcher: a verb failure reaches the script as a Lua
            # error carrying the message, never as a Python exception.
            self.calls.append(f"git.{name}")
            try:
                value = fn(base, deadline=time.monotonic() + 30, ctx=self.ctx)
            except Exception as exc:  # noqa: BLE001 -- the dispatcher's own catch-all
                return self.rt.table_from({"ok": False, "error": str(exc)})
            return self.rt.table_from(
                {"ok": True, "value": self.rt.table_from(value, recursive=True)}
            )

        return call

    def _env(self):
        rt = self.rt
        build = rt.execute(
            """
            return function(base, libs, diff_names, diff_stat)
            local env = {}
            for _, name in ipairs(base) do env[name] = _G[name] end
            for lib, members in pairs(libs) do
              local t = {}
              for _, m in ipairs(members) do t[m] = _G[lib][m] end
              env[lib] = t
            end
            getmetatable("").__index = env.string
            local function verb(f)
              return function(b)
                local r = f(b)
                if not r.ok then error(r.error, 2) end
                return r.value
              end
            end
            env.git = {diff_names = verb(diff_names), diff_stat = verb(diff_stat)}
            return env
            end
            """
        )
        return build(  # type: ignore[operator]
            rt.table_from(sorted(sc.BASE_FUNCTIONS)),
            rt.table_from(
                {lib: rt.table_from(sorted(m)) for lib, m in sc.LIB_MEMBERS.items()}
            ),
            self._verb("diff_names"),
            self._verb("diff_stat"),
        )

    def __call__(self, base: str = "base") -> dict:
        load = self.rt.eval("load")
        loaded = load(self.source.decode("ascii"), "=preflight", "t", self._env())  # type: ignore[operator]
        fn, err = loaded if isinstance(loaded, tuple) else (loaded, None)
        assert fn is not None, err
        out = _py(fn(self.rt.table_from({"base": base})))
        assert isinstance(out, dict)
        return out


def _commands(out: dict) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for c in out["checks"] or []:
        by.setdefault(c["class"], []).append(c["command"])
    return by


def _command(out: dict, check_prefix: str) -> str:
    [cmd] = [c["command"] for c in out["checks"] if c["check"].startswith(check_prefix)]
    return cmd


def _class_names(out: dict) -> list[str]:
    return [c["name"] for c in out["classes"] or []]


# ---- running an emitted command with stub tools ----------------------------------


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """A PATH directory whose uv, uvx, actionlint and lab-remote print their arguments."""
    d = tmp_path / "stubs"
    d.mkdir()
    for tool in ("uv", "uvx", "actionlint", "lab-remote"):
        stub = d / tool
        stub.write_text(
            "#!/bin/sh\n"
            # A selector prints its file list on one line, as the real ones do
            # (STUB_SELECT overrides it, empty for "nothing selected").
            'case "$*" in *select_*_tests.py*) echo "${STUB_SELECT-tests/a.py tests/b c.py}"; exit 0;; esac\n'
            f'printf "%s\\n" "{tool}"\n'
            'for a in "$@"; do printf "<%s>\\n" "$a"; done\n'
            # A pytest run ends with its summary line and exit code, as pytest does
            # (STUB_SUMMARY and STUB_RC override them).
            'case "$*" in *pytest*) printf "%s\\n" "${STUB_SUMMARY-1 passed in 0.01s}"; exit "${STUB_RC-0}";; esac\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return d


def _run(
    repo: Path,
    stubs: Path,
    command: str,
    shell: str | None = BASH,
    env_extra: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    """Run an emitted command; returns its exit code and the lines it showed.

    A gated pytest command (the result contract, R44/R55) keeps its output in a
    log made by ``mktemp``. ``TMPDIR`` points at a directory of the test's own, and
    the log's lines come first, then the command's own stdout.
    """
    assert shell is not None
    logs = stubs.parent / f"logs-{len(list(stubs.parent.iterdir()))}"
    logs.mkdir()
    env = _env()
    env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
    env["TMPDIR"] = str(logs)
    env.update(env_extra or {})
    proc = subprocess.run(
        [shell, "-c", command],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    shown = [
        line
        for log in sorted(logs.iterdir())
        for line in log.read_text(encoding="utf-8").splitlines()
    ]
    return proc.returncode, shown + proc.stdout.splitlines()


def _shell(
    repo: Path, stubs: Path, command: str, shell: str | None = BASH
) -> list[str]:
    rc, lines = _run(repo, stubs, command, shell)
    assert rc == 0, lines
    return lines


def _inner(command: str) -> str:
    """The command a result-contract wrapper (R44, R55) runs. The wrapper itself is
    tested by what it does, in the section on the contract."""
    m = re.fullmatch(
        r'log=\$\(mktemp "\$\{TMPDIR:-/tmp\}/preflight\.XXXXXX"\) && \{ \{ (?P<c>.*?); \} >"\$log" 2>&1; rc=\$\?; s=.*',
        command,
        re.S,
    )
    assert m is not None, command
    return m.group("c")


def _bare(commands: list[str]) -> list[str]:
    return [_inner(c) if c.startswith("log=$(mktemp ") else c for c in commands]


def _operands(lines: list[str]) -> list[str]:
    """The arguments a stub saw after its `--`."""
    args = [line[1:-1] for line in lines if line.startswith("<") and line.endswith(">")]
    return args[args.index("--") + 1 :] if "--" in args else []


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
        "git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
        "':(glob)src/**/*.py' | xargs -0 -r uvx ruff@0.15.18 check --force-exclude --",
        # R13: the default-loop guard that judges only what its invocation collected
        # rides in the same invocation as the changed tests.
        "{ git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
        "':(glob)tests/**/test_*.py'; printf '%s\\0' "
        "tests/test_mode_declarations_are_explicit.py; } | "
        "xargs -0 -r uv run pytest -q -rf -p no:randomly --",
    ]
    assert _bare(_commands(out)["code"]) == [
        "scripts/build-gate-jar.sh",
        UNTRACKED,
        "lab-remote -- bash -c 'scripts/build-gate-jar.sh && uv run pytest -q -rf -p no:randomly -m lint -n auto'",
        "uv run pytest -q -rf -p no:randomly tests/test_plugin_release_drift_ledger.py tests/daemon/test_lifecycle_gate.py",
        "uv run scripts/tenant_delta.py --strict --base=base",
        "uv run scripts/windows_hand_test.py --check",
        "f=$(git diff --no-renames --name-only base...HEAD | uv run python "
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
        "git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
        "':(glob).github/workflows/*.yml' ':(glob).github/workflows/*.yaml'"
        " | xargs -0 -r actionlint --"
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


def test_a_procedure_change_runs_the_procedure_tests(repo: Path) -> None:
    """A shipped or repo Lua procedure is its own class, not unclassified: a
    change to one runs every procedure's tests (gate_record.lua was
    unclassified, so preflight never named its tests; nexus-qo7ie.34).
    From aba431241, with its command moved to the form the collect-only pin accepts."""
    _change(repo, {"src/nexus/procedures/x.lua": "return 1\n", ".nexus/procedures/y.lua": "return 2\n"})
    out = _Run(repo)()
    assert out["unclassified"] == {}
    assert _class_names(out) == ["procedure"]
    assert out["code"] is True
    assert _bare(_commands(out)["procedure"]) == [
        "uv run pytest -q -rf -p no:randomly -m 'not integration and not slow'"
        " tests/test_*procedure*.py tests/test_run_cmd.py"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "src/nexus/procedures/research_add.lua",
        ".nexus/procedures/preflight.lua",
        # The shipped procedures' table inputs (test_mcp_procedure_run builds the tool description
        # from the shipped headers; these two tables feed the procedures themselves).
        "src/nexus/tables/audit-tags.toml",
        "src/nexus/tables/rdr-research-vocab.toml",
    ],
)
def test_each_procedure_input_is_the_procedure_class(repo: Path, path: str) -> None:
    _change(repo, {path: "x\n"})
    out = _Run(repo)()
    assert _class_names(out) == ["procedure"], path
    assert out["unclassified"] == {}


@pytest.mark.parametrize(
    "near",
    [
        "src/nexus/procedures/sub/x.lua",
        ".nexus/procedures/sub/x.lua",
        "src/nexus/procedures/x.py",
        "src/nexus/procedures/README.md",
        ".nexus/other.lua",
        ".nexus/procedures/x.txt",
        "src/nexus/tables/other.toml",
        "src/nexus/tables/audit-tags.toml.bak",
    ],
)
def test_a_near_miss_is_not_the_procedure_class(repo: Path, near: str) -> None:
    _change(repo, {near: "x\n"})
    assert "procedure" not in _class_names(_Run(repo)()), near


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


_SHELLS = [
    pytest.param(
        BASH, id="bash", marks=pytest.mark.skipif(BASH is None, reason="no bash")
    ),
    pytest.param(ZSH, id="zsh", marks=pytest.mark.skipif(ZSH is None, reason="no zsh")),
]


def _args(lines: list[str]) -> list[str]:
    return [line[1:-1] for line in lines if line.startswith("<") and line.endswith(">")]


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
        "f=$(uv run python scripts/select_affected_integration_tests.py --repo . --all) && "
    )


def _ci_ruff() -> str:
    """The ruff pin of ci.yml's lint job, ``ruff@<version>``."""
    m = re.search(r"uvx (ruff@[0-9.]+) check src/", CI.read_text(encoding="utf-8"))
    assert m is not None, "ci.yml's ruff lint job moved"
    return m.group(1)


def test_ruff_is_the_version_ci_pins() -> None:
    assert f'local RUFF = "{_ci_ruff()}"' in SOURCE.decode("ascii")


# ---- the rows a diff's paths decide (nexus-qo7ie.36) ------------------------------------
#
# Source: the maintainer's memory file feedback_test_runs_before_push.md. Each test names the
# rule it pins by the census's number (T2 nexus/qo7ie.36-census-feedback_test_runs_before_push,
# R1..R74). A row is a check that fires on a path pattern; the tests say which paths fire it,
# which near-misses do not, and that the file it runs exists.

EVERYTHING: dict[str, str | None] = {
    "src/nexus/exporter.py": "x = 1\n",
    "src/nexus/db/t2/http_memory_store.py": "x = 1\n",
    "tests/sub/test_x.py": "def test(): pass\n",
    "tests/e2e/x.sh": "x\n",
    "service/src/main/java/A.java": "class A {}\n",
    "service/src/main/resources/db/changelog/x-001.xml": "<x/>\n",
    "conexus/hooks/hooks.json": "{}\n",
    "docs/a.md": "a\n",
}


def _rows(out: dict, prefix: str) -> list[dict]:
    return [c for c in out["checks"] or [] if c["check"].startswith(prefix)]


def _fires(repo: Path, paths: list[str], prefix: str) -> list[dict]:
    _change(repo, {p: "x\n" for p in paths})
    return _rows(_Run(repo)(), prefix)


@pytest.mark.parametrize(
    ("paths", "fires"),
    [
        (["src/nexus/x.py"], True),
        (["service/A.java"], True),
        (["tests/test_x.py"], False),
        (["scripts/s.py"], False),
        (["docs/a.md"], False),
        (["conexus/skills/x/SKILL.md"], False),
    ],
)
def test_r5_the_full_python_suite_follows_src_nexus_and_service(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the full Python suite")
    assert bool(rows) is fires
    if fires:
        [row] = rows
        assert (row["class"], row["heavy"]) == ("tripwire", True)
        # Once per tree (AGENTS.md rule 13): the text says when to skip it.
        assert "already has a green full run" in row["check"]
        inner = _inner(row["command"])
        assert inner.startswith("lab-remote -c lab-l --fetch ")
        assert "scripts/build-gate-jar.sh" in inner and "-n auto" in inner


def test_r5_the_full_suite_is_the_last_check(repo: Path) -> None:
    _change(repo, EVERYTHING)
    out = _Run(repo)()
    assert out["checks"][-1]["check"].startswith("the full Python suite")


@pytest.mark.parametrize(
    ("paths", "fires"),
    [
        (["service/A.java", "src/nexus/x.py"], True),
        (["service/A.java"], False),
        (["src/nexus/x.py"], False),
        (["service/A.java", "tests/test_x.py"], False),
    ],
)
def test_r7_the_wire_pairing_check_needs_both_halves(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the wire-contract pairing check")
    assert bool(rows) is fires
    if fires:
        [row] = rows
        assert row["command"] == "uv run python scripts/check_wire_contract_pairing.py"
        assert row["heavy"] is False
        # It scans commits, so it runs after the commit; the ledger bullet's shape.
        assert "after the commit" in row["check"]
        assert "docs/wire-contract-pending.md" in row["check"]


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/nexus/db/t2/http_memory_store.py", True),
        ("src/nexus/db/t2/http_taxonomy_store.py", True),
        ("src/nexus/db/http_scratch_store.py", True),
        ("src/nexus/db/t2/records.py", False),
        ("src/nexus/db/http_vector_client.py", False),
        ("tests/db/test_http_t2_store_parity.py", False),
    ],
)
def test_r8_the_store_parity_test_follows_the_http_stores(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the Http store parity test")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly tests/db/test_http_t2_store_parity.py"
        )


@pytest.mark.parametrize(
    ("path", "fires"),
    [("conexus/hooks/hooks.json", True), ("conexus/hooks/x.py", False), ("conexus/hooks.json", False)],
)
def test_r10_hooks_json_gets_the_sc8_upgrade_test(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the SC8 hooks.json test")
    assert bool(rows) is fires
    if fires:
        assert rows[0]["class"] == "plugin"
        assert _inner(rows[0]["command"]) == (
            "uv run pytest -q -rf -p no:randomly tests/test_upgrade_e2e.py -k SC8"
        )


@pytest.mark.parametrize(
    ("path", "fires"),
    [("src/nexus/x.py", True), ("src/nexus/catalog/y.py", True), ("tests/test_x.py", False), ("service/A.java", False)],
)
def test_r11_the_catalog_gate_census_follows_src_nexus(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the catalog gate census")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly tests/test_kmo9h_catalog_gate_census.py"
        )


def test_r13_the_cli_output_invariant_is_in_the_lint_bucket_not_the_changed_tests_run(
    repo: Path,
) -> None:
    """The source names two default-loop guards. The mode guard is still one (it judges
    ``request.session.items``, so it rides in the changed tests' invocation). The output
    invariant became a lint test (dc2a2cca0), which default addopts deselect: naming it in that
    invocation would run nothing, so the whole lint bucket carries it. This pin goes red the
    day it loses the marker, when it has to be appended again."""
    invariant = (REPO_ROOT / "tests/test_cli_runner_output_invariant.py").read_text("utf-8")
    assert re.search(r"^pytestmark = pytest\.mark\.lint$", invariant, re.M)
    mode = (REPO_ROOT / "tests/test_mode_declarations_are_explicit.py").read_text("utf-8")
    assert not re.search(r"^pytestmark\s*=.*lint", mode, re.M)
    _change(repo, {"tests/sub/test_x.py": "def test(): pass\n"})
    cmd = _inner(_command(_Run(repo)(), "the changed test files"))
    assert "test_mode_declarations_are_explicit.py" in cmd
    assert "test_cli_runner_output_invariant" not in cmd


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("tests/sub/test_x.py", True),
        ("tests/_tmp_root.py", True),
        ("tests/conftest.py", True),
        ("src/nexus/x.py", False),
        ("tests/e2e/x.sh", False),
    ],
)
def test_r31_the_tmp_root_scan_follows_any_test_file(
    repo: Path, path: str, fires: bool
) -> None:
    """The scan (test_every_tmpdir_override...) walks every tests/**/*.py and is not
    lint-marked, so any changed python file under tests/ can trip it."""
    rows = _fires(repo, [path], "the TMPDIR override scan")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly tests/test_tmp_root.py"
        )


def test_r33_untracked_files_are_checked_before_the_lint_bucket(
    repo: Path, stubs: Path
) -> None:
    _change(repo, {"src/nexus/x.py": "x = 1\n"})
    out = _Run(repo)()
    names = [c["check"] for c in out["checks"]]
    pre = next(i for i, n in enumerate(names) if n.startswith("precondition of the lint bucket"))
    assert names.index("the whole lint bucket") == pre + 1
    assert out["checks"][pre]["heavy"] is False
    cmd = out["checks"][pre]["command"]
    assert _run(repo, stubs, cmd)[0] == 0
    (repo / "new file.txt").write_text("x\n", encoding="utf-8")
    rc, lines = _run(repo, stubs, cmd)
    assert rc != 0 and any("new file.txt" in line for line in lines), lines
    _git(repo, "add", "--", "new file.txt")
    assert _run(repo, stubs, cmd)[0] == 0


@pytest.mark.parametrize(
    ("paths", "fires"),
    [(["docs/a.md"], True), (["docs/rdr/x.md", "src/nexus/x.py"], True), (["README.md"], False), (["web/index.html"], False)],
)
def test_r37_a_docs_change_refreshes_the_bead_manifest(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the bead ids cited in docs/")
    assert bool(rows) is fires
    if fires:
        assert (rows[0]["class"], rows[0]["heavy"]) == ("docs", False)
        assert rows[0]["command"] == BEAD_MANIFEST


def test_r37_the_refresh_must_leave_the_manifest_as_committed(
    repo: Path, stubs: Path, tmp_path: Path
) -> None:
    _change(repo, {"tests/fixtures/bead_ids.txt": "nexus-aaaaa\n"})
    _git(repo, "tag", "-f", "base")
    _change(repo, {"docs/a.md": "a\n"})
    cmd = _command(_Run(repo)(), "the bead ids cited in docs/")
    assert _run(repo, stubs, cmd)[0] == 0  # the refresh finds nothing new
    adder = tmp_path / "adder"
    adder.mkdir()
    uv = adder / "uv"
    uv.write_text("#!/bin/sh\nprintf 'nexus-bbbbb\\n' >> tests/fixtures/bead_ids.txt\n", encoding="utf-8")
    uv.chmod(0o755)
    rc, lines = _run(repo, adder, cmd)  # the refresh adds an id nobody committed
    assert rc != 0 and any("commit tests/fixtures/bead_ids.txt" in line for line in lines), lines
    _git(repo, "commit", "-qam", "manifest")
    assert _run(repo, stubs, cmd)[0] == 0


@pytest.mark.parametrize(
    ("path", "fires"),
    [("tests/e2e/x.sh", True), ("tests/e2e/sub/y.py", True), ("tests/scripts/test_x.py", False), ("tests/hooks/x.py", False)],
)
def test_r39_an_e2e_change_runs_tests_scripts(repo: Path, path: str, fires: bool) -> None:
    rows = _fires(repo, [path], "the e2e script census")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith("pytest -q -rf -p no:randomly tests/scripts")


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/nexus/exporter.py", True),
        ("src/nexus/pipeline_stages.py", True),
        ("src/nexus/chunk_batcher.py", True),
        ("src/nexus/catalog/recovery_bundle.py", True),
        ("src/nexus/commands/store.py", True),
        ("src/nexus/commands/memory.py", True),
        ("src/nexus/mcp/core.py", True),
        ("src/nexus/health.py", False),
        ("src/nexus/commands/other.py", False),
        ("tests/test_vw594_fence_coverage_gate.py", False),
    ],
)
def test_r62_the_fence_gate_follows_the_files_it_anchors(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the vw594 fence coverage gate")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly tests/test_vw594_fence_coverage_gate.py"
        )


# ---- three rows from the day's develop reds (coordinator, 2026-11-09) ------------------


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/nexus/commands/store.py", True),
        ("src/nexus/commands/sub/x.py", True),
        ("src/nexus/cli.py", True),
        ("src/nexus/cli_helpers.py", False),
        ("src/nexus/x.py", False),
        ("tests/test_cli_registration.py", False),
    ],
)
def test_the_cli_registration_test_follows_commands_and_cli(
    repo: Path, path: str, fires: bool
) -> None:
    """It checks every command module is wired into cli.py: a new or moved command module
    under src/nexus/commands/, or an edit to cli.py, can break it, and no other test names them."""
    rows = _fires(repo, [path], "the CLI registration test")
    assert bool(rows) is fires
    if fires:
        assert rows[0]["class"] == "python"
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly tests/test_cli_registration.py"
        )


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("service/src/main/resources/db/changelog/x-001.xml", True),
        ("service/src/main/java/dev/nexus/service/db/CatalogRepository.java", True),
        ("service/src/main/java/dev/nexus/service/db/CatalogHandler.java", False),
        ("service/A.java", False),
        ("src/nexus/x.py", False),
    ],
)
def test_the_collection_scoped_tables_parity_test_follows_changelogs_and_catalog_repository(
    repo: Path, path: str, fires: bool
) -> None:
    """It reads the changelogs against CatalogRepository.COLLECTION_SCOPED_TABLES through a
    live engine substrate (a physical_collection column went red at 939625129), so it is heavy
    and comes after the gate jar."""
    rows = _fires(repo, [path], "the collection-scoped tables schema parity test")
    assert bool(rows) is fires
    if fires:
        assert (rows[0]["class"], rows[0]["heavy"]) == ("schema", True)
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly tests/catalog/test_collection_scoped_tables_schema_parity.py"
        )


def test_a_catalog_repository_change_is_schema_but_needs_no_changeset_validation(
    repo: Path,
) -> None:
    _change(repo, {"service/src/main/java/dev/nexus/service/db/CatalogRepository.java": "x\n"})
    out = _Run(repo)()
    assert _class_names(out) == ["engine", "schema"]
    assert not _rows(out, "the changeset validation lint")


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("conexus/hooks/hooks.json", True),
        ("tests/e2e/hook-cli-skew/run.sh", True),
        ("conexus/hooks/x.py", False),
        ("tests/e2e/hook-cli/run.sh", False),
    ],
)
def test_the_hooks_json_pin_tests_follow_hooks_json_and_the_skew_harness(
    repo: Path, path: str, fires: bool
) -> None:
    """They pin hooks.json's entry count and form; the hook-floor red came from a new hook
    entry. Two are lint-marked, so the step selects whatever the mark."""
    rows = _fires(repo, [path], "the hooks.json pin tests")
    assert bool(rows) is fires
    if fires:
        assert rows[0]["class"] == "plugin"
        assert _inner(rows[0]["command"]) == (
            "uv run pytest -q -rf -p no:randomly -m 'not integration and not slow'"
            " tests/scripts/test_hook_harness_launcher_form.py"
            " tests/test_hook_budgets_pinned_to_hooks_json.py"
            " tests/test_hooks_json_verb_release_floor.py"
        )
    if path.startswith("tests/e2e/hook-cli-skew/"):
        assert "plugin" in _class_names(_Run(repo)())


@pytest.mark.parametrize(
    ("paths", "fires"),
    [
        (["changelog/unreleased/nexus-x.md"], True),
        (["CHANGELOG.md"], True),
        (["conexus/CHANGELOG.md"], True),
        (["docs/a.md"], False),
        (["changelog/unreleased/sub/nexus-x.md"], False),
    ],
)
def test_a_fragment_or_changelog_change_runs_the_fragment_check(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the changelog fragments")
    assert bool(rows) is fires
    if fires:
        assert (rows[0]["class"], rows[0]["heavy"]) == ("docs", False)
        assert rows[0]["command"] == FOLD_CHECK


def test_a_fragment_alone_is_a_docs_change_as_ci_reads_it(repo: Path) -> None:
    _change(repo, {"changelog/unreleased/nexus-x.md": "### Added\n- x\n"})
    out = _Run(repo)()
    assert (out["code"], _class_names(out), out["unclassified"]) == (False, ["docs"], {})


# ---- a step that names files must collect them under its own argv ------------------------

_PYTEST_AT = re.compile(r"uv run pytest (.*?)(?:; fi; \}|'$|$)", re.S)
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
    "git diff -z --no-renames --diff-filter=d --name-only base...HEAD -- "
    f"':(glob)src/**/*.py' | xargs -0 -r uvx {_ci_ruff()} check --force-exclude --"
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
