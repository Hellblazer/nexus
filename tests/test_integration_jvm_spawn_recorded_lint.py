# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every test-tree site that spawns the engine JVM must record the child for the crash sweep (nexus-qsqc6).

WHY. An integration fixture makes its Postgres cluster under a chosen root (tmpfs or ``NX_TEST_PG_ROOT``)
and starts the engine JVM in another step. The owner record that guards the cluster
(``tests/_engine_substrate.py`` ``_flush_owner_record``) lists the engine children too, so a killed run's
sweep stops the JVM before it removes the cluster. The child gets in only through
``record_integration_child(proc)``. :func:`tests.db._service_fixture.spawn_service` calls it for every
caller; a fixture that builds its own ``Popen`` has to call it itself, and nothing made it. A JVM left
running on a deleted cluster keeps its heap, and its port, until someone finds it by hand.

WHAT IT CHECKS (AST over every ``tests/**/*.py``, this file excluded).
- A *launch argv* is a list or tuple literal holding the string ``"-jar"``, or a call to ``jar_argv`` or
  ``engine_argv``. It is followed through wrapper calls (``with_substrate_sizing(...)``) and through a
  name it is assigned to, within the same function, to a call that spawns it: ``spawn_service`` (the
  *launcher* kind) or ``Popen`` / ``popen_in_group`` / ``subprocess.run`` and its siblings (the *direct*
  kind). A launch argv that reaches no spawn call is not a site: an assertion on an argv, a builder that
  returns one.
- A launcher site is covered because ``spawn_service`` records inside itself; a test pins that.
- A direct site must call ``record_integration_child(x)`` in the same function, with ``x`` the name the
  spawn call's result is assigned to (or the spawn call itself). A call on some other process does not
  count. Otherwise the site must be in :data:`EXEMPT_DIRECT_SPAWNS` with a reason.

NOT COVERED, on purpose. An argv built dynamically, one passed into a helper that spawns it, ``java -cp``
and non-Python launchers are invisible to a static check. The daemon tests that spawn ``nx daemon service
start`` start the JVM through the supervisor, not from a fixture.
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

_RECORD = "record_integration_child"
_LAUNCHERS = frozenset({"spawn_service"})
_SPAWN_CALLS = frozenset(
    {"Popen", "popen_in_group", "run", "call", "check_call", "check_output", "create_subprocess_exec"}
)
_ARGV_BUILDERS = frozenset({"jar_argv", "engine_argv"})

#: A direct JVM spawn that does not call ``record_integration_child``, with the reason. Keyed by
#: (path relative to the repo root, enclosing function). An entry must match a live direct site that does
#: NOT record, so a fixed or removed site cannot leave a stale excuse behind.
EXEMPT_DIRECT_SPAWNS: dict[tuple[str, str], str] = {
    ("tests/_engine_substrate.py", "_boot"): (
        "the substrate engine is guarded by the substrate sidecar, whose engine_pid leg the sweep stops "
        "before it removes the cluster; it makes no integration owner record"
    ),
}

#: Ceiling on the table, so an exemption is a decision and not a habit.
EXEMPT_CEILING = 1
_MIN_REASON_WORDS = 8


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    func: str
    kind: str  # "launcher" | "direct"
    recorded: bool


def _call_name(call: ast.Call) -> str:
    fn = call.func
    return fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")


def _is_launch_argv(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(isinstance(e, ast.Constant) and e.value == "-jar" for e in node.elts)
    return isinstance(node, ast.Call) and _call_name(node) in _ARGV_BUILDERS


def _enclosing_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return cur


def _arg_values(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(kw.value for kw in call.keywords)]


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {n for e in target.elts for n in _target_names(e)}
    return set()


def _spawn_calls_for(launch: ast.AST, parents: dict[ast.AST, ast.AST], scope: ast.AST) -> list[ast.Call]:
    """The spawn / launcher calls a launch argv flows into, within *scope*."""
    cur = launch
    while cur in parents:
        parent = parents[cur]
        if isinstance(parent, ast.Call) and cur in _arg_values(parent):
            name = _call_name(parent)
            if name in _LAUNCHERS or name in _SPAWN_CALLS:
                return [parent]
            cur = parent  # a wrapper such as with_substrate_sizing(argv)
        elif isinstance(parent, (ast.List, ast.Tuple, ast.Starred, ast.IfExp, ast.BinOp)):
            cur = parent
        elif isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            names = {n for t in targets for n in _target_names(t)}
            return [
                c for c in ast.walk(scope)
                if isinstance(c, ast.Call)
                and (_call_name(c) in _LAUNCHERS or _call_name(c) in _SPAWN_CALLS)
                and any(
                    isinstance(n, ast.Name) and n.id in names
                    for a in _arg_values(c) for n in ast.walk(a)
                )
            ]
        else:
            return []
    return []


def _call_is_recorded(call: ast.Call, scope: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Does *scope* hand this spawn's process to ``record_integration_child``?"""
    names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and any(c is call for c in ast.walk(node.value)):
            names |= {n for t in node.targets for n in _target_names(t)}
    for node in ast.walk(scope):
        if isinstance(node, ast.Call) and _call_name(node) == _RECORD and node.args:
            arg = node.args[0]
            if arg is call or (isinstance(arg, ast.Name) and arg.id in names):
                return True
    return False


def scan_source(source: str, label: str) -> list[Site]:
    """Every JVM spawn site in one source text."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    seen: dict[int, Site] = {}
    for node in ast.walk(tree):
        if not _is_launch_argv(node):
            continue
        scope = _enclosing_scope(node, parents)
        for call in _spawn_calls_for(node, parents, scope):
            if id(call) in seen:
                continue
            kind = "launcher" if _call_name(call) in _LAUNCHERS else "direct"
            func = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
            recorded = kind == "launcher" or _call_is_recorded(call, scope, parents)
            seen[id(call)] = Site(label, call.lineno, func, kind, recorded)
    return sorted(seen.values(), key=lambda s: s.line)


def scan_tree(tests_dir: Path, repo_root: Path) -> list[Site]:
    """Every JVM spawn site under *tests_dir*, this file excluded."""
    out: list[Site] = []
    for p in sorted(tests_dir.rglob("*.py")):
        if _SKIP_PARTS.intersection(p.parts) or p.resolve() == _SELF:
            continue
        out.extend(scan_source(p.read_text(encoding="utf-8"), p.relative_to(repo_root).as_posix()))
    return out


def unrecorded_sites(sites: list[Site], exempt: dict[tuple[str, str], str]) -> list[Site]:
    return [s for s in sites if not s.recorded and (s.path, s.func) not in exempt]


def stale_exemptions(sites: list[Site], exempt: dict[tuple[str, str], str]) -> list[tuple[str, str]]:
    """Exemptions that match no live direct site that fails to record."""
    live = {(s.path, s.func) for s in sites if s.kind == "direct" and not s.recorded}
    return sorted(k for k in exempt if k not in live)


# ── the live tree ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_sites() -> list[Site]:
    return scan_tree(_TESTS, _REPO_ROOT)


def test_every_jvm_spawn_in_the_test_tree_records_itself_or_is_exempt(live_sites) -> None:
    bad = unrecorded_sites(live_sites, EXEMPT_DIRECT_SPAWNS)
    assert not bad, (
        "these sites spawn the engine JVM without record_integration_child(proc), so a killed run's sweep "
        "cannot stop it with its cluster (nexus-qsqc6). Record the child, or add an EXEMPT_DIRECT_SPAWNS "
        "entry with a reason:\n" + "\n".join(f"  {s.path}:{s.line} in {s.func}" for s in bad)
    )


def test_the_scan_is_not_vacuous(live_sites) -> None:
    """A scan that examined nothing passes everything. The tree holds a known set of sites; losing one means
    the matcher broke or a fixture moved out of its sight."""
    launchers = [s for s in live_sites if s.kind == "launcher"]
    direct = [s for s in live_sites if s.kind == "direct"]
    assert len(launchers) >= 20, f"only {len(launchers)} spawn_service sites found; 24 at the time of writing"
    assert len(direct) >= 4, f"only {len(direct)} direct spawn sites found; 4 at the time of writing"
    known = {(s.path, s.func) for s in direct}
    for path, func in (
        ("tests/db/test_embed_parity.py", "_start_service"),
        ("tests/test_main_bootstrap_failure.py", "_spawn_engine"),
        ("tests/test_main_sigterm_during_model_init.py", "_spawn"),
        ("tests/_engine_substrate.py", "_boot"),
    ):
        assert (path, func) in known, f"known direct spawn {path}::{func} not found; found {sorted(known)}"


def test_the_exemption_table_is_small_justified_and_not_stale(live_sites) -> None:
    assert len(EXEMPT_DIRECT_SPAWNS) <= EXEMPT_CEILING
    for key, reason in EXEMPT_DIRECT_SPAWNS.items():
        assert len(reason.split()) >= _MIN_REASON_WORDS, f"{key}: say why in a sentence"
    assert stale_exemptions(live_sites, EXEMPT_DIRECT_SPAWNS) == [], (
        "an exemption matches no direct JVM spawn that lacks the record call; drop it"
    )


def test_spawn_service_records_every_child_it_starts() -> None:
    """The launcher kind is covered only because of this."""
    src = (_TESTS / "db" / "_service_fixture.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "spawn_service")
    spawns = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _call_name(c) in _SPAWN_CALLS]
    assert spawns, "spawn_service no longer spawns through Popen / popen_in_group; update this pin"
    assert all(_call_is_recorded(c, fn, parents) for c in spawns)


# ── the matcher, on snippets ──────────────────────────────────────────────────

_HEAD = "import subprocess\nfrom tests._engine_substrate import record_integration_child\n"


def _direct(src: str) -> list[Site]:
    return [s for s in scan_source(_HEAD + src, "t.py") if s.kind == "direct"]


def test_a_new_unrecorded_popen_of_the_jar_fails() -> None:
    sites = scan_source(
        _HEAD + "def f(java, jar):\n    return subprocess.Popen([java, '-jar', jar])\n", "tests/new.py"
    )
    assert [(s.func, s.recorded) for s in sites] == [("f", False)]
    assert unrecorded_sites(sites, {}) == sites


def test_the_same_spawn_passes_once_it_records_the_child() -> None:
    src = (
        "def f(java, jar):\n"
        "    proc = subprocess.Popen([java, '-jar', jar])\n"
        "    record_integration_child(proc)\n"
        "    return proc\n"
    )
    assert [s.recorded for s in _direct(src)] == [True]


@pytest.mark.parametrize(
    "src",
    [
        # argv held in a name
        "def f(java):\n    argv = [java, '-jar', 'x.jar']\n    p = subprocess.Popen(argv)\n    return p\n",
        # a helper builds the argv
        "def f(j, k):\n    return popen_in_group(jar_argv(j, k), env={})\n",
        "def f(java):\n    return popen_in_group(engine_argv(java), env={})\n",
        # a wrapper around the argv
        "def f(java):\n    return subprocess.Popen(with_substrate_sizing([java, '-jar', 'x']))\n",
        # subprocess.run is a spawn too
        "def f(java):\n    return subprocess.run([java, '-jar', 'x'])\n",
        # the result is returned inside a tuple, never named
        "def f(java, fh):\n    return subprocess.Popen([java, '-jar', 'x']), fh\n",
    ],
)
def test_the_spawn_shapes_in_use_are_all_seen_and_flagged(src) -> None:
    assert [s.recorded for s in _direct(src)] == [False]


def test_recording_some_other_process_does_not_count() -> None:
    src = (
        "def f(java, other):\n"
        "    proc = subprocess.Popen([java, '-jar', 'x'])\n"
        "    record_integration_child(other)\n"
        "    return proc\n"
    )
    assert [s.recorded for s in _direct(src)] == [False]


def test_a_record_call_in_another_function_does_not_count() -> None:
    src = (
        "def g(proc):\n    record_integration_child(proc)\n"
        "def f(java):\n    proc = subprocess.Popen([java, '-jar', 'x'])\n    return proc\n"
    )
    assert [(s.func, s.recorded) for s in _direct(src)] == [("f", False)]


def test_a_tuple_target_and_a_record_of_the_call_itself_both_count() -> None:
    named = (
        "def f(java):\n"
        "    proc, fh = subprocess.Popen([java, '-jar', 'x']), open('l')\n"
        "    record_integration_child(proc)\n"
        "    return proc, fh\n"
    )
    inline = "def f(java):\n    record_integration_child(subprocess.Popen([java, '-jar', 'x']))\n"
    assert [s.recorded for s in _direct(named)] == [True]
    assert [s.recorded for s in _direct(inline)] == [True]


def test_a_launch_argv_that_reaches_no_spawn_is_not_a_site() -> None:
    src = (
        "def builder(java):\n    return [java, '-jar', 'x']\n"
        "def check(argv):\n    assert argv == ['java', '-jar', 'x']\n"
        "def check2(java):\n    assert jar_argv(java, 'x')[2] == '-jar'\n"
    )
    assert scan_source(_HEAD + src, "t.py") == []


def test_spawn_service_is_the_launcher_kind_and_needs_no_call_of_its_own() -> None:
    sites = scan_source(_HEAD + "def f(j):\n    return spawn_service([j, '-jar', 'x'], {})\n", "t.py")
    assert [(s.kind, s.recorded) for s in sites] == [("launcher", True)]


def test_a_new_file_with_an_unrecorded_spawn_fails_the_tree_scan(tmp_path) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_ok.py").write_text(
        _HEAD + "def f(j):\n    p = subprocess.Popen([j, '-jar', 'x'])\n    record_integration_child(p)\n"
    )
    assert unrecorded_sites(scan_tree(tests, repo), {}) == []
    (tests / "test_new.py").write_text(_HEAD + "def g(j):\n    return subprocess.Popen([j, '-jar', 'x'])\n")
    bad = unrecorded_sites(scan_tree(tests, repo), {})
    assert [(s.path, s.func) for s in bad] == [("tests/test_new.py", "g")]


def test_an_exemption_is_stale_once_its_site_records_or_goes_away() -> None:
    exempt = {("tests/x.py", "f"): "x"}
    unrecorded = [Site("tests/x.py", 3, "f", "direct", False)]
    assert stale_exemptions(unrecorded, exempt) == []
    assert unrecorded_sites(unrecorded, exempt) == []
    assert stale_exemptions([Site("tests/x.py", 3, "f", "direct", True)], exempt) == [("tests/x.py", "f")]
    assert stale_exemptions([], exempt) == [("tests/x.py", "f")]
