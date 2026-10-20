# SPDX-License-Identifier: AGPL-3.0-or-later
"""No test-tree spawn of the engine JVM bypasses ``tests._child_process.popen_in_group`` (nexus-qsqc6).

WHY. ``popen_in_group`` records every engine-JVM spawn (an argv with ``-jar`` on ``nexus-service*.jar``) via
``record_integration_child``, in a record the sweep reads whether or not the process made an integration
cluster (``tests/_engine_substrate.py``, ``TestEngineSpawnsAreRecordedWithoutACluster`` in
``tests/test_integration_pg_cluster.py``). A killed run's JVM is then stopped by the next sweep. The
recording is a property of the primitive, so the only way to lose it is to spawn the JVM some other way: a
bare ``subprocess.Popen`` also leaves the child in the test runner's own process group, which the sweep's
group kill would then aim at the runner. This lint is the tripwire for that one thing.

WHAT IT CHECKS (AST over every ``tests/**/*.py``, this file excluded). A *launch argv* is a list or tuple
literal holding the string ``"-jar"``, or a call to ``jar_argv`` or ``engine_argv``. A name (or ``self.x``)
assigned from one, or from an expression holding one or another such name (plain, annotated and augmented
assignment, so a derived or renamed argv follows), is tainted within its function, and a module-level one in
the whole file. A call to ``Popen`` / ``subprocess.run`` / ``call`` / ``check_*`` /
``create_subprocess_exec`` whose positional or keyword arguments hold a launch argv or a tainted name is a
bypass and fails. ``popen_in_group`` and ``spawn_service`` (which spawns through it) are the primitive.
Where the result goes is not checked (``with ... as p``, ``self.p =``, a tuple return), because the primitive
records it.

NOT COVERED, on purpose. Spawns in ``src/`` and ``scripts/`` (the product's own supervisor starts the JVM
there, and its tests assert argv without spawning). ``java -cp ...`` launches and non-Python launchers. An
argv built dynamically, or handed to a helper that spawns it. A test that mocks ``Popen`` with the engine
argv. The primitive's detector (``is_engine_jvm_argv``) is exercised by its own tests.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _REPO_ROOT / "tests"
_SELF = Path(__file__).resolve()
_SKIP_PARTS = frozenset({".venv", "node_modules", "__pycache__", "target"})

_PRIMITIVES = frozenset({"popen_in_group", "spawn_service"})
_SPAWN_CALLS = frozenset(
    {"Popen", "run", "call", "check_call", "check_output", "create_subprocess_exec"}
)
_ARGV_BUILDERS = frozenset({"jar_argv", "engine_argv"})


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    func: str
    kind: str  # "primitive" | "bypass"
    call: str


def _call_name(call: ast.Call) -> str:
    fn = call.func
    return fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")


def _is_launch_argv(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(isinstance(e, ast.Constant) and e.value == "-jar" for e in node.elts)
    return isinstance(node, ast.Call) and _call_name(node) in _ARGV_BUILDERS


def _ref(node: ast.AST) -> str | None:
    """``name`` or ``a.b`` for a Name / Attribute reference, else None."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        try:
            return ast.unparse(node)
        except ValueError:
            return None
    return None


def _holds_launch(node: ast.AST, tainted: set[str]) -> bool:
    for n in ast.walk(node):
        if _is_launch_argv(n):
            return True
        if isinstance(n, (ast.Name, ast.Attribute)) and _ref(n) in tainted:
            return True
    return False


def _taint(scope: ast.AST, inherited: set[str]) -> set[str]:
    """Names assigned from a launch argv or from a tainted name, to a fixed point."""
    tainted = set(inherited)
    changed = True
    while changed:
        changed = False
        for n in ast.walk(scope):
            if isinstance(n, ast.Assign):
                targets, value = n.targets, n.value
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                targets, value = [n.target], n.value
            else:
                continue
            if not _holds_launch(value, tainted):
                continue
            for t in targets:
                for leaf in ast.walk(t):
                    r = _ref(leaf) if isinstance(leaf, (ast.Name, ast.Attribute)) else None
                    if r and isinstance(getattr(leaf, "ctx", None), ast.Store) and r not in tainted:
                        tainted.add(r)
                        changed = True
    return tainted


def _own_calls(scope: ast.AST):
    """Calls in *scope*, not descending into nested function defs (they are scopes of their own)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            yield n
        stack.extend(ast.iter_child_nodes(n))


def scan_source(source: str, label: str) -> list[Site]:
    """Every spawn call in one source text that is handed a launch argv."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    module_only = ast.Module(
        body=[n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
        type_ignores=[],
    )
    module_taint = _taint(module_only, set())
    scopes: list[tuple[str, ast.AST, set[str]]] = [("<module>", tree, module_taint)]
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((n.name, n, _taint(n, module_taint)))
    out: dict[int, Site] = {}
    for func, scope, tainted in scopes:
        for call in _own_calls(scope):
            name = _call_name(call)
            kind = "primitive" if name in _PRIMITIVES else "bypass" if name in _SPAWN_CALLS else ""
            if not kind:
                continue
            if any(_holds_launch(a, tainted) for a in [*call.args, *(k.value for k in call.keywords)]):
                out[id(call)] = Site(label, call.lineno, func, kind, name)
    return sorted(out.values(), key=lambda s: (s.line, s.call))


def scan_tree(tests_dir: Path, repo_root: Path) -> list[Site]:
    """Every engine-JVM spawn under *tests_dir*, this file excluded."""
    out: list[Site] = []
    for p in sorted(tests_dir.rglob("*.py")):
        if _SKIP_PARTS.intersection(p.parts) or p.resolve() == _SELF:
            continue
        out.extend(scan_source(p.read_text(encoding="utf-8"), p.relative_to(repo_root).as_posix()))
    return out


def bypasses(sites: list[Site]) -> list[Site]:
    return [s for s in sites if s.kind == "bypass"]


# ── the live tree ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_sites() -> list[Site]:
    return scan_tree(_TESTS, _REPO_ROOT)


def test_no_engine_jvm_spawn_in_the_test_tree_bypasses_the_recording_primitive(live_sites) -> None:
    bad = bypasses(live_sites)
    assert not bad, (
        "these spawn the engine JVM without tests._child_process.popen_in_group, so a killed run's sweep "
        "has no record of the child (nexus-qsqc6). Spawn it with popen_in_group (or spawn_service):\n"
        + "\n".join(f"  {s.path}:{s.line} in {s.func} ({s.call})" for s in bad)
    )


def test_the_scan_is_not_vacuous(live_sites) -> None:
    """A scan that examined nothing passes everything. The tree holds a known set of engine spawns."""
    launchers = [s for s in live_sites if s.kind == "primitive" and s.call == "spawn_service"]
    direct = [s for s in live_sites if s.kind == "primitive" and s.call == "popen_in_group"]
    assert len(launchers) >= 20, f"only {len(launchers)} spawn_service sites found; 25 at the time of writing"
    assert len(direct) >= 4, f"only {len(direct)} popen_in_group sites found; 5 at the time of writing"
    known = {(s.path, s.func) for s in direct}
    for path, func in (
        ("tests/db/test_embed_parity.py", "_start_service"),
        ("tests/test_main_bootstrap_failure.py", "_spawn_engine"),
        ("tests/test_main_sigterm_during_model_init.py", "_spawn"),
        ("tests/_engine_substrate.py", "_boot"),
    ):
        assert (path, func) in known, f"known engine spawn {path}::{func} not found; found {sorted(known)}"


def test_spawn_service_spawns_through_the_primitive() -> None:
    """The launcher kind is covered only because of this."""
    tree = ast.parse((_TESTS / "db" / "_service_fixture.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "spawn_service")
    names = {_call_name(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "popen_in_group" in names
    assert not names & _SPAWN_CALLS, "spawn_service spawns directly again; it must go through popen_in_group"


def test_the_primitive_records_engine_spawns_and_only_those() -> None:
    """The primitive's own wiring, read from its source: the record call sits behind the engine-argv test."""
    tree = ast.parse((_TESTS / "_child_process.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "popen_in_group")
    guarded = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Call) and _call_name(n.test) == "is_engine_jvm_argv"
        and any(isinstance(c, ast.Call) and _call_name(c) == "_record_engine_child" for c in ast.walk(n))
    ]
    assert guarded, "popen_in_group no longer records engine-JVM spawns"


# ── the matcher, on snippets ──────────────────────────────────────────────────

_HEAD = "import subprocess\n"


def _kinds(src: str) -> list[tuple[str, str]]:
    return [(s.kind, s.func) for s in scan_source(_HEAD + src, "t.py")]


@pytest.mark.parametrize(
    "src",
    [
        "def f(java, jar):\n    return subprocess.Popen([java, '-jar', jar])\n",
        # keyword argv
        "def f(java):\n    return subprocess.Popen(args=[java, '-jar', 'x'])\n",
        "def f(java):\n    return subprocess.run(args=jar_argv(java, 'x'))\n",
        # held in a name, renamed, derived, appended to
        "def f(java):\n    argv = [java, '-jar', 'x']\n    return subprocess.Popen(argv)\n",
        "def f(java):\n    a = [java, '-jar', 'x']\n    b = a\n    return subprocess.Popen(b)\n",
        "def f(java):\n    a = [java, '-jar', 'x']\n    b = list(a) + ['--port', '1']\n    return subprocess.Popen(b)\n",
        "def f(java):\n    cmd = [java]\n    cmd += engine_argv(java)\n    return subprocess.Popen(cmd)\n",
        "def f(java):\n    argv: list[str] = [java, '-jar', 'x']\n    return subprocess.Popen(argv)\n",
        # attribute target
        "class C:\n    def f(self, java):\n        self.argv = [java, '-jar', 'x']\n        return subprocess.Popen(self.argv)\n",
        # wrapper around the argv
        "def f(java):\n    return subprocess.Popen(with_substrate_sizing([java, '-jar', 'x']))\n",
        # the other spawn calls
        "def f(java):\n    return subprocess.check_output([java, '-jar', 'x'])\n",
        # a module-level argv used inside a function
        "ARGV = ['java', '-jar', 'x']\ndef f():\n    return subprocess.Popen(ARGV)\n",
        # with-as does not excuse a bypass
        "def f(java):\n    with subprocess.Popen([java, '-jar', 'x']) as p:\n        return p.pid\n",
    ],
)
def test_every_bypass_shape_is_flagged(src) -> None:
    assert [k for k, _ in _kinds(src)] == ["bypass"]


@pytest.mark.parametrize(
    "src",
    [
        "def f(java):\n    return popen_in_group([java, '-jar', 'x'], env={})\n",
        "def f(j, k):\n    return popen_in_group(jar_argv(j, k), env={})\n",
        "def f(java):\n    return popen_in_group(argv=engine_argv(java), popen=subprocess.Popen)\n",
        "def f(java):\n    return spawn_service(cmd=[java, '-jar', 'x'], env={})\n",
        # result shapes: none of them is the lint's business
        "def f(java):\n    with popen_in_group([java, '-jar', 'x']) as p:\n        return p.pid\n",
        "class C:\n    def f(self, java):\n        self.p = popen_in_group([java, '-jar', 'x'])\n",
        "def f(java, fh):\n    return popen_in_group([java, '-jar', 'x']), fh\n",
        "def f(java):\n    argv = [java, '-jar', 'x']\n    p = popen_in_group(argv)\n    return p\n",
    ],
)
def test_the_primitive_is_never_flagged_whatever_happens_to_its_result(src) -> None:
    assert [k for k, _ in _kinds(src)] == ["primitive"]


@pytest.mark.parametrize(
    "src",
    [
        "def builder(java):\n    return [java, '-jar', 'x']\n",
        "def check(argv):\n    assert argv == ['java', '-jar', 'x']\n",
        "def check2(java):\n    assert jar_argv(java, 'x')[2] == '-jar'\n",
        # a spawn of something else, in a file that also names an argv
        "def f():\n    a = ['java', '-jar', 'x']\n    return subprocess.Popen(['ls'])\n",
        # the same variable name, tainted in another function only
        "def f():\n    argv = ['java', '-jar', 'x']\n    return argv\ndef g():\n    argv = ['ls']\n    return subprocess.Popen(argv)\n",
    ],
)
def test_an_argv_that_reaches_no_spawn_is_not_a_site(src) -> None:
    assert _kinds(src) == []


def test_a_new_file_with_a_bypass_fails_the_tree_scan(tmp_path) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_ok.py").write_text(_HEAD + "def f(j):\n    return popen_in_group([j, '-jar', 'x'])\n")
    assert bypasses(scan_tree(tests, repo)) == []
    (tests / "test_new.py").write_text(_HEAD + "def g(j):\n    return subprocess.Popen([j, '-jar', 'x'])\n")
    assert [(s.path, s.func) for s in bypasses(scan_tree(tests, repo))] == [("tests/test_new.py", "g")]
