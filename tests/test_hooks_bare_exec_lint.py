# SPDX-License-Identifier: AGPL-3.0-or-later
"""No bare-name spawn and no ``shutil.which`` under ``conexus/hooks`` or
``conexus/launch`` (RDR-224 review finding A, nexus-f9bgu.36; RDR-228 added
``conexus/launch``, whose launcher starts every server and hook program).

``tests/test_nx_bare_exec_lint.py`` polices ``src/nexus``. The plugin's hook
scripts are a different tree with a worse exposure: Claude Code runs a hook with
the PROJECT as its working directory, and on Windows ``CreateProcess`` (and
``shutil.which``) search the current directory before ``PATH``. A ``git.exe``,
``uv.exe`` or ``nx-hook.exe`` planted in a cloned repository would run on every
hook, and the shim relays what ``nx-hook`` prints as the hook's own verdict.

The sanctioned spelling is ``_exec_path.which_off_cwd(name)`` (stdlib only, PATH
alone, returns an absolute path), passed as argv[0], or ``_cli_lookup.resolve``,
which tries the generation layout and then calls it. This scan flags, in every
``conexus/hooks/**/*.py`` and ``conexus/launch/**/*.py`` except the helper itself:

* a call to ``shutil.which`` or any ``which(...)``;
* a subprocess or ``os`` spawn whose argv starts with a bare string literal, or a
  name assigned one;
* any list or tuple literal that starts with a bare launcher name
  (``nx``, ``nx-hook``, ``uv``, ``git``, ``bd``, ``claude``, ...), which also
  catches ``run_cmd(["nx", "upgrade"])``-style wrappers. ``_ALLOWED`` carries each
  justified exception, and the non-vacuity tests pin that the wrapper behind it
  really resolves.

Non-vacuity: the allowed sites must all be found, every file that spawns anything
must go through the helper, and the detector is driven over seeded sources.
"""
from __future__ import annotations

import ast
import pathlib
import shutil

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
#: The plugin trees whose scripts a Claude application starts with the project,
#: or the plugin root, as the working directory.
SCANNED = ("conexus/hooks", "conexus/launch")
HELPER = "conexus/hooks/scripts/_exec_path.py"

_LAUNCHERS = frozenset({
    "nx", "nx-hook", "nx-session-end-launcher", "uv", "uvx", "git", "bd", "claude",
    "python", "python3", "schtasks", "bash", "sh", "pwsh", "powershell", "cmd",
})
_BARE = frozenset(_LAUNCHERS | {f"{n}.exe" for n in _LAUNCHERS})

_SPAWN_CALLEES = frozenset({
    "run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
    "system", "popen", "execv", "execvp", "execvpe", "execl", "execlp", "execle",
    "execlpe", "spawnv", "spawnvp", "spawnl", "spawnlp", "run_bounded",
})

_VLS = "conexus/hooks/scripts/version_lockstep_action.py"

#: Files that spawn, and need no resolution, because every spawn they make starts
#: ``sys.executable``: an absolute path the running interpreter already knows, so
#: there is no name for ``CreateProcess`` to search the working directory for.
#: ``tests/hooks/test_nx_standin.py`` pins that nothing else is spawned there.
_SPAWNS_ONLY_SYS_EXECUTABLE = {
    "conexus/launch/nx_standin.py": "starts nx_setup.py, beside it, with its own interpreter",
    "conexus/hooks/scripts/routing/_lib.py": (
        "starts its own routing_events poster (this file, --post-routing-event) with its own interpreter "
        "(nexus-njn1a.1); its one spawn is [sys.executable, this file, --post-routing-event]"
    ),
}

#: (path, enclosing function, name): each list literal that names a bare launcher
#: on purpose. ``run_cmd`` resolves ``cmd[0]`` through ``which_off_cwd`` itself;
#: ``test_run_cmd_resolves_argv0_before_it_spawns`` pins that.
_ALLOWED: frozenset[tuple[str, str, str]] = frozenset({
    (_VLS, "main", "nx"),
    (_VLS, "main", "uv"),
})


def _callee(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _bare(value: object) -> bool:
    """A string literal that names a launcher with no directory part."""
    return isinstance(value, str) and value.strip().lower() in _BARE


def _shell_head(node: ast.AST) -> str | None:
    text = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            text = first.value
    if not text:
        return None
    head = text.lstrip().split(None, 1)
    return head[0] if head and _bare(head[0]) else None


class _Finder(ast.NodeVisitor):
    def __init__(self, constants: set[str]) -> None:
        self._scope: list[str] = []
        self._constants = constants
        self.hits: list[tuple[str, str, int]] = []

    def _enter(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def _here(self) -> str:
        return self._scope[-1] if self._scope else "<module>"

    def _argv_head(self, first: ast.AST, line: int) -> None:
        if isinstance(first, ast.Constant) and _bare(first.value):
            self.hits.append((self._here(), str(first.value), line))
        elif isinstance(first, ast.Name) and first.id in self._constants:
            self.hits.append((self._here(), first.id, line))

    def _seq(self, node: ast.List | ast.Tuple) -> None:
        if node.elts:
            self._argv_head(node.elts[0], node.lineno)

    def visit_List(self, node: ast.List) -> None:
        self._seq(node)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._seq(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee(node)
        if name == "which":
            self.hits.append((self._here(), "which()", node.lineno))
        if name in _SPAWN_CALLEES and node.args:
            arg = node.args[0]
            shell = any(
                kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
            )
            if isinstance(arg, ast.Constant) and _bare(arg.value):
                self.hits.append((self._here(), str(arg.value), node.lineno))
            elif shell or name in {"system", "popen"}:
                head = _shell_head(arg)
                if head is not None:
                    self.hits.append((self._here(), head, node.lineno))
        self.generic_visit(node)


def _bare_constants(tree: ast.Module) -> set[str]:
    """Names assigned a bare launcher literal anywhere in the file."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and _bare(node.value.value):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and _bare(node.value.value)
        ):
            out.add(node.target.id)
    return out


def _hits_in(source: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(source)
    finder = _Finder(_bare_constants(tree))
    finder.visit(tree)
    return finder.hits


def _sources(repo_root: pathlib.Path = REPO_ROOT) -> list[tuple[str, pathlib.Path]]:
    """Every scanned script under *repo_root*, as (repo-relative path, file)."""
    out: list[tuple[str, pathlib.Path]] = []
    for sub in SCANNED:
        for path in sorted((repo_root / sub).rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            if rel != HELPER:
                out.append((rel, path))
    return out


def _scan(repo_root: pathlib.Path = REPO_ROOT) -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for rel, path in _sources(repo_root):
        for scope, name, line in _hits_in(path.read_text(encoding="utf-8")):
            found.setdefault((rel, scope, name), []).append(line)
    return found


def test_no_hook_script_spawns_a_bare_name_or_calls_which() -> None:
    found = _scan()
    assert set(_ALLOWED) <= set(found), (
        "the scan did not find an allowed bare launcher; the detector is broken or "
        f"the site moved. Missing: {sorted(set(_ALLOWED) - set(found))}"
    )
    stray = {k: v for k, v in found.items() if k not in _ALLOWED}
    assert not stray, (
        "a conexus hook script names a bare executable or calls which(); on Windows "
        "that searches the project's cwd first. Resolve it with "
        "_exec_path.which_off_cwd and spawn the absolute path: "
        + ", ".join(f"{p}:{lines} in {s} ({n!r})" for (p, s, n), lines in sorted(stray.items()))
    )


def _spawning_files() -> set[str]:
    """Files that import ``subprocess`` (or use an ``os`` exec/spawn) AND call a
    spawn-shaped function with an argument."""
    out: set[str] = set()
    for rel, path in _sources():
        src = path.read_text(encoding="utf-8")
        if not ("import subprocess" in src or "os.exec" in src or "os.spawn" in src):
            continue
        if any(
            isinstance(n, ast.Call) and _callee(n) in _SPAWN_CALLEES and n.args
            for n in ast.walk(ast.parse(src))
        ):
            out.add(rel)
    return out


def test_every_file_that_spawns_resolves_through_the_helper() -> None:
    spawners = _spawning_files()
    expected = {
        "conexus/hooks/scripts/_interpreter.py",
        "conexus/hooks/scripts/nx_hook_shim.py",
        _VLS,
        "conexus/hooks/scripts/version_lockstep_hook.py",
        "conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py",
        "conexus/launch/nx_launch.py",
    }
    assert expected <= spawners, f"the spawner census lost a known spawner: {sorted(expected - spawners)}"
    assert set(_SPAWNS_ONLY_SYS_EXECUTABLE) <= spawners, "an exempted spawner no longer spawns; drop its exemption"
    for rel in sorted(spawners - set(_SPAWNS_ONLY_SYS_EXECUTABLE)):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "which_off_cwd" in src or "_cli_lookup.resolve" in src, (
            f"{rel} spawns a process but never resolves through _exec_path.which_off_cwd "
            "or _cli_lookup.resolve"
        )


def test_every_scanned_tree_holds_a_script() -> None:
    for sub in SCANNED:
        assert any(rel.startswith(sub + "/") for rel, _ in _sources()), f"{sub} holds no script; the scan examined nothing there"


def test_a_bare_spawn_planted_under_conexus_launch_is_caught(tmp_path: pathlib.Path) -> None:
    """The scan reaches ``conexus/launch``: a copy of the tree with one bare-name
    spawn added to the launcher is reported, and the untouched copy is not."""
    copy = tmp_path / "conexus" / "launch"
    shutil.copytree(REPO_ROOT / "conexus" / "launch", copy, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "conexus" / "hooks").mkdir()
    assert not _scan(tmp_path), "the unmodified launcher tree already trips the scan"
    launcher = copy / "nx_launch.py"
    launcher.write_text(
        launcher.read_text(encoding="utf-8")
        + '\n\ndef _planted():\n    import subprocess\n    subprocess.run(["nx-hook", "preflight"])\n',
        encoding="utf-8",
    )
    found = _scan(tmp_path)
    assert list(found) == [("conexus/launch/nx_launch.py", "_planted", "nx-hook")], found


def test_run_cmd_resolves_argv0_before_it_spawns() -> None:
    tree = ast.parse((REPO_ROOT / _VLS).read_text(encoding="utf-8"))
    run_cmd = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_cmd")
    resolve = [
        n.lineno for n in ast.walk(run_cmd)
        if isinstance(n, ast.Call) and _callee(n) == "which_off_cwd"
    ]
    spawn = [
        n.lineno for n in ast.walk(run_cmd)
        if isinstance(n, ast.Call) and _callee(n) == "run" and isinstance(n.func, ast.Attribute)
    ]
    assert resolve and spawn, "run_cmd lost its resolver or its spawn; the allowed sites are no longer covered"
    assert min(resolve) < min(spawn)


def test_the_detector_sees_every_shape_it_claims_to_police() -> None:
    # argv lists, tuples, a constant standing in for the literal
    assert _hits_in('def f():\n    return ["nx-hook", "x"]\n') == [("f", "nx-hook", 2)]
    assert _hits_in('X = ("uv", "run")\n') == [("<module>", "uv", 1)]
    assert _hits_in('_G = "git"\ndef f():\n    return [_G, "log"]\n') == [("f", "_G", 3)]
    # a direct spawn, a shell string (plain and f-string), an os.system call
    assert _hits_in('import subprocess\ndef f():\n    subprocess.run("git")\n') == [("f", "git", 3)]
    assert _hits_in('import subprocess\ndef f():\n    subprocess.run("git log", shell=True)\n') == [("f", "git", 3)]
    assert _hits_in('import subprocess\ndef f(a):\n    subprocess.run(f"nx {a}", shell=True)\n') == [("f", "nx", 3)]
    assert _hits_in('import os\ndef f():\n    os.system("uv sync")\n') == [("f", "uv", 3)]
    # a lookup, however spelled
    assert _hits_in('import shutil\ndef f():\n    return shutil.which("uv")\n') == [("f", "which()", 3)]
    assert _hits_in('def f(which):\n    return which("git")\n') == [("f", "which()", 2)]
    # a .exe spelling is the same name
    assert _hits_in('def f():\n    return ["git.exe", "x"]\n') == [("f", "git.exe", 2)]
    # Not flagged: an absolute or computed argv0, prose, a non-launcher name.
    assert _hits_in('import sys\ndef f():\n    return [sys.executable, "-c", "x"]\n') == []
    assert _hits_in('def f(exe):\n    return [exe, "x"]\n') == []
    assert _hits_in('def f(log):\n    log("git is missing")\n') == []
    assert _hits_in('def f():\n    return ["other", "x"]\n') == []
    assert _hits_in('def f(a):\n    return [*a, "git"]\n') == []
    assert _hits_in('def f():\n    return _exec_path.which_off_cwd("git")\n') == []
