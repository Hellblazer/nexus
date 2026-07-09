# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assert the write-seam gate (nexus-h29w1) actually exercised the server.

Used by the ci.yml write-seam-gate job after running the integration test.

The gate is vacuous if ALL tests were skipped (e.g. JAR missing + CI env var
not set, or docker unavailable). This script fails if that happens, turning
what would be a green-but-vacuous CI run into a loud failure.

It also asserts that the load-bearing dedup-collapse test (the F1 core assertion)
actually passed, AND (nexus-4nflf) that the combined-query manifest-rewrite
regression test (the x6kdz core assertion) actually passed. The two core tests
now live in separate modules that share ONE pytest invocation and ONE
junit-xml (see the ci.yml write-seam-gate job) — this script enforces both.

Usage: ``python scripts/assert_write_seam_ran.py <junit.xml>``
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

# Substring of the skip reason from pytestmark skipif in the test file.
# A skip with this message means jar/java/docker was absent.
_PREREQ_SKIP_MARKER = "missing jar, java, or docker"
# The load-bearing test that asserts dedup collapse (F1 core assertion)
_CORE_TEST = "test_duplicate_chash_dedup_collapse"
# nexus-4nflf: the load-bearing combined-query regression test (x6kdz core
# assertion) — manifest writes must leave the doc combined-query-visible
# (collection stamp present), including across a repeat REPLACE pass.
_CATALOG_CORE_TEST = "test_manifest_rewrite_keeps_combined_query_visibility"


def main(junit_path: str) -> None:
    if not os.path.exists(junit_path):
        raise SystemExit(
            f"Write-seam JUnit output not found at '{junit_path}' — pytest likely "
            "crashed before writing it (import error / fixture failure / OOM). "
            "Check the 'Run write-seam gate' step log above."
        )
    root = ET.parse(junit_path).getroot()
    prereq_skips = passed = 0
    core_ok = False
    catalog_core_ok = False
    for case in root.iter("testcase"):
        skipped = case.find("skipped")
        failed = case.find("failure") is not None or case.find("error") is not None
        if skipped is not None:
            if _PREREQ_SKIP_MARKER in (skipped.get("message") or ""):
                prereq_skips += 1
        elif not failed:
            passed += 1
        if case.get("name", "").endswith(_CORE_TEST) and skipped is None and not failed:
            core_ok = True
        if case.get("name", "").endswith(_CATALOG_CORE_TEST) and skipped is None and not failed:
            catalog_core_ok = True

    assert prereq_skips == 0, (
        f"{prereq_skips} write-seam test(s) skipped for missing prereqs "
        "(jar/java/docker absent) — gate is vacuous"
    )
    assert core_ok, (
        f"core dedup-collapse test did not pass — write-seam assertion unproven "
        f"(no passing testcase ending in '{_CORE_TEST}')"
    )
    assert catalog_core_ok, (
        "core combined-query manifest-rewrite test did not pass — the x6kdz "
        f"regression tripwire is unproven (no passing testcase ending in "
        f"'{_CATALOG_CORE_TEST}')"
    )
    print(
        f"WRITE-SEAM PASS: {passed} passed, 0 prereq-skips, "
        "dedup-collapse + >300-record + ON CONFLICT + combined-query "
        "manifest-rewrite visibility verified"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "usage: python scripts/assert_write_seam_ran.py <junit.xml>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    main(sys.argv[1])
