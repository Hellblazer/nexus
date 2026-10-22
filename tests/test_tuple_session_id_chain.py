# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The tuple-space tools' session-id chain and its one shared refusal (nexus-zl1jv).

A host with no Claude Code session (the desktop/cowork app) used to meet two
variable names for one missing thing: T1 resolves ``NX_SESSION_ID`` while the
mailbox and subscribe tools read the tuple-watch session marker and
``NX_T1_SESSION_ID`` only. The chain is now marker, ``NX_T1_SESSION_ID``,
``NX_SESSION_ID`` (``tuple_subscribe`` and its siblings skip the marker, by
design: their subscription set is keyed on the T1 env var). Every refusal
carries ONE message, ``NO_SESSION_ID_MESSAGE``; the tests below pin the text
and the sites so they cannot drift apart again.

No engine is needed: every refusal fires before any service call.
"""
from __future__ import annotations

import pytest

import nexus.session as session_mod
from nexus.mcp import core as mcp_core
from nexus.session_marker import write_session_marker
from nexus.tuple_directory import (
    NO_SESSION_ID_MESSAGE,
    DirectoryResolutionError,
    resolve_default_from,
    resolve_env_session_id,
)

_PINNED_MESSAGE = (
    "no resolvable session id: this is not a Claude Code session and "
    "NX_T1_SESSION_ID and NX_SESSION_ID are unset; run inside Claude Code "
    "or set NX_SESSION_ID"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
    monkeypatch.delenv("NX_SESSION_ID", raising=False)


class TestEnvChain:
    def test_nx_session_id_resolves_when_nx_t1_session_id_is_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        assert resolve_env_session_id() == "desktop-session"

    def test_nx_t1_session_id_beats_nx_session_id(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", "t1-session")
        monkeypatch.setenv("NX_SESSION_ID", "outer-session")
        assert resolve_env_session_id() == "t1-session"

    def test_blank_values_count_as_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", "   ")
        monkeypatch.setenv("NX_SESSION_ID", " desktop ")
        assert resolve_env_session_id() == "desktop"

    def test_nothing_set_resolves_empty(self) -> None:
        assert resolve_env_session_id() == ""


class TestMailboxSender:
    def test_nx_session_id_is_the_sender_with_no_marker_and_no_t1_id(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "desktop-session"

    def test_marker_beats_both_variables(self, tmp_path, monkeypatch) -> None:
        write_session_marker(tmp_path, 4242, "marker-session")
        monkeypatch.setenv("NX_T1_SESSION_ID", "t1-session")
        monkeypatch.setenv("NX_SESSION_ID", "outer-session")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "marker-session"

    def test_nx_t1_session_id_beats_nx_session_id(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", "t1-session")
        monkeypatch.setenv("NX_SESSION_ID", "outer-session")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "t1-session"

    def test_a_nested_claude_with_its_own_marker_never_takes_the_inherited_id(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Identity safety: operator dispatch forwards the parent's
        NX_SESSION_ID to a nested `claude -p`. That child's SessionStart
        writes a marker for ITS claude pid, which wins, so it does not
        deliver as its parent."""
        write_session_marker(tmp_path, 5151, "child-session")
        monkeypatch.setenv("NX_SESSION_ID", "parent-session")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=5151) == "child-session"

    def test_refusal_is_the_shared_message(self, tmp_path) -> None:
        with pytest.raises(DirectoryResolutionError) as exc:
            resolve_default_from(state_dir=tmp_path, claude_pid=4242)
        assert str(exc.value) == NO_SESSION_ID_MESSAGE

    def test_mailbox_send_returns_the_shared_message(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 0)
        result = mcp_core.mailbox_send("a0123456789abcdef", body="x")
        assert result == {"error": NO_SESSION_ID_MESSAGE}


class TestSubscribeResolution:
    def test_nx_session_id_resolves_for_subscribe(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        assert mcp_core._current_subscription_session_id() == "desktop-session"

    def test_nx_t1_session_id_beats_nx_session_id_for_subscribe(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", "t1-session")
        monkeypatch.setenv("NX_SESSION_ID", "outer-session")
        assert mcp_core._current_subscription_session_id() == "t1-session"

    def test_no_session_id_resolves_empty(self) -> None:
        assert mcp_core._current_subscription_session_id() == ""

    def test_all_three_refusals_carry_the_shared_message(self) -> None:
        assert NO_SESSION_ID_MESSAGE in mcp_core.tuple_subscribe("board/x")
        assert NO_SESSION_ID_MESSAGE in mcp_core.tuple_unsubscribe("board/x")
        [row] = mcp_core.tuple_subscriptions()
        assert row["error"] == NO_SESSION_ID_MESSAGE


class TestSharedMessage:
    def test_text_is_pinned_and_names_the_whole_chain(self) -> None:
        assert NO_SESSION_ID_MESSAGE == _PINNED_MESSAGE
        # The marker is not named: tuple_subscribe resolves from the env vars only (it keys its set on T1's
        # id), so a message that claimed "no marker" would be untrue on that path. "Not a Claude Code
        # session" is true on both: the marker exists only inside Claude Code.
        for name in ("Claude Code", "NX_T1_SESSION_ID", "NX_SESSION_ID"):
            assert name in NO_SESSION_ID_MESSAGE
        assert "marker" not in NO_SESSION_ID_MESSAGE

    def test_no_refusal_site_spells_its_own_message(self) -> None:
        """Every refusal imports the constant; a literal copy is how the two
        wordings drifted in the first place."""
        import inspect

        import nexus.tuple_directory as td

        assert "NX_T1_SESSION_ID is unset" not in inspect.getsource(mcp_core)
        assert inspect.getsource(td).count("no resolvable session id:") == 1  # the constant's own definition
