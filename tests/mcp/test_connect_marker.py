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

from nexus.mcp.connect_marker import (
    clear_mcp_connect_marker,
    publish_mcp_connect_marker,
    read_mcp_connect_marker,
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
