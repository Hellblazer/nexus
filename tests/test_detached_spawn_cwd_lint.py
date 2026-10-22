# SPDX-License-Identifier: AGPL-3.0-or-later
"""A detached process in ``src/nexus`` never inherits its launcher's working
directory (nexus-x3m8e).

Sessions run in disposable worktrees. A long-lived process that inherited one
loses its cwd when the worktree is deleted, and from then on every
``os.getcwd()``, ``os.path.abspath()`` of a relative path and ``Path.cwd()`` in
it or in any library it imports raises ``FileNotFoundError`` (a MinerU server
answered every task that way for hours while ``/health`` said 200). The rule is
``nexus.util.stable_cwd`` (re-exported by ``service_registry``): a detached
spawn takes ``**detached_spawn_kwargs()`` (or passes ``cwd=``), a detached fork
calls ``enter_stable_daemon_cwd()``.

The census of ``Popen`` sites that detach (``start_new_session``,
``creationflags``, ``isolation_popen_kwargs()`` and the daemon spawn helpers)
and of ``os.setsid`` sites: each must either carry the cwd or be listed below
with the reason it is a short-lived, bounded child. A new detached spawn is a
decision, not an accident. Two earlier sites were missed by hand-fixing (the
session-end launcher and the per-page MinerU worker); this file is the sweep.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

SRC = pathlib.Path(__file__).parent.parent / "src" / "nexus"

#: Calls whose result is the stable-cwd kwargs (or contains them).
_CWD_HELPERS = frozenset({"detached_spawn_kwargs", "_supervisor_popen_kwargs", "stable_daemon_cwd"})
#: Calls whose result is a detaching kwargs dict.
_DETACH_HELPERS = frozenset({
    "isolation_popen_kwargs", "_supervisor_popen_kwargs", "_engine_popen_kwargs", "_isolation_kwargs",
})
_DETACH_KEYWORDS = frozenset({"start_new_session", "creationflags"})

#: (module relative to src/nexus, enclosing function) -> why it may keep the
#: caller's directory. Short-lived and bounded, or itself a child of a daemon
#: that has already entered the stable cwd.
_ALLOWED: dict[tuple[str, str], str] = {
    ("aspect_extractor.py", "_run_claude_isolated"): (
        "a `claude -p` extraction bounded by a timeout and group-killed on expiry; it runs inside "
        "the aspect worker, which has entered the stable cwd, so it inherits that"
    ),
    ("daemon/storage_service_daemon.py", "_spawn_service"): (
        "the engine is a child of the supervisor, which entered the stable cwd at its --foreground "
        "entry; the engine inherits it"
    ),
    ("db/pg_provision.py", "_pg_ctl_start_detached"): (
        "pg_ctl start: the postmaster chdirs to PGDATA itself"
    ),
    ("daemon/aspect_worker_daemon.py", "_popen_outliving_host_job"): (
        "a pass-through: it forwards the caller's kwargs, which are _supervisor_popen_kwargs()"
    ),
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_popen(call: ast.Call) -> bool:
    return _call_name(call.func).lower() in ("popen", "_popen", "create_subprocess_exec", "create_subprocess_shell", "_spawn_with_prompt_file")


def _expansion_calls(call: ast.Call) -> list[ast.Call]:
    return [k.value for k in call.keywords if k.arg is None and isinstance(k.value, ast.Call)]


def _detaches(call: ast.Call) -> bool:
    if any(k.arg in _DETACH_KEYWORDS for k in call.keywords):
        return True
    return any(_call_name(c.func) in _DETACH_HELPERS for c in _expansion_calls(call))


def _carries_cwd(call: ast.Call, scope: ast.AST) -> bool:
    if any(k.arg == "cwd" for k in call.keywords):
        return True
    if any(_call_name(c.func) in _CWD_HELPERS for c in _expansion_calls(call)):
        return True
    # `**name` / `**name`-style expansion: fine if this function builds the dict
    # from a cwd helper (assignment, .update, or a literal containing the helper).
    names = [k.value.id for k in call.keywords if k.arg is None and isinstance(k.value, ast.Name)]
    if names:
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and _call_name(node.func) in _CWD_HELPERS:
                return True
    return False


class _Sites(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[ast.AST] = []
        self.names: list[str] = []
        self.popens: list[tuple[str, int, bool, bool]] = []  # (function, line, detaches, carries_cwd)
        self.setsids: list[tuple[str, int, bool]] = []  # (function, line, chdirs)

    def _enter(self, node: ast.AST, name: str) -> None:
        self.scope.append(node)
        self.names.append(name)
        self.generic_visit(node)
        self.scope.pop()
        self.names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        fn = self.names[-1] if self.names else "<module>"
        scope = self.scope[-1] if self.scope else ast.Module(body=[], type_ignores=[])
        if _is_popen(node) and _detaches(node):
            self.popens.append((fn, node.lineno, True, _carries_cwd(node, scope)))
        if _call_name(node.func) == "setsid":
            chdirs = any(
                isinstance(n, ast.Call) and _call_name(n.func) in ("enter_stable_daemon_cwd", "chdir")
                for n in ast.walk(scope)
            )
            self.setsids.append((fn, node.lineno, chdirs))
        self.generic_visit(node)


def scan(source: str) -> _Sites:
    sites = _Sites()
    sites.visit(ast.parse(source))
    return sites


def test_every_detached_spawn_carries_a_stable_cwd_or_says_why_not() -> None:
    offenders: list[str] = []
    seen_allowed: set[tuple[str, str]] = set()
    compliant = 0
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        sites = scan(path.read_text(encoding="utf-8"))
        for fn, line, _detaches, carries in sites.popens:
            if carries:
                compliant += 1
            elif (rel, fn) in _ALLOWED:
                seen_allowed.add((rel, fn))
            else:
                offenders.append(f"src/nexus/{rel}:{line} in {fn}(): Popen detaches without cwd=")
        for fn, line, chdirs in sites.setsids:
            if chdirs:
                compliant += 1
            else:
                offenders.append(f"src/nexus/{rel}:{line} in {fn}(): os.setsid() daemon never leaves its cwd")
    assert compliant >= 7, f"only {compliant} compliant sites found: the detector is blind (expected the daemons, the MinerU server, the hooks)"
    stale = sorted(set(_ALLOWED) - seen_allowed)
    assert not stale, f"allowlist entries that match no detaching Popen any more (drop them): {stale}"
    assert not offenders, (
        "a detached process inherits its launcher's working directory, which may be a session "
        "worktree that is later deleted (nexus-x3m8e). Merge **detached_spawn_kwargs() into the "
        "spawn (nexus.daemon.service_registry), pass cwd=, or call enter_stable_daemon_cwd() in "
        "the forked daemon; or list a short-lived bounded child in _ALLOWED with its reason:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("source,offends", [
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'], start_new_session=True)\n", True),
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'], creationflags=1)\n", True),
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'], **isolation_popen_kwargs())\n", True),
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'], start_new_session=True, cwd='/')\n", False),
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'], start_new_session=True, **detached_spawn_kwargs())\n", False),
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'], **_supervisor_popen_kwargs(p))\n", False),
    (
        "import subprocess\ndef f():\n    s = detached_spawn_kwargs()\n"
        "    subprocess.Popen(['x'], start_new_session=True, **s)\n", False,
    ),
    (
        "import subprocess\ndef f():\n    common = {}\n    common.update(detached_spawn_kwargs())\n"
        "    subprocess.Popen(['x'], start_new_session=True, **common)\n", False,
    ),
    ("import subprocess\ndef f():\n    subprocess.Popen(['x'])\n", False),  # not detaching: out of scope
    ("import os\ndef d():\n    os.setsid()\n", True),
    ("import os\ndef d():\n    os.setsid()\n    enter_stable_daemon_cwd()\n", False),
    ("import os\ndef d():\n    os.setsid()\n    os.chdir('/')\n", False),
])
def test_the_detector_sees_each_shape(source: str, offends: bool) -> None:
    sites = scan(source)
    bad = [p for p in sites.popens if not p[3]] + [s for s in sites.setsids if not s[2]]
    assert bool(bad) is offends, source
