#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-vacuity check for ci.yml's affected-integration job (nexus-rpaat).

pytest exits 0 when every test skips. This check reads the job's JUnit XML
and fails when:

* the number of testcases differs from the number ``--collect-only``
  reported for the same selection and mark expression, or
* a selected file collected tests but executed none of them, unless the
  file is in :data:`ALL_SKIP_ALLOWED`.

A selection that collects zero tests is reported and passes. That happens
when the selected files carry only carved-out marks (``lived_in``,
``keyed_voyage``, ``mandatory_regression_pin``), which run in other gates.

Stdlib only.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import xml.etree.ElementTree as ET

#: Integration files that execute nothing in this job. A file not listed
#: here that skips every test fails the job: that is the silent coverage loss
#: this job exists to catch. Both entries are runner limits. A test no gate
#: arms yet gets an entry naming its bead; remove the entry when the bead
#: lands.
ALL_SKIP_ALLOWED: dict[str, str] = {
    "tests/test_dt_mcp_stdio_transport.py": "runner limit: DEVONthink MCP binary, macOS only",
    "tests/test_search_fanout_recall_parity.py": (
        "runner limit: needs a real nexus config pointing at the live cloud engine"
    ),
    "tests/integration/test_llm_live.py": (
        "runner limit: spends money against a paid LLM API; hand-run with NX_LIVE_LLM=1 and a key"
    ),
}


def _module(rel: str) -> str:
    return rel.removesuffix(".py").replace("/", ".")


def _executed(case: ET.Element) -> bool:
    """True unless the test was skipped.

    pytest writes an expected failure as ``<skipped type="pytest.xfail">``.
    The test body ran and failed as pinned, so it counts as executed; a file
    of strict xfail pins is not a vacuous file. JUnit records an imperative
    ``pytest.xfail(...)`` call the same way, so a test that bails out with
    one also counts as executed: a known hole, no integration file does it.
    """
    skipped = case.find("skipped")
    return skipped is None or skipped.get("type") == "pytest.xfail"


def check(junit: pathlib.Path, selected: list[str], collected: int) -> list[str]:
    """Return a list of problems; empty means the run was not vacuous."""
    root = ET.parse(junit).getroot()
    cases: list[tuple[str, bool]] = []
    # A module-level pytest.skip(allow_module_level=True) collects nothing but
    # writes one testcase with an empty classname and the module as its name.
    module_skipped: set[str] = set()
    for c in root.iter("testcase"):
        cls = c.get("classname") or ""
        if not cls and c.find("skipped") is not None:
            module_skipped.add(c.get("name") or "")
            continue
        cases.append((cls, _executed(c)))
    problems: list[str] = []
    if len(cases) != collected:
        problems.append(
            f"JUnit has {len(cases)} testcases but --collect-only reported "
            f"{collected} for the same selection"
        )
    if collected == 0 and not module_skipped:
        sys.stdout.write("selected files collect no tests under the mark expression; nothing to run\n")
        return problems
    mods = {_module(rel) for rel in selected}
    stray = [cls for cls, _ in cases if not any(cls == m or cls.startswith(m + ".") for m in mods)]
    if stray:
        problems.append(
            f"{len(stray)} JUnit testcases belong to no selected file "
            f"(first: {stray[0]!r}); per-file attribution cannot be trusted"
        )
    for rel in selected:
        mod = _module(rel)
        mine = [ran for cls, ran in cases if cls == mod or cls.startswith(mod + ".")]
        executed = sum(mine)
        if mod in module_skipped:
            sys.stdout.write(f"{rel}: skipped at module level\n")
            mine = [False]
        else:
            sys.stdout.write(f"{rel}: {len(mine)} collected, {executed} executed\n")
        if mine and not executed:
            if rel in ALL_SKIP_ALLOWED:
                sys.stdout.write(f"  allowed to skip: {ALL_SKIP_ALLOWED[rel]}\n")
            else:
                problems.append(
                    f"{rel}: all {len(mine)} tests skipped; add the missing "
                    "prerequisite to the job, or list the file in "
                    "ALL_SKIP_ALLOWED with the reason the runner cannot have it"
                )
    return problems


def _lines(path: pathlib.Path) -> list[str]:
    return [s.strip() for s in path.read_text().splitlines() if s.strip()]


def aggregate(shard_dirs: list[pathlib.Path], all_files: list[str], min_executed: int, max_skipped: int) -> list[str]:
    """The release-PR floor over every shard (nexus-jh1f2 cut 4 review r1).

    Each shard directory holds that shard's ``integration-selected.txt`` and
    ``integration-affected.xml``. Every selected file must be in exactly one
    shard, and the shards together must execute at least *min_executed* tests and
    skip at most *max_skipped*: the old lsg floor and skip budget, which the
    per-file check above cannot see (a file can skip 29 of its 30 tests)."""
    problems: list[str] = []
    owner: dict[str, str] = {}
    executed = skipped = 0
    for d in sorted(shard_dirs):
        sel_path, junit = d / "integration-selected.txt", d / "integration-affected.xml"
        if not sel_path.is_file() or not junit.is_file():
            problems.append(f"{d.name}: missing integration-selected.txt or integration-affected.xml")
            continue
        for rel in _lines(sel_path):
            if rel in owner:
                problems.append(f"{rel} ran in two shards ({owner[rel]} and {d.name})")
            owner[rel] = d.name
        e = s = 0
        for c in ET.parse(junit).getroot().iter("testcase"):
            if _executed(c):
                e += 1
            else:
                s += 1
        sys.stdout.write(f"{d.name}: {len(_lines(sel_path))} files, {e} executed, {s} skipped\n")
        executed, skipped = executed + e, skipped + s
    lost = sorted(set(all_files) - set(owner))
    extra = sorted(set(owner) - set(all_files))
    if lost:
        problems.append(f"{len(lost)} selected files ran in no shard (first: {lost[0]})")
    if extra:
        problems.append(f"{len(extra)} files ran that the selection does not list (first: {extra[0]})")
    sys.stdout.write(f"all shards: {executed} executed (floor {min_executed}), {skipped} skipped (budget {max_skipped})\n")
    if executed < min_executed:
        problems.append(f"the shards executed {executed} tests, under the floor of {min_executed}")
    if skipped > max_skipped:
        problems.append(f"the shards skipped {skipped} tests, over the budget of {max_skipped}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--junit", type=pathlib.Path)
    parser.add_argument("--selected", type=pathlib.Path)
    parser.add_argument("--collected", type=int)
    parser.add_argument("--aggregate", type=pathlib.Path, help="a directory of shard directories (release PRs)")
    parser.add_argument("--all", type=pathlib.Path, help="with --aggregate: the whole selection, one file per line")
    parser.add_argument("--min-executed", type=int, default=0)
    parser.add_argument("--max-skipped", type=int, default=0)
    args = parser.parse_args(argv)
    if args.aggregate is not None:
        if args.all is None:
            parser.error("--aggregate needs --all")
        shard_dirs = [p for p in args.aggregate.iterdir() if p.is_dir()]
        if not shard_dirs:
            problems = [f"no shard directories under {args.aggregate}"]
        else:
            problems = aggregate(shard_dirs, _lines(args.all), args.min_executed, args.max_skipped)
    else:
        if args.junit is None or args.selected is None or args.collected is None:
            parser.error("--junit, --selected and --collected are required without --aggregate")
        problems = check(args.junit, _lines(args.selected), args.collected)
    for p in problems:
        sys.stdout.write(f"::error::{p}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
