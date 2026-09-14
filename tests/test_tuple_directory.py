# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for `nexus.tuple_directory`'s pure classifier (RDR-208 Phase 2
fix round, code review T2 `nexus/rdr-208-phase2b-cre-2026-09-14` item 1 /
critic T2 `nexus/rdr-208-phase2b-critic-2026-09-14`).

`list_directory_entries` does the one paged I/O read against the engine;
`classify_directory_holders` is a PURE function over an already-fetched
entries list, so `resolve_send_address` (`mailbox_send`) and `nx tuple
directory NAME` can each read once and classify from that same list,
instead of reading independently and risking a lapse or a re-nonce between
two reads making the two callers describe different moments. These tests
exercise the classifier directly, against plain fake rows -- no engine
substrate, no I/O.
"""
from __future__ import annotations

import types
from typing import Any

import pytest

from nexus.tuple_directory import DirectoryResolutionError, classify_directory_holders


def _row(session_id: str, *, id_: str = "id") -> Any:
    """A minimal stand-in for a `TupleRow` -- `classify_directory_holders`
    only reads `.dims`."""
    return types.SimpleNamespace(id=id_, dims={"session_id": session_id})


class TestClassifyDirectoryHolders:
    def test_no_rows_refuses_naming_the_name(self) -> None:
        with pytest.raises(DirectoryResolutionError, match="no live holder"):
            classify_directory_holders("some-name", [])

    def test_one_session_resolves(self) -> None:
        result = classify_directory_holders("some-name", [_row("sid-1")])
        assert result == ("sid-1", "session")

    def test_one_session_two_nonces_is_one_holder_not_a_conflict(self) -> None:
        result = classify_directory_holders(
            "some-name", [_row("sid-1", id_="a"), _row("sid-1", id_="b")],
        )
        assert result == ("sid-1", "session")

    def test_two_sessions_refuses_naming_both(self) -> None:
        with pytest.raises(DirectoryResolutionError) as exc_info:
            classify_directory_holders("some-name", [_row("sid-1"), _row("sid-2")])
        msg = str(exc_info.value)
        assert "sid-1" in msg
        assert "sid-2" in msg

    def test_a_row_with_no_session_id_dim_is_never_counted_as_a_holder(self) -> None:
        """A malformed/dimless row must not silently count as a holder --
        the classifier only trusts a present, non-empty session_id dim."""
        blank = types.SimpleNamespace(id="x", dims={})
        with pytest.raises(DirectoryResolutionError, match="no live holder"):
            classify_directory_holders("some-name", [blank])
