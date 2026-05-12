# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-84a6: source-level guard preventing the
``json.loads(result.output)`` anti-pattern from reappearing.

Click 8.2 changed semantics: ``result.output`` is now stdout + stderr
interleaved, not just stdout. JSON-parsing the merged stream fails
non-deterministically on Linux CI when a structlog/warn line lands
before the JSON payload — surfaces as ``JSONDecodeError`` with
either ``Extra data`` or ``Expecting value`` (empty if the log
flushes after stdout was captured).

The fix: parse ``result.stdout`` instead. This test file enforces
the rule across the whole test tree so the rot can't sneak back in
via a copy-paste.

Allow-list: this file legitimately mentions the anti-pattern in a
docstring; it's filtered out before the assertion.
"""
from __future__ import annotations

import re
from pathlib import Path


_PATTERN = re.compile(r"json\.loads\(\s*result\.output\s*\)")


def test_no_json_loads_on_result_output() -> None:
    """Every CliRunner-based test that JSON-parses the result must
    use ``result.stdout`` (clean), not ``result.output`` (stdout +
    stderr interleaved). See nexus-84a6 and Click 8.2 release notes.
    """
    tests_dir = Path(__file__).parent
    offenders: list[tuple[Path, int, str]] = []
    for path in tests_dir.rglob("test_*.py"):
        if path.name == Path(__file__).name:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append((path.relative_to(tests_dir), lineno, line.strip()))

    if offenders:
        sample = "\n  ".join(
            f"{p}:{n}  {l!r}" for p, n, l in offenders[:10]
        )
        suffix = (
            f"\n  ... (+{len(offenders) - 10} more)"
            if len(offenders) > 10
            else ""
        )
        raise AssertionError(
            "nexus-84a6: tests use json.loads(result.output) which "
            "parses stdout+stderr interleaved (Click 8.2+ change). "
            "Switch to json.loads(result.stdout):\n  "
            + sample
            + suffix
        )
