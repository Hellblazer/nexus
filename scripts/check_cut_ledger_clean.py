#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plugin-cut gate: does PENDING_RELEASE.md still name a path this cut ships?

Extracted from an inline `grep -qF` loop in
`.github/workflows/plugin-release.yml`, which failed EVERY cut (found
2026-08-23 exercising 7.16.0).

The loop scanned each touched path against the WHOLE ledger file:

    if grep -qF -- "$path" conexus/PENDING_RELEASE.md; then offender

`grep -qF` is an unanchored whole-file substring match. It neither
restricts to bullet entries nor skips the header. The ledger's permanent
header prose contains the literal string `.claude-plugin/marketplace.json`,
and every cut touches that file by construction -- moving `source.ref` IS
the cut. So the offender list was never empty and the step always exited 1.

The step had no test, and the channel has never been cut, so it shipped
having never executed once. That is the reason this logic now lives in
Python: a `run:` block inside workflow YAML is not executed by anything in
the test suite, so its first execution is a real tag push. The workflow
step is now a thin invocation and the decision is unit-testable
(`stale_ledger_offenders`), with the wiring pinned by
`tests/test_plugin_release_workflow.py`.

What the ledger means and why entry-vs-header is the right cut is
`conexus/PENDING_RELEASE.md`'s own header. `path_entries` (reused from
`cut_plugin_release`) already draws exactly that distinction for the
attribution rule: an ENTRY is a bullet block carrying a path-shaped
backtick span; header contract prose is not an entry and is never
attributed.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
from collections.abc import Iterable

from cut_plugin_release import path_entries

#: Ledger path, repo-relative. The one the plugin channel reads.
LEDGER_PATH = "conexus/PENDING_RELEASE.md"


def stale_ledger_offenders(touched: Iterable[str], ledger_text: str) -> list[str]:
    """Paths in *touched* that a ledger ENTRY still names.

    Header prose is excluded by construction: only bullet blocks carrying
    a path-shaped span are entries. Matching within an entry stays a
    substring test, preserving the original step's semantics for
    everything except the header it should never have been reading.
    """
    entries = path_entries(ledger_text)
    offenders: list[str] = []
    for raw in touched:
        path = raw.strip()
        if not path:
            continue
        if any(path in entry for entry in entries):
            offenders.append(path)
    return offenders


def _touched_paths(repo: pathlib.Path, base: str, cut: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..{cut}"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("BASE_CLIENT_TAG", ""),
                        help="base client tag the cut anchors to (default: $BASE_CLIENT_TAG)")
    parser.add_argument("--cut", default=os.environ.get("CUT_TAG", ""),
                        help="the plugin cut tag (default: $CUT_TAG)")
    parser.add_argument("--repo", default=".", help="repository root")
    args = parser.parse_args(argv)

    if not args.base or not args.cut:
        print("both --base and --cut are required (or BASE_CLIENT_TAG/CUT_TAG "
              "in the environment) -- refusing to check a range I cannot name",
              file=sys.stderr)
        return 2

    repo = pathlib.Path(args.repo).resolve()
    touched = _touched_paths(repo, args.base, args.cut)
    print("paths touched by this cut's range:")
    for path in touched:
        print(f"  {path}")

    ledger = (repo / LEDGER_PATH).read_text(encoding="utf-8")
    offenders = stale_ledger_offenders(touched, ledger)
    if offenders:
        print("STALE LEDGER ENTRY: this cut ships the following path(s), but "
              f"{LEDGER_PATH} still names them:", file=sys.stderr)
        for path in offenders:
            print(f"  {path}", file=sys.stderr)
        print("Empty the covering bullet(s) before cutting -- see "
              f"{LEDGER_PATH}'s own header for the rule.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
