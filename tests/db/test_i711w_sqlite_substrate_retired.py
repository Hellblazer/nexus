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

Each child gets its OWN build-lease root (``NX_BUILD_LEASE_ROOT``), so the
box's live lease never decides the outcome (nexus-fam6l): a Maven run holding
the shared lease used to make the child exit 75 at the session-start gate
before the substrate check could name the bad value.
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


def _run(substrate: str, lease_root: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NX_TEST_T2_SUBSTRATE": substrate, "NX_BUILD_LEASE_ROOT": str(lease_root)}
    env.pop("NX_BUILD_LEASE_WAIT", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", _PROBE, "-q", "--no-header"],
        env=env,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _hold_build_lease(lease_root: Path) -> None:
    """Write a lease the reader in ``tests/db/_service_fixture.py`` treats as
    HELD: a ``service`` directory whose ``pid`` is this (live) test process."""
    lease = lease_root / "service"
    lease.mkdir(parents=True)
    (lease / "pid").write_text(str(os.getpid()))
    (lease / "label").write_text("nexus-fam6l-test")
    (lease / "command").write_text("scripts/mvnw-leased.sh ./mvnw test")
    (lease / "ts").write_text("2026-09-13T00:00:00Z")


def _assert_sqlite_refused_by_name(proc: subprocess.CompletedProcess[str]) -> None:
    combined = proc.stdout + proc.stderr
    assert "NX_TEST_T2_SUBSTRATE=sqlite" in combined, (
        "the run failed but never named the offending variable:\n" + combined[-2000:]
    )


def test_sqlite_substrate_is_rejected_not_silently_upgraded(tmp_path: Path) -> None:
    """``=sqlite`` must ERROR, and say what to use instead."""
    proc = _run("sqlite", tmp_path)

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


def test_sqlite_refusal_is_not_masked_by_a_held_build_lease(tmp_path: Path) -> None:
    """A held build lease must not pre-empt the ``=sqlite`` refusal (nexus-fam6l).

    Rejecting a substrate value needs no engine, so the session-start lease
    gate has nothing to protect. When it ran first, a Maven build anywhere on
    the box turned "you asked for a deleted substrate" into "a build is in
    progress", which is the wrong diagnosis and sends the reader to wait for
    a build that has nothing to do with their mistake.
    """
    _hold_build_lease(tmp_path)
    proc = _run("sqlite", tmp_path)
    # The refusal, not the session-start stale-jar BANNER: that advisory line
    # also quotes the held lease and prints on every run, so matching the
    # build text alone would test the banner instead of the gate.
    assert proc.returncode not in (0, 75), proc.returncode
    assert "refusing to start" not in proc.stdout + proc.stderr, (
        "the session-start build-lease gate refused the run before the "
        "substrate check could name =sqlite:\n" + (proc.stdout + proc.stderr)[-2000:]
    )
    _assert_sqlite_refused_by_name(proc)


def test_held_lease_gate_still_fires_for_the_engine_substrate(tmp_path: Path) -> None:
    """Non-vacuity control for the test above: the lease written here IS read
    as held, so an engine-substrate run is refused over it. Without this, a
    lease the reader never recognised would make the masking test pass for
    the wrong reason."""
    _hold_build_lease(tmp_path)
    env_substrate = ""
    proc = _run(env_substrate, tmp_path)
    assert proc.returncode == 75, (
        "an engine-substrate run beside a held lease was not refused at the "
        "session-start gate:\n" + (proc.stdout + proc.stderr)[-2000:]
    )
    assert "refusing to start" in proc.stdout + proc.stderr


def test_none_substrate_still_runs(tmp_path: Path) -> None:
    """Positive control: the child harness itself is fine.

    Without this, a broken subprocess invocation (bad cwd, missing dep, import
    error) would make the test above pass for entirely the wrong reason — the
    exact vacuity that a returncode-only assertion invites.
    """
    proc = _run("none", tmp_path)
    assert proc.returncode == 0, (
        "NX_TEST_T2_SUBSTRATE=none could not run the probe, so the =sqlite "
        "rejection above proves nothing about the value:\n"
        + (proc.stdout + proc.stderr)[-2000:]
    )
