# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pytest gate for ``tests/e2e/lib/expectations_test.sh`` (nexus-3zu8g).

WHY THIS FILE EXISTS. ``expectations_test.sh`` is a 624-line, 63-assertion
unit suite for the RDR-184 ``expectations.sh`` shellib that the SubagentStop
guard, the census, and the declaration audit all read. Its own header says
"Run directly: ``bash tests/e2e/lib/expectations_test.sh``" — a command a
human has to remember. Before this file, NOTHING ran it: no pytest wrapper,
no CI job, no e2e runner step (nexus-3zu8g, found 2026-07-31). That gap was
not theoretical. nexus-7z7rj round 2 (2026-08-08) added an unguarded
``$NX_EXPECT_LOCK_HOLD_DELAY_S`` dereference to ``expectations.sh``; the
suite runs under ``set -u -o pipefail``, so from that commit onward it
ABORTED at "Test 4: named background teammate owes a report" with
``NX_EXPECT_LOCK_HOLD_DELAY_S: unbound variable``. Tests 4-15 went dark for
TWO DAYS during an active multi-round fix effort on that exact shellib,
because no gate would have noticed the abort either way. When round 3
restored the suite (fixed the deref, commit 6ae85883), it immediately caught
a real defect in round 3's OWN change (an awk field-count bug masked by
``read -a``'s silent-drop of a trailing empty field). See nexus-a4nun for
the two pre-existing assertion failures this file's companion fix
(nexus-3zu8g, 2026-08-09) retired — both were stale assertions pinning
REMOVED behavior (the named-agent-morphology gate dropped at nexus-hbr4x,
and the pre-nexus-suuja rc contract), not live defects in ``expectations.sh``
itself; see ``tests/e2e/lib/expectations_test.sh``'s Test 7 and the Test 15
CONTROL block for the corrected assertions and their rationale.

THE LOAD-BEARING REQUIREMENT. An abort produces NEITHER a "[FAIL]" line nor
a complete "N passed, M failed" summary line — a wrapper that greps stdout
for "failed" or "FAIL" would have stayed green through the entire two-day
blind window described above (it would see nothing, and "nothing" is not
the same as "clean"). This gate therefore asserts on the PROCESS EXIT CODE
first (the suite's own ``set -u -o pipefail`` plus its explicit
``exit 1``/``exit 0`` tail makes rc the honest signal an abort cannot fake),
and separately requires the summary line to be present with an EXACT pinned
count as a non-vacuity floor, so a hypothetical truncated-but-rc-0 run would
still fail. ``expectations_test.sh`` has no randomness in its assertion
COUNT (Test 2b races 40 concurrent writers but always asserts exactly 40
rows, never a variable number), so the count is pinned exactly rather than
via an inequality floor — matching this repo's "exact fixture assertions"
convention for deterministic fixtures.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE = REPO_ROOT / "tests" / "e2e" / "lib" / "expectations_test.sh"

# Exact pin (nexus-3zu8g, 2026-08-09). A deliberate addition of new
# assertions to expectations_test.sh updates this constant in the SAME
# commit; a mismatch here without that intent is exactly the drift class
# this gate exists to catch — never widen this to a floor/inequality to
# make a failing edit pass quietly.
EXPECTED_PASSED = 63
EXPECTED_FAILED = 0

_SUMMARY_RE = re.compile(r"expectations_test\.sh: (\d+) passed, (\d+) failed")


def test_expectations_shellib_suite_is_green() -> None:
    """The RDR-184 ledger shellib's own unit suite must run to completion
    and report zero failures.

    Gates on the subprocess return code, not on grepping stdout for a
    failure marker (see module docstring for why that distinction is the
    entire point of this test). A future edit to expectations.sh that
    reintroduces an unguarded ``set -u`` deref, or any other early-abort
    defect, fails THIS test via the rc assertion even though it would
    print no "[FAIL]" line at all.
    """
    result = subprocess.run(
        ["bash", str(SUITE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    match = _SUMMARY_RE.search(result.stdout)
    assert match is not None, (
        "expectations_test.sh produced no 'N passed, M failed' summary "
        "line -- the suite aborted before reaching its own final print. "
        "This IS the nexus-a4nun/nexus-3zu8g failure shape (e.g. an "
        "unguarded `set -u` deref): no [FAIL] line, no complete summary, "
        "nothing for a stdout-grep wrapper to see.\n"
        f"rc={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    passed, failed = int(match.group(1)), int(match.group(2))

    assert result.returncode == 0, (
        f"expectations_test.sh exited {result.returncode} "
        f"(reported passed={passed} failed={failed}) -- the process exit "
        "code is the load-bearing signal here, not stdout content.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert (passed, failed) == (EXPECTED_PASSED, EXPECTED_FAILED), (
        "expectations_test.sh pass/fail counts drifted from the pinned "
        f"exact contract (passed={EXPECTED_PASSED}, failed={EXPECTED_FAILED}) "
        f"-- got (passed={passed}, failed={failed}). If this is a "
        "deliberate addition of assertions to expectations_test.sh, update "
        "EXPECTED_PASSED/EXPECTED_FAILED in this file in the same commit; "
        "an unexplained drift is exactly the vacuous-verification class "
        "nexus-3zu8g exists to catch."
    )
