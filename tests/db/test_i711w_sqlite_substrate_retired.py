# SPDX-License-Identifier: AGPL-3.0-or-later
"""``NX_TEST_T2_SUBSTRATE=sqlite`` fails loudly rather than silently (nexus-i711w).

Stage 1b deleted the SQLite test substrate. ``=sqlite`` was a DOCUMENTED escape
hatch — ``_pin_t2_substrate``'s own docstring offered it for "bisecting a
suspected engine-side regression against the old baseline" — so it is exactly
the value a stale shell or a stale runbook still carries. Resolving it to the
engine would hand that person a green run that did not test what they believe it
tested: the silent-fallback-on-a-correctness-question class the project bans.

Asserting this in-process is impossible: the autouse fixture raises during
setup, so the assertion would have to survive its own harness. The proof
therefore runs a real pytest in a SUBPROCESS with the variable exported, and
pairs it with a positive control on ``=none`` so a failure means "the value was
rejected", not "the child was broken anyway".

Delete the raise in ``_pin_t2_substrate`` and the first test fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: A substrate-free target: it asserts on env vars only, so it passes under
#: ``=none`` without booting a PG or a JVM. That keeps the positive control
#: cheap and keeps this file from spawning a database to check a string.
_PROBE = (
    "tests/db/test_ambient_service_env_isolation.py"
    "::test_scrub_is_visible_to_the_running_test"
)


def _run(substrate: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", _PROBE, "-q", "--no-header"],
        env={**os.environ, "NX_TEST_T2_SUBSTRATE": substrate},
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_sqlite_substrate_is_rejected_not_silently_upgraded() -> None:
    """``=sqlite`` must ERROR, and say what to use instead."""
    proc = _run("sqlite")

    assert proc.returncode != 0, (
        "NX_TEST_T2_SUBSTRATE=sqlite ran to green. The SQLite substrate was "
        "deleted in nexus-i711w, so this run silently used the ENGINE while "
        "reporting success — anyone bisecting against 'the old baseline' now "
        "gets a result that means nothing. _pin_t2_substrate must raise."
    )
    combined = proc.stdout + proc.stderr
    assert "NX_TEST_T2_SUBSTRATE=sqlite" in combined, (
        "the run failed but never named the offending variable:\n" + combined[-2000:]
    )
    assert "nexus-i711w" in combined, (
        "the refusal does not point at the bead that removed the substrate, so "
        "the reader has nowhere to go:\n" + combined[-2000:]
    )
    assert "NX_TEST_T2_SUBSTRATE=none" in combined, (
        "the refusal does not offer the replacement spelling for 'this test "
        "needs no T2 substrate', which is what most =sqlite users actually "
        "meant:\n" + combined[-2000:]
    )


def test_none_substrate_still_runs() -> None:
    """Positive control: the child harness itself is fine.

    Without this, a broken subprocess invocation (bad cwd, missing dep, import
    error) would make the test above pass for entirely the wrong reason — the
    exact vacuity that a returncode-only assertion invites.
    """
    proc = _run("none")
    assert proc.returncode == 0, (
        "NX_TEST_T2_SUBSTRATE=none could not run the probe, so the =sqlite "
        "rejection above proves nothing about the value:\n"
        + (proc.stdout + proc.stderr)[-2000:]
    )
