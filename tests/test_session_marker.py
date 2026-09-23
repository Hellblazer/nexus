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
    cleared_record_path,
    record_clear_and_write_session_marker,
    session_marker_path,
    write_session_marker,
)

class TestPathShapes:
    def test_session_marker_path_shape(self, tmp_path: Path) -> None:
        assert session_marker_path(tmp_path, 4242) == tmp_path / "tuple-watch" / "session.4242"

    def test_cleared_record_path_shape(self, tmp_path: Path) -> None:
        assert cleared_record_path(tmp_path, "sid") == tmp_path / "tuple-watch" / "cleared.sid"


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


class TestRecordClearAndWriteSessionMarker:
    def test_record_clear_true_with_a_prior_marker_writes_the_record(
        self, tmp_path: Path,
    ) -> None:
        write_session_marker(tmp_path, 4242, "old-sess")
        record_clear_and_write_session_marker(
            tmp_path, 4242, "new-sess", record_clear=True,
        )
        assert _read_session_marker(tmp_path, 4242) == "new-sess"
        record = cleared_record_path(tmp_path, "new-sess")
        assert record.read_text(encoding="utf-8").splitlines() == ["old-sess"]

    def test_record_clear_false_writes_no_record(self, tmp_path: Path) -> None:
        write_session_marker(tmp_path, 4242, "old-sess")
        record_clear_and_write_session_marker(
            tmp_path, 4242, "new-sess", record_clear=False,
        )
        assert _read_session_marker(tmp_path, 4242) == "new-sess"
        assert not cleared_record_path(tmp_path, "new-sess").exists()

    def test_no_prior_marker_writes_no_record(self, tmp_path: Path) -> None:
        record_clear_and_write_session_marker(
            tmp_path, 4242, "new-sess", record_clear=True,
        )
        assert not cleared_record_path(tmp_path, "new-sess").exists()

    def test_same_id_writes_no_record(self, tmp_path: Path) -> None:
        write_session_marker(tmp_path, 4242, "same-sess")
        record_clear_and_write_session_marker(
            tmp_path, 4242, "same-sess", record_clear=True,
        )
        assert not cleared_record_path(tmp_path, "same-sess").exists()
