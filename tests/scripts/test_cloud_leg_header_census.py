# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``X-Nexus-*`` header the client names is either driven through the edge by the cloud leg or exempt with a
reason (nexus-srl9e).

The leg's ledger mapping sees only routes, so a header added on an existing route has no wire-ledger entry and no leg
step: X-Nexus-Identity-Token reached 8.5.0 users stripped by the CDN, and X-Nexus-Part-Sha256 needed conexus #710. The
census reads every string literal under ``src/`` (docstrings left out; a comment is not a literal) and compares the
headers it names with ``HEADER_COVERAGE`` in tests/e2e/lib/cloud_leg_headers.py, in both directions.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from functools import cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
LIB = REPO_ROOT / "tests" / "e2e" / "lib"
HEADER_RE = re.compile(r"X-Nexus-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_census", LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod  # type: ignore[union-attr]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cl = _load("cloud_leg")
COVERAGE: dict[str, str] = _load("cloud_leg_headers").HEADER_COVERAGE


def header_literals(root: Path) -> dict[str, list[str]]:
    """{header: ["path:line", ...]} for every X-Nexus-* header named in a non-docstring string literal under *root*."""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                for header in HEADER_RE.findall(node.value):
                    found.setdefault(header, []).append(f"{path.relative_to(root.parent)}:{node.lineno}")
    return found


@cache
def _src_headers() -> dict[str, list[str]]:
    return header_literals(SRC)


def census(found: dict[str, list[str]], coverage: dict[str, str]) -> list[str]:
    """Every disagreement between the headers in src and the coverage map, one line each; empty when they agree."""
    core = {s.name for s in cl.core_steps() if not s.cleanup}
    problems: list[str] = []
    for header in sorted(set(found) - set(coverage)):
        problems.append(f"{header} ({', '.join(found[header][:3])}) is not in cloud_leg_headers.HEADER_COVERAGE: name the core "
                        "step whose client call carries it through the edge, or 'exempt: <reason>'")
    for header in sorted(set(coverage) - set(found)):
        problems.append(f"{header} is in cloud_leg_headers.HEADER_COVERAGE but no string literal in src names it: remove it")
    for header, cover in sorted(coverage.items()):
        if cover.startswith("exempt:"):
            if not cover[len("exempt:"):].strip():
                problems.append(f"{header} is exempt with no reason")
        elif cover not in core:
            problems.append(f"{header} names step {cover!r}, which is not a core step of the leg's plan "
                            f"(core steps: {', '.join(sorted(core))})")
    return problems


def test_every_header_in_src_is_covered_or_exempt() -> None:
    found = _src_headers()
    assert len(found) >= 10, f"the census found only {sorted(found)}: the scan is broken, not the code clean"
    problems = census(found, COVERAGE)
    assert not problems, "\n".join(problems)
    driven = [h for h, c in COVERAGE.items() if not c.startswith("exempt:")]
    assert driven, "every header exempt: the leg would drive no header at all"


def test_the_census_fails_both_ways(tmp_path: Path) -> None:
    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "m.py").write_text('"""X-Nexus-Docstring-Only is prose."""\n'
                              '# X-Nexus-Comment-Only is a comment\n'
                              'H = {"X-Nexus-Tenant": t, "X-Nexus-Brand-New": f"{v}"}\n')
    found = header_literals(pkg)
    assert sorted(found) == ["X-Nexus-Brand-New", "X-Nexus-Tenant"]
    problems = census(found, {"X-Nexus-Tenant": "store-put", "X-Nexus-Gone": "exempt: x"})
    assert any(p.startswith("X-Nexus-Brand-New ") and "not in cloud_leg_headers.HEADER_COVERAGE" in p for p in problems)
    assert any(p.startswith("X-Nexus-Gone ") and "no string literal in src" in p for p in problems)
    assert len(problems) == 2
    bad = census({"X-Nexus-Tenant": ["m.py:1"], "X-Nexus-A": ["m.py:2"]},
                 {"X-Nexus-Tenant": "store-delete", "X-Nexus-A": "exempt: "})
    assert any("store-delete" in p and "not a core step" in p for p in bad)   # a cleanup step does not count
    assert any(p == "X-Nexus-A is exempt with no reason" for p in bad)
    assert census({"X-Nexus-Tenant": ["m.py:1"]}, {"X-Nexus-Tenant": "route-docs-by-meta"}), \
        "a release-only step runs only when the ledger names its route, so it covers nothing"
