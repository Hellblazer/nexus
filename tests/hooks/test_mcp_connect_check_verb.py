# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.hooks.mcp_connect_check`` -- the mid-session `nx-mcp`
disconnect detector (RDR-215, bead nexus-veh77 round 5).

Two layers, matching the bead's own proof requirement exactly:

1. ``_decide`` -- the pure warn-once-per-episode state machine -- against
   synthetic states, covering all four named cases: live marker means
   silent; dead pid means one warning then silent; missing marker after a
   prior one means a warning; never-connected means silent.
2. ``run(payload)`` -- the verb's own wiring -- against REAL files (the
   connect marker via ``nexus.mcp.connect_marker.publish_mcp_connect_marker``,
   this OS's own live pid via ``os.getpid()`` for "alive", and an
   unallocated pid for "dead") plus the real
   ``nexus.daemon.service_registry.pid_alive``, never mocked.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from nexus._hook_runtime import entry
from nexus.daemon.service_registry import pid_alive
from nexus.hooks.mcp_connect_check import (
    _DISCONNECT_MESSAGE,
    _State,
    _decide,
    _read_state,
    _state_path,
    _write_state,
    run,
)
from nexus.mcp.connect_marker import publish_mcp_connect_marker

#: A pid essentially guaranteed to name no live process, for the "dead
#: pid" cases below. Reused from the same style tests elsewhere in this
#: suite use for an unallocated pid (very high, above any realistic
#: allocation on the platforms this runs on).
_DEAD_PID = 999_999_999


class TestDecidePureStateMachine:
    def test_live_marker_means_silent(self) -> None:
        message, new_state = _decide(currently_connected=True, state=_State())
        assert message is None
        assert new_state == _State(ever_connected=True, warned_since_last_connected=False)

    def test_never_connected_means_silent_even_if_currently_disconnected(self) -> None:
        message, new_state = _decide(
            currently_connected=False, state=_State(ever_connected=False),
        )
        assert message is None
        assert new_state == _State(ever_connected=False, warned_since_last_connected=False)

    def test_dead_pid_after_a_prior_connection_warns_once(self) -> None:
        message, new_state = _decide(
            currently_connected=False, state=_State(ever_connected=True),
        )
        assert message == _DISCONNECT_MESSAGE
        assert new_state == _State(ever_connected=True, warned_since_last_connected=True)

    def test_dead_pid_already_warned_this_episode_stays_silent(self) -> None:
        message, new_state = _decide(
            currently_connected=False,
            state=_State(ever_connected=True, warned_since_last_connected=True),
        )
        assert message is None
        assert new_state == _State(ever_connected=True, warned_since_last_connected=True)

    def test_reconnecting_resets_the_episode_flag(self) -> None:
        """A session that was warned, then reconnected, must warn again on
        a LATER disconnect -- the episode flag resets on any live sighting."""
        message, new_state = _decide(
            currently_connected=True,
            state=_State(ever_connected=True, warned_since_last_connected=True),
        )
        assert message is None
        assert new_state.warned_since_last_connected is False


class TestStateFileRoundTrip:
    def test_missing_state_file_reads_as_zero_state(self, tmp_path: Path) -> None:
        assert _read_state(_state_path("sess-A", tmp_path)) == _State()

    def test_malformed_state_file_reads_as_zero_state(self, tmp_path: Path) -> None:
        path = _state_path("sess-B", tmp_path)
        path.write_text("not json")
        assert _read_state(path) == _State()

    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        path = _state_path("sess-C", tmp_path)
        state = _State(ever_connected=True, warned_since_last_connected=True)
        _write_state(path, state)
        assert _read_state(path) == state


class TestRunWiringAgainstRealFiles:
    def test_never_connected_is_silent_and_writes_no_state(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        result = run({"session_id": "sess-D"})
        assert result.stdout is None
        assert not _state_path("sess-D", tmp_path).exists()

    def test_live_marker_is_silent(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        publish_mcp_connect_marker("sess-E", tmp_path, ttl_seconds=3600)
        # This test process's own pid is definitionally alive.
        result = run({"session_id": "sess-E"})
        assert result.stdout is None
        state = _read_state(_state_path("sess-E", tmp_path))
        assert state.ever_connected is True
        assert state.warned_since_last_connected is False

    def test_dead_pid_after_a_prior_live_marker_warns_once_then_silent(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        # First: a live marker, so ever_connected becomes True.
        publish_mcp_connect_marker("sess-F", tmp_path, ttl_seconds=3600)
        first = run({"session_id": "sess-F"})
        assert first.stdout is None

        # Now the process dies: overwrite the marker to name a dead pid,
        # simulating a crashed nx-mcp that never got to clear its own
        # marker (a clean shutdown would have removed the file entirely;
        # this exercises the "marker present, pid dead" branch).
        marker_path = tmp_path / "mcp_connect_marker.sess-F"
        payload = json.dumps(
            {"pid": _DEAD_PID, "published_at": time.time(), "expires_at": time.time() + 3600}
        )
        marker_path.write_text(payload)

        second = run({"session_id": "sess-F"})
        assert second.stdout == _DISCONNECT_MESSAGE

        third = run({"session_id": "sess-F"})
        assert third.stdout is None  # already warned this episode

    def test_missing_marker_after_a_prior_live_one_warns(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """A clean-ish shutdown that DID clear the marker (rather than
        leaving a dead pid behind) must still be caught: 'missing' and
        'dead pid' are the same episode from this detector's point of
        view."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        publish_mcp_connect_marker("sess-G", tmp_path, ttl_seconds=3600)
        first = run({"session_id": "sess-G"})
        assert first.stdout is None

        (tmp_path / "mcp_connect_marker.sess-G").unlink()

        second = run({"session_id": "sess-G"})
        assert second.stdout == _DISCONNECT_MESSAGE

        third = run({"session_id": "sess-G"})
        assert third.stdout is None

    def test_reconnect_after_a_warned_episode_re_arms_the_warning(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        publish_mcp_connect_marker("sess-H", tmp_path, ttl_seconds=3600)
        run({"session_id": "sess-H"})  # ever_connected=True

        (tmp_path / "mcp_connect_marker.sess-H").unlink()
        warned = run({"session_id": "sess-H"})
        assert warned.stdout == _DISCONNECT_MESSAGE

        # Reconnect: republish under this process's own (live) pid.
        publish_mcp_connect_marker("sess-H", tmp_path, ttl_seconds=3600)
        silent_again = run({"session_id": "sess-H"})
        assert silent_again.stdout is None

        # A SECOND disconnect must warn again, not stay silent forever.
        (tmp_path / "mcp_connect_marker.sess-H").unlink()
        warned_again = run({"session_id": "sess-H"})
        assert warned_again.stdout == _DISCONNECT_MESSAGE

    def test_missing_session_id_is_a_fast_noop(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for payload in ({}, None, {"session_id": ""}, {"session_id": 12345}):
            result = run(payload)
            assert result.stdout is None
        assert list(tmp_path.iterdir()) == []

    def test_this_process_pid_is_alive_sanity(self) -> None:
        """Confirms the test's own premise: os.getpid() is a genuinely
        live pid for the 'connected' cases above to rely on."""
        assert pid_alive(os.getpid()) is True

    def test_unallocated_pid_is_dead_sanity(self) -> None:
        assert pid_alive(_DEAD_PID) is False


class TestRegisteredInTheRealVerbTable:
    def test_mcp_connect_check_resolves_to_the_new_module(self) -> None:
        assert entry.VERB_TABLE["mcp-connect-check"] == "nexus.hooks.mcp_connect_check"
