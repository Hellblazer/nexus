# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness the preflight tests share (nexus-qo7ie.32, nexus-qo7ie.36).

``_Run`` loads ``.nexus/procedures/preflight.lua`` in a test-local environment shaped
like RDR-242 Technical Design 7's (the static check's own allowlists, the two granted
verbs and nothing else; the pattern of tests/test_procedure_research_add.py), over a
temp repository. ``_run`` runs an emitted command in bash or zsh with stub tools on
PATH. The tests: tests/test_procedure_preflight.py (the classes, the mirrors, the real run
path), tests/test_procedure_preflight_rows.py (the rows a diff's paths decide) and
tests/test_procedure_preflight_contract.py (the result contract, the collect-only pin and
the real-tool runs) and tests/test_procedure_preflight_enumerations.py (the rows for the
hand-kept enumerations: MCP surface, tuple templates, changesets). Split from one file at the 1000-line cap (scripts/file_size_ratchet.py).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import lupa.lua54 as lua54
import pytest

from nexus.lua_runtime import header as hd
from nexus.lua_runtime import static_check as sc
from nexus.lua_runtime import verbs_git
from nexus.lua_runtime.verb_types import VerbContext
from tests._tmp_root import child_env_with_tmpdir


REPO_ROOT = Path(hd.__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".nexus" / "procedures" / "preflight.lua"
SOURCE = SCRIPT.read_bytes()
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DRIFT = REPO_ROOT / ".github" / "workflows" / "plugin-drift-ledger.yml"
BASH = shutil.which("bash")
ZSH = shutil.which("zsh")
MARK = "'integration and not lived_in and not keyed_voyage and not mandatory_regression_pin and not pg_bundle_artifact'"
#: R37: the two lint tests that resolve the bead ids cited under docs/ (Addendum
#: 2026-09-18). The manifest is not required to hold every live bead, only the cited
#: ones (tests/test_docs_reference_rot.py, test_bead_id_manifest_is_fresh_where_bd_is),
#: so a refresh-then-clean-diff check reds on every bead filed since the last refresh.
BEAD_MANIFEST = (
    "uv run pytest -q -rf -p no:randomly --color=no -m 'not integration and not slow'"
    " -k 'cited_bead_ids_resolve or bead_id_manifest_is_fresh_where_bd_is' tests/test_docs_reference_rot.py"
)
#: What a gated command runs after a clean summary: its log is not kept.
PASSING_LOG_REMOVAL = ' && { rm -f "$log"; true; }'
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


#: The variables by which an outer storage-touch guard adopts a nested pytest session as its
#: child (tests/_storage_touch_guard.py, _INHERITED_ENV).
_GUARD_LINK = ("NX_STORAGE_TOUCH_RUN_ID", "NX_STORAGE_TOUCH_GUARD_PID", "NX_STORAGE_TOUCH_CHILD_LOG")


def collect_env() -> dict[str, str]:
    """The environment for a ``pytest --collect-only`` child: its own session, not this one's.

    A collect-only listing runs no test; it imports the collected modules, and what a module
    builds at import (tests/scripts parametrizes ManagedService* errors) is collection, which no
    in-process session is ever charged with. Inheriting PYTEST_CURRENT_TEST charged it to the
    calling test; inheriting the guard's link reported it as a late child touch with no test
    running, which develop CI's shard gate fails (window 57, run 42737391383). With neither, the
    child is a separately guarded session and applies the guard's own collection rule."""
    env = {k: v for k, v in _env().items() if not k.startswith("PYTEST_") and k not in _GUARD_LINK}
    env["NX_TEST_T2_SUBSTRATE"] = "none"
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
            # It reads its stdin to the end first, as a real selector does: a pipeline under
            # pipefail must not take a SIGPIPE from the diff writer for a failure.
            'case "$*" in *select_*_tests.py*) cat >/dev/null; echo "${STUB_SELECT-tests/a.py tests/b c.py}"; exit 0;; esac\n'
            # STUB_NUL puts a NUL byte in the output first, as a test's raw output can: a
            # plain grep then reads the log as binary and prints no matching line.
            '[ -n "${STUB_NUL-}" ] && printf "raw\\000out\\n"\n'
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
    env_extra: dict[str, str | None] | None = None,
    keep_passing_log: bool = True,
) -> tuple[int, list[str]]:
    """Run an emitted command; returns its exit code and the lines it showed.

    A gated pytest command (the result contract, R44/R55) keeps its output in a
    log made by ``mktemp``. ``TMPDIR`` points at a directory of the test's own, and
    the log's lines come first, then the command's own stdout. A passing run removes
    its log; *keep_passing_log* (the default) drops that removal, so a test can read
    what a stub tool printed. The removal itself is pinned with it False.
    """
    assert shell is not None
    if keep_passing_log:
        assert command.count(PASSING_LOG_REMOVAL) <= 1, command
        command = command.replace(PASSING_LOG_REMOVAL, "")
    logs = stubs.parent / f"logs-{len(list(stubs.parent.iterdir()))}"
    logs.mkdir()
    env = child_env_with_tmpdir(_env(), logs)
    env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
    for key, value in (env_extra or {}).items():
        if value is None:  # None unsets: a nested pytest must not inherit the outer run's
            env.pop(key, None)
        else:
            env[key] = value
    proc = subprocess.run(
        [shell, "-c", command],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    shown = [
        line
        for log in sorted(logs.glob("preflight.*"))  # a nested pytest makes its own directory here
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


_SHELLS = [
    pytest.param(
        BASH, id="bash", marks=pytest.mark.skipif(BASH is None, reason="no bash")
    ),
    pytest.param(ZSH, id="zsh", marks=pytest.mark.skipif(ZSH is None, reason="no zsh")),
]


def _args(lines: list[str]) -> list[str]:
    return [line[1:-1] for line in lines if line.startswith("<") and line.endswith(">")]


def _ci_ruff() -> str:
    """The ruff pin of ci.yml's lint job, ``ruff@<version>``."""
    m = re.search(r"uvx (ruff@[0-9.]+) check src/", CI.read_text(encoding="utf-8"))
    assert m is not None, "ci.yml's ruff lint job moved"
    return m.group(1)


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
    # the mcp-surface and tuple-template classes, so the all-shell contract test reaches them
    "src/nexus/mcp/core.py": "x = 1\n",
    "service/src/main/resources/tuples/templates/aspects.yaml": "x\n",
    "conexus/hooks/hooks.json": "{}\n",
    "docs/a.md": "a\n",
}


def _rows(out: dict, prefix: str) -> list[dict]:
    return [c for c in out["checks"] or [] if c["check"].startswith(prefix)]


def _fires(repo: Path, paths: list[str], prefix: str) -> list[dict]:
    _change(repo, {p: "x\n" for p in paths})
    return _rows(_Run(repo)(), prefix)
