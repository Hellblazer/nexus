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
on tmpfs N, on disk M" summary). :func:`data_dir_uses` finds every ``initdb`` argv, ``_init_cluster``
and ``pg_ctl_start`` call and traces its data-dir argument back to ``tests._child_process.pg_data_tempdir``;
:func:`provenance_problems` is what ``tests/test_ddl_uncounted_clusters_lint.py`` asserts is empty. The
files that do not (or cannot) use the helper are listed WITH A REASON in
``tests/pg_data_dir_exceptions.toml``.
"""
from __future__ import annotations

import ast
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


@dataclass(frozen=True)
class DataDirUse:
    """One place a test hands a data directory to ``initdb`` / ``pg_ctl start``."""

    file: str
    func: str
    kind: str  # initdb | init_cluster | pg_ctl_start | pg_ctl_start_argv
    line: int
    expr: str
    from_helper: bool


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _scope_assignments(scope: ast.AST) -> dict[str, list[ast.expr | None]]:
    """name -> every value assigned to it directly in *scope* (not in nested defs); ``None`` marks an
    assignment whose value is not a single expression (tuple unpacking, ``for``/``with`` targets)."""
    out: dict[str, list[ast.expr | None]] = {}

    def bind(target: ast.expr, value: ast.expr | None) -> None:
        if isinstance(target, ast.Name):
            out.setdefault(target.id, []).append(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                bind(elt, None)

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    bind(t, child.value)
            elif isinstance(child, ast.AnnAssign) and child.value is not None:
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


def _from_helper(expr: ast.expr | None, scopes: list[ast.AST], seen: frozenset[str] = frozenset()) -> bool:
    """True when *expr* is the helper's result, possibly through ``str``/``Path`` and plain names bound
    in the use's own function or the module. A parameter, an f-string, a ``/`` join (a NESTED data dir:
    the owner-record sweep reads ``postmaster.pid`` at the top of the recorded dir only), a helper call
    with ``parent_dir=`` (which skips the root chooser) and anything unknown are all False."""
    if isinstance(expr, ast.Call):
        name = _call_name(expr)
        if name == DATA_DIR_HELPER:
            return not any(kw.arg == "parent_dir" or kw.arg is None for kw in expr.keywords) and len(expr.args) <= 1
        if name in _PATH_WRAPPERS and len(expr.args) == 1 and not expr.keywords:
            return _from_helper(expr.args[0], scopes, seen)
        return False
    if isinstance(expr, ast.Name):
        if expr.id in seen:
            return False
        for scope in scopes:  # innermost first
            if expr.id in _params(scope):
                return False
            values = _scope_assignments(scope).get(expr.id)
            if values:
                return all(_from_helper(v, scopes, seen | {expr.id}) for v in values)
        return False
    return False


def _argv_data_dir(arg: ast.expr) -> tuple[str, ast.expr] | None:
    """For a literal argv ``[exe, ..., "-D", DIR, ...]`` that starts initdb or ``pg_ctl start``: (kind, DIR)."""
    if not isinstance(arg, ast.List) or len(arg.elts) < 3:
        return None
    elts = arg.elts
    flag = next(
        (i for i, e in enumerate(elts) if isinstance(e, ast.Constant) and e.value == "-D" and i + 1 < len(elts)),
        None,
    )
    if flag is None or flag == 0:
        return None
    head = ast.unparse(elts[0]).lower()
    if "initdb" in head:
        return "initdb", elts[flag + 1]
    if "pg_ctl" in head and any(isinstance(e, ast.Constant) and e.value == "start" for e in elts):
        return "pg_ctl_start_argv", elts[flag + 1]
    return None


def data_dir_uses(repo: Path = REPO) -> list[DataDirUse]:
    """Every data-dir handed to initdb / ``_init_cluster`` / ``pg_ctl_start`` under ``tests/``. ``scripts/`` is
    not scanned: its Windows smoke tools are standalone programs run on a Windows host, not part of a test run,
    and cannot import the helper."""
    uses: list[DataDirUse] = []
    for top in ("tests",):
        for path in sorted((repo / top).rglob("*.py")):
            rel = path.relative_to(repo).as_posix()
            if rel in PROVENANCE_EXEMPT:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            chain: list[ast.AST] = []

            class _Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
                    chain.append(node)
                    self.generic_visit(node)
                    chain.pop()

                visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

                def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                    found: list[tuple[str, ast.expr | None]] = []
                    name = _call_name(node)
                    if name in ("pg_ctl_start", "_init_cluster"):
                        kw = {k.arg: k.value for k in node.keywords if k.arg}
                        found.append((
                            "pg_ctl_start" if name == "pg_ctl_start" else "init_cluster",
                            node.args[1] if len(node.args) > 1 else kw.get("pgdata"),
                        ))
                    for arg in (*node.args, *(k.value for k in node.keywords)):
                        hit = _argv_data_dir(arg)
                        if hit is not None:
                            found.append(hit)
                    for kind, expr in found:
                        scopes = [*reversed(chain), tree]
                        uses.append(DataDirUse(
                            file=rel,
                            func=chain[0].name if chain else "<module>",  # type: ignore[attr-defined]
                            kind=kind,
                            line=node.lineno,
                            expr=ast.unparse(expr) if expr is not None else "<missing>",
                            from_helper=_from_helper(expr, scopes),
                        ))
                    self.generic_visit(node)

            _Visitor().visit(tree)
    return uses


def exceptions(path: Path = EXCEPTIONS) -> tuple[dict[str, str], dict[str, str]]:
    """``(not_a_cluster, disk_required)``: file -> reason, from ``tests/pg_data_dir_exceptions.toml``."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("not_a_cluster", {})), dict(data.get("disk_required", {}))


def provenance_problems(repo: Path = REPO, exceptions_path: Path | None = None) -> list[str]:
    """Everything wrong with where test clusters get their data directory. Empty means clean.

    1. A use whose data dir is not the helper's result, in a file not listed in ``disk_required`` or
       ``not_a_cluster``.
    2. A file with an initdb / ``pg_ctl_start`` census site that is neither proven (>= 1 use, all from
       the helper) nor listed: a provisioning test that never names its data dir still needs a reason.
    3. A listed file that is gone, that has a blank reason, or that no longer has anything to except
       (no census site and no ``provision(`` / ``_init_cluster`` call): the list must not outlive its subject.
    4. A file listed under both tables.
    """
    ex_path = exceptions_path or (repo / "tests" / EXCEPTIONS.name)
    not_cluster, disk = exceptions(ex_path)
    uses = data_dir_uses(repo)
    census_files = {
        k.split("::")[0] for k in scan(repo) if k.startswith("tests/") and k.split("::")[0].endswith(".py")
    } - set(PROVENANCE_EXEMPT)
    listed = set(not_cluster) | set(disk)
    problems: list[str] = []
    for both in sorted(set(not_cluster) & set(disk)):
        problems.append(f"{both}: listed in both not_a_cluster and disk_required")
    for u in uses:
        if not u.from_helper and u.file not in listed:
            problems.append(
                f"{u.file}:{u.line} ({u.func}, {u.kind}): data dir `{u.expr}` is not {DATA_DIR_HELPER}(...) "
                f"(no parent_dir=, not nested, not a parameter); use it, or list the file in "
                f"tests/{EXCEPTIONS.name} with a reason"
            )
    proven = {u.file for u in uses}
    for f in sorted(census_files - listed):
        if f not in proven:
            problems.append(
                f"{f}: has an initdb/pg_ctl_start census site but no data-dir use the scan can trace; "
                f"list it in tests/{EXCEPTIONS.name} (not_a_cluster or disk_required) with a reason"
            )
    for table, entries in (("not_a_cluster", not_cluster), ("disk_required", disk)):
        for f, reason in sorted(entries.items()):
            target = repo / f
            if not target.is_file():
                problems.append(f"{table}: {f} no longer exists")
                continue
            if len(reason.strip()) < 20:
                problems.append(f"{table}: {f} has no real reason ({reason!r})")
            text = target.read_text(encoding="utf-8")
            if f not in census_files and "provision(" not in text and "_init_cluster(" not in text:
                problems.append(f"{table}: {f} has no cluster start site or provision call left to except")
    return problems


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
