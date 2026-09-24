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


def test_a_timed_out_hook_does_not_keep_the_process_alive() -> None:
    """The first cut of this fix used a ThreadPoolExecutor, and that is the
    one thing it could not do.

    ``concurrent.futures.thread`` registers a process-wide ``atexit`` handler
    that ``join()``s every live pool thread; ``shutdown(wait=False)`` does not
    exempt it. So an abandoned worker kept the interpreter from exiting —
    measured at the time as rc 124 under ``timeout 8`` against 0.26s for the
    same shape on a daemon thread. nx-mcp exits at stdin EOF and runs its T1
    shutdown there, so that would have moved the hang from the Stop hook to
    session end and left the orphaned process this bead opened with.

    A subprocess, because the claim is about interpreter exit and nothing
    asserted inside a live interpreter can see it.
    """
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent(
        """
        import threading
        from nexus._hook_runtime._io import HookResult
        from nexus.mcp import hooks

        release = threading.Event()

        def blocks(_payload):
            release.wait(timeout=600)
            return HookResult(stdout="late")

        spec = hooks.HookToolSpec(name="probe", run=blocks, timeout_s=0.2)
        result = hooks._run_bounded(spec, None, "hook_probe")
        assert result.crashed is True
        print("RETURNED", flush=True)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "RETURNED" in completed.stdout, (
        f"the bound itself did not hold in a subprocess: {completed.stderr[-500:]}"
    )
    assert completed.returncode == 0, (
        "the process did not exit cleanly with a hook still blocked in its "
        f"abandoned worker: rc={completed.returncode}. If this times out "
        "instead, the worker is being joined at interpreter exit and nx-mcp "
        f"will hang at stdin EOF.\n{completed.stderr[-500:]}"
    )


def _wired_mcp_tool_timeouts() -> dict[str, float]:
    """``hook_<name>`` -> the smallest ``timeout`` hooks.json gives it.

    Read from the real plugin file, not a fixture: the number that matters is
    the one shipped sessions actually run under, and a fixture would let the
    two drift in exactly the direction that makes this check vacuous.
    """
    import json
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "hooks.json"
    wired: dict[str, float] = {}
    for entries in json.loads(path.read_text())["hooks"].values():
        for group in entries:
            for entry in group.get("hooks", []):
                if entry.get("type") != "mcp_tool":
                    continue
                tool, budget = entry.get("tool"), entry.get("timeout")
                if tool is None or budget is None:
                    continue
                wired[tool] = min(float(budget), wired.get(tool, float("inf")))
    return wired


def test_every_registered_hook_bound_is_positive() -> None:
    """A spec with ``timeout_s=0`` or negative would answer before its hook
    ever ran — "the hook said nothing" on every fire, which is the inert-gate
    failure this module's ``DECIDING_HOOKS`` comment describes, arrived at
    from the other side.
    """
    assert len(_hooks.HOOK_TOOLS) >= 8, (
        f"only {len(_hooks.HOOK_TOOLS)} hook specs found; this check has lost "
        "its grip on the registration table rather than the table having shrunk"
    )
    for spec in _hooks.HOOK_TOOLS:
        assert spec.timeout_s > 0, f"{spec.name}: timeout_s must be positive"


def test_no_wired_hook_outlasts_the_budget_hooks_json_gives_it() -> None:
    """A bound above its own harness budget can never fire, so the hook it
    was supposed to bound still holds its tool call open — the bug, with a
    number in front of it.

    This replaces an earlier version of this check that compared every spec
    against a single 180s ceiling, the largest budget any conexus hook has.
    Eight of the nine wired hooks run at 5s or 10s, so that check passed
    while every one of them carried a 30s bound it could never reach: a
    ceiling wide enough to admit the defect it was written to catch.
    """
    wired = _wired_mcp_tool_timeouts()
    assert len(wired) >= 8, (
        f"hooks.json yielded {len(wired)} mcp_tool entries; the parse has lost "
        "its grip on the file rather than the entries having gone"
    )
    by_name = {f"hook_{spec.name}": spec for spec in _hooks.HOOK_TOOLS}
    checked = 0
    for tool, budget in sorted(wired.items()):
        spec = by_name.get(tool)
        assert spec is not None, (
            f"hooks.json wires {tool} but no HookToolSpec registers it"
        )
        assert spec.timeout_s < budget, (
            f"{tool}: bound is {spec.timeout_s}s against a hooks.json budget "
            f"of {budget}s. At or above it the harness gives up first and the "
            "bound never fires; AT it the answer races the client's own "
            "deadline, and a late answer arrives as an unknown message id and "
            "tears the transport down (nexus-dgvsz). It has to land inside."
        )
        checked += 1
    assert checked >= 8, f"only {checked} wired hooks compared"
