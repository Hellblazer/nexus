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

Scope, stated so the figure is not trusted past it: only the Python test tree and ``scripts/`` are
scanned (product code under ``src/`` provisions Postgres for real use, and tests that run it are
listed through their own ``initdb`` lookups); the Java suite is gated by
``PgContainerTmpfsGateTest`` (every container goes through ``PgContainerHelper``) and its one
uncounted cluster, the jOOQ code generator's build-time container, is entered here by hand.

    uv run python -m tests._ddl_cluster_sites --write   # regenerate the allowlist from the tree
"""
from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST = Path(__file__).resolve().parent / "ddl_uncounted_clusters.toml"

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


def render(sites: Counter[str]) -> str:
    """The allowlist file's text for *sites*."""
    uncounted = {k: n for k, n in sorted(sites.items()) if k not in COUNTED}
    out = [
        "# Postgres cluster start sites the DDL/bytes counter does not read (nexus-sz651, epic nexus-aca0q).",
        "#",
        "# tests/_ddl_cluster_sites.py explains what a site is and what the scan covers. The terminal line",
        "# prints the sum of [uncounted] as `uncounted_clusters=N`; tests/test_ddl_uncounted_clusters_lint.py",
        "# fails when this file and the tree differ, in either direction. Regenerate with",
        "#   uv run python -m tests._ddl_cluster_sites --write",
        "# and review the diff: a new line is a cluster the counter cannot see.",
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
