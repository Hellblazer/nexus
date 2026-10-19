#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed ``pytest.mark.t2_tenant`` from a storage-touch census (nexus-j23ni).

The suite mints a tenant only for a test that asks for one (a ``t2_tenant`` /
``t2_service_env`` parameter, or ``pytest.mark.t2_tenant`` on the test, its
class or its module). A census run (``NX_TEST_T2_SUBSTRATE=optin
NX_STORAGE_TOUCH_GUARD_MODE=report NX_STORAGE_TOUCH_SWEEP=1
NX_STORAGE_TOUCH_REPORT_DIR=<dir>``, whole suite, ``tests/_storage_touch_guard.py``)
leaves one JSON line per test in ``<dir>/nx-storage-touch-tests-<run>-*.jsonl``.
This script reads those lines and writes the marks:

* a test NEEDS a tenant when it touched storage without one (including from a
  thread, a child process, or a fixture), or when it FAILED without one (a test
  that reads ``NX_SERVICE_TOKEN`` from the environment raises ``KeyError`` and
  constructs no guarded error, so the guard cannot see it; the failure is the
  evidence). A skipped or xfailed test needs one only if it touched storage. A
  test already provisioned or exempt needs nothing; one that is only a soft peek
  needs nothing unless ``--include-peeks``;
* a module whose needing tests are at least ``--module-threshold`` of the tests
  that would otherwise mint nothing gets one ``pytestmark``; otherwise a class
  whose every such test needs one gets a class mark; otherwise each test is marked;
* a test that already asks for the tenant by parameter or ``usefixtures`` is left
  alone, and so is anything already marked, so a rerun adds nothing.

Not marked, only listed: a test that leaked a thread or child which touched
storage after the test finished (a mark cannot help; the test must join it), and
a test that asked for a tenant yet touched storage before it was active (a
fixture-order bug).

The default is a dry run that prints the plan. ``--apply`` edits the files. Run
from the repo root::

    uv run python scripts/annotate_t2_tenant.py <census-dir>            # plan
    uv run python scripts/annotate_t2_tenant.py <census-dir> --apply    # write

Failures with no storage evidence are listed by name in the plan: each is either
a test that needs the environment a tenant provides (marked here) or a failure
that has another cause (investigate before trusting the mark).
"""
from __future__ import annotations

import argparse
import ast
import collections
import dataclasses
import glob
import json
import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MARK = "pytest.mark.t2_tenant"
JSONL_GLOB = "nx-storage-touch-tests-*.jsonl"


def _parse(src: str) -> ast.Module:
    """``ast.parse`` without the SyntaxWarnings a test's own regex literals raise."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(src)


# ── reading a census ─────────────────────────────────────────────────────────


def _run_of(path: str) -> str:
    return Path(path).name.split("-")[4]  # nx-storage-touch-tests-<run>-<pid>-<id>.jsonl


def load_records(census_dir: Path, run: str | None = None) -> list[dict]:
    """Every JSON line of the census run's per-test files. A directory holding
    more than one run needs ``run`` (the id in the file names) to say which."""
    files = sorted(glob.glob(str(census_dir / JSONL_GLOB)))
    runs = {_run_of(f) for f in files}
    if run is not None:
        files = [f for f in files if _run_of(f) == run]
    elif len(runs) > 1:
        raise SystemExit(f"{census_dir} holds {len(runs)} runs ({', '.join(sorted(runs))}); pick one with --run")
    if not files:
        raise SystemExit(f"no {JSONL_GLOB} files in {census_dir}")
    out: list[dict] = []
    for f in files:
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a line cut short by a kill
    return out


@dataclasses.dataclass
class Census:
    """What the records say, per test."""

    need: set[str]  # nodeids that need a tenant
    peek: set[str]  # soft peek and nothing else
    mint: dict[str, set[str]]  # module -> tests that mint nothing today and are neither exempt nor skipped
    failed_blind: list[str]  # failed with no storage evidence
    provisioned_dirty: list[str]  # asked for a tenant but touched before it was active
    late_owners: list[str]  # tests whose leaked thread/child touched storage after they finished
    total: int
    outcomes: collections.Counter


def classify(records: list[dict], *, include_peeks: bool = False) -> Census:
    need: set[str] = set()
    peek: set[str] = set()
    mint: dict[str, set[str]] = collections.defaultdict(set)
    blind: set[str] = set()
    dirty: set[str] = set()
    late: set[str] = set()
    outcomes: collections.Counter = collections.Counter()
    total = 0
    for rec in records:
        if rec["k"] in ("late_child", "late_thread") and rec["nodeid"] and not rec["nodeid"].startswith("<"):
            late.add(rec["nodeid"])
        if rec["k"] != "test":
            continue
        total += 1
        nodeid = rec["nodeid"]
        outcomes[rec["outcome"]] += 1
        touched = bool(rec["unprov_events"])
        # A skipped or xfailed test that never touched storage needs nothing. One that DID
        # needs a tenant all the same: it would run (or xfail for its own reason) with one,
        # and an xfail turns the guard's teardown failure into an expected one.
        if rec["exempt"] or rec.get("stale") or (rec["outcome"] in ("skipped", "xfailed") and not touched):
            continue
        if rec["provisioned"]:
            if touched:
                dirty.add(nodeid)
            continue
        mint[nodeid.split("::")[0]].add(nodeid)
        if touched:
            need.add(nodeid)
        elif rec["outcome"] == "failed":
            need.add(nodeid)
            blind.add(nodeid)
        elif rec.get("soft_peeks"):
            peek.add(nodeid)
            if include_peeks:
                need.add(nodeid)
    return Census(need, peek, dict(mint), sorted(blind), sorted(dirty), sorted(late), total, outcomes)


# ── locating a test in its source ────────────────────────────────────────────


def split_nodeid(nodeid: str) -> tuple[str, list[str]]:
    """``tests/a.py::Cls::test_x[param]`` -> (``tests/a.py``, [``Cls``, ``test_x``])."""
    cut = nodeid.find("[")
    base = nodeid if cut == -1 else nodeid[:cut]
    path, *chain = base.split("::")
    return path, chain


def _decorator_text(src_lines: list[str], node: ast.AST) -> str:
    decorators = getattr(node, "decorator_list", [])
    if not decorators:
        return ""
    first = min(d.lineno for d in decorators)
    return "\n".join(src_lines[first - 1:node.lineno])


def _is_marked(src_lines: list[str], node: ast.AST) -> bool:
    """The node already carries ``pytest.mark.t2_tenant``. A ``t2_service_env``
    PARAMETER does not count: the census saw the test unprovisioned, which is how
    a fixture requested after one that touches storage (``db``, a catalog
    fixture) looks, and only the mark makes the autouse pin run first."""
    return "mark.t2_tenant" in _decorator_text(src_lines, node)


def _pytestmark_assignment(tree: ast.Module) -> ast.Assign | None:
    return next(
        (s for s in tree.body if isinstance(s, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in s.targets)),
        None,
    )


def _module_is_marked(tree: ast.Module, src_lines: list[str]) -> bool:
    stmt = _pytestmark_assignment(tree)
    if stmt is None:
        return False
    return "mark.t2_tenant" in "\n".join(src_lines[stmt.lineno - 1:stmt.end_lineno])


def _marked_in_scope(tree: ast.Module, src_lines: list[str], chain: list[str]) -> bool:
    """The test, or a class that encloses it, already carries the mark."""
    scope: list[ast.stmt] = tree.body
    for name in chain:
        hit = next(
            (s for s in scope if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and s.name == name),
            None,
        )
        if hit is None:
            return False
        if _is_marked(src_lines, hit):
            return True
        if not isinstance(hit, ast.ClassDef):
            return False
        scope = hit.body
    return False


def _find(tree: ast.Module, chain: list[str]) -> tuple[ast.AST | None, ast.ClassDef | None]:
    """The function (or class) node for ``chain`` and, when it is not in the class
    body (inherited, generated), the innermost class that was found."""
    scope: list[ast.stmt] = tree.body
    cls: ast.ClassDef | None = None
    for i, name in enumerate(chain):
        hit = next(
            (s for s in scope if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and s.name == name),
            None,
        )
        if hit is None:
            return None, cls
        if i == len(chain) - 1:
            return hit, cls
        if not isinstance(hit, ast.ClassDef):
            return None, cls
        cls = hit
        scope = hit.body
    return None, cls


# ── the plan ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class FilePlan:
    path: str
    module_mark: bool = False
    class_marks: list[tuple[str, ...]] = dataclasses.field(default_factory=list)  # class chains
    func_marks: list[tuple[str, ...]] = dataclasses.field(default_factory=list)  # function chains
    covered_need: int = 0  # tests that need a tenant and now get one from a mark
    covered_extra: int = 0  # tests that mint only because a module or class mark covers them
    already: int = 0  # tests that already ask for a tenant
    unresolved: list[str] = dataclasses.field(default_factory=list)


def plan_file(rel: str, census: Census, repo: Path, *, threshold: float) -> FilePlan:
    plan = FilePlan(rel)
    src = (repo / rel).read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = _parse(src)
    minting = census.mint.get(rel, set())
    if _module_is_marked(tree, lines):
        plan.already = len(minting)
        return plan

    # which node each needing test resolves to; one already marked is done
    asking: set[str] = set()
    resolved: dict[str, tuple[str, ...] | None] = {}
    for nodeid in sorted(minting & census.need):
        _, chain = split_nodeid(nodeid)
        node, cls = _find(tree, chain)
        if _marked_in_scope(tree, lines, chain):
            asking.add(nodeid)
        elif node is not None:
            resolved[nodeid] = tuple(chain)
        elif cls is not None:
            resolved[nodeid] = tuple(chain[: chain.index(cls.name) + 1])
        else:
            resolved[nodeid] = None
    plan.already = len(asking)
    need = (minting & census.need) - asking
    denominator = minting - asking
    if need and len(need) / len(denominator) >= threshold:
        plan.module_mark = True
        plan.covered_need = len(need)
        plan.covered_extra = len(denominator) - len(need)
        return plan

    # a class whose every minting test needs a tenant gets one mark; the rest are marked one by one
    by_class: dict[tuple[str, ...], set[str]] = collections.defaultdict(set)
    for nodeid in denominator:
        _, chain = split_nodeid(nodeid)
        if len(chain) >= 2:
            by_class[(chain[0],)].add(nodeid)
    covered: set[str] = set()
    for cls_key, members in sorted(by_class.items()):
        if len(members) >= 2 and members <= need:
            plan.class_marks.append(cls_key)
            covered |= members
    done: set[tuple[str, ...]] = set(plan.class_marks)
    for nodeid in sorted(need - covered):
        key = resolved.get(nodeid)
        if key is None:
            plan.unresolved.append(nodeid)
            continue
        covered.add(nodeid)
        node, _ = _find(tree, list(key))
        if isinstance(node, ast.ClassDef):
            if key not in done:
                done.add(key)
                plan.class_marks.append(key)
        elif key not in done:
            done.add(key)
            plan.func_marks.append(key)
    plan.covered_need = len(covered)
    return plan


# ── editing ──────────────────────────────────────────────────────────────────


def _pytest_import_end(tree: ast.Module) -> int:
    """End line of a module-level ``import pytest``, or 0."""
    for s in tree.body:
        if isinstance(s, ast.Import) and any(a.name == "pytest" and a.asname is None for a in s.names):
            return s.end_lineno or s.lineno
    return 0


def _is_import_like(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return True
    if isinstance(stmt, (ast.If, ast.Try)):
        inner = [s for s in ast.walk(stmt) if isinstance(s, ast.stmt) and s is not stmt]
        return any(isinstance(s, (ast.Import, ast.ImportFrom)) for s in inner) and not any(
            isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for s in inner
        )
    return False


def _import_block_end(tree: ast.Module) -> int:
    """1-based line the module's leading docstring and import block end on (0 when
    there is neither): the point module-level additions go after. The block stops at
    the first definition or statement that follows an import."""
    last = 0
    seen_import = False
    for i, stmt in enumerate(tree.body):
        is_doc = i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)
        if is_doc or _is_import_like(stmt):
            seen_import = seen_import or not is_doc
            last = stmt.end_lineno or stmt.lineno
            continue
        if seen_import or isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            break
    return last


def apply_plan(plan: FilePlan, repo: Path) -> str | None:
    """Edit the file for ``plan``. Returns an error string (file left untouched)
    or None."""
    path = repo / plan.path
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    tree = _parse(src)
    inserts: list[tuple[int, list[str]]] = []  # (insert BEFORE this 1-based line, new lines)

    def decorate(chain: tuple[str, ...]) -> None:
        node, _ = _find(tree, list(chain))
        assert node is not None
        first = min([node.lineno, *[d.lineno for d in node.decorator_list]])  # type: ignore[attr-defined]
        inserts.append((first, [" " * node.col_offset + f"@{MARK}"]))  # type: ignore[attr-defined]

    for chain in (*plan.class_marks, *plan.func_marks):
        decorate(chain)

    pytest_import = _pytest_import_end(tree)
    wants_mark = plan.module_mark or plan.class_marks or plan.func_marks
    add_import = bool(wants_mark) and not pytest_import
    if plan.module_mark:
        existing = _pytestmark_assignment(tree)
        if existing is None:
            after = max(_import_block_end(tree), pytest_import)
            inserts.append((after + 1, ["", f"pytestmark = {MARK}"] if after else [f"pytestmark = {MARK}", ""]))
        elif isinstance(existing.value, (ast.List, ast.Tuple)):
            inserts.append(((existing.end_lineno or existing.lineno) + 1, [f"pytestmark = [*pytestmark, {MARK}]"]))
        else:  # a single mark (or expression): wrap it in place; the line count is unchanged
            v = existing.value
            end_line, end_col = v.end_lineno or v.lineno, v.end_col_offset or 0
            lines[end_line - 1] = lines[end_line - 1][:end_col] + f", {MARK}]" + lines[end_line - 1][end_col:]
            lines[v.lineno - 1] = lines[v.lineno - 1][:v.col_offset] + "[" + lines[v.lineno - 1][v.col_offset:]
    if add_import:
        after = _import_block_end(tree)
        inserts.append((after + 1, ["import pytest"] if after else ["import pytest", ""]))

    for before, new in sorted(inserts, key=lambda item: -item[0]):  # bottom-up keeps the numbers valid
        lines[before - 1:before - 1] = new
    out = "\n".join(lines)
    try:
        _parse(out)
    except SyntaxError as exc:
        return f"edit would not parse ({exc}); left untouched"
    path.write_text(out, encoding="utf-8")
    return None


# ── reporting ────────────────────────────────────────────────────────────────


def summarize(census: Census, plans: list[FilePlan]) -> dict:
    covered = sum(p.covered_need for p in plans)
    extra = sum(p.covered_extra for p in plans)
    already = sum(p.already for p in plans)
    return {
        "census_tests": census.total,
        "outcomes": dict(census.outcomes),
        "need_a_tenant": len(census.need),
        "peek_only": len(census.peek),
        "modules_with_a_need": sum(1 for p in plans if p.covered_need or p.already),
        "module_marks": sum(p.module_mark for p in plans),
        "class_marks": sum(len(p.class_marks) for p in plans),
        "function_marks": sum(len(p.func_marks) for p in plans),
        "tests_covered_that_need_it": covered,
        "tests_covered_only_by_a_wider_mark": extra,
        "tests_that_already_ask": already,
        "tests_that_mint_after": covered + extra + already,
        "unresolved": [n for p in plans for n in p.unresolved],
        "failed_without_storage_evidence": len(census.failed_blind),
        "late_touch_owners": len(census.late_owners),
        "asked_for_a_tenant_but_touched_before_it": len(census.provisioned_dirty),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("census_dir", type=Path, help="NX_STORAGE_TOUCH_REPORT_DIR of a whole-suite optin run")
    ap.add_argument("--repo", type=Path, default=REPO_ROOT, help="repo root the nodeids are relative to")
    ap.add_argument("--run", help="run id in the file names, when the directory holds several")
    ap.add_argument("--module-threshold", type=float, default=0.9,
                    help="mark a whole module when this share of its otherwise-unprovisioned tests need a tenant (0.9)")
    ap.add_argument("--include-peeks", action="store_true", help="also mark tests that only soft-peeked at the endpoint")
    ap.add_argument("--apply", action="store_true", help="edit the files (default: print the plan)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)

    census = classify(load_records(args.census_dir, args.run), include_peeks=args.include_peeks)
    files = {split_nodeid(n)[0] for n in census.need}
    missing = sorted(f for f in files if not (args.repo / f).exists())
    plans = [plan_file(rel, census, args.repo, threshold=args.module_threshold) for rel in sorted(files) if rel not in missing]
    errors: list[str] = []
    if args.apply:
        for p in plans:
            if p.module_mark or p.class_marks or p.func_marks:
                err = apply_plan(p, args.repo)
                if err:
                    errors.append(f"{p.path}: {err}")
    summary = summarize(census, plans)
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        for k, v in summary.items():
            print(f"{k}: {v}")
        for p in plans:
            if p.module_mark:
                print(f"  module   {p.path} ({p.covered_need} need it, {p.covered_extra} ride along)")
            for c in p.class_marks:
                print(f"  class    {p.path}::{'::'.join(c)}")
            for f in p.func_marks:
                print(f"  function {p.path}::{'::'.join(f)}")
        if census.failed_blind:
            print("failed with no storage evidence (marked; each needs the environment a tenant provides, or has another cause):")
            for n in census.failed_blind:
                print(f"  {n}")
        if census.late_owners:
            print("leaked a thread or child that touched storage after the test finished (join it inside the test):")
            for n in census.late_owners:
                print(f"  {n}")
        if census.provisioned_dirty:
            print("asked for a tenant but touched storage before it was active (fix the fixture order, a mark cannot help):")
            for n in census.provisioned_dirty:
                print(f"  {n}")
    for n in missing:
        print(f"census names a file that does not exist here: {n}", file=sys.stderr)
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    return 1 if errors or missing or summary["unresolved"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
