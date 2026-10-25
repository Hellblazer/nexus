# SPDX-License-Identifier: AGPL-3.0-or-later
"""A subprocess that starts something meant to outlive the call is never job-contained
(nexus-9pc2f).

``run_bounded`` puts its child in a Windows Job Object with KILL_ON_JOB_CLOSE and closes
the job when the call returns. ``nx daemon service start`` detach-spawns the supervisor,
which joins that job, so the return of ``run_bounded`` killed the supervisor and its
engine (no Java shutdown hook, no supervisor log line). The 7.86.0 shakeout on a native
Windows guest hit it after every ``nx upgrade`` that cycled the service. The same class
was fixed once for the setup tools (c67bdb609, nexus-sv2wx.16) and not swept.

Two halves. The behavioural pins call each start site with a fake spawner and read the
``contained`` keyword it was given: start is ``False``, stop stays contained. The lint
scans every call in ``src/nexus`` whose argv holds a start-verb sequence and requires
``contained=False`` on it, so a new site cannot regress.

The job itself exists only on Windows; no test here creates one. The Windows-only check
is the guest run in the bead.
"""
from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

SRC = pathlib.Path(__file__).parent.parent / "src" / "nexus"


# --------------------------------------------------------------------------- behaviour


def test_upgrade_cycle_starts_uncontained_and_stops_contained() -> None:
    from nexus.commands.upgrade import _cycle_storage_service_to_current

    seen: list[tuple[str, dict]] = []

    def _run(argv, **kwargs):
        seen.append((argv[-1], kwargs))

    _cycle_storage_service_to_current(
        _discover_fn=lambda: object(), _run_fn=_run, _nx_bin_fn=lambda: ["nx"],
    )
    assert [v for v, _ in seen] == ["stop", "start"]
    by_verb = dict(seen)
    assert by_verb["start"].get("contained") is False, "start must not run in a kill-on-close job"
    assert by_verb["stop"].get("contained", True) is True, "stop stays contained"


def test_restart_and_verify_starts_uncontained_and_stops_contained(tmp_path) -> None:
    from nexus import upgrade_finish as uf

    ok = MagicMock(returncode=0, stdout="", stderr="")
    running = MagicMock(version="v0.1.60", pid=214)
    with patch.object(uf, "service_stack_pids", return_value=[]), \
         patch.object(uf, "run_bounded", side_effect=[ok, ok]) as sp, \
         patch.object(uf, "_running_engine", return_value=running):
        uf._restart_and_verify(tmp_path, [], "v0.1.60")
    kw = {c.args[0][3]: c.kwargs for c in sp.call_args_list}
    assert kw["start"].get("contained") is False
    assert kw["stop"].get("contained", True) is True


def test_unit_reinstall_restart_starts_uncontained(tmp_path) -> None:
    from nexus.upgrade_finish import _restart_service_after_unit_reinstall

    with patch("nexus.upgrade_finish.run_bounded",
               return_value=MagicMock(returncode=0, stdout="", stderr="")) as sp:
        _restart_service_after_unit_reinstall(tmp_path)
    assert sp.call_args.kwargs.get("contained") is False


def test_mineru_cycle_starts_uncontained_and_stops_contained() -> None:
    from nexus import pdf_stack
    from nexus.upgrade_finish import SkewReport, StaleProcess, restart_stale

    report = SkewReport(installed_version="6.7.1")
    report.stale = [StaleProcess(pid=300, kind="mineru", command="mineru-api", age_s=99)]
    with patch("nexus.daemon.mineru_lifecycle.spawn_policy_allows", return_value=True), \
         patch("nexus.upgrade_finish.process_command", return_value="mineru-api --host 127.0.0.1"), \
         patch.object(pdf_stack, "current_generation_has_stack", return_value=True), \
         patch("nexus.upgrade_finish.run_bounded", return_value=MagicMock(returncode=0)) as sp:
        restart_stale(report)
    stop_call, start_call = sp.call_args_list
    assert stop_call.args[0][-2:] == ["mineru", "stop"] and start_call.args[0][-2:] == ["mineru", "start"]
    assert start_call.kwargs.get("contained") is False
    assert stop_call.kwargs.get("contained", True) is True


# --------------------------------------------------------------------------- lint

#: Verb sequences that start a process meant to outlive the call. Contiguous, in order.
_START_VERBS: tuple[tuple[str, ...], ...] = (
    ("daemon", "service", "start"),
    ("daemon", "aspect-worker", "start"),
    ("mineru", "start"),
)
#: Callees that ARE the detach (they spawn the long-lived child themselves) or merely
#: build argv; a start-verb argv reaching them is not a contained run_bounded.
_NOT_A_CONTAINED_RUN = frozenset({"popen", "_popen", "popen_outliving_host_job", "_popen_outliving_host_job"})


def _callee(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _strings(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _holds(strings: list[str], verbs: tuple[str, ...]) -> bool:
    n = len(verbs)
    return any(tuple(strings[i:i + n]) == verbs for i in range(len(strings) - n + 1))


class _StartCalls(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.scopes: list[ast.AST] = [tree]
        self.bad: list[int] = []
        self.total = 0

    def _fn(self, node: ast.AST) -> None:
        self.scopes.append(node)
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _fn  # type: ignore[assignment]

    def visit_Call(self, call: ast.Call) -> None:
        self.generic_visit(call)
        # ``timeout=`` is what makes a call a bounded run (run_bounded requires it); it
        # keeps ``" ".join(argv)`` and log calls that merely mention the argv out of scope.
        if _callee(call).lower() in _NOT_A_CONTAINED_RUN or not call.args or not any(
            k.arg == "timeout" for k in call.keywords
        ):
            return
        first = call.args[0]
        if isinstance(first, ast.Constant):
            return  # nx_argv("daemon", ...) itself: its first arg is a verb, not an argv
        strings = _strings(first)
        if isinstance(first, ast.Name):  # argv = nx_argv(...); run(argv, ...)
            for stmt in ast.walk(self.scopes[-1]):
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == first.id for t in stmt.targets
                ):
                    strings += _strings(stmt.value)
        if not any(_holds(strings, v) for v in _START_VERBS):
            return
        self.total += 1
        c = {k.arg: k.value for k in call.keywords}.get("contained")
        if not (isinstance(c, ast.Constant) and c.value is False):
            self.bad.append(call.lineno)


def _start_calls_not_uncontained(source: str) -> tuple[list[int], int]:
    """(lines of start-verb calls lacking ``contained=False``, count of start-verb calls)."""
    tree = ast.parse(source)
    v = _StartCalls(tree)
    v.visit(tree)
    return v.bad, v.total


def _scan_src() -> tuple[list[str], int]:
    offenders: list[str] = []
    examined = 0
    for path in sorted(SRC.rglob("*.py")):
        bad, total = _start_calls_not_uncontained(path.read_text(encoding="utf-8"))
        examined += total
        offenders += [f"src/nexus/{path.relative_to(SRC).as_posix()}:{ln}" for ln in bad]
    return offenders, examined


@pytest.mark.lint
def test_start_verb_subprocesses_are_never_job_contained() -> None:
    offenders, _ = _scan_src()
    assert not offenders, (
        "a call that starts a service, worker or server must pass contained=False: "
        "run_bounded closes its kill-on-close job on return and the Windows job takes the "
        "long-lived child with it (nexus-9pc2f). Sites:\n" + "\n".join(offenders)
    )


@pytest.mark.lint
def test_the_scan_examined_the_known_start_sites() -> None:
    """Non-vacuity: upgrade.py (1), upgrade_finish.py (service start twice, mineru start)."""
    _, examined = _scan_src()
    assert examined >= 4, f"only {examined} start-verb calls examined; the detector is blind"


@pytest.mark.lint
@pytest.mark.parametrize("source,offends", [
    ("def f():\n    run_bounded(['nx', 'daemon', 'service', 'start'], timeout=1)\n", True),
    ("def f():\n    run_bounded(nx_argv('daemon', 'service', 'start', '--config-dir', d), timeout=1)\n", True),
    ("def f():\n    argv = nx_argv('daemon', 'service', 'start')\n    run_bounded(argv, timeout=1)\n", True),
    ("def f():\n    run_bounded(nx_argv('mineru', 'start'), timeout=1, contained=True)\n", True),
    ("def f():\n    _run([*nx, 'daemon', 'service', 'start'], timeout=1)\n", True),
    ("def f():\n    run_bounded(['nx', 'daemon', 'service', 'start'], timeout=1, contained=False)\n", False),
    ("def f():\n    argv = nx_argv('daemon', 'service', 'start')\n    run_bounded(argv, timeout=1, contained=False)\n", False),
    ("def f():\n    run_bounded(['nx', 'daemon', 'service', 'stop'], timeout=1)\n", False),
    ("def f():\n    run_bounded(['nx', 'mineru', 'stop'], timeout=1)\n", False),
    ("def f():\n    popen(['nx', 'daemon', 'service', 'start', '--foreground'])\n", False),
    ("def f():\n    argv = nx_argv('daemon', 'service', 'start')\n    return ' '.join(argv)\n", False),
])
def test_the_detector_recognises_the_shapes_it_claims_to(source: str, offends: bool) -> None:
    bad, total = _start_calls_not_uncontained(source)
    assert bool(bad) is offends, source
    assert total == (1 if "timeout" in source and "start" in source else 0), source
