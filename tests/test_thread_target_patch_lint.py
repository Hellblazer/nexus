# SPDX-License-Identifier: AGPL-3.0-or-later
"""A test enters its patches on its own thread, never inside a thread it starts (nexus-9tmvy).

A ``patch`` entered in a worker thread is left by that thread. When the test's ``join`` times
out (the test is already failing) the patch stays installed after the test returns, and the
fake leaks to every later test in the same xdist worker. Two threads patching one target
make it worse: their exits restore out of order, and the last one puts the other's mock back
for good (aa947b5f3: it broke the RDR-237 pymupdf resume tests on the 7.88.0 release suite).

The remedy is to enter the patch around the thread's whole life, on the test's thread
(``with patch(...):`` containing the start and the join, or an ``ExitStack``), and to assert
the thread is dead before the ``with`` ends.

This lint finds a thread target (``Thread(target=f)``, ``Timer(t, f)``, ``pool.submit(f, ...)``,
``pool.map(f, ...)``, a lambda target) whose body, nested defs included, calls ``patch``,
``patch.object``, ``patch.dict``, ``patch.multiple``, ``patch_in``, ``patch_time``,
``setattr_in`` or a ``monkeypatch`` setter. Blind spots, stated: a target that is not defined
in the module that starts it (imported, a bound method of another object, ``functools.partial``)
is not followed, and neither is a patch reached through a helper the target calls.
A site that must patch from inside its thread goes in ``ALLOWED`` with the reason (today only this file's own leak demonstration).
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_TESTS = Path(__file__).resolve().parent

#: ``(path relative to the repo root, target function name)`` -> why the patch must be entered
#: inside the thread. Empty on purpose: the five sites found by the nexus-9tmvy sweep were fixed.
ALLOWED: dict[tuple[str, str], str] = {
    ("tests/test_thread_target_patch_lint.py", "worker"): (
        "_hung_worker's worker is the leaking shape on purpose: it demonstrates the leak the lint exists for"
    ),
}

_PATCH_CALLS = frozenset({"patch", "patch_in", "patch_time", "setattr_in", "delattr_in"})
_PATCH_ATTRS = frozenset({"object", "dict", "multiple"})
_MONKEYPATCH_SETTERS = frozenset({"setattr", "delattr", "setenv", "delenv", "setitem", "delitem", "syspath_prepend", "chdir"})
_SPAWN_ATTRS = frozenset({"submit", "map"})


def _parse(source: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # a test file's own bad escape is not ours to report
        return ast.parse(source)


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_patch_call(call: ast.Call) -> bool:
    name = _dotted(call.func)
    parts = name.split(".")
    if parts[-1] in _PATCH_CALLS:
        return True
    if len(parts) >= 2 and parts[-2] == "patch" and parts[-1] in _PATCH_ATTRS:
        return True
    # ``monkeypatch.setattr`` and ``mp.setattr`` / ``patched.setattr`` (the fixture's other names)
    return parts[-1] in _MONKEYPATCH_SETTERS and len(parts) >= 2 and (
        "monkeypatch" in parts[-2] or parts[-2] in {"mp", "patched", "m"}
    )


def _target_nodes(call: ast.Call) -> list[ast.AST]:
    """The callables ``call`` hands to a new thread, if it does."""
    name = _dotted(call.func)
    last = name.split(".")[-1]
    out: list[ast.AST] = []
    if last == "Thread":
        out += [kw.value for kw in call.keywords if kw.arg == "target"]
    elif last == "Timer":
        if len(call.args) > 1:
            out.append(call.args[1])
        out += [kw.value for kw in call.keywords if kw.arg == "function"]
    elif last in _SPAWN_ATTRS and "." in name and call.args:
        out.append(call.args[0])
    return out


def _patching_targets(source: str) -> list[tuple[int, str]]:
    """``(line of the patch call, target name)`` for every thread target that patches."""
    tree = _parse(source)
    defs: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, []).append(node)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for target in _target_nodes(node):
            if isinstance(target, ast.Lambda):
                bodies, label = [target], "<lambda>"
            else:
                label = target.id if isinstance(target, ast.Name) else (
                    target.attr if isinstance(target, ast.Attribute) else ""
                )
                bodies = defs.get(label, [])
            for body in bodies:
                found += [
                    (inner.lineno, label)
                    for inner in ast.walk(body)
                    if isinstance(inner, ast.Call) and _is_patch_call(inner)
                ]
    return sorted(set(found))


def _thread_target_count(source: str) -> int:
    return sum(len(_target_nodes(n)) for n in ast.walk(_parse(source)) if isinstance(n, ast.Call))


def _offenders() -> tuple[list[str], set[tuple[str, str]], int]:
    found: list[str] = []
    used: set[tuple[str, str]] = set()
    targets = 0
    for path in sorted(_TESTS.rglob("*.py")):
        rel = path.relative_to(_TESTS.parent).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            targets += _thread_target_count(source)
            hits = _patching_targets(source)
        except SyntaxError:
            continue
        for line, label in hits:
            if (rel, label) in ALLOWED:
                used.add((rel, label))
                continue
            found.append(f"{rel}:{line}: patch inside thread target {label}")
    return found, used, targets


def test_no_test_enters_a_patch_inside_a_thread_it_starts() -> None:
    offenders, used, _targets = _offenders()
    assert offenders == [], (
        "a patch entered in a worker thread outlives the test when the join times out "
        "(nexus-9tmvy); enter it on the test's thread around start and join:\n" + "\n".join(offenders)
    )
    stale = sorted(set(ALLOWED) - used)
    assert stale == [], f"ALLOWED entries that no longer match a site: {stale}"


_BAD_SAMPLES = (
    "def w():\n    with patch('a.b'):\n        pass\nthreading.Thread(target=w).start()\n",
    "def w():\n    with patch.object(M, 'x', 1):\n        pass\nThread(target=w)\n",
    "def w():\n    with patch_in('m', 'os.kill'):\n        pass\nthreading.Thread(target=w)\n",
    "def w():\n    monkeypatch.setattr(M, 'x', 1)\nthreading.Thread(target=w)\n",
    "def w():\n    with mock.patch.dict(os.environ, {}):\n        pass\nthreading.Timer(1, w)\n",
    "def w():\n    with patch('a.b'):\n        pass\npool.submit(w)\n",
    "def run():\n    def go():\n        with patch('a.b'):\n            pass\n    return go\n"
    "threading.Thread(target=run)\n",  # a nested def inside the target is the target's body
    "threading.Thread(target=lambda: patch('a.b').start())\n",
    "class T:\n    def _w(self):\n        with patch('a.b'):\n            pass\n"
    "    def go(self):\n        threading.Thread(target=self._w)\n",
)

_GOOD_SAMPLES = (
    "def w():\n    pass\nwith patch('a.b'):\n    t = threading.Thread(target=w)\n    t.start()\n    t.join()\n",
    "def w():\n    pass\nthreading.Thread(target=w)\n",
    "def helper():\n    with patch('a.b'):\n        pass\nthreading.Thread(target=other)\n",
    "def w():\n    pass\n# threading.Thread(target=w) patch('a.b')\n",
    "def w():\n    return run(patch_here=False)\nthreading.Thread(target=w)\n",
)


@pytest.mark.parametrize("sample", _BAD_SAMPLES)
def test_the_lint_sees_each_shape(sample: str) -> None:
    """Non-vacuity: every shape the lint exists for is matched."""
    assert len(_patching_targets(sample)) == 1, sample


@pytest.mark.parametrize("sample", _GOOD_SAMPLES)
def test_the_lint_passes_a_patch_on_the_tests_own_thread(sample: str) -> None:
    assert _patching_targets(sample) == [], sample


def test_the_lint_has_thread_targets_to_check() -> None:
    """Non-vacuity over the tree: the sweep parses the whole suite and meets its thread
    targets, so an empty offender list means something (274 at landing)."""
    assert len(list(_TESTS.rglob("*.py"))) > 500
    _offenders_found, used, targets = _offenders()
    assert targets >= 150, f"only {targets} thread targets seen; the scan has lost its input"
    assert used == set(ALLOWED)


# ── The leak itself, and the shape that cannot leak ──────────────────────────


class _Seam:
    """A module-level function a test fakes."""

    @staticmethod
    def real() -> str:
        return "real"


def _hung_worker(enter_in_thread: bool):
    """Start a worker that blocks past a short join, with the fake entered in the worker
    (``enter_in_thread``) or on this thread. Returns what ``_Seam.real`` is once the test
    body would have returned, the worker, and the event that frees it."""
    import threading
    from contextlib import ExitStack
    from unittest.mock import patch

    release = threading.Event()
    entered = threading.Event()

    def worker() -> None:
        with ExitStack() as stack:
            if enter_in_thread:
                stack.enter_context(patch.object(_Seam, "real", staticmethod(lambda: "fake")))
            entered.set()
            release.wait(30)

    with ExitStack() as mine:
        if not enter_in_thread:
            mine.enter_context(patch.object(_Seam, "real", staticmethod(lambda: "fake")))
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert entered.wait(5)
        t.join(timeout=0.1)
        assert t.is_alive(), "the worker was meant to outlive its join"
        # the failing test's assertion would raise here; ``mine`` unwinds as the exception passes
    return _Seam.real(), t, release


def test_a_patch_entered_in_the_worker_outlives_a_timed_out_join() -> None:
    """The old shape: the fake is still installed after the test body is gone."""
    seen, t, release = _hung_worker(enter_in_thread=True)
    try:
        assert seen == "fake"
    finally:
        release.set()
        t.join(5)
        assert not t.is_alive()
    assert _Seam.real() == "real", "the worker's exit restored it once it was freed"


def test_a_patch_entered_on_the_tests_thread_is_gone_when_the_body_returns() -> None:
    """The fixed shape: the same hung worker leaves nothing installed."""
    seen, t, release = _hung_worker(enter_in_thread=False)
    try:
        assert seen == "real"
    finally:
        release.set()
        t.join(5)
        assert not t.is_alive()
