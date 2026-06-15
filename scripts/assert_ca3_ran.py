# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assert a CA-3 run actually exercised the bundle (not vacuously all-skipped).

Used by the ci.yml CA-3 jobs (linux matrix + macOS). Platform-conditional tests
are LEGITIMATELY skipped — the GLIBC-floor tests skip on macOS, the macOS-floor
tests skip on linux — so a blanket ``skipped == 0`` is wrong. Only a skip caused
by the BUNDLE being absent makes the gate vacuous; that is what we guard against.
Also require the load-bearing ``CREATE EXTENSION vector`` test to have passed.

Usage: ``python scripts/assert_ca3_ran.py <junit.xml>``
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

# Substring of the bundle-absence skip reason (pytestmark skipif in
# tests/db/test_pg_provision_ca3_bundle.py). A skip carrying this message means
# NEXUS_CA3_BUNDLE was not materialized — the gate would prove nothing.
_BUNDLE_ABSENT_MARKER = "no CA-3 bundle"
_CORE_TEST = "test_create_extension_vector_loads"


def main(junit_path: str) -> None:
    # A missing JUnit file means pytest crashed before writing it (import error,
    # fixture failure, OOM) — fail with the real cause, not a bare
    # FileNotFoundError that points at the wrong place (code-review M3).
    if not os.path.exists(junit_path):
        raise SystemExit(
            f"CA-3 JUnit output not found at '{junit_path}' — pytest likely crashed "
            "before writing it (import error / fixture failure / OOM). Check the "
            "'Run CA-3 live test' step log above."
        )
    root = ET.parse(junit_path).getroot()
    bundle_skips = passed = 0
    core_ok = False
    for case in root.iter("testcase"):
        skipped = case.find("skipped")
        failed = case.find("failure") is not None or case.find("error") is not None
        if skipped is not None:
            if _BUNDLE_ABSENT_MARKER in (skipped.get("message") or ""):
                bundle_skips += 1
        elif not failed:
            passed += 1
        # endswith, not ==: future pytest may class-qualify the @name.
        if case.get("name", "").endswith(_CORE_TEST) and skipped is None and not failed:
            core_ok = True

    assert bundle_skips == 0, (
        f"{bundle_skips} CA-3 test(s) skipped for bundle-absence — gate is vacuous "
        "(NEXUS_CA3_BUNDLE not materialized)"
    )
    assert core_ok, (
        "core CREATE EXTENSION vector test did not pass — assemble path unproven "
        f"(no passing testcase ending in '{_CORE_TEST}')"
    )
    print(f"CA-3 PASS: {passed} passed, 0 bundle-skips, pgvector load + floor verified")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/assert_ca3_ran.py <junit.xml>", file=sys.stderr)
        raise SystemExit(2)
    main(sys.argv[1])
