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
``setattr_in``, ``delattr_in`` or a ``monkeypatch`` setter. A ``self.x`` target resolves to a
method of the class that encloses the call; any other attribute target resolves by its bare name
in the module.

Blind spots, stated: a target that is not defined in the module that starts it (imported, a
bound method of another object, ``functools.partial``) is not followed, and neither is a patch
reached through a helper the target calls. Spawn shapes the lint does not recognise at all:
``loop.run_in_executor(pool, f)``, ``asyncio.to_thread(f)`` and the ``run`` of a ``Thread``
subclass. A bare ``client.patch(...)`` (an HTTP verb) in a target body reads as ``patch`` and
over-matches. A site that must patch from inside its thread goes in ``ALLOWED`` with the reason
(today only this file's own leak demonstration).

The tables below are each pinned entry by entry: ``_SHAPE_SAMPLES`` holds one bad sample per
entry and ``test_every_table_entry_is_load_bearing`` removes the entry and expects the sample
to go clean.
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
#: names a ``monkeypatch`` fixture is bound to besides one containing "monkeypatch"
_MONKEYPATCH_NAMES = frozenset({"mp", "patched", "m"})
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
        "monkeypatch" in parts[-2] or parts[-2] in _MONKEYPATCH_NAMES
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


def _enclosing_class(node: ast.AST, parent: dict[ast.AST, ast.AST]) -> ast.ClassDef | None:
    while node in parent:
        node = parent[node]
        if isinstance(node, ast.ClassDef):
            return node
    return None


def _resolve(
    target: ast.AST, call: ast.Call, parent: dict[ast.AST, ast.AST], defs: dict[str, list[ast.AST]]
) -> tuple[list[ast.AST], str]:
    """The bodies a thread target names in this module, and its label (``[]`` when unresolved)."""
    if isinstance(target, ast.Lambda):
        return [target], "<lambda>"
    if isinstance(target, ast.Name):
        return defs.get(target.id, []), target.id
    if isinstance(target, ast.Attribute):
        label = target.attr
        if isinstance(target.value, ast.Name) and target.value.id in {"self", "cls"}:
            cls = _enclosing_class(call, parent)
            methods = [
                n for n in (cls.body if cls else [])
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == label
            ]
            return methods, label
        return defs.get(label, []), label
    return [], ""


def _scan(source: str) -> tuple[list[tuple[int, str]], int, int]:
    """``(patch hits, thread targets seen, thread targets resolved to a def in this module)``.

    A hit is ``(line of the patch call, target name)`` for a thread target that patches."""
    tree = _parse(source)
    parent: dict[ast.AST, ast.AST] = {}
    defs: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, []).append(node)
    found: list[tuple[int, str]] = []
    seen = resolved = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for target in _target_nodes(node):
            seen += 1
            bodies, label = _resolve(target, node, parent, defs)
            resolved += bool(bodies)
            for body in bodies:
                found += [
                    (inner.lineno, label)
                    for inner in ast.walk(body)
                    if isinstance(inner, ast.Call) and _is_patch_call(inner)
                ]
    return sorted(set(found)), seen, resolved


def _patching_targets(source: str) -> list[tuple[int, str]]:
    """``(line of the patch call, target name)`` for every thread target that patches."""
    return _scan(source)[0]


def _offenders(root: Path | None = None) -> tuple[list[str], set[tuple[str, str]], int, int, list[str]]:
    """``(offenders, ALLOWED entries used, targets seen, targets resolved, files skipped)``.

    ``root`` defaults to the tests tree; a test points it at a directory of its own.
    """
    tests = _TESTS if root is None else root
    found: list[str] = []
    used: set[tuple[str, str]] = set()
    seen = resolved = 0
    skipped: list[str] = []
    for path in sorted(tests.rglob("*.py")):
        rel = path.relative_to(tests.parent).as_posix()
        try:
            hits, n_seen, n_resolved = _scan(path.read_text(encoding="utf-8"))
        except SyntaxError:
            skipped.append(rel)
            continue
        seen += n_seen
        resolved += n_resolved
        for line, label in hits:
            if (rel, label) in ALLOWED:
                used.add((rel, label))
                continue
            found.append(f"{rel}:{line}: patch inside thread target {label}")
    return found, used, seen, resolved, skipped


def test_no_test_enters_a_patch_inside_a_thread_it_starts() -> None:
    offenders, used, _seen, _resolved, skipped = _offenders()
    assert skipped == [], f"files the lint could not parse, so did not check: {skipped}"
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
    "def w():\n    with patch('a.b'):\n        pass\nthreading.Timer(1, function=w)\n",  # Timer(function=)
    "def w():\n    with patch('a.b'):\n        pass\npool.map(w, [1])\n",
)

_GOOD_SAMPLES = (
    "def w():\n    pass\nwith patch('a.b'):\n    t = threading.Thread(target=w)\n    t.start()\n    t.join()\n",
    "def w():\n    pass\nthreading.Thread(target=w)\n",
    "def helper():\n    with patch('a.b'):\n        pass\nthreading.Thread(target=other)\n",
    "def w():\n    pass\n# threading.Thread(target=w) patch('a.b')\n",
    "def w():\n    return run(patch_here=False)\nthreading.Thread(target=w)\n",
    # a ``self.x`` target is the enclosing class's own ``x``, not another class's method of that name
    "class A:\n    def _w(self):\n        with patch('a.b'):\n            pass\n"
    "class B:\n    def _w(self):\n        pass\n    def go(self):\n        threading.Thread(target=self._w)\n",
)


@pytest.mark.parametrize("sample", _BAD_SAMPLES)
def test_the_lint_sees_each_shape(sample: str) -> None:
    """Non-vacuity: every shape the lint exists for is matched."""
    assert len(_patching_targets(sample)) == 1, sample


@pytest.mark.parametrize("sample", _GOOD_SAMPLES)
def test_the_lint_passes_a_patch_on_the_tests_own_thread(sample: str) -> None:
    assert _patching_targets(sample) == [], sample


def _worker_with(expr: str, spawn: str = "threading.Thread(target=w)") -> str:
    return f"def w():\n    {expr}\n{spawn}\n"


#: one bad sample per table entry, keyed ``(table name, entry)``
_SHAPE_SAMPLES: dict[tuple[str, str], str] = {
    **{("_PATCH_CALLS", name): _worker_with(f"{name}('a.b')") for name in (
        "patch", "patch_in", "patch_time", "setattr_in", "delattr_in")},
    **{("_PATCH_ATTRS", name): _worker_with(f"mock.patch.{name}(M, 'x')") for name in (
        "object", "dict", "multiple")},
    **{("_MONKEYPATCH_SETTERS", name): _worker_with(f"monkeypatch.{name}(M, 'x')") for name in (
        "setattr", "delattr", "setenv", "delenv", "setitem", "delitem", "syspath_prepend", "chdir")},
    **{("_MONKEYPATCH_NAMES", name): _worker_with(f"{name}.setattr(M, 'x', 1)") for name in (
        "mp", "patched", "m")},
    **{("_SPAWN_ATTRS", name): _worker_with("patch('a.b')", f"pool.{name}(w, [1])") for name in (
        "submit", "map")},
}

_TABLES = ("_PATCH_CALLS", "_PATCH_ATTRS", "_MONKEYPATCH_SETTERS", "_MONKEYPATCH_NAMES", "_SPAWN_ATTRS")


def test_every_table_entry_has_a_sample() -> None:
    """A new entry in a table without a sample here fails: the entry would be unpinned."""
    in_tables = {(t, e) for t in _TABLES for e in globals()[t]}
    assert in_tables == set(_SHAPE_SAMPLES)


@pytest.mark.parametrize("key", sorted(_SHAPE_SAMPLES), ids=lambda k: f"{k[0]}.{k[1]}")
def test_every_table_entry_is_load_bearing(key: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing one entry from its table turns its sample clean, so each entry is exercised."""
    table, entry = key
    sample = _SHAPE_SAMPLES[key]
    assert len(_patching_targets(sample)) == 1, f"{key}: the sample is not seen with the entry present"
    monkeypatch.setitem(globals(), table, globals()[table] - {entry})
    assert _patching_targets(sample) == [], f"{key}: the sample is still seen without the entry"


def test_a_scan_counts_seen_and_resolved_targets() -> None:
    sample = (
        "def w():\n    pass\n"
        "threading.Thread(target=w)\n"           # resolved
        "threading.Thread(target=lambda: 1)\n"   # resolved (a lambda is its own body)
        "threading.Thread(target=other.run)\n"   # seen, not defined here
        "threading.Thread(target=imported)\n"    # seen, not defined here
    )
    assert _scan(sample)[1:] == (4, 2)


def test_the_lint_has_thread_targets_to_check() -> None:
    """Non-vacuity over the tree: the sweep parses the whole suite, meets its thread targets and
    resolves them to a def, so an empty offender list means something (275 seen and 190
    resolved at landing). The resolved floor is the one that can go blind alone: a resolver that
    stopped matching would leave ``seen`` untouched."""
    assert len(list(_TESTS.rglob("*.py"))) > 500
    _offenders_found, used, seen, resolved, skipped = _offenders()
    assert skipped == [], f"files the lint could not parse: {skipped}"
    assert seen >= 150, f"only {seen} thread targets seen; the scan has lost its input"
    assert resolved >= 170, f"only {resolved} of {seen} thread targets resolved to a def; the resolver has gone blind"
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


def test_a_file_the_lint_cannot_parse_is_reported_not_passed(tmp_path: Path) -> None:
    """The scan counts an unparsable file as skipped; dropping it silently would hide a target."""
    root = tmp_path / "tests"
    root.mkdir()
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "fine.py").write_text("x = 1\n", encoding="utf-8")
    _found, _used, _seen, _resolved, skipped = _offenders(root)
    assert skipped == ["tests/broken.py"]
