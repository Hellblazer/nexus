# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census of the test tree's Postgres cluster start sites, against a committed allowlist (nexus-sz651).

``tests/_ddl_counter.py`` counts the DDL and the bytes of ONE kind of cluster: the engine
substrate's. Every other cluster a test starts (``throwaway_pg_cluster``, the ``pg_instance``
fixtures of the integration modules, the product's own provisioning under test) is invisible to
it, and a number that silently omits some of the work reads as a smaller number. This module finds
those clusters in the source so the omission is a visible, committed figure, not an absence.

A "start site" is a call to ``pg_ctl_start`` (the one helper every test cluster start goes
through) or to ``exe_name("initdb")`` (a test that bootstraps a data directory itself), keyed by
file, outermost enclosing function (``<module>`` at module level) and kind. ``COUNTED`` are the
sites whose cluster the counter reads; every other site is in the ``[uncounted]`` table of
``tests/ddl_uncounted_clusters.toml`` with how many times it occurs. A new site that is in neither
fails ``tests/test_ddl_uncounted_clusters_lint.py``: it must be counted or listed on purpose.

The figure is a STATIC CENSUS OF CALL SITES, labelled "uncounted cluster start sites" wherever it
prints: not a count of clusters that ran (it is the same on every run, whichever tests ran), not
weighted by size, and not complete. Docker-run sites and e2e shell clusters are not scanned.

Scope, stated so the figure is not trusted past it: only the Python test tree and ``scripts/`` are
scanned (product code under ``src/`` provisions Postgres for real use, and tests that run it are
listed through their own ``initdb`` lookups); the Java suite is gated by
``PgContainerTmpfsGateTest`` (every container goes through ``PgContainerHelper``) and its one
uncounted cluster, the jOOQ code generator's build-time container, is entered here by hand.

    uv run python -m tests._ddl_cluster_sites --write   # regenerate the allowlist from the tree

Second half (nexus-szyun): WHERE each test cluster's data directory comes from. A cluster made
under ``tmp_path`` / ``tmp_path_factory`` / ``mkdtemp`` writes its data and WAL to the CI host's disk,
outside the substrate's root chooser (tmpfs on Linux, decided once per run, counted in the "clusters
on tmpfs N, on disk M" summary). :func:`data_dir_uses` finds every ``initdb`` / ``postgres`` argv, ``_init_cluster``,
``pg_ctl_start`` and ``provision(<dir>)`` call (also through ``from m import x as y``) and traces its data-dir
argument back to ``tests._child_process.pg_data_tempdir``; an argv it cannot read (splat, ``-Dx`` / ``--pgdata=x``,
shell string, a variable argv in a file that starts a cluster) is itself a use to list. ``provision(<dir>)`` nests the
cluster at ``<dir>/postgres``; the crash sweep reads that nested ``postmaster.pid`` (nexus-0ajjm), so ``<dir>`` itself
must be the helper's result (a traced use, like an initdb data dir), a join under it is rejected, and any other
``<dir>`` must be listed. A ``provision(x)`` whose ``x`` is rooted at a parameter naming a fixture of the same file
that returns or yields a helper-made dir re-provisions a dir another function made, and is accepted in a file that makes
one through the helper; any other parameter, a fixture returning ``tmp_path``, ``self.x`` and the like are flagged. :func:`provenance_problems` is what
``tests/test_ddl_uncounted_clusters_lint.py`` asserts is empty. The files that do not (or cannot) use the helper are
listed WITH A REASON in ``tests/pg_data_dir_exceptions.toml``, whose two tables have ceilings and may only shrink
(``disk_required`` is empty and its ceiling 0 since nexus-0ajjm).
:func:`docker_pg_problems` checks that a ``docker run`` of a postgres image mounts its data dir as tmpfs. What none of
this scans is itemised in the lint test's docstring.
"""
from __future__ import annotations

import ast
import functools
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST = Path(__file__).resolve().parent / "ddl_uncounted_clusters.toml"
EXCEPTIONS = Path(__file__).resolve().parent / "pg_data_dir_exceptions.toml"

#: Sites whose cluster ``tests/_ddl_counter.py`` reads (the engine substrate's ``_boot``).
COUNTED: tuple[str, ...] = ("tests/_engine_substrate.py::_boot::pg_ctl_start",)

#: The jOOQ code generator boots a Postgres container at build time (service/pom.xml); no counter reads it.
POM_SITE = "service/pom.xml::testcontainers-jooq-codegen-maven-plugin"
_POM_MARKER = "<artifactId>testcontainers-jooq-codegen-maven-plugin</artifactId>"

_SCANNED = ("tests", "scripts")


def _site_kind(call: ast.Call) -> str | None:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name == "pg_ctl_start":
        return "pg_ctl_start"
    if (
        name == "exe_name"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "initdb"
    ):
        return "initdb"
    return None


def scan(repo: Path = REPO) -> Counter[str]:
    """Every start site under ``tests/`` and ``scripts/`` (plus the pom's code generator), with its count."""
    sites: Counter[str] = Counter()
    for top in _SCANNED:
        for path in sorted((repo / top).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            rel = path.relative_to(repo).as_posix()
            stack: list[str] = []

            class _Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

                def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                    kind = _site_kind(node)
                    if kind is not None:
                        sites[f"{rel}::{stack[0] if stack else '<module>'}::{kind}"] += 1
                    self.generic_visit(node)

            _Visitor().visit(tree)
    pom = repo / "service" / "pom.xml"
    if pom.is_file():
        if _POM_MARKER in pom.read_text(encoding="utf-8"):
            sites[POM_SITE] = 1  # one cluster per build, however many declarations (profiles, lifecycle mapping)
    return sites


# ── Where the data directory comes from (nexus-szyun) ────────────────────────

#: The one helper whose result a test cluster's data directory may be.
DATA_DIR_HELPER = "pg_data_tempdir"

#: Files the provenance rule does not apply to: the substrate IS the root chooser (its own
#: ``_boot`` / ``throwaway_pg_cluster`` choose the directory), ``_pg_ctl`` defines ``pg_ctl_start``
#: (its argv is the helper's own), and this module names the calls it looks for.
PROVENANCE_EXEMPT: tuple[str, ...] = (
    "tests/_engine_substrate.py",
    "tests/_pg_ctl.py",
    "tests/_ddl_cluster_sites.py",
)

#: Wrappers that keep a path a path: ``str(x)``, ``Path(x)``.
_PATH_WRAPPERS = frozenset({"str", "Path", "PurePath", "fspath"})

#: A literal argv head that names one of the programs: a bare name or a path to it, with no spaces.
_PROGRAM_PATH = re.compile(r"(^|[/\\])(initdb|pg_ctl|postgres)(\.exe)?$")

#: Call names the scan follows (also through ``from m import name as alias``).
_TRACKED_CALLS = frozenset({"pg_ctl_start", "_init_cluster", "provision", DATA_DIR_HELPER})

#: Calls whose first argument is an argv (or a shell string).
_SUBPROCESS_CALLS = frozenset({
    "run", "Popen", "check_call", "check_output", "call", "create_subprocess_exec", "create_subprocess_shell",
})

#: Ceilings on the exceptions table: it may only shrink. ``disk_required`` held the five nested-pgdata
#: ``provision()`` files until nexus-0ajjm taught the owner-record sweep a nested ``postmaster.pid`` and moved them
#: to the helper; it is empty. ``not_a_cluster`` is the files that name initdb / ``provision`` but never run a
#: postmaster. Raising either number is a decision to make on purpose, in a diff a reviewer reads.
#: The five files whose clusters come from ``pg_provision.provision(config_dir)`` (nexus-0ajjm): ``config_dir`` is a
#: ``pg_data_tempdir`` dir, the cluster sits at ``<config_dir>/postgres`` and the owner-record sweep reads that. ONE
#: definition: the lint test and ``tests/test_integration_pg_cluster.py`` (the conf census) both read it. Repo-relative.
PROVISION_DRIVEN: tuple[str, ...] = (
    "tests/db/test_pg_provision.py",
    "tests/db/test_pg_provision_ca3_bundle.py",
    "tests/db/test_pg_bundle_relocation.py",
    "tests/db/test_pg_scram_auth.py",
    "tests/daemon/test_storage_service_daemon_pg_monitor_backfill.py",
)
NOT_A_CLUSTER_CEILING = 9
DISK_REQUIRED_CEILING = 0

#: Names pytest hands a test as a directory: a ``provision()`` on one is a new cluster on disk, never a dir a
#: fixture made through the helper.
_PYTEST_DIR_FIXTURES = frozenset({"tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"})

#: A bead id: every ``disk_required`` reason names the follow-up that drains it.
_BEAD_ID = re.compile(r"\bnexus-[0-9a-z]{4,}\b")

#: The ``DataDirUse.kind`` values. ``init_cluster`` and ``pg_ctl_start`` are calls; ``initdb``,
#: ``pg_ctl_start_argv`` and ``postgres_argv`` are literal argv lists with a ``-D DIR`` (or
#: ``--pgdata DIR``); ``provision`` is the product's ``provision(config_dir)``, which nests the cluster at
#: ``<config_dir>/postgres`` (the owner-record sweep reads that nested pid file, nexus-0ajjm); ``opaque_argv`` is an
#: initdb / ``pg_ctl start`` / ``postgres`` argv whose data dir cannot be read from it (splat, ``-Dx`` / ``--pgdata=x`` attached, shell string, no ``-D``);
#: ``unknown_argv`` is a subprocess call whose argv is not a literal list, seen only in files that start a
#: cluster.
USE_KINDS: tuple[str, ...] = (
    "initdb", "init_cluster", "pg_ctl_start", "pg_ctl_start_argv", "postgres_argv",
    "provision", "opaque_argv", "unknown_argv",
)


@dataclass(frozen=True)
class DataDirUse:
    """One place a test hands a data directory to ``initdb`` / ``pg_ctl start`` / ``provision``."""

    file: str
    func: str
    kind: str  # one of USE_KINDS
    line: int
    expr: str
    from_helper: bool
    #: ``provision`` only: the directory handed in is built UNDER the helper's result (``Path(h()) / "cfg"``), a
    #: pgdata nested deeper than the recorded dir, which the owner-record sweep does not read.
    nests_helper: bool = False
    #: ``provision`` only: the directory is rooted at a parameter naming a same-file fixture that returns or
    #: yields a helper-made dir (directly, unpacked, or as an attribute of an object built from it), so the call
    #: re-provisions a dir another function made. It is not a new cluster and is not counted as a traced use.
    handed_in: bool = False


def _call_name(call: ast.Call, aliases: dict[str, str] | None = None) -> str | None:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name is not None and aliases and isinstance(func, ast.Name):
        return aliases.get(name, name)
    return name


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """``from m import pg_ctl_start as _start`` -> ``{"_start": "pg_ctl_start"}`` for the tracked names."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.asname and a.name in _TRACKED_CALLS:
                    out[a.asname] = a.name
    return out


@functools.lru_cache(maxsize=8192)
def _scope_assignments(scope: ast.AST) -> dict[str, list[ast.expr | None]]:
    """name -> every value assigned to it directly in *scope* (not in nested defs); ``None`` marks an
    assignment whose value is not a single expression (``for``/``with`` targets, ``+=``, unpacking a literal tuple);
    unpacking any other value binds each name to ``value[i]``.
    Read-only: the result is cached per scope node."""
    out: dict[str, list[ast.expr | None]] = {}

    def bind(target: ast.expr, value: ast.expr | None) -> None:
        if isinstance(target, ast.Name):
            out.setdefault(target.id, []).append(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for i, elt in enumerate(target.elts):
                # `a, b = fixture_value` binds each name to an element of it; a literal tuple on the right,
                # a `for` and a `with` target stay unknown.
                if value is not None and not isinstance(value, (ast.Tuple, ast.List)):
                    bind(elt, ast.Subscript(value=value, slice=ast.Constant(i), ctx=ast.Load()))
                else:
                    bind(elt, None)
        elif isinstance(target, ast.Starred):
            bind(target.value, None)

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    bind(t, child.value)
            elif isinstance(child, ast.AnnAssign) and child.value is not None:
                bind(child.target, child.value)
            elif isinstance(child, ast.AugAssign):
                bind(child.target, None)  # `pgdata += "/data"` is a nested data dir, never the helper's result
            elif isinstance(child, ast.NamedExpr):
                bind(child.target, child.value)
            elif isinstance(child, (ast.For, ast.AsyncFor)):
                bind(child.target, None)
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None:
                        bind(item.optional_vars, None)
            walk(child)

    walk(scope)
    return out


def _params(fn: ast.AST) -> set[str]:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    a = fn.args
    names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return set(names)


def _from_helper(
    expr: ast.expr | None, scopes: list[ast.AST], aliases: dict[str, str], seen: frozenset[str] = frozenset(),
) -> bool:
    """True when *expr* is the helper's result, possibly through ``str``/``Path`` and plain names bound
    in the use's own function or the module. A parameter, an f-string, a ``/`` join or ``+=`` (a NESTED
    data dir: the owner-record sweep reads ``postmaster.pid`` at the top of the recorded dir only), a helper
    call with ``parent_dir=`` (which skips the root chooser) and anything unknown are all False."""
    if isinstance(expr, ast.Call):
        name = _call_name(expr, aliases)
        if name == DATA_DIR_HELPER:
            return not any(kw.arg == "parent_dir" or kw.arg is None for kw in expr.keywords) and len(expr.args) <= 1
        if name in _PATH_WRAPPERS and len(expr.args) == 1 and not expr.keywords:
            return _from_helper(expr.args[0], scopes, aliases, seen)
        return False
    if isinstance(expr, ast.Name):
        if expr.id in seen:
            return False
        for scope in scopes:  # innermost first
            if expr.id in _params(scope):
                return False
            values = _scope_assignments(scope).get(expr.id)
            if values:
                return all(_from_helper(v, scopes, aliases, seen | {expr.id}) for v in values)
        return False
    return False


def _is_fixture(fn: ast.AST) -> bool:
    """A ``def`` decorated with ``fixture`` / ``pytest.fixture`` / ``pytest.fixture(...)``."""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (isinstance(target, ast.Name) and target.id == "fixture") or (
            isinstance(target, ast.Attribute) and target.attr == "fixture"
        ):
            return True
    return False


def _fixture_values(fn: ast.AST) -> list[ast.expr | None]:
    """Every value a fixture returns or yields (not in nested defs); ``[]`` when it gives none."""
    out: list[ast.expr | None] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Return, ast.Yield)) and child.value is not None:
                out.append(child.value)
            walk(child)

    walk(fn)
    return out


def _has_default(fn: ast.AST, name: str) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    defaulted = {x.arg for x in positional[len(positional) - len(a.defaults):]} if a.defaults else set()
    defaulted |= {x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None}
    return name in defaulted


def _handed_in(expr: ast.expr | None, scopes: list[ast.AST], aliases: dict[str, str]) -> bool:
    """True when *expr* is a dir that a same-file FIXTURE made through the helper, handed to the use as a parameter.

    The root of *expr* (through ``str``/``Path``, attributes, subscripts and a tuple unpack) must be a parameter of
    the use's function that names a fixture defined in this file, and every such fixture must return or yield a
    value that carries ``pg_data_tempdir(...)``: the dir itself, element *i* of a tuple (``a, b = fx`` binds
    ``fx[i]``), or an object built from it (an attribute of ``Box(config_dir, ...)``). A bare parameter is accepted
    on the same terms. A parameter with a default, ``self``/``request``, a name that is no fixture here, a fixture
    that returns a pytest dir fixture, a ``/`` join, an f-string, a call, a ``for``/``with`` target and anything
    unknown are all False. Fixtures that take other fixtures are followed, not trusted."""
    tree = scopes[-1]
    fixtures: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if _is_fixture(node):
            fixtures.setdefault(node.name, []).append(node)  # type: ignore[attr-defined]

    visiting: set[tuple[int, str]] = set()  # a name being resolved: `d = d` must not loop

    def fixture_value_ok(name: str, idx: int | None, container: bool, seen_fx: frozenset[str]) -> bool:
        defs = fixtures.get(name)
        if not defs or name in seen_fx:
            return False
        for fn in defs:
            values = _fixture_values(fn)
            fscopes = [fn, tree]
            if not values or not all(
                v is not None and (
                    container_ok(v, idx, fscopes, seen_fx | {name}) if container else dir_ok(v, fscopes, seen_fx | {name})
                )
                for v in values
            ):
                return False
        return True

    def container_ok(base: ast.expr, idx: int | None, sc: list[ast.AST], seen_fx: frozenset[str]) -> bool:
        """*base* holds a helper-made dir at element *idx* (any element or attribute when ``None``)."""
        if isinstance(base, (ast.Tuple, ast.List)):
            if any(isinstance(e, ast.Starred) for e in base.elts):
                return False
            if idx is None:
                return any(dir_ok(e, sc, seen_fx) for e in base.elts)
            return idx < len(base.elts) and dir_ok(base.elts[idx], sc, seen_fx)
        if isinstance(base, ast.Call):
            if idx is not None or _call_name(base, aliases) in _PATH_WRAPPERS:
                return False
            return any(dir_ok(a, sc, seen_fx) for a in (*base.args, *(k.value for k in base.keywords)))
        if isinstance(base, (ast.Attribute, ast.Subscript)):
            return container_ok(base.value, None, sc, seen_fx)
        if isinstance(base, ast.Name):
            key = (id(sc[0]), base.id)
            if key in visiting:
                return False
            for scope in sc:  # innermost first
                values = _scope_assignments(scope).get(base.id)
                if values:
                    visiting.add(key)
                    try:
                        return all(v is not None and container_ok(v, idx, sc, seen_fx) for v in values)
                    finally:
                        visiting.discard(key)
                if base.id in _params(scope):
                    return not _has_default(scope, base.id) and fixture_value_ok(base.id, idx, True, seen_fx)
        return False

    def dir_ok(e: ast.expr | None, sc: list[ast.AST], seen_fx: frozenset[str]) -> bool:
        """*e* is a helper-made dir, a bare fixture parameter included."""
        if e is None:
            return False
        if _from_helper(e, sc, aliases):
            return True
        if isinstance(e, ast.Call):
            if _call_name(e, aliases) in _PATH_WRAPPERS and len(e.args) == 1 and not e.keywords:
                return dir_ok(e.args[0], sc, seen_fx)
            return False
        if isinstance(e, ast.Subscript):
            i = e.slice.value if isinstance(e.slice, ast.Constant) and type(e.slice.value) is int else None
            return i is not None and container_ok(e.value, i, sc, seen_fx)
        if isinstance(e, ast.Attribute):
            return container_ok(e.value, None, sc, seen_fx)
        if isinstance(e, ast.Name):
            key = (id(sc[0]), e.id)
            if key in visiting:
                return False
            for scope in sc:  # innermost first
                values = _scope_assignments(scope).get(e.id)
                if values:  # a reassigned parameter is judged by what it is reassigned to
                    visiting.add(key)
                    try:
                        return all(v is not None and dir_ok(v, sc, seen_fx) for v in values)
                    finally:
                        visiting.discard(key)
                if e.id in _params(scope):
                    return not _has_default(scope, e.id) and fixture_value_ok(e.id, None, False, seen_fx)
        return False

    return dir_ok(expr, scopes, frozenset())


def _mentions_helper(expr: ast.expr | None, scopes: list[ast.AST], aliases: dict[str, str]) -> bool:
    """True when *expr* is the helper's result or is built from it: a call to it inside the expression
    (``Path(h()) / "cfg"``) or a name bound to its result (``d = h(); Path(d) / "cfg"``)."""
    if expr is None:
        return False
    if _from_helper(expr, scopes, aliases):
        return True
    return any(
        (isinstance(n, ast.Call) and _call_name(n, aliases) == DATA_DIR_HELPER)
        or (isinstance(n, ast.Name) and _from_helper(n, scopes, aliases))
        for n in ast.walk(expr)
    )


def _prefix_text(e: ast.expr) -> str | None:
    """The leading literal text of a string-building expression, or None."""
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return e.value
    if isinstance(e, ast.JoinedStr) and e.values and isinstance(e.values[0], ast.Constant):
        return str(e.values[0].value)
    if isinstance(e, ast.BinOp) and isinstance(e.op, (ast.Add, ast.Mod)):
        return _prefix_text(e.left)
    return None


def _attached_dir_flag(e: ast.expr) -> bool:
    """``--pgdata=DIR`` or ``-DDIR`` written as ONE argv element (a Constant, f-string or concatenation)."""
    prefix = _prefix_text(e)
    if prefix is None:
        return False
    if prefix.startswith("--pgdata="):
        return True
    if prefix.startswith("-D"):
        return not (isinstance(e, ast.Constant) and prefix == "-D")
    return False


def _classify_argv(arg: ast.expr, min_len: int = 2) -> tuple[str, ast.expr | None] | None:
    """For a literal argv list that runs initdb, ``pg_ctl start`` or ``postgres``: ``(kind, DIR)``, with
    ``kind == "opaque_argv"`` and DIR None when the data dir cannot be read from it. None when the list is
    not such an argv. *min_len* is 1 only for the argv of a subprocess call (`cmd = [exe]` then `cmd += [...]`)."""
    if not isinstance(arg, (ast.List, ast.Tuple)) or len(arg.elts) < min_len:
        return None
    elts = arg.elts
    if isinstance(elts[0], (ast.Starred, ast.Tuple, ast.List)):
        return None
    head = ast.unparse(elts[0]).lower()
    if isinstance(elts[0], ast.Constant) and not _PROGRAM_PATH.search(str(elts[0].value).lower()):
        return None  # a string that is not a program path (a parametrize id, a shell line) is not an argv head
    exe = "pg_ctl" if "pg_ctl" in head else "initdb" if "initdb" in head else "postgres" if "postgres" in head else None
    if exe is None:
        return None
    splat = any(isinstance(e, ast.Starred) for e in elts)
    attached = any(_attached_dir_flag(e) for e in elts[1:])
    flag = next(
        (
            i for i, e in enumerate(elts)
            if i > 0 and isinstance(e, ast.Constant) and e.value in ("-D", "--pgdata") and i + 1 < len(elts)
        ),
        None,
    )
    if flag is None and not attached and not splat and any(
        isinstance(e, ast.Constant) and e.value in ("--version", "-V", "--help", "-?") for e in elts[1:]
    ):
        return None  # a version / usage probe starts nothing
    if exe == "pg_ctl":
        starts = splat or any(isinstance(e, ast.Constant) and e.value == "start" for e in elts)
        if not starts:
            return None  # stop / status / reload / init: no data dir is being created
        kind = "pg_ctl_start_argv"
    elif exe == "initdb":
        kind = "initdb"
    else:
        if flag is None and not attached and not splat:
            return None  # `postgres --version`, `postgres --help`
        kind = "postgres_argv"
    if flag is None or splat or attached:
        return "opaque_argv", None
    return kind, elts[flag + 1]


def _shell_string_runs_a_cluster(arg: ast.expr) -> bool:
    text = ast.unparse(arg).lower() if isinstance(arg, (ast.Constant, ast.JoinedStr, ast.BinOp)) else ""
    if not text or (isinstance(arg, ast.Constant) and not isinstance(arg.value, str)):
        return False
    return "initdb" in text or "pg_ctl" in text or "postgres -d" in text


def _resolve_list(arg: ast.expr, scopes: list[ast.AST]) -> ast.expr:
    """A plain name bound to a list or tuple (and otherwise only augmented), in the use's scopes, resolves to that list."""
    if isinstance(arg, ast.Name):
        for scope in scopes:
            if arg.id in _params(scope):
                return arg
            values = _scope_assignments(scope).get(arg.id)
            if values:
                lists = [v for v in values if isinstance(v, (ast.List, ast.Tuple))]
                if lists and all(v is None or isinstance(v, (ast.List, ast.Tuple)) for v in values):
                    return lists[0]  # `cmd = [exe, ...]` then `cmd += [...]`: the first list names the program
                return arg
    return arg


def data_dir_uses(repo: Path = REPO) -> list[DataDirUse]:
    """Every data-dir handed to initdb / ``_init_cluster`` / ``pg_ctl_start`` / ``provision`` under ``tests/``.
    ``scripts/`` is not scanned: its Windows smoke tools are standalone programs run on a Windows host, not
    part of a test run, and cannot import the helper."""
    uses: list[DataDirUse] = []
    census = {k.split("::")[0] for k in scan(repo) if k.startswith("tests/")}
    for path in sorted((repo / "tests").rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        if rel in PROVENANCE_EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        aliases = _import_aliases(tree)
        starts_clusters = rel in census
        chain: list[ast.AST] = []

        class _Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
                chain.append(node)
                self.generic_visit(node)
                chain.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

            def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                scopes = [*reversed(chain), tree]
                found: list[tuple[str, ast.expr | None, bool]] = []  # (kind, dir expr, nests_helper)
                name = _call_name(node, aliases)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                if name in ("pg_ctl_start", "_init_cluster"):
                    found.append((
                        "pg_ctl_start" if name == "pg_ctl_start" else "init_cluster",
                        node.args[1] if len(node.args) > 1 else kw.get("pgdata"),
                        False,
                    ))
                elif name == "provision" and (node.args or "config_dir" in kw):
                    cfg = node.args[0] if node.args else kw["config_dir"]
                    made = _from_helper(cfg, scopes, aliases)
                    found.append(("provision", cfg, _mentions_helper(cfg, scopes, aliases) and not made))
                for arg in (*node.args, *(k.value for k in node.keywords)):
                    first_of_subprocess = name in _SUBPROCESS_CALLS and bool(node.args) and arg is node.args[0]
                    hit = _classify_argv(_resolve_list(arg, scopes), 1 if first_of_subprocess else 2)
                    if hit is not None:
                        found.append((hit[0], hit[1] if hit[1] is not None else arg, False))
                if name in _SUBPROCESS_CALLS and node.args:
                    first = node.args[0]
                    resolved = _resolve_list(first, scopes)
                    if _shell_string_runs_a_cluster(first):
                        found.append(("opaque_argv", first, False))
                    elif starts_clusters and not isinstance(resolved, (ast.List, ast.Tuple, ast.Constant, ast.JoinedStr)):
                        found.append(("unknown_argv", first, False))
                for kind, expr, nests in found:
                    uses.append(DataDirUse(
                        file=rel,
                        func=chain[0].name if chain else "<module>",  # type: ignore[attr-defined]
                        kind=kind,
                        line=node.lineno,
                        expr=ast.unparse(expr) if expr is not None else "<missing>",
                        from_helper=kind not in ("opaque_argv", "unknown_argv") and _from_helper(expr, scopes, aliases),
                        nests_helper=nests,
                        handed_in=(
                            kind == "provision" and not nests
                            and not _from_helper(expr, scopes, aliases) and _handed_in(expr, scopes, aliases)
                        ),
                    ))
                self.generic_visit(node)

        _Visitor().visit(tree)
    return uses


def exceptions(path: Path = EXCEPTIONS) -> tuple[dict[str, str], dict[str, str]]:
    """``(not_a_cluster, disk_required)``: file -> reason, from ``tests/pg_data_dir_exceptions.toml``."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("not_a_cluster", {})), dict(data.get("disk_required", {}))


def provenance_problems(
    repo: Path = REPO,
    exceptions_path: Path | None = None,
    ceilings: tuple[int, int] = (NOT_A_CLUSTER_CEILING, DISK_REQUIRED_CEILING),
) -> list[str]:
    """Everything wrong with where test clusters get their data directory. Empty means clean.

    1. A use whose data dir is not the helper's result, in a file not listed in ``disk_required`` or
       ``not_a_cluster``. That covers a literal initdb / ``pg_ctl start`` / ``postgres`` argv, an argv whose
       data dir cannot be read (``opaque_argv``), a variable argv in a file that starts a cluster
       (``unknown_argv``), ``_init_cluster``, ``pg_ctl_start`` and ``provision(<dir>)``.
    2. ``provision(<dir>)`` where the dir is built UNDER the helper's result (``Path(h()) / "cfg"``): ``provision``
       nests the cluster at ``<dir>/postgres`` and the owner-record sweep reads ``postmaster.pid`` at the recorded
       dir and at its ``postgres/`` child only, so a deeper nest is a problem (listing does not excuse it).
       ``provision(h())`` itself is fine: a traced use, as an initdb data dir is (nexus-0ajjm). A dir handed in
       as a parameter is accepted only when the parameter names a fixture defined in the same file whose return or
       yield carries a helper-made dir (directly, through a tuple unpack, or as an attribute of an object built from
       it), and only in a file that also makes one through the helper; anything else is a problem.
    3. An unlisted file with fewer traced (helper-made) uses than census sites: each ``pg_ctl_start`` call and
       each ``exe_name("initdb")`` lookup is a cluster the scan must have traced, not one proof per file. A
       ``provision(h())`` is one traced use.
    4. A listed file that is gone, whose reason is not a sentence, whose ``disk_required`` reason names no
       bead, or that has no cluster start, ``provision`` or ``_init_cluster`` CALL left (the AST decides, so a
       word in a docstring or comment keeps nothing alive).
    5. A file listed under both tables.
    6. A table longer than its ceiling.
    """
    ex_path = exceptions_path or (repo / "tests" / EXCEPTIONS.name)
    not_cluster, disk = exceptions(ex_path)
    uses = data_dir_uses(repo)
    sites_per_file: Counter[str] = Counter()
    for key, n in scan(repo).items():
        f = key.split("::")[0]
        if f.startswith("tests/") and f.endswith(".py") and f not in PROVENANCE_EXEMPT:
            sites_per_file[f] += n
    census_files = set(sites_per_file)
    listed = set(not_cluster) | set(disk)
    problems: list[str] = []
    made_by_helper = {u.file for u in uses if u.kind == "provision" and u.from_helper}
    for both in sorted(set(not_cluster) & set(disk)):
        problems.append(f"{both}: listed in both not_a_cluster and disk_required")
    for u in uses:
        where = f"{u.file}:{u.line} ({u.func}, {u.kind})"
        listing = f"list the file in tests/{EXCEPTIONS.name} with a reason"
        if u.kind == "provision":
            if u.nests_helper:
                problems.append(
                    f"{where}: provision(`{u.expr}`) nests the cluster at <dir>/postgres under a {DATA_DIR_HELPER}() "
                    f"dir; the owner-record sweep reads postmaster.pid at the recorded dir and its postgres/ child "
                    f"only, so pass the helper's result itself"
                )
            elif u.from_helper:
                pass
            elif u.handed_in:
                if u.file not in made_by_helper and u.file not in listed:
                    problems.append(
                        f"{where}: provision(`{u.expr}`) re-provisions a dir handed in by a parameter or fixture, but "
                        f"no provision() in this file takes its dir from {DATA_DIR_HELPER}(); make it there, or {listing}"
                    )
            elif u.file not in listed:
                problems.append(
                    f"{where}: provision(`{u.expr}`) puts a cluster on a dir that is not made by {DATA_DIR_HELPER}(); "
                    f"use it, or {listing} (not_a_cluster, or disk_required naming the bead that drains it)"
                )
        elif u.kind == "unknown_argv":
            if u.file not in listed:
                problems.append(
                    f"{where}: `{u.expr}` is a subprocess argv the scan cannot read, in a file that starts a cluster; "
                    f"write the argv as a literal list with `-D {DATA_DIR_HELPER}(...)`, or {listing}"
                )
        elif not u.from_helper and u.file not in listed:
            problems.append(
                f"{where}: data dir `{u.expr}` is not {DATA_DIR_HELPER}(...) "
                f"(no parent_dir=, not nested, not a parameter, no `+=`); use it, or {listing}"
            )
    traced = Counter(u.file for u in uses if u.from_helper)
    for f in sorted(census_files - listed):
        if traced[f] < sites_per_file[f]:
            problems.append(
                f"{f}: {sites_per_file[f]} initdb/pg_ctl_start census site(s) but only {traced[f]} data-dir use(s) "
                f"the scan could trace to {DATA_DIR_HELPER}(); trace the rest, or list the file in "
                f"tests/{EXCEPTIONS.name} (not_a_cluster or disk_required) with a reason"
            )
    has_call = census_files | {u.file for u in uses}
    for table, entries in (("not_a_cluster", not_cluster), ("disk_required", disk)):
        for f, reason in sorted(entries.items()):
            if not (repo / f).is_file():
                problems.append(f"{table}: {f} no longer exists")
                continue
            if len(reason.split()) < 4 or len(reason.strip()) < 20:
                problems.append(f"{table}: {f} has no real reason ({reason!r})")
            if table == "disk_required" and not _BEAD_ID.search(reason):
                problems.append(f"{table}: {f} names no bead in its reason; name the follow-up that drains it")
            if f not in has_call:
                problems.append(f"{table}: {f} has no cluster start, provision() or _init_cluster() call left to except")
    for table, entries, ceiling in (
        ("not_a_cluster", not_cluster, ceilings[0]),
        ("disk_required", disk, ceilings[1]),
    ):
        if len(entries) > ceiling:
            problems.append(
                f"{table}: {len(entries)} entries exceed the ceiling {ceiling}; the table may only shrink "
                f"(NOT_A_CLUSTER_CEILING / DISK_REQUIRED_CEILING in tests/{Path(__file__).name} are raised on purpose, in a reviewed diff)"
            )
    return problems


# ── Docker-run Postgres (nexus-szyun) ─────────────────────────────────────────

#: The image's data directory (``PgContainerHelper.PGDATA`` on the Java side).
DOCKER_PGDATA = "/var/lib/postgresql/data"


def docker_pg_runs(repo: Path = REPO) -> list[tuple[str, int, bool]]:
    """Every literal ``["docker", "run", ...]`` argv under ``tests/`` that names a postgres or pgvector image,
    as ``(file, line, has_tmpfs_on_pgdata)``. Without ``--tmpfs`` the image's data directory sits in the docker
    data-root of whatever host runs the test."""
    out: list[tuple[str, int, bool]] = []
    for path in sorted((repo / "tests").rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        if rel in PROVENANCE_EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 3:
                continue
            c = node.elts
            if not (
                isinstance(c[0], ast.Constant) and c[0].value == "docker"
                and isinstance(c[1], ast.Constant) and c[1].value == "run"
            ):
                continue
            strings = [e.value for e in c if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if not any("postgres" in s or "pgvector" in s for s in strings):
                continue
            tmpfs = any(
                isinstance(e, ast.Constant) and e.value == "--tmpfs" and i + 1 < len(c)
                and isinstance(c[i + 1], ast.Constant) and str(c[i + 1].value).startswith(DOCKER_PGDATA)
                for i, e in enumerate(c)
            )
            out.append((rel, node.lineno, tmpfs))
    return out


def docker_pg_problems(repo: Path = REPO) -> list[str]:
    return [
        f"{f}:{line}: `docker run` of a postgres image without `--tmpfs {DOCKER_PGDATA}`; its data would be written to the "
        f"docker data-root of the host (the Java suites mount it as tmpfs, PgContainerHelper.PGDATA)"
        for f, line, ok in docker_pg_runs(repo) if not ok
    ]


def render(sites: Counter[str]) -> str:
    """The allowlist file's text for *sites*."""
    uncounted = {k: n for k, n in sorted(sites.items()) if k not in COUNTED}
    out = [
        "# Postgres cluster start sites the DDL/bytes counter does not read (nexus-sz651, epic nexus-aca0q).",
        "#",
        "# tests/_ddl_cluster_sites.py explains what a site is and what the scan covers. The terminal line",
        "# prints the sum of [uncounted] as `N uncounted cluster start sites (static tree census)`;",
        "# tests/test_ddl_uncounted_clusters_lint.py fails when this file and the tree differ, in either",
        "# direction. The scan covers tests/**/*.py, scripts/**/*.py and the pom's code generator; docker-run",
        "# and e2e shell clusters are not scanned, so this is not every start site. Regenerate with",
        "#   uv run python -m tests._ddl_cluster_sites --write",
        "# and review the diff: a new line is a start site whose cluster the counter cannot see.",
        "",
        "[counted]",
        "sites = [" + ", ".join(f'"{s}"' for s in COUNTED) + "]",
        "",
        "[uncounted]",
    ]
    out += [f'"{k}" = {n}' for k, n in uncounted.items()]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    sites = scan()
    if "--write" in argv:
        ALLOWLIST.write_text(render(sites), encoding="utf-8")
        print(f"wrote {ALLOWLIST} ({sum(n for k, n in sites.items() if k not in COUNTED)} uncounted sites)")
        return 0
    print(render(sites), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
