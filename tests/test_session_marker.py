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

import os
import time
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

    @staticmethod
    def _aged(path: Path, age_s: float) -> None:
        mtime = time.time() - age_s
        os.utime(path, (mtime, mtime))

    def test_deletes_every_old_cleared_record_and_nothing_else(self, tmp_path: Path) -> None:
        watch = tmp_path / "tuple-watch"
        watch.mkdir()
        for name in ("cleared.s2", "cleared.s3", "session.4242", "i12.pending.json"):
            (watch / name).write_text("x\n", encoding="utf-8")
            self._aged(watch / name, 2 * 3600)
        assert remove_legacy_cleared_records(tmp_path) == 2
        assert sorted(p.name for p in watch.iterdir()) == ["i12.pending.json", "session.4242"]

    def test_a_record_younger_than_an_hour_stays_for_a_live_older_session(self, tmp_path: Path) -> None:
        """An 8.5.x session between ``/clear`` and its next prompt still reads the record it just wrote."""
        watch = tmp_path / "tuple-watch"
        watch.mkdir()
        young, old = watch / "cleared.young", watch / "cleared.old"
        for path in (young, old):
            path.write_text("x\n", encoding="utf-8")
        self._aged(young, 600)
        self._aged(old, 3 * 3600)
        assert remove_legacy_cleared_records(tmp_path) == 1
        assert young.exists() and not old.exists()

    def test_the_age_bound_is_one_hour(self, tmp_path: Path) -> None:
        watch = tmp_path / "tuple-watch"
        watch.mkdir()
        record = watch / "cleared.edge"
        record.write_text("x\n", encoding="utf-8")
        now = 1_000_000.0
        os.utime(record, (now - 3599, now - 3599))
        assert remove_legacy_cleared_records(tmp_path, now=now) == 0
        os.utime(record, (now - 3601, now - 3601))
        assert remove_legacy_cleared_records(tmp_path, now=now) == 1

    def test_no_directory_is_nothing_to_do(self, tmp_path: Path) -> None:
        assert remove_legacy_cleared_records(tmp_path) == 0
