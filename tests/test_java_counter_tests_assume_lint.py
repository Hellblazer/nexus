# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lint: a Java test that needs the DDL counter must skip when it is off (nexus-3cwsw).

``-Dnexus.ddl.count=false`` runs the Java suite with no collector, probe, marks or count files, for a same-sha
A/B. A test that asserts on those goes red in the off arm and the arm stops being a measurement of the suite.
Every ``@Test`` whose body names a counter-only surface must carry
``assumeTrue(DdlCounts.enabled(), ...)``; the first live run of this check (counts below) is pasted into the
bead.

The scan is textual: a segment runs from one ``@Test`` to the next (or the end of the file), so a helper
written after a test counts as that test's. A class that toggles the switch itself or never touches a container
is exempt, by name and with the reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

JAVA_TESTS = Path(__file__).resolve().parents[1] / "service" / "src" / "test" / "java"

# Surfaces that exist only with the counter on.
COUNTER_ONLY = (
    "DdlLogParser.parse",
    "SERVER_LOG_FILE",
    "SERVER_LOG_DIR",
    "DdlCounts.walBytes",
    "DdlCounts.markEnd",
    "DdlCounts.markReady",
    "DdlCounts.flush",
    "DdlCounts.record",
    "nx_ddl_probe",
    "logging_collector",
    "log_statement",
)
ASSUMPTION = "assumeTrue(DdlCounts.enabled()"

# Files exempt from the check, each with its reason.
EXEMPT = {
    "DdlCountSwitchTest.java": "toggles the switch itself and asserts both states",
    "DdlLogParserTest.java": "parses string literals; starts no container",
}

# Single tests exempt from the check, as "<path under JAVA_TESTS>::<method>", each with its reason.
EXEMPT_TESTS = {
    "dev/nexus/service/PgServerLogTest.java::serverLog_cats_the_file_and_retries_a_stuck_exec_three_times": (
        "a stub container that is counter-on by construction (ddlCounted defaults to true); starts no server, "
        "so it runs the same with the switch off (nexus-t4y7b)"
    ),
}

# The scan must have examined at least this many counter-dependent tests: a pass over an empty set is a failure.
MIN_COUNTER_TESTS_SEEN = 5

_TEST = re.compile(r"^\s*@Test\b", re.MULTILINE)


def counter_dependent_segments(text: str) -> list[tuple[str, bool]]:
    """(first line of segment, has the assumption) for each @Test segment that names a counter-only surface."""
    starts = [m.start() for m in _TEST.finditer(text)]
    out: list[tuple[str, bool]] = []
    for begin, end in zip(starts, starts[1:] + [len(text)]):
        segment = text[begin:end]
        if any(token in segment for token in COUNTER_ONLY):
            head = re.search(r"void\s+(\w+)", segment)
            out.append((head.group(1) if head else "?", ASSUMPTION in segment))
    return out


def test_the_scan_flags_an_unguarded_test_and_passes_a_guarded_one() -> None:
    unguarded = "@Test\nvoid a() { PgContainerHelper.SERVER_LOG_FILE.length(); }\n"
    guarded = "@Test\nvoid b() { assumeTrue(DdlCounts.enabled(), \"x\"); DdlLogParser.parse(l); }\n"
    plain = "@Test\nvoid c() { assertThat(1).isEqualTo(1); }\n"
    assert counter_dependent_segments(unguarded + guarded + plain) == [("a", False), ("b", True)]


def test_every_counter_dependent_java_test_skips_when_the_counter_is_off() -> None:
    seen = 0
    unguarded: list[str] = []
    exempt_seen: set[str] = set()
    for path in sorted(JAVA_TESTS.rglob("*Test.java")):
        if path.name in EXEMPT:
            continue
        for name, guarded in counter_dependent_segments(path.read_text()):
            seen += 1
            key = f"{path.relative_to(JAVA_TESTS).as_posix()}::{name}"
            if key in EXEMPT_TESTS:
                exempt_seen.add(key)
            elif not guarded:
                unguarded.append(key)
    assert seen >= MIN_COUNTER_TESTS_SEEN, f"the scan saw {seen} counter-dependent tests: it is looking at the wrong tree"
    stale = sorted(set(EXEMPT_TESTS) - exempt_seen)
    assert stale == [], f"EXEMPT_TESTS names tests the scan no longer flags; drop them: {stale}"
    assert unguarded == [], (
        "these Java tests assert on the DDL counter's surfaces but do not "
        f"{ASSUMPTION}, ...): they fail in a -Dnexus.ddl.count=false run: {unguarded}"
    )
