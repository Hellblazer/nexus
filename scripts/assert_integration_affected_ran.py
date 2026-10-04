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
``cloud_mode``, ``mandatory_regression_pin``), which run in other gates.

Stdlib only.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import xml.etree.ElementTree as ET

#: Integration files that execute nothing on a GitHub-hosted Linux runner,
#: each for a reason the runner cannot remove. A file not listed here that
#: skips every test fails the job: that is the silent coverage loss this
#: job exists to catch.
ALL_SKIP_ALLOWED: dict[str, str] = {
    "tests/test_dt_mcp_stdio_transport.py": "DEVONthink MCP binary, macOS only",
    "tests/test_search_fanout_recall_parity.py": (
        "needs a real nexus config pointing at the live cloud engine"
    ),
    "tests/db/test_pg_bundle_extract.py": (
        "needs a NEXUS_PG_BUNDLE .txz that only a CA-3 bundle build produces"
    ),
    "tests/daemon/test_binary_install_verify.py": (
        "its integration test needs a published engine .sigstore.json fixture"
    ),
}


def _module(rel: str) -> str:
    return rel.removesuffix(".py").replace("/", ".")


def check(junit: pathlib.Path, selected: list[str], collected: int) -> list[str]:
    """Return a list of problems; empty means the run was not vacuous."""
    root = ET.parse(junit).getroot()
    cases = [
        (c.get("classname") or "", c.find("skipped") is None)
        for c in root.iter("testcase")
    ]
    problems: list[str] = []
    if len(cases) != collected:
        problems.append(
            f"JUnit has {len(cases)} testcases but --collect-only reported "
            f"{collected} for the same selection"
        )
    if collected == 0:
        sys.stdout.write("selected files collect no tests under the mark expression; nothing to run\n")
        return problems
    for rel in selected:
        mod = _module(rel)
        mine = [ran for cls, ran in cases if cls == mod or cls.startswith(mod + ".")]
        executed = sum(mine)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--junit", type=pathlib.Path, required=True)
    parser.add_argument("--selected", type=pathlib.Path, required=True)
    parser.add_argument("--collected", type=int, required=True)
    args = parser.parse_args(argv)
    selected = [s.strip() for s in args.selected.read_text().splitlines() if s.strip()]
    problems = check(args.junit, selected, args.collected)
    for p in problems:
        sys.stdout.write(f"::error::{p}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
