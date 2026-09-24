# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.mcp.connect_marker`` -- the MCP connect-readiness
marker (RDR-215, nexus-veh77 round 2).

This is the signal ``nexus.hooks.mcp_connect_wait`` polls instead of the T1
lease, specifically so T1 being down never costs the barrier more than the
actual connect time. Pure filesystem, so every case here is against real
files -- no fakes for the signal itself.
"""
from __future__ import annotations

from pathlib import Path

import os

from nexus.mcp.connect_marker import (
    clear_mcp_connect_marker,
    publish_mcp_connect_marker,
    read_mcp_connect_marker,
    read_mcp_connect_marker_info,
)


class TestPublishAndRead:
    def test_published_marker_reads_ready(self, tmp_path: Path) -> None:
        publish_mcp_connect_marker("sess-A", tmp_path, ttl_seconds=3600)
        assert read_mcp_connect_marker("sess-A", tmp_path) is True

    def test_unpublished_session_id_reads_not_ready(self, tmp_path: Path) -> None:
        assert read_mcp_connect_marker("sess-never-published", tmp_path) is False

    def test_a_different_session_ids_marker_is_never_read_as_ready(
        self, tmp_path: Path,
    ) -> None:
        publish_mcp_connect_marker("sess-OTHER", tmp_path, ttl_seconds=3600)
        assert read_mcp_connect_marker("sess-MINE", tmp_path) is False

    def test_expired_marker_reads_not_ready(self, tmp_path: Path) -> None:
        publish_mcp_connect_marker("sess-B", tmp_path, ttl_seconds=-10.0)
        assert read_mcp_connect_marker("sess-B", tmp_path) is False

    def test_republishing_refreshes_rather_than_erroring(self, tmp_path: Path) -> None:
        publish_mcp_connect_marker("sess-C", tmp_path, ttl_seconds=3600)
        publish_mcp_connect_marker("sess-C", tmp_path, ttl_seconds=3600)
        assert read_mcp_connect_marker("sess-C", tmp_path) is True

    def test_malformed_marker_file_reads_not_ready(self, tmp_path: Path) -> None:
        # Same fail-safe contract as read_t1_session_lease: a corrupt or
        # pre-format file is treated as absent, never trusted.
        path = tmp_path / "mcp_connect_marker.sess-D"
        path.write_text("not json")
        assert read_mcp_connect_marker("sess-D", tmp_path) is False


class TestReadInfo:
    """The TTL-unaware raw read (round 5, nexus.hooks.mcp_connect_check's signal)."""

    def test_published_marker_yields_this_process_pid(self, tmp_path: Path) -> None:
        publish_mcp_connect_marker("sess-G", tmp_path, ttl_seconds=3600)
        info = read_mcp_connect_marker_info("sess-G", tmp_path)
        assert info is not None
        assert info.pid == os.getpid()

    def test_missing_marker_yields_none(self, tmp_path: Path) -> None:
        assert read_mcp_connect_marker_info("sess-never-published", tmp_path) is None

    def test_malformed_marker_yields_none(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp_connect_marker.sess-H"
        path.write_text("not json")
        assert read_mcp_connect_marker_info("sess-H", tmp_path) is None

    def test_expired_marker_STILL_yields_info_unlike_the_bool_read(
        self, tmp_path: Path,
    ) -> None:
        """The whole point of this function: TTL expiry does not hide the
        pid from the mid-session detector, which has its own liveness
        signal (pid_alive) and must not confuse a merely-old marker with a
        dead process."""
        publish_mcp_connect_marker("sess-I", tmp_path, ttl_seconds=-10.0)
        assert read_mcp_connect_marker("sess-I", tmp_path) is False  # the bool read says stale
        info = read_mcp_connect_marker_info("sess-I", tmp_path)
        assert info is not None  # but the raw info is still there
        assert info.pid == os.getpid()


class TestClear:
    def test_clear_removes_a_published_marker(self, tmp_path: Path) -> None:
        publish_mcp_connect_marker("sess-E", tmp_path, ttl_seconds=3600)
        assert read_mcp_connect_marker("sess-E", tmp_path) is True
        clear_mcp_connect_marker("sess-E", tmp_path)
        assert read_mcp_connect_marker("sess-E", tmp_path) is False

    def test_clear_of_a_never_published_marker_is_not_an_error(
        self, tmp_path: Path,
    ) -> None:
        clear_mcp_connect_marker("sess-never-existed", tmp_path)  # must not raise

    def test_clear_is_idempotent(self, tmp_path: Path) -> None:
        publish_mcp_connect_marker("sess-F", tmp_path, ttl_seconds=3600)
        clear_mcp_connect_marker("sess-F", tmp_path)
        clear_mcp_connect_marker("sess-F", tmp_path)  # must not raise
