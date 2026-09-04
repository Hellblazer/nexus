# SPDX-License-Identifier: AGPL-3.0-or-later
"""The no-bare-green advisory (bead nexus-1c7oq).

A gate that passes because a dependency was absent, or because a fallback
path took over from the one it was written for, still exits 0 and reads as
a plain green. The vacuous-gate doctrine (nexus-moht0) fails a gate that
examined nothing; this covers the quieter case where the gate examined
something else. The gate prints ONE line in a fixed shape and keeps its
exit code, so a summary can count the lines and a reader can see what the
green rested on.

Producers: :func:`scripts.release_choreography.emit_choreography` for a
table row carrying ``advisory = "passed-by-default"``, the second-parent
evidence path in ``scripts/check_release_ci_evidence.py``, and
``tests/e2e/lib/gate_advisory.sh``'s ``passed_by_default`` for the shell
gates. Consumers count with :func:`count_passed_by_default` or a grep on
the prefix. One prefix, all of them; a stray space would split the count.
"""
from __future__ import annotations

from typing import Final

#: The literal every producer writes and every consumer greps.
PASSED_BY_DEFAULT_PREFIX: Final = "GATE PASSED-BY-DEFAULT: "


def passed_by_default(gate: str, reason: str) -> str:
    """The advisory line for *gate*, exit 0 unchanged: ``GATE
    PASSED-BY-DEFAULT: <gate> <reason>``. *gate* is the gate's own name
    (a script or row id), *reason* one clause naming the default that
    carried the pass."""
    gate = " ".join(gate.split())
    reason = " ".join(reason.split())
    if not gate or not reason:
        raise ValueError("passed_by_default needs a gate name and a reason")
    return f"{PASSED_BY_DEFAULT_PREFIX}{gate} {reason}"


def count_passed_by_default(text: str) -> int:
    """How many advisory lines *text* carries (one per gate that passed
    by default; a gate never prints two)."""
    return sum(1 for line in text.splitlines() if line.startswith(PASSED_BY_DEFAULT_PREFIX))
