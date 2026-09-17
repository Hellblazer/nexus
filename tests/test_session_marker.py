# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for `nexus.session_marker` (RDR-211 nexus-rplay.24): the
tuple-watch session-marker contract, rehomed out of `nexus.tuple_watch`
ahead of that module's deletion.

Pure unit tests, no engine substrate: every function here is a flat-file
read/write against a `tmp_path`. The on-disk path SHAPE is pinned twice --
once directly (the literal `tuple-watch/session.<pid>` /
`tuple-watch/cleared.<session_id>` shapes) and once against
`conexus/hooks/scripts/mailbox_drain.py`'s own hardcoded copies of those
same literals, since that plugin script cannot import this package and
must keep matching it by construction.
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

_MAILBOX_DRAIN_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "conexus" / "hooks" / "scripts" / "mailbox_drain.py"
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


class TestPathsMatchTheMailboxDrainHookLiterals:
    """`conexus/hooks/scripts/mailbox_drain.py` cannot import `nexus`, so it
    carries its own literal copies of the directory name and the
    `session.<pid>` / `cleared.<session_id>` file-name shapes. Moving this
    contract to a new module must never move those strings out from under
    that script.
    """

    def test_the_script_still_exists_and_carries_the_literals(self) -> None:
        text = _MAILBOX_DRAIN_SCRIPT.read_text(encoding="utf-8")
        assert '"tuple-watch"' in text
        assert '"session."' in text
        assert 'f"cleared.{session_id}"' in text

    def test_session_marker_path_matches_the_scripts_own_naming(
        self, tmp_path: Path,
    ) -> None:
        # Reproduces the script's own `_session_marker_path`-equivalent
        # construction (it globs `_SESSION_MARKER_PREFIX + "*"` under
        # `<config>/tuple-watch/`) directly against this module's output.
        path = session_marker_path(tmp_path, 4242)
        assert path.parent == tmp_path / "tuple-watch"
        assert path.name == "session.4242"

    def test_cleared_record_path_matches_the_scripts_own_naming(
        self, tmp_path: Path,
    ) -> None:
        path = cleared_record_path(tmp_path, "sid-with-dashes.and.dots")
        assert path.parent == tmp_path / "tuple-watch"
        assert path.name == "cleared.sid-with-dashes.and.dots"
