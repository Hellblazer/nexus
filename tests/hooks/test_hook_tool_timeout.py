# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A hook tool answers even when its ``run()`` never does (nexus-5dcky).

THE BUG. On native Windows with no service endpoint — the default state for
every Windows box, since the PG bundle has no Windows target and ``nx init``
refuses — the first storage-touching MCP tool call in a server process never
returned. Measured 2026-09-22 on qwentescence: ``tuple_registry`` and
``hook_stop_verification`` both blocked past 300s, while ``hook_auto_approve``
and ``hook_stop_failure``, which touch no storage, returned in 0.0s.
``hooks.json`` wires ``hook_stop_verification`` on Stop, so ``claude -p``
answered the prompt and then sat there — the reported symptom, and the reason
two observations of it read as "past 100s" and "past 150s" rather than as a
number: both were under the harness's own 180s bound and the probe was killed
first.

WHAT THESE TESTS PIN, and what they deliberately do not. They pin that a
hook whose ``run()`` does not return still produces a tool result, within its
bound, in the shape a crashed hook already produces. They do NOT reproduce
the Windows import pathology: it does not reproduce on Linux (the same tool
returns its endpoint error in 0.5s there) and it is not the only way a hook
can block, which is exactly why the fix is a bound at the boundary rather
than a repair to one blocking path. A test that needed that pathology could
only run on a Windows box, and would pin the cause instead of the contract.
"""
from __future__ import annotations

import threading
import time

import pytest

from nexus._hook_runtime._io import HookResult
from nexus.mcp import hooks as _hooks

pytestmark = pytest.mark.unit


def _never_returns(started: threading.Event, release: threading.Event):
    def run(_payload):  # noqa: ANN001, ANN202 — matches HookToolSpec.run's shape
        started.set()
        # Bounded by the test's own teardown rather than by wall clock: a
        # test that slept for real would trade determinism for nothing.
        release.wait(timeout=30)
        return HookResult(stdout="TOO LATE")

    return run


def test_a_hook_that_never_returns_still_answers_within_its_bound() -> None:
    started, release = threading.Event(), threading.Event()
    spec = _hooks.HookToolSpec(
        name="probe_blocking",
        run=_never_returns(started, release),
        timeout_s=0.25,
    )
    try:
        t0 = time.monotonic()
        result = _hooks._run_bounded(spec, None, "hook_probe_blocking")
        elapsed = time.monotonic() - t0

        assert started.wait(timeout=5), "the hook never ran; the test proves nothing"
        assert elapsed < 5, (
            f"the bound did not hold: {elapsed:.1f}s for a 0.25s timeout. "
            "Without it this call does not return at all, which is nexus-5dcky."
        )
        assert result.crashed is True
        assert not result.stdout, (
            "a timed-out hook must say nothing, not deliver a late answer"
        )
    finally:
        release.set()


def test_a_hook_that_returns_in_time_is_untouched() -> None:
    """The bound must not change the ordinary path — the case that is not
    the bug, and the one every wired hook takes on every fire.
    """
    spec = _hooks.HookToolSpec(
        name="probe_fast",
        run=lambda _payload: HookResult(stdout="ANSWERED"),
        timeout_s=30.0,
    )
    result = _hooks._run_bounded(spec, None, "hook_probe_fast")
    assert result.stdout == "ANSWERED"
    assert result.crashed is False


def test_the_payload_reaches_run_through_the_bound() -> None:
    """A bound that dropped the payload would pass the timeout tests above
    and break every hook that reads one.
    """
    seen: dict[str, object] = {}

    def run(payload):  # noqa: ANN001, ANN202
        seen["payload"] = payload
        return HookResult(stdout="ok")

    spec = _hooks.HookToolSpec(name="probe_payload", run=run, timeout_s=30.0)
    _hooks._run_bounded(spec, {"session_id": "s-1"}, "hook_probe_payload")
    assert seen["payload"] == {"session_id": "s-1"}


def test_every_registered_hook_bound_is_positive_and_finite() -> None:
    """Non-vacuity plus the one invariant a future spec could get wrong.

    A spec added with ``timeout_s=0`` or a negative would make its tool
    answer before its hook ever ran, which reads as "the hook said nothing"
    on every single fire — the inert-gate failure this module's sibling
    ``DECIDING_HOOKS`` comment describes, arrived at from the other side.
    """
    assert len(_hooks.HOOK_TOOLS) >= 8, (
        f"only {len(_hooks.HOOK_TOOLS)} hook specs found; this check has lost "
        "its grip on the registration table rather than the table having shrunk"
    )
    for spec in _hooks.HOOK_TOOLS:
        assert spec.timeout_s > 0, f"{spec.name}: timeout_s must be positive"
        assert spec.timeout_s <= 180, (
            f"{spec.name}: timeout_s={spec.timeout_s} is at or above the "
            "largest budget hooks.json gives any conexus hook (Stop, 180s), "
            "so the bound could never fire before the harness gave up"
        )
