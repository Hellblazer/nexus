#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Select the integration-marked test files a change affects (nexus-rpaat).

The repo's default addopts deselect ``-m integration``, so CI's shard
matrix never runs those tests. The nightly local-service gate runs the full
integration family once a day. Before this selector, a test added in the
same push as the change it proves went unrun until that nightly; the 7.67.0
evidence table found four such tests in one release range.

ci.yml's ``changes`` job pipes the push's changed paths into this script and
the ``integration (affected)`` job runs what it prints. A file is selected
when it is integration-marked and either:

* the push changed the test file itself, or
* the push changed a ``src/nexus`` module the test file imports (any
  ``import`` or ``from ... import`` statement, function-local ones too).

A change to ``service/`` or to shared test fixtures selects nothing by
itself. Those move every integration test at once, and the nightly gate
runs the whole family; this job exists for the change-local gap.

Stdlib only: ci.yml runs it under the runner's bare ``python3`` before any
``uv sync``.

Usage::

    git diff --name-only BASE HEAD | scripts/select_affected_integration_tests.py --repo .
    scripts/select_affected_integration_tests.py --repo . --all
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

#: Integration files that a dedicated ci.yml job already runs with the
#: environment they need. The affected-integration job lacks that
#: environment (a source-built PG bundle, a Docker PG), so the files would
#: only skip there. tests/scripts/test_select_affected_integration_tests.py
#: checks that ci.yml still names each one.
COVERED_ELSEWHERE: dict[str, str] = {
    "tests/db/test_pg_provision_ca3_bundle.py": (
        "ca3-pgvector-bundle jobs, path-filtered on this file, with NEXUS_CA3_BUNDLE"
    ),
    "tests/db/test_pg_bundle_relocation.py": (
        "ca3-pgvector-bundle jobs, path-filtered on this file, with the source-built bundle"
    ),
    "tests/db/test_write_seam_gate_integration.py": (
        "write-seam-gate job, path-filtered on this file and the seam sources"
    ),
    "tests/db/test_http_combined_query_integration.py": (
        "write-seam-gate job, path-filtered on this file and the catalog sources"
    ),
}

_MARK = re.compile(r"\bmark\.integration\b")


def _module_of(rel: str) -> str | None:
    if not (rel.startswith("src/nexus/") and rel.endswith(".py")):
        return None
    mod = rel[len("src/") : -len(".py")].replace("/", ".")
    return mod.removesuffix(".__init__")


def _imported_modules(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
            mods.update(f"{node.module}.{alias.name}" for alias in node.names)
    return mods


def _integration_files(repo: pathlib.Path) -> dict[str, set[str]]:
    """Map each integration-marked test file to the modules it imports."""
    found: dict[str, set[str]] = {}
    for path in sorted((repo / "tests").rglob("test_*.py")):
        rel = path.relative_to(repo).as_posix()
        if rel in COVERED_ELSEWHERE:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if _MARK.search(source):
            found[rel] = _imported_modules(source)
    return found


def select(repo: pathlib.Path, changed: list[str]) -> list[str]:
    """Return the sorted integration test files affected by *changed* paths."""
    files = _integration_files(repo)
    changed_set = {c.strip() for c in changed if c.strip()}
    modules = {m for m in map(_module_of, changed_set) if m}
    picked = {rel for rel in files if rel in changed_set}
    picked.update(rel for rel, imports in files.items() if imports & modules)
    return sorted(picked)


def select_all(repo: pathlib.Path) -> list[str]:
    """Return every integration-marked test file this job may run."""
    return sorted(_integration_files(repo))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument(
        "--all",
        action="store_true",
        help="select every integration file (used when no diff base is usable)",
    )
    args = parser.parse_args(argv)
    if args.all:
        result = select_all(args.repo)
    else:
        result = select(args.repo, sys.stdin.read().splitlines())
    for rel in result:
        sys.stdout.write(rel + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
