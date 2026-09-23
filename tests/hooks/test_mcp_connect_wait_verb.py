# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.hooks.mcp_connect_wait`` -- the interactive MCP
connection barrier (RDR-215, bead nexus-veh77).

Round 2 (Sam's review, same day): the readiness signal moved from the T1
lease to ``nexus.mcp.connect_marker``, published unconditionally regardless
of T1 mint outcome, so a T1 outage never costs this barrier more than the
actual connect time. See ``tests/db/test_t1_cli_dedicated_session.py::
TestMintErrorWrapping::test_branch0_mint_failure_still_publishes_the_connect_marker``
for the end-to-end proof against the real ``_t1_lifespan`` deferred-mint
branch; the cases here are this module's own unit-level coverage.

Two layers, per the bead's own proof requirement:

1. ``wait_for_mcp_connect_marker`` -- the polling primitive -- against REAL
   files via ``nexus.mcp.connect_marker.publish_mcp_connect_marker``/
   ``read_mcp_connect_marker`` (no fakes for the signal itself: it is pure
   filesystem, so there is no reason to mock it). Covers: ready
   immediately, ready after N polls, never ready (times out), a marker for
   a DIFFERENT session id is never mistaken for readiness, and the
   explicit "T1 down, marker up" case that is this round's whole point.
2. ``run(payload)`` -- the verb's own wiring -- proving the matcher-source
   gate (only ``startup`` waits), the missing-session-id no-op, and that a
   timeout still returns a silent, exit-0-shaped ``HookResult`` (fail-open
   is a property of the RETURN VALUE here, since ``nexus._hook_runtime.
   entry.main`` forces exit 0 for every non-ledger verb regardless of what
   ``run`` returns -- ``mcp-connect-wait`` is not in ``LEDGER_VERBS``).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from nexus._hook_runtime import entry
from nexus.db.t1 import read_t1_session_lease
from nexus.hooks.mcp_connect_wait import run, wait_for_mcp_connect_marker
from nexus.mcp.connect_marker import publish_mcp_connect_marker

# -- wait_for_mcp_connect_marker: the polling primitive, against real files -


class TestWaitForMcpConnectMarker:
    def test_ready_immediately(self, tmp_path: Path) -> None:
        publish_mcp_connect_marker("sess-A", tmp_path, ttl_seconds=3600)
        ready, elapsed = wait_for_mcp_connect_marker(
            "sess-A", tmp_path, bound_seconds=1.0, poll_interval_seconds=0.05,
        )
        assert ready is True
        assert elapsed < 0.05  # first read hits it, no sleep needed

    def test_ready_after_n_polls(self, tmp_path: Path) -> None:
        # Deterministic and fast: a fake clock/sleep pair that advances the
        # clock by the poll interval on every sleep call, and publishes the
        # real marker file after the third poll -- so the loop's own
        # read_ready call (the real, unmocked function) is what discovers it.
        calls = {"n": 0}
        clock = {"t": 0.0}

        def fake_monotonic() -> float:
            return clock["t"]

        def fake_sleep(interval: float) -> None:
            calls["n"] += 1
            clock["t"] += interval
            if calls["n"] == 3:
                publish_mcp_connect_marker("sess-B", tmp_path, ttl_seconds=3600)

        ready, elapsed = wait_for_mcp_connect_marker(
            "sess-B", tmp_path, bound_seconds=5.0, poll_interval_seconds=0.2,
            sleep=fake_sleep, monotonic=fake_monotonic,
        )
        assert ready is True
        assert calls["n"] == 3
        assert elapsed == pytest.approx(0.6, abs=1e-9)

    def test_never_ready_times_out(self, tmp_path: Path) -> None:
        ready, elapsed = wait_for_mcp_connect_marker(
            "sess-C", tmp_path, bound_seconds=0.3, poll_interval_seconds=0.05,
        )
        assert ready is False
        assert elapsed >= 0.3

    def test_wrong_session_id_is_never_read_as_ready(self, tmp_path: Path) -> None:
        """A marker published for a DIFFERENT session id must not satisfy the wait.

        Real ``read_mcp_connect_marker`` keys the lookup on the exact
        session id (the marker file path names it), so this is really a
        proof that the wiring passes the right id through -- a wrong id
        here would silently pass the barrier for a session that never
        connected."""
        publish_mcp_connect_marker("sess-OTHER", tmp_path, ttl_seconds=3600)
        ready, elapsed = wait_for_mcp_connect_marker(
            "sess-MINE", tmp_path, bound_seconds=0.3, poll_interval_seconds=0.05,
        )
        assert ready is False
        assert elapsed >= 0.3

    def test_expired_marker_reads_as_not_ready(self, tmp_path: Path) -> None:
        """A marker past its own expiry is ABSENT per read_mcp_connect_marker's
        own freshness contract -- proven here because this verb's fail-open
        posture depends on that, not on this module re-implementing it."""
        publish_mcp_connect_marker("sess-D", tmp_path, ttl_seconds=-10.0)
        ready, elapsed = wait_for_mcp_connect_marker(
            "sess-D", tmp_path, bound_seconds=0.2, poll_interval_seconds=0.05,
        )
        assert ready is False
        assert elapsed >= 0.2

    def test_t1_down_marker_up_resolves_well_under_the_bound(
        self, tmp_path: Path,
    ) -> None:
        """This round's whole point, at the unit level (the end-to-end
        proof against the real _t1_lifespan deferred-mint branch lives in
        tests/db/test_t1_cli_dedicated_session.py). No T1 lease exists
        anywhere in tmp_path -- simulating T1 genuinely down -- yet the
        connect marker alone is enough to resolve the wait almost
        immediately, not at the 15s-scale bound a T1-lease-keyed signal
        would have required."""
        publish_mcp_connect_marker("sess-E", tmp_path, ttl_seconds=3600)
        assert read_t1_session_lease("sess-E", tmp_path) is None  # T1 is down
        ready, elapsed = wait_for_mcp_connect_marker(
            "sess-E", tmp_path, bound_seconds=15.0, poll_interval_seconds=0.05,
        )
        assert ready is True
        assert elapsed < 0.1


# -- run(): the verb's own wiring --------------------------------------------


class TestRunMatcherSourceGate:
    def test_non_startup_source_is_a_fast_noop(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for source in ("resume", "clear", "compact", "fork", "", None):
            result = run({"session_id": "sess-F", "source": source})
            assert result.stdout is None
            assert result.exit_code == 0
        # Nothing was ever published, and the fast no-op path never reads
        # nexus.config or nexus.mcp.connect_marker -- the config dir stays empty.
        assert list(tmp_path.iterdir()) == []

    def test_missing_session_id_is_a_fast_noop(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for payload in ({"source": "startup"}, {"source": "startup", "session_id": ""},
                        {"source": "startup", "session_id": 12345}, None, {}):
            result = run(payload)
            assert result.stdout is None
            assert result.exit_code == 0
        assert list(tmp_path.iterdir()) == []


class TestRunWaitsOnStartup:
    def test_marker_already_published_returns_immediately(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("_NX_HOOK_TEST_MCP_CONNECT_WAIT_BOUND_S", "2.0")
        monkeypatch.setenv("_NX_HOOK_TEST_MCP_CONNECT_WAIT_POLL_S", "0.05")
        publish_mcp_connect_marker("sess-G", tmp_path, ttl_seconds=3600)
        result = run({"session_id": "sess-G", "source": "startup"})
        assert result.stdout is None
        assert result.exit_code == 0
        assert result.crashed is False

    def test_never_ready_still_returns_a_silent_exit_zero_result(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Fail-open: a timeout produces exactly the same HookResult shape
        as success. entry.main forces exit 0 for non-ledger verbs regardless,
        so this asserts the property at the level that matters -- run()
        never turns "the barrier gave up" into a stdout envelope or a
        raised exception."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("_NX_HOOK_TEST_MCP_CONNECT_WAIT_BOUND_S", "0.2")
        monkeypatch.setenv("_NX_HOOK_TEST_MCP_CONNECT_WAIT_POLL_S", "0.05")
        result = run({"session_id": "sess-H", "source": "startup"})
        assert result.stdout is None
        assert result.exit_code == 0
        assert result.crashed is False

    def test_bound_and_poll_overrides_are_actually_honoured(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A tiny bound must make run() return promptly rather than waiting
        the real 15 s default -- proves the test-override env vars are wired
        all the way through, not merely present."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("_NX_HOOK_TEST_MCP_CONNECT_WAIT_BOUND_S", "0.15")
        monkeypatch.setenv("_NX_HOOK_TEST_MCP_CONNECT_WAIT_POLL_S", "0.03")
        started = time.monotonic()
        run({"session_id": "sess-I", "source": "startup"})
        assert time.monotonic() - started < 2.0  # nowhere near the real 15 s default


class TestRegisteredInTheRealVerbTable:
    def test_mcp_connect_wait_resolves_to_the_new_module(self) -> None:
        assert entry.VERB_TABLE["mcp-connect-wait"] == "nexus.hooks.mcp_connect_wait"
