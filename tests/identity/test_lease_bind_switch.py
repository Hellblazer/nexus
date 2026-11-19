# SPDX-License-Identifier: AGPL-3.0-or-later
"""``NX_IDENTITY_BIND=0`` also keeps the MCP server's identity lease from starting, so the one switch that stops the hook
from binding stops the lease from binding or renewing as well (a process may set both switches; an installed nx
that has only the hook's, or only the lease's, still stops). Tenant-free: the lease returns before it looks for a
Claude ancestor or builds a client."""
from __future__ import annotations

import pytest

from nexus.mcp import identity_lease


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A lease that is not already running, no LEASE switch (the suite sets one), and a Claude-ancestor search that records."""
    monkeypatch.setattr(identity_lease, "_LEASE", None)
    monkeypatch.delenv("NX_IDENTITY_LEASE", raising=False)
    monkeypatch.delenv("NX_IDENTITY_BIND", raising=False)
    looked: list[str] = []

    def find(start_pid: int = 0) -> int:
        looked.append("looked for a claude ancestor")
        return 0  # none: the lease would then return None for want of one

    monkeypatch.setattr("nexus.session.find_immediate_claude_pid", find)
    return looked


@pytest.mark.parametrize("value", ["0", " 0 ", "0\n"])
def test_the_bind_switch_stops_the_lease_before_it_looks_for_a_process(probe: list[str], monkeypatch, value) -> None:
    monkeypatch.setenv("NX_IDENTITY_BIND", value)
    assert identity_lease.start() is None
    assert probe == [], "the switch returns before anything is read"


@pytest.mark.parametrize("value", [None, "1", ""])
def test_without_the_bind_switch_the_lease_goes_on_to_look_for_its_process(probe: list[str], monkeypatch, value) -> None:
    if value is not None:
        monkeypatch.setenv("NX_IDENTITY_BIND", value)
    assert identity_lease.start() is None            # no claude ancestor in this test
    assert probe == ["looked for a claude ancestor"], "behaviour is unchanged"
