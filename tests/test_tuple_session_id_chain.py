# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The tuple-space tools' session-id chain and its shared refusals (nexus-zl1jv).

A host with no Claude Code session (a self-configured MCP server entry) used
to meet two variable names for one missing thing: T1 resolves
``NX_SESSION_ID`` while the mailbox and subscribe tools read the tuple-watch
session marker and ``NX_T1_SESSION_ID`` only. The chain is now marker,
``NX_T1_SESSION_ID``, ``NX_SESSION_ID`` (``tuple_subscribe`` and its
siblings skip the marker, by design: their subscription set is keyed on the
T1 env var). ``NX_SESSION_ID`` must be a UUID, because a peer reads any other
sender shape as a NAME and cannot reply. Every refusal comes from
``no_session_id_message()``, which branches on whether the process was
started by Claude Code; the tests below pin both texts and the sites.

No engine is needed: every refusal fires before any service call.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import uuid

import pytest

import nexus.session as session_mod
from nexus.mcp import core as mcp_core
from nexus.session_marker import write_session_marker
from nexus.tuple_directory import (
    NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE,
    NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE,
    DirectoryResolutionError,
    invalid_env_session_id_message,
    no_session_id_message,
    resolve_default_from,
    resolve_env_session_id,
)

_PINNED_IN_CLAUDE_CODE = (
    "no resolvable session id: T1 is not up yet in this Claude Code "
    "session, so it has published no session id; make any scratch call or "
    "run `nx doctor --check-t1`, then retry"
)
_PINNED_OUTSIDE_CLAUDE_CODE = (
    "no resolvable session id: NX_T1_SESSION_ID and NX_SESSION_ID are "
    "unset; set NX_SESSION_ID to a UUID, unique per server, in a "
    "self-configured MCP server entry. The account-plugin server (Claude "
    "Desktop, Cowork) cannot set it; use memory_put to carry notes there"
)

_DESKTOP = "6f1c1a0e-3b7d-4c52-9d0a-1f2e3d4c5b6a"
_T1 = "0b9f6c3e-52a4-4d7e-8a11-7c9e2f4b6d80"
_OUTER = "c3d2e1f0-9a8b-4c7d-b6a5-0e1f2a3b4c5d"
_MARKER = "5d4c3b2a-1f0e-4d9c-8b7a-6e5f4d3c2b1a"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts outside Claude Code with neither variable set."""
    for name in ("NX_T1_SESSION_ID", "NX_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)


class TestEnvChain:
    def test_nx_session_id_resolves_when_nx_t1_session_id_is_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", _DESKTOP)
        assert resolve_env_session_id() == _DESKTOP

    def test_nx_t1_session_id_beats_nx_session_id(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", _T1)
        monkeypatch.setenv("NX_SESSION_ID", _OUTER)
        assert resolve_env_session_id() == _T1

    def test_blank_values_count_as_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", "   ")
        monkeypatch.setenv("NX_SESSION_ID", f" {_DESKTOP} ")
        assert resolve_env_session_id() == _DESKTOP

    def test_nothing_set_resolves_empty(self) -> None:
        assert resolve_env_session_id() == ""

    def test_a_non_uuid_nx_session_id_is_refused_naming_the_shape(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        with pytest.raises(DirectoryResolutionError) as exc:
            resolve_env_session_id()
        assert str(exc.value) == invalid_env_session_id_message("desktop-session")
        assert "UUID" in str(exc.value)
        assert "NX_SESSION_ID" in str(exc.value)

    def test_a_non_uuid_nx_session_id_behind_a_set_t1_id_is_never_read(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", _T1)
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        assert resolve_env_session_id() == _T1


    def test_a_non_uuid_copied_into_nx_t1_session_id_by_the_mint_is_refused(self, monkeypatch) -> None:
        """The T1 mint copies NX_SESSION_ID verbatim into NX_T1_SESSION_ID, so on a healthy service a
        non-UUID NX_SESSION_ID arrives through the T1 tier; it must be refused there as well."""
        monkeypatch.setenv("NX_T1_SESSION_ID", "desktop-session")
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        with pytest.raises(DirectoryResolutionError) as exc:
            resolve_env_session_id()
        assert str(exc.value) == invalid_env_session_id_message("desktop-session")

class TestMailboxSender:
    def test_nx_session_id_is_the_sender_with_no_marker_and_no_t1_id(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", _DESKTOP)
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == _DESKTOP

    def test_marker_beats_both_variables(self, tmp_path, monkeypatch) -> None:
        write_session_marker(tmp_path, 4242, _MARKER)
        monkeypatch.setenv("NX_T1_SESSION_ID", _T1)
        monkeypatch.setenv("NX_SESSION_ID", _OUTER)
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == _MARKER

    def test_nx_t1_session_id_beats_nx_session_id(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", _T1)
        monkeypatch.setenv("NX_SESSION_ID", _OUTER)
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == _T1

    def test_a_non_uuid_nx_session_id_is_refused_not_stamped_as_from(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        with pytest.raises(DirectoryResolutionError) as exc:
            resolve_default_from(state_dir=tmp_path, claude_pid=4242)
        assert str(exc.value) == invalid_env_session_id_message("desktop-session")

    def test_refusal_is_the_shared_message(self, tmp_path) -> None:
        with pytest.raises(DirectoryResolutionError) as exc:
            resolve_default_from(state_dir=tmp_path, claude_pid=4242)
        assert str(exc.value) == NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE

    def test_mailbox_send_returns_the_shared_message(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 0)
        result = mcp_core.mailbox_send("a0123456789abcdef", body="x")
        assert result == {"error": NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE}

    def test_mailbox_send_refuses_a_non_uuid_nx_session_id_writing_nothing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 0)
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        result = mcp_core.mailbox_send("a0123456789abcdef", body="x")
        assert result == {"error": invalid_env_session_id_message("desktop-session")}


class TestSubscribeResolution:
    def test_nx_session_id_resolves_for_subscribe(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", _DESKTOP)
        assert mcp_core._current_subscription_session_id() == _DESKTOP

    def test_nx_t1_session_id_beats_nx_session_id_for_subscribe(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", _T1)
        monkeypatch.setenv("NX_SESSION_ID", _OUTER)
        assert mcp_core._current_subscription_session_id() == _T1

    def test_no_session_id_resolves_empty(self) -> None:
        assert mcp_core._current_subscription_session_id() == ""

    def test_all_three_refusals_carry_the_shared_message(self) -> None:
        assert NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE in mcp_core.tuple_subscribe("board/x")
        assert NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE in mcp_core.tuple_unsubscribe("board/x")
        [row] = mcp_core.tuple_subscriptions()
        assert row["error"] == NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE

    def test_all_three_refusals_name_the_shape_for_a_non_uuid_nx_session_id(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        expected = invalid_env_session_id_message("desktop-session")
        assert expected in mcp_core.tuple_subscribe("board/x")
        assert expected in mcp_core.tuple_unsubscribe("board/x")
        [row] = mcp_core.tuple_subscriptions()
        assert row["error"] == expected


class TestSharedMessages:
    def test_both_texts_are_pinned(self) -> None:
        assert NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE == _PINNED_IN_CLAUDE_CODE
        assert NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE == _PINNED_OUTSIDE_CLAUDE_CODE

    def test_the_inside_text_is_true_inside_claude_code(self) -> None:
        # Inside Claude Code the session exists: the text must not say it is not one, nor send the
        # reader to set a variable that a Claude Code session does not need.
        assert "not a Claude Code session" not in NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE
        assert "nx doctor --check-t1" in NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE
        assert "scratch call" in NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE

    def test_the_outside_text_names_the_remedy_and_the_account_plugin_limit(self) -> None:
        text = NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE
        for fragment in ("NX_T1_SESSION_ID", "NX_SESSION_ID", "UUID", "self-configured MCP server entry", "memory_put"):
            assert fragment in text
        assert "run inside Claude Code" not in text

    def test_the_text_follows_whether_claude_code_started_the_process(self, monkeypatch) -> None:
        assert no_session_id_message() == NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE
        monkeypatch.setenv("CLAUDECODE", "1")
        assert no_session_id_message() == NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE
        monkeypatch.delenv("CLAUDECODE")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        assert no_session_id_message() == NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE

    def test_refusals_inside_claude_code_carry_the_inside_text(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 0)
        assert mcp_core.mailbox_send("a0123456789abcdef", body="x") == {
            "error": NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE,
        }
        assert NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE in mcp_core.tuple_subscribe("board/x")

    def test_no_refusal_site_spells_its_own_message(self) -> None:
        """A literal copy, of the old text or any new one, is how the wordings drifted in the first place.

        The three subscribe tools must raise exactly ``ValueError(no_session_id_message())`` for the
        empty-id refusal; no string constant in ``core.py`` may carry either text's opening words; and
        ``tuple_directory`` defines each text once.
        """
        import nexus.tuple_directory as td

        tree = ast.parse(inspect.getsource(mcp_core))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # T1's own message ("... session id for this MCP server instance") is a different refusal
                # and stays; the colon form is the tuple tools' prefix.
                assert "no resolvable session id:" not in node.value, node.value
                assert "NX_T1_SESSION_ID is unset" not in node.value, node.value
                assert "T1 is not up yet" not in node.value, node.value

        for name in ("tuple_subscribe", "tuple_unsubscribe", "tuple_subscriptions"):
            fn = next(
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name
            )
            raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
            assert raises, f"{name}: the empty-id refusal is gone"
            for raise_node in raises:
                call = raise_node.exc
                assert isinstance(call, ast.Call) and getattr(call.func, "id", "") == "ValueError", name
                [arg] = call.args
                assert isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "no_session_id_message", (
                    f"{name} spells its own refusal: {ast.unparse(raise_node)}"
                )

        td_source = inspect.getsource(td)
        assert td_source.count('"no resolvable session id: ') == 2  # the two constants' own definitions


class TestChannelWaiterWithOnlyNxSessionId:
    """With only NX_SESSION_ID set and the startup T1 mint deferred, the session id resolves in
    `_start_channel_waiter` while `NX_T1_SESSION_ID` stays unset. The waiter's `_get_t1()` would then
    retry the failed mint on the lifespan thread and block startup, so it must not run."""

    @pytest.fixture
    def _t1_calls(self, monkeypatch) -> list[str]:
        calls: list[str] = []

        def _get_t1_probe():
            calls.append("get_t1")
            raise RuntimeError("T1 scratch is unavailable: the mint retry ran")

        monkeypatch.setattr(mcp_core, "_get_t1", _get_t1_probe)
        monkeypatch.setattr(mcp_core, "_DEFERRED_T1_MINT", {})
        return calls

    def test_a_deferred_mint_skips_the_waiter_without_touching_t1(self, monkeypatch, _t1_calls) -> None:
        monkeypatch.setenv("NX_SESSION_ID", str(uuid.uuid4()))
        monkeypatch.setitem(mcp_core._DEFERRED_T1_MINT, "session_id", "deferred")
        mcp_core._start_channel_waiter()
        assert _t1_calls == []
        assert mcp_core._channel.active_waiter(mcp_core._current_subscription_session_id()) is None

    def test_the_skip_is_the_guard_not_a_missing_session_id(self, monkeypatch, _t1_calls) -> None:
        """Control: the same environment without the deferral reaches `_get_t1()`."""
        monkeypatch.setenv("NX_SESSION_ID", str(uuid.uuid4()))
        mcp_core._start_channel_waiter()  # the RuntimeError is swallowed: the waiter is best-effort
        assert _t1_calls == ["get_t1"]

    def test_cancel_with_no_waiter_is_a_noop_with_only_nx_session_id(self, monkeypatch, _t1_calls) -> None:
        monkeypatch.setenv("NX_SESSION_ID", str(uuid.uuid4()))
        monkeypatch.setitem(mcp_core._DEFERRED_T1_MINT, "session_id", "deferred")
        mcp_core._start_channel_waiter()
        asyncio.run(mcp_core._cancel_channel_waiter_task())
        assert _t1_calls == []

    def test_a_non_uuid_nx_session_id_never_raises_in_the_lifespan_helpers(self, monkeypatch, _t1_calls) -> None:
        monkeypatch.setenv("NX_SESSION_ID", "desktop-session")
        mcp_core._start_channel_waiter()
        asyncio.run(mcp_core._cancel_channel_waiter_task())
        assert _t1_calls == []


class TestTheWaiterStartsWhenADeferredMintSucceeds:
    """A session that started with the service down skips the waiter (above). When the first T1 call then
    completes the deferred mint, the waiter must be started on the event loop, or the session never gets
    channel push for its whole life."""

    def test_a_successful_deferred_mint_schedules_the_waiter_on_the_loop(self, monkeypatch) -> None:
        import nexus.db.t1 as t1_mod
        from nexus import mcp_infra

        sid = str(uuid.uuid4())
        scheduled: list[object] = []

        class _Loop:
            def call_soon_threadsafe(self, fn, *args):
                scheduled.append(fn)

        monkeypatch.setenv("NX_T1_SESSION", "placeholder")
        monkeypatch.setenv("NX_T1_SESSION_ID", sid)
        monkeypatch.setattr(t1_mod, "_lock_guarded_mint_or_borrow", lambda s, c: ("token", False, None))
        monkeypatch.setattr(mcp_core, "_note_t1_borrowed", lambda *a, **k: None)
        monkeypatch.setattr(mcp_infra, "set_t1_pre_init_hook", lambda hook: None)
        monkeypatch.setattr(
            mcp_core, "_DEFERRED_T1_MINT", {"session_id": sid, "config_dir": None, "loop": _Loop()}
        )
        mcp_core._retry_deferred_t1_mint()
        assert mcp_core._start_channel_waiter in scheduled

    def test_a_failed_deferred_mint_schedules_nothing(self, monkeypatch) -> None:
        import nexus.db.t1 as t1_mod

        scheduled: list[object] = []

        class _Loop:
            def call_soon_threadsafe(self, fn, *args):
                scheduled.append(fn)

        def _down(s, c):
            raise ConnectionError("service down")

        monkeypatch.setattr(t1_mod, "_lock_guarded_mint_or_borrow", _down)
        monkeypatch.setattr(
            mcp_core, "_DEFERRED_T1_MINT", {"session_id": str(uuid.uuid4()), "config_dir": None, "loop": _Loop()}
        )
        with pytest.raises(RuntimeError):
            mcp_core._retry_deferred_t1_mint()
        assert scheduled == []
