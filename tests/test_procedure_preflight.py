# SPDX-License-Identifier: AGPL-3.0-or-later
"""The preflight procedure (RDR-240 Technical Design 8, nexus-qo7ie.32).

Until the runtime of nexus-mdbds.5 and run_script of nexus-mdbds.9 land, the
script runs here in a test-local harness shaped like RDR-242 Technical Design
7's environment: the static check's own allowlists (the base functions;
string without dump, its metatable's __index the reduced table; table, math,
utf8), the two granted verbs and nothing else, loaded in mode "t" with the
arguments as the main chunk's vararg (contract v6), the pattern of
tests/test_procedure_research_add.py. The verbs are the real ones over a temp
git repository. The header is read by ``header.parse`` and the static check
passes the file with those grants, as run_script will. The run_script-level
test waits for the runtime (recorded on the bead).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import lupa.lua54 as lua54
import pytest

from nexus.lua_runtime import header as hd
from nexus.lua_runtime import static_check as sc
from nexus.lua_runtime import verbs_git
from nexus.lua_runtime.verb_types import VerbContext

REPO_ROOT = Path(hd.__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".nexus" / "procedures" / "preflight.lua"
SOURCE = SCRIPT.read_bytes()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("r\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    return r


def _change(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")


def _py(value: object) -> object:
    """A lupa table as Python: a list when its keys are 1..n, else a dict."""
    if lua54.lua_type(value) != "table":
        return value
    items = dict(value.items())
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

    def env_names(self) -> set[str]:
        return set(dict(self._env().items()))

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
        return build(
            rt.table_from(sorted(sc.BASE_FUNCTIONS)),
            rt.table_from(
                {lib: rt.table_from(sorted(m)) for lib, m in sc.LIB_MEMBERS.items()}
            ),
            self._verb("diff_names"),
            self._verb("diff_stat"),
        )

    def __call__(self, base: str = "base") -> dict:
        load = self.rt.eval("load")
        loaded = load(self.source.decode("ascii"), "=preflight", "t", self._env())
        fn, err = loaded if isinstance(loaded, tuple) else (loaded, None)
        assert fn is not None, err
        out = _py(fn(self.rt.table_from({"base": base})))
        assert isinstance(out, dict)
        return out


def _commands(out: dict) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for c in out["checks"]:
        by.setdefault(c["class"], []).append(c["command"])
    return by


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
    assert SCRIPT.is_file()
    assert not (
        Path(hd.__file__).parent.parent / "procedures" / "preflight.lua"
    ).exists()


# ---- each change class --------------------------------------------------------


def test_a_python_change_gets_ruff_on_its_files_its_tests_and_the_code_checks(
    repo: Path,
) -> None:
    _change(
        repo, {"src/nexus/x.py": "x = 1\n", "tests/test_x.py": "def test(): pass\n"}
    )
    out = _Run(repo)()
    assert out["code"] is True
    assert [c["name"] for c in out["classes"]] == ["python"]
    cmds = _commands(out)
    assert cmds["python"] == [
        "uv run ruff check src/nexus/x.py tests/test_x.py",
        "uv run ruff format --check src/nexus/x.py tests/test_x.py",
        "uv run pytest -q -p no:randomly tests/test_x.py",
    ]
    code = cmds["code"]
    assert "lab-remote -- uv run pytest -q -p no:randomly -m lint -n auto" in code
    assert any("tests/daemon/test_lifecycle_gate.py" in c for c in code)
    assert "uv run scripts/tenant_delta.py --base base" in code
    assert "uv run scripts/windows_hand_test.py --check" in code
    assert (
        "git diff --name-only base...HEAD | uv run python "
        "scripts/select_affected_integration_tests.py --repo ."
    ) in code
    assert "docs" not in cmds


def test_an_engine_change_gets_the_gate_jar_and_the_engine_suite(repo: Path) -> None:
    _change(repo, {"service/src/main/java/A.java": "class A {}\n"})
    out = _Run(repo)()
    cmds = _commands(out)
    assert cmds["engine"] == [
        "scripts/build-gate-jar.sh",
        "lab-remote -- scripts/mvnw-leased.sh -f service/pom.xml verify",
    ]
    assert "schema" not in cmds
    assert out["code"] is True


def test_a_changeset_is_engine_and_schema(repo: Path) -> None:
    _change(repo, {"service/src/main/resources/db/changelog/x-001.xml": "<x/>\n"})
    out = _Run(repo)()
    assert [c["name"] for c in out["classes"]] == ["engine", "schema"]
    assert _commands(out)["schema"] == [
        "uv run pytest -q -p no:randomly -m lint tests/test_changelog_validate_precondition_lint.py"
    ]


def test_a_plugin_surface_change_gets_the_drift_ledger(repo: Path) -> None:
    _change(repo, {"conexus/skills/x/SKILL.md": "x\n"})
    out = _Run(repo)()
    assert _commands(out)["plugin"] == [
        "uv run pytest -q -p no:randomly tests/test_plugin_release_drift_ledger.py"
    ]
    assert out["code"] is True


def test_a_plugin_file_outside_the_watched_paths_is_not_plugin(repo: Path) -> None:
    _change(repo, {"conexus/README.md": "x\n"})
    out = _Run(repo)()
    assert "plugin" not in _commands(out)
    assert out["unclassified"] == ["conexus/README.md"]


def test_a_workflow_change_gets_actionlint_on_that_file(repo: Path) -> None:
    _change(repo, {".github/workflows/ci.yml": "on: push\n"})
    out = _Run(repo)()
    assert _commands(out)["workflow"] == ["actionlint .github/workflows/ci.yml"]


def test_a_docs_only_change_gets_the_docs_readers_and_no_code_checks(
    repo: Path,
) -> None:
    _change(
        repo, {"docs/a.md": "a\n", "web/index.html": "<p/>\n", "CHANGELOG.md": "c\n"}
    )
    out = _Run(repo)()
    assert out["code"] is False
    cmds = _commands(out)
    assert set(cmds) == {"docs"}
    assert cmds["docs"] == [
        "uv run pytest -q -p no:randomly $(uv run python scripts/select_docs_reader_tests.py --repo .)"
    ]


def test_web_install_is_code_as_ci_reads_it(repo: Path) -> None:
    _change(repo, {"web/install.sh": "echo\n"})
    out = _Run(repo)()
    assert out["code"] is True
    assert "docs" not in _commands(out)
    assert out["unclassified"] == ["web/install.sh"]


def test_docs_beside_code_take_the_code_checks_only(repo: Path) -> None:
    _change(repo, {"docs/a.md": "a\n", "src/nexus/x.py": "x = 1\n"})
    out = _Run(repo)()
    assert [c["name"] for c in out["classes"]] == ["python", "docs"]
    cmds = _commands(out)
    assert "docs" not in cmds
    assert "code" in cmds


def test_an_unclassified_path_is_listed_and_still_code(repo: Path) -> None:
    _change(repo, {"data/blob.bin": "b\n"})
    out = _Run(repo)()
    assert out["unclassified"] == ["data/blob.bin"]
    assert out["code"] is True
    assert set(_commands(out)) == {"code"}


def test_counts_come_from_diff_stat(repo: Path) -> None:
    _change(repo, {"src/nexus/x.py": "a\nb\nc\n"})
    out = _Run(repo)()
    assert (out["files"], out["added"], out["deleted"]) == (1, 3, 0)
    assert out["base"] == "base"


# ---- quoting, refusals, and running nothing ----------------------------------------


def test_a_path_a_shell_would_split_is_quoted(repo: Path) -> None:
    _change(repo, {"src/it's a.py": "x = 1\n"})
    out = _Run(repo)()
    assert _commands(out)["python"][0] == "uv run ruff check 'src/it'\\''s a.py'"


def test_a_base_with_shell_characters_is_quoted_in_every_command(repo: Path) -> None:
    _git(repo, "tag", "v$(x)", "base")
    _change(repo, {"src/nexus/x.py": "x = 1\n"})
    out = _Run(repo)("v$(x)")
    code = _commands(out)["code"]
    assert "uv run scripts/tenant_delta.py --base 'v$(x)'" in code
    assert any(c.startswith("git diff --name-only 'v$(x)...HEAD' | ") for c in code)


def test_a_bad_base_fails_the_run_with_the_verbs_error(repo: Path) -> None:
    with pytest.raises(lua54.LuaError, match="does not name a commit"):
        _Run(repo)("no-such-ref")


def test_it_runs_nothing_but_the_two_git_verbs(repo: Path) -> None:
    _change(repo, {"src/nexus/x.py": "x = 1\n", "service/A.java": "x\n"})
    run = _Run(repo)
    run()
    assert run.calls == ["git.diff_names", "git.diff_stat"]
    # The environment it ran in has no way to start a process.
    assert run.env_names().isdisjoint(
        {"os", "io", "require", "load", "dofile", "debug"}
    )


def test_an_empty_diff_needs_no_checks(repo: Path) -> None:
    out = _Run(repo)()
    assert out["files"] == 0
    assert out["checks"] in ([], {})
    assert out["code"] is False


def test_a_dependency_change_is_python_but_not_handed_to_ruff(repo: Path) -> None:
    _change(repo, {"pyproject.toml": "[project]\n", "uv.lock": "x\n"})
    out = _Run(repo)()
    assert [c["name"] for c in out["classes"]] == ["python"]
    cmds = _commands(out)
    assert "python" not in cmds
    assert "code" in cmds
