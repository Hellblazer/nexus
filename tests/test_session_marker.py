# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for `nexus.session_marker` (RDR-211 nexus-rplay.24): the
tuple-watch session-marker contract, rehomed out of the former CLI
mailbox-watch module ahead of its deletion (RDR-211 nexus-rplay.14).

Pure unit tests, no engine substrate: every function here is a flat-file
read/write against a `tmp_path`. The on-disk path SHAPE is pinned directly
(the literal `tuple-watch/session.<pid>` / `tuple-watch/cleared.<session_id>`
shapes). The mailbox drain once carried its own copies of those literals,
pinned here too; since nexus-t9klx it is a wheel module that calls
`cleared_record_path` itself, so there is no second copy to pin.
"""
from __future__ import annotations

from pathlib import Path

from nexus.session_marker import (
    _read_session_marker,
    remove_legacy_cleared_records,
    session_marker_path,
    write_session_marker,
)

class TestPathShapes:
    def test_session_marker_path_shape(self, tmp_path: Path) -> None:
        assert session_marker_path(tmp_path, 4242) == tmp_path / "tuple-watch" / "session.4242"


class TestWriteAndReadRoundTrip:
    def test_write_then_read_returns_the_written_id(self, tmp_path: Path) -> None:
        write_session_marker(tmp_path, 4242, "sess-a")
        assert _read_session_marker(tmp_path, 4242) == "sess-a"

    def test_missing_marker_reads_as_none(self, tmp_path: Path) -> None:
        assert _read_session_marker(tmp_path, 4242) is None

    def test_empty_marker_reads_as_none(self, tmp_path: Path) -> None:
        path = session_marker_path(tmp_path, 4242)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        assert _read_session_marker(tmp_path, 4242) is None

    def test_rewrite_overwrites_the_previous_value(self, tmp_path: Path) -> None:
        write_session_marker(tmp_path, 4242, "sess-a")
        write_session_marker(tmp_path, 4242, "sess-b")
        assert _read_session_marker(tmp_path, 4242) == "sess-b"


class TestRemoveLegacyClearedRecords:
    """RDR-253 Phase 2: nothing writes or reads a ``cleared.<session id>`` record any more (a session id is not a mail
    address). An older client may have left some; SessionStart deletes them, since the drain's 7-day prune is gone."""

    def test_deletes_every_cleared_record_and_nothing_else(self, tmp_path: Path) -> None:
        watch = tmp_path / "tuple-watch"
        watch.mkdir()
        for name in ("cleared.s2", "cleared.s3", "session.4242", "i12.pending.json"):
            (watch / name).write_text("x\n", encoding="utf-8")
        assert remove_legacy_cleared_records(tmp_path) == 2
        assert sorted(p.name for p in watch.iterdir()) == ["i12.pending.json", "session.4242"]

    def test_no_directory_is_nothing_to_do(self, tmp_path: Path) -> None:
        assert remove_legacy_cleared_records(tmp_path) == 0
