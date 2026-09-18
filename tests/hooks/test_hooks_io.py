# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nexus.hooks._io`` — the shared payload reader, decision
envelope writers, and never-fail boundary (RDR-215 Phase 1, bead nexus-q02nx.1).

The envelope assertions are byte-for-byte against the shapes the bash layer
emits today, as inventoried in T2 ``nexus_rdr/215-hook-contract-map``. The port
reproduces those bytes; a reformatted-but-equivalent JSON object is a contract
break, because Claude Code is not the only reader — several tests and the
routing framework match on the rendered text.
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from structlog.testing import capture_logs

from nexus.hooks import _io


class _TTYStream(io.StringIO):
    """A StringIO that claims to be a terminal, to exercise the TTY branch."""

    def isatty(self) -> bool:
        return True


class _ExplodingStream(io.StringIO):
    def isatty(self) -> bool:
        return False

    def read(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("stream is gone")


# -- read_payload -------------------------------------------------------------

def test_read_payload_returns_the_parsed_dict():
    assert _io.read_payload(io.StringIO('{"session_id": "s1", "source": "fork"}')) == {
        "session_id": "s1",
        "source": "fork",
    }


def test_read_payload_on_a_tty_returns_none_without_reading():
    """A TTY read would block until EOF; the helper must not call read() at all."""
    stream = _TTYStream("this would be returned if read() were called")
    assert _io.read_payload(stream) is None
    # Nothing consumed: the cursor never moved.
    assert stream.tell() == 0


@pytest.mark.parametrize(
    "raw",
    ["", "   not json   ", "[1, 2, 3]", '"a bare string"', "null"],
    ids=["empty", "malformed", "list-not-dict", "str-not-dict", "null"],
)
def test_read_payload_swallows_unusable_input(raw):
    assert _io.read_payload(io.StringIO(raw)) is None


def test_read_payload_swallows_a_raising_stream():
    assert _io.read_payload(_ExplodingStream()) is None


# -- permission_decision (the hookSpecificOutput/permissionDecision form) ------

def test_permission_decision_bare_allow():
    assert _io.permission_decision("PreToolUse", "allow") == (
        '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}'
    )


def test_permission_decision_allow_with_additional_context():
    """divergence-language-guard.sh:13 and pre_close_verification_hook.sh:21."""
    assert _io.permission_decision("PostToolUse", "allow", additional_context="note") == (
        '{"hookSpecificOutput": {"hookEventName": "PostToolUse", '
        '"permissionDecision": "allow", "additionalContext": "note"}}'
    )


def test_permission_decision_deny_carries_every_field_in_the_bash_key_order():
    """pre_close_verification_hook.sh:38 — the shared deny() helper's full shape."""
    assert _io.permission_decision(
        "PreToolUse",
        "deny",
        permission_decision_reason="blocked",
        reason="blocked",
        system_message="see the remedy",
    ) == (
        '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
        '"permissionDecisionReason": "blocked", "reason": "blocked"}, '
        '"systemMessage": "see the remedy"}'
    )


def test_permission_decision_escapes_embedded_quotes_and_newlines():
    rendered = _io.permission_decision("PreToolUse", "deny", reason='he said "no"\nthen left')
    assert json.loads(rendered)["hookSpecificOutput"]["reason"] == 'he said "no"\nthen left'
    assert "\n" not in rendered  # one line, always


# -- additional_context (the SubagentStart form, no permissionDecision) --------

def test_additional_context_envelope():
    """subagent-start.sh:32 and sn/mcp-inject.sh's EXIT-trap envelope."""
    assert _io.additional_context("SubagentStart", "body text") == (
        '{"hookSpecificOutput": {"hookEventName": "SubagentStart", '
        '"additionalContext": "body text"}}'
    )


# -- permission_request (the PermissionRequest behavior form) -----------------

def test_permission_request_allow():
    """auto-approve-nx-mcp.sh:89-96."""
    assert _io.permission_request("allow") == (
        '{"hookSpecificOutput": {"hookEventName": "PermissionRequest", '
        '"decision": {"behavior": "allow"}}}'
    )


# -- stop_decision (the top-level decision form the stop hooks use) -----------

def test_stop_decision_bare_approve():
    """stop_verification_hook.sh:22."""
    assert _io.stop_decision("approve") == '{"decision": "approve"}'


def test_stop_decision_with_reason():
    """stop_verification_hook.sh:20 and subagent-stop.sh:255,289."""
    assert _io.stop_decision("block", reason="owes a report") == (
        '{"decision": "block", "reason": "owes a report"}'
    )


# -- HookResult ---------------------------------------------------------------

def test_hook_result_defaults_to_silent_and_zero():
    """Most hooks emit nothing and exit 0; that must be the cheapest thing to say."""
    result = _io.HookResult()
    assert result.stdout is None
    assert result.exit_code == 0


def test_hook_result_carries_stdout_and_a_code():
    result = _io.HookResult(stdout='{"decision": "approve"}', exit_code=2)
    assert result.stdout == '{"decision": "approve"}'
    assert result.exit_code == 2


# -- the never-fail boundary --------------------------------------------------

def test_never_fail_returns_the_wrapped_result_untouched():
    assert _io.never_fail(lambda: _io.HookResult(stdout="x", exit_code=3), "demo") == _io.HookResult(
        stdout="x", exit_code=3
    )


def test_never_fail_swallows_an_exception_into_a_silent_zero_result():
    """The bash layer sets no ``set -e`` by design; Python needs this explicitly.

    A crashing hook must look exactly like a hook that decided to say nothing.
    """

    def boom() -> _io.HookResult:
        raise RuntimeError("hook logic exploded")

    assert _io.never_fail(boom, "demo") == _io.HookResult(stdout=None, exit_code=0)


def test_never_fail_logs_the_swallowed_exception():
    """Swallowed is not invisible: the crash still reaches the hook log, naming
    the hook, so a hook that silently stopped deciding is diagnosable."""

    def boom() -> _io.HookResult:
        raise RuntimeError("hook logic exploded")

    with capture_logs() as cap:
        _io.never_fail(boom, "demo_hook")

    assert any(
        entry.get("hook") == "demo_hook" and "hook logic exploded" in str(entry.get("error", ""))
        for entry in cap
    ), cap


def test_never_fail_swallows_an_error_raised_outside_the_normal_hierarchy():
    """Not every hook crash is a tidy ValueError; a recursion blowout in the
    logic must look the same to the harness as a quiet decision."""

    def boom() -> _io.HookResult:
        raise RecursionError("too deep")

    assert _io.never_fail(boom, "demo").exit_code == 0


@pytest.mark.parametrize(
    "exc",
    [KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError],
    ids=["sigint", "sysexit", "generatorexit", "cancelled"],
)
def test_never_fail_lets_shutdown_and_cancellation_through(exc):
    """The harness kills hooks; swallowing that would turn a SIGTERM into a hang.

    Cancellation is here for a different reason: ``asyncio.CancelledError`` is a
    ``BaseException``, so a boundary that catches ``BaseException`` swallows it
    by default. The tool tier calls ``run()`` from async handlers, so swallowing
    it would break the *caller's* timeout while the hook reported success.
    """

    def boom() -> _io.HookResult:
        raise exc()

    with pytest.raises(exc):
        _io.never_fail(boom, "demo")
