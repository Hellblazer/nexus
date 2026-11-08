# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Technical Design 6, OS limits: the CPU and memory values, and the macOS RLIMIT_AS ValueError."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from nexus.lua_runtime import oslimits
from tests.lua_runtime._support import platform_only


class _FakeResource:
    RLIMIT_CPU = "cpu"
    RLIMIT_AS = "as"

    def __init__(self, as_error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, tuple[int, int]]] = []
        self._as_error = as_error

    def setrlimit(self, which: Any, value: tuple[int, int]) -> None:
        if which == self.RLIMIT_AS and self._as_error is not None:
            raise self._as_error
        self.calls.append((which, value))


def test_cpu_is_the_wall_limit_plus_5_soft_and_plus_10_hard_and_memory_is_1_gib() -> (
    None
):
    lim = oslimits.limits_for(60)
    assert (lim.cpu_soft_s, lim.cpu_hard_s, lim.as_bytes) == (65, 70, 1 << 30)
    assert (
        oslimits.limits_for(1.2).cpu_soft_s == 2 + 5
    )  # a fractional wall limit rounds up


def test_limits_travel_as_arguments_and_come_back() -> None:
    lim = oslimits.limits_for(30, cpu_margins=(1, 2), as_bytes=123456)
    assert oslimits.OsLimits.from_args(lim.to_args()) == lim
    with pytest.raises(ValueError):
        oslimits.OsLimits.from_args(["--evil", "1"])


def test_posix_sets_cpu_then_address_space() -> None:
    res = _FakeResource()
    report = oslimits.apply_in_worker(
        oslimits.limits_for(60), platform="linux", resource_module=res
    )
    assert res.calls == [("cpu", (65, 70)), ("as", (1 << 30, 1 << 30))]
    assert report["as"] == 1 << 30 and report["as_error"] is None


def test_macos_rlimit_as_valueerror_is_caught_and_the_run_goes_on() -> None:
    res = _FakeResource(as_error=ValueError("current limit exceeds maximum limit"))
    report = oslimits.apply_in_worker(
        oslimits.limits_for(60), platform="darwin", resource_module=res
    )
    assert res.calls == [("cpu", (65, 70))]  # CPU still set
    assert report["as"] is None and report["as_error"].startswith("ValueError")


def test_the_valueerror_is_caught_on_macos_only() -> None:
    res = _FakeResource(as_error=ValueError("nope"))
    with pytest.raises(ValueError):
        oslimits.apply_in_worker(
            oslimits.limits_for(60), platform="linux", resource_module=res
        )


def test_any_other_failure_to_set_a_limit_propagates_on_macos_too() -> None:
    res = _FakeResource(as_error=OSError("operation not permitted"))
    with pytest.raises(OSError):
        oslimits.apply_in_worker(
            oslimits.limits_for(60), platform="darwin", resource_module=res
        )


def test_windows_sets_nothing_in_the_worker() -> None:
    res = _FakeResource()
    report = oslimits.apply_in_worker(
        oslimits.limits_for(60), platform="win32", resource_module=res
    )
    assert res.calls == [] and report["by"] == "job"


def _apply_in_child(wall: int) -> dict[str, Any]:
    code = textwrap.dedent(
        f"""
        import json, resource, sys
        from nexus.lua_runtime import oslimits
        rep = oslimits.apply_in_worker(oslimits.limits_for({wall}))
        json.dump({{"report": rep, "cpu": resource.getrlimit(resource.RLIMIT_CPU),
                   "as": resource.getrlimit(resource.RLIMIT_AS)}}, sys.stdout)
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


@platform_only("darwin")
def test_macos_really_refuses_rlimit_as_and_the_cpu_limit_still_lands() -> None:
    """Assumption of research 32, on this platform: setrlimit(RLIMIT_AS) raises ValueError."""
    got = _apply_in_child(60)
    assert got["report"]["as"] is None, got
    assert str(got["report"]["as_error"]).startswith("ValueError"), got
    assert got["cpu"] == [65, 70], got


@platform_only("linux")
def test_linux_really_enforces_rlimit_as_at_1_gib() -> None:
    """Assumption of research 33, on this platform: RLIMIT_AS is set and read back."""
    got = _apply_in_child(60)
    assert got["report"]["as"] == 1 << 30, got
    assert got["as"] == [1 << 30, 1 << 30], got
    assert got["cpu"] == [65, 70], got
