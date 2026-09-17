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
import uuid
from typing import Any

import pytest

from nexus.db.limits import MAX_QUERY_RESULTS
from nexus.session_marker import write_session_marker
from nexus.tuple_directory import (
    DirectoryResolutionError,
    classify_directory_holders,
    list_directory_entries,
    resolve_default_from,
    resolve_send_address,
    validate_from_address,
)


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


def _paged_row(session_id: str, row_id: str) -> Any:
    """A `list_directory_entries` paging test needs `.created_at` too (the
    cursor `since=(last.created_at, last.id)` is built from it) -- `_row`
    above omits it since the classifier alone never reads it."""
    return types.SimpleNamespace(
        id=row_id, dims={"session_id": session_id}, created_at=f"created-{row_id}",
    )


class _PagedDirectoryStore:
    """A fake `HttpTupleStore.rd()` returning `MAX_QUERY_RESULTS`-sized
    pages until a short final page, with a distinct holder ONLY on that
    final page (test validation gap 1, T2 `nexus_rdr/208-p2-test-
    validation-galkv16-2026-09-14`).

    Cursor-driven, not call-count-driven: a fresh call with `since=None`
    always restarts at page 0, matching the real engine's own stateless
    `rd` contract, so one store instance safely backs more than one
    `list_directory_entries`/`resolve_send_address` call across a test
    module (unlike a running call counter, which would treat a SECOND
    top-level call as a continuation of the first and run off the end of
    `pages`).

    What turns this test red: `list_directory_entries` (`src/nexus/
    tuple_directory.py:87`) stopping after its first `tuples.rd(...)`
    call -- its `if len(page) < MAX_QUERY_RESULTS: break` continuation
    replaced by an unconditional break after page 0, or the whole
    `while True` loop replaced by one bare, unpaged call. Either edit
    means page 3's `session-B` holder is never read, and this module's
    two-holder refusal (`test_a_holder_appearing_only_on_the_last_page_
    is_counted`) silently becomes a false single-holder resolution
    instead of raising.
    """

    def __init__(self, name: str) -> None:
        self.pages: list[list[Any]] = [
            [_paged_row("session-A", f"p0-{i}") for i in range(MAX_QUERY_RESULTS)],
            [_paged_row("session-A", f"p1-{i}") for i in range(MAX_QUERY_RESULTS)],
            [_paged_row("session-B", "p2-0")],  # the short page; the only session-B row
        ]
        self.calls: list[tuple[str, str] | None] = []

    def rd(self, subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0):
        self.calls.append(since)
        if since is None:
            return self.pages[0]
        cursors = {
            (page[-1].created_at, page[-1].id): i
            for i, page in enumerate(self.pages[:-1])
        }
        idx = cursors.get(since)
        if idx is None:
            raise AssertionError(f"unexpected since cursor {since!r} for a fresh page request")
        return self.pages[idx + 1]


class TestListDirectoryEntriesPaging:
    def test_pages_until_a_short_page_in_cursor_order(self) -> None:
        name = "some-name"
        store = _PagedDirectoryStore(name)

        entries = list_directory_entries(name, store)

        assert len(entries) == 2 * MAX_QUERY_RESULTS + 1
        # Stops on the short (3rd) page -- no 4th call was ever made.
        assert len(store.calls) == 3
        assert store.calls[0] is None
        assert store.calls[1] == (store.pages[0][-1].created_at, store.pages[0][-1].id)
        assert store.calls[2] == (store.pages[1][-1].created_at, store.pages[1][-1].id)

    def test_a_holder_appearing_only_on_the_last_page_is_counted(self) -> None:
        name = "some-other-name"
        store = _PagedDirectoryStore(name)

        with pytest.raises(DirectoryResolutionError) as exc_info:
            resolve_send_address(name, store)
        msg = str(exc_info.value)
        assert "session-A" in msg
        assert "session-B" in msg


class TestValidateFromAddress:
    def test_session_id_shape_is_accepted(self) -> None:
        sid = str(uuid.uuid4())
        assert validate_from_address(sid) == sid

    def test_agent_id_shape_is_accepted(self) -> None:
        agent_id = "a" + "0" * 16
        assert validate_from_address(agent_id) == agent_id

    def test_neither_shape_is_refused(self) -> None:
        with pytest.raises(DirectoryResolutionError, match="neither a session id nor an agent id"):
            validate_from_address("not-a-valid-shape")


class TestResolveDefaultFrom:
    """`resolve_default_from`'s own unit coverage (RDR-211 nexus-rplay.24):
    previously exercised only indirectly, through `mailbox_send`'s
    engine-substrate tests in `tests/test_mcp_tuple_tools.py`. Pins the
    reader through the REHOMED contract (`nexus.session_marker`) directly,
    with no engine and no process-table read: *state_dir*/*claude_pid* are
    both overridable for exactly this.
    """

    def test_resolves_through_the_rehomed_marker_reader(self, tmp_path) -> None:
        write_session_marker(tmp_path, 4242, "marker-session-id")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "marker-session-id"

    def test_falls_back_to_env_when_no_marker_present(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", "env-session-id")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "env-session-id"

    def test_marker_wins_over_a_stale_env_var(self, tmp_path, monkeypatch) -> None:
        write_session_marker(tmp_path, 4242, "marker-session-id")
        monkeypatch.setenv("NX_T1_SESSION_ID", "stale-env-session-id")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "marker-session-id"

    def test_refuses_when_neither_marker_nor_env_is_present(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        with pytest.raises(DirectoryResolutionError, match="unresolvable"):
            resolve_default_from(state_dir=tmp_path, claude_pid=4242)
