# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 P3.1 (nexus-f9bgu.15): ``current_platform_tag`` with the platform
injected, so the Windows branch runs on every host (no skip-pass)."""
from __future__ import annotations

import platform

import pytest

from nexus.db import pg_bundle

_CASES = [
    ("Darwin", "arm64", "mac-arm64"),
    ("Darwin", "x86_64", "mac-x64"),
    ("Linux", "x86_64", "linux-amd64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Windows", "AMD64", "windows-x64"),
    ("Windows", "x86_64", "windows-x64"),
    ("Windows", "amd64", "windows-x64"),
]


@pytest.mark.parametrize(("system", "machine", "expected"), _CASES)
def test_tag_for_injected_platform(system: str, machine: str, expected: str) -> None:
    assert pg_bundle.current_platform_tag(system=system, machine=machine) == expected


def test_windows_x64_branch_is_exercised() -> None:
    """Non-vacuity: the table above must contain the Windows branch, and it
    must produce the tag the release legs publish."""
    windows = [c for c in _CASES if c[0] == "Windows"]
    assert windows, "no Windows case in the table: the Windows branch would never run"
    assert {c[2] for c in windows} == {"windows-x64"}


@pytest.mark.parametrize("machine", ["ARM64", "arm64", "aarch64"])
def test_windows_arm_still_refuses(machine: str) -> None:
    with pytest.raises(RuntimeError) as exc:
        pg_bundle.current_platform_tag(system="Windows", machine=machine)
    msg = str(exc.value)
    assert "ARM" in msg
    assert "N+1" not in msg


@pytest.mark.parametrize("machine", ["x86", "i386"])
def test_windows_32bit_refuses(machine: str) -> None:
    with pytest.raises(RuntimeError):
        pg_bundle.current_platform_tag(system="Windows", machine=machine)


def test_unknown_system_refuses_without_release_n_plus_1_wording() -> None:
    with pytest.raises(RuntimeError) as exc:
        pg_bundle.current_platform_tag(system="FreeBSD", machine="amd64")
    assert "N+1" not in str(exc.value)


def test_default_arguments_read_the_real_host() -> None:
    system = platform.system().lower()
    if system in {"darwin", "linux", "windows"}:
        assert pg_bundle.current_platform_tag() == pg_bundle.current_platform_tag(
            system=platform.system(), machine=platform.machine()
        )
