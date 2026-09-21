# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-rjmyk — the SDK must not answer a request it just cancelled.

MEASURED 2026-09-20 across 121 Claude Code client logs for this one server: ten
teardowns of the stdio transport, each immediately preceded by ``Connection
error: Received a response for an unknown message ID``. Five carried
``{"code":0,"message":"Request cancelled"}`` — the SDK replying to a request
the client had already cancelled and stopped tracking. Every tool on the server
went away at once, and at least once it stayed away until a human reconnected.

The patch is carried locally because upstream is not going to land it on any
timescale that helps, and because bumping inside our ``mcp>=1.0,<2`` pin fixes
nothing: 1.30.0, the newest 1.x, is byte-identical to the 1.27.1 we resolve.
"""
from __future__ import annotations

import inspect

import anyio
import pytest

from nexus.mcp import _sdk_patches


def _live_cancel_source() -> str:
    from mcp.shared.session import RequestResponder

    return inspect.getsource(RequestResponder.cancel)


# ── the shape check itself ─────────────────────────────────────────────────

def test_the_shape_check_tells_a_call_from_a_mention() -> None:
    """The trap this check was written wrong for, first time round.

    ``_sdk_patches``'s own replacement docstring NAMES ``_send_response`` while
    explaining that it removed the call. A substring test reports that as the
    bug still being present, so the patch would re-apply itself forever and,
    worse, would report a fixed upstream as still broken. Parsed, not grepped.
    """
    calls = "async def f(self):\n    await self._session._send_response(x)\n"
    mentions = 'async def f(self):\n    """We removed the _send_response call."""\n    return None\n'

    assert _sdk_patches._calls_attribute(calls, "_send_response") is True
    assert _sdk_patches._calls_attribute(mentions, "_send_response") is False


# ── non-vacuity: is there still a defect to correct? ───────────────────────

def test_the_installed_sdk_still_has_the_defect_or_this_patch_is_dead_weight() -> None:
    """THE self-retiring signal, and the reason the rest of this file means
    something.

    Everything below shows the patched ``cancel`` sends nothing. That is worth
    nothing if the SDK stopped sending it anyway — the patch would be
    reimplementing upstream behaviour while looking like protection. So this
    asks the unpatched source directly.

    IF THIS TEST GOES RED, upstream has fixed it: delete
    ``_patch_cancellation_response`` and this file, rather than adjusting
    anything. The patch is written to no-op in that world, so nothing breaks in
    the meantime.
    """
    original = inspect.getsource(_sdk_patches._patch_cancellation_response)
    # The pristine SDK body, read from the file on disk rather than from the
    # possibly-already-patched attribute.
    import mcp.shared.session as sdk_session

    disk_source = inspect.getsource(sdk_session)
    cancel_on_disk = disk_source[disk_source.index("    async def cancel(self)"):]
    cancel_on_disk = cancel_on_disk[: cancel_on_disk.index("\n    @property")]

    assert _sdk_patches._calls_attribute(cancel_on_disk, "_send_response"), (
        "the installed mcp SDK's RequestResponder.cancel no longer answers a "
        "cancelled request — the defect is fixed upstream and this whole patch "
        "is now dead weight; delete it rather than keeping a no-op"
    )
    assert "_nx_patched" in original, "the patch must mark what it replaced"


# ── the patch's effect ─────────────────────────────────────────────────────

def test_the_live_cancel_does_not_call_send_response() -> None:
    """After startup applies it — and importing nexus.mcp.core is what does —
    the attribute the SDK actually dispatches must no longer answer."""
    _sdk_patches.apply_sdk_patches()
    assert not _sdk_patches._calls_attribute(_live_cancel_source(), "_send_response")


def test_cancelling_sends_nothing_on_the_wire() -> None:
    """The behaviour, not the source text: a cancelled request produces no
    outbound message at all. Source inspection could pass against a patch that
    still wrote to the stream through some other name."""
    from mcp.shared.session import RequestResponder

    _sdk_patches.apply_sdk_patches()

    sent: list[object] = []

    class _Session:
        async def _send_response(self, **kwargs: object) -> None:
            sent.append(kwargs)

    async def run() -> None:
        responder = RequestResponder(
            request_id=1, request_meta=None, request=object(),
            session=_Session(), on_complete=lambda _r: None,
        )
        with responder:
            await responder.cancel()
            assert responder._completed is True, "still has to leave in_flight"
            assert responder._cancel_scope.cancel_called is True

    anyio.run(run)

    assert sent == [], f"a cancelled request was answered anyway: {sent}"


def test_respond_after_cancel_refuses_and_sends_nothing() -> None:
    """The neighbouring path, pinned because this patch's whole justification
    is that ``cancel()`` disagreed with ``respond()`` and now agrees.

    Written first as "respond() quietly skips", which is what its
    ``if not self.cancelled`` guard suggests and is WRONG: the assertion on
    ``_completed`` sits ABOVE that guard, and both upstream's cancel() and ours
    set ``_completed``. So it raises. That is unchanged by this patch -- the
    point here is that nothing reaches the wire either way, which is the
    property the transport actually cares about.
    """
    from mcp.shared.session import RequestResponder

    _sdk_patches.apply_sdk_patches()
    sent: list[object] = []

    class _Session:
        async def _send_response(self, **kwargs: object) -> None:
            sent.append(kwargs)

    async def run() -> None:
        responder = RequestResponder(
            request_id=2, request_meta=None, request=object(),
            session=_Session(), on_complete=lambda _r: None,
        )
        with responder:
            await responder.cancel()
            with pytest.raises(AssertionError, match="already responded"):
                await responder.respond({"ignored": True})  # type: ignore[arg-type]

    anyio.run(run)
    assert sent == [], f"a cancelled request reached the wire: {sent}"


def test_it_is_idempotent() -> None:
    _sdk_patches.apply_sdk_patches()
    assert _sdk_patches.apply_sdk_patches() == {"cancellation_response": "already applied"}


# ── it must never kill startup ─────────────────────────────────────────────

def test_a_missing_symbol_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded ``mcp>=1.0,<2`` pin means a future SDK can move or delete
    this internal. A server that will not boot is worse than the bug."""
    import mcp.shared.session as sdk_session

    monkeypatch.delattr(sdk_session.RequestResponder, "cancel", raising=False)
    result = _sdk_patches.apply_sdk_patches()
    assert result["cancellation_response"].startswith("unavailable")


def test_an_unreadable_source_refuses_rather_than_patching_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the shape cannot be proven, do NOT patch: an unconditional override
    would silently replace whatever upstream does now."""
    def _boom(_obj: object) -> str:
        raise OSError("no source")

    monkeypatch.setattr(_sdk_patches.inspect, "getsource", _boom)
    # Force the not-yet-patched branch so getsource is actually reached.
    import mcp.shared.session as sdk_session

    original = sdk_session.RequestResponder.cancel
    unmarked = getattr(original, "__func__", original)

    async def _fresh(self: object) -> None:  # no _nx_patched marker
        return None

    monkeypatch.setattr(sdk_session.RequestResponder, "cancel", _fresh)
    result = _sdk_patches.apply_sdk_patches()
    assert result["cancellation_response"].startswith("skipped"), result
    assert unmarked is not None


def test_a_fixed_upstream_becomes_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-retiring branch, exercised rather than asserted in prose."""
    import mcp.shared.session as sdk_session

    async def _already_correct(self: object) -> None:
        self._cancel_scope.cancel()  # type: ignore[attr-defined]
        self._completed = True  # type: ignore[attr-defined]

    monkeypatch.setattr(sdk_session.RequestResponder, "cancel", _already_correct)
    result = _sdk_patches.apply_sdk_patches()
    assert result["cancellation_response"] == "not needed: cancel() no longer sends a response"


# ── the wiring ─────────────────────────────────────────────────────────────

def test_the_server_applies_the_patch_before_it_builds_the_server() -> None:
    """A patch nothing calls is the same as no patch. It must also run BEFORE
    FastMCP is constructed, so no request can be cancelled through the
    unpatched path."""
    source = inspect.getsource(__import__("nexus.mcp.core", fromlist=["core"]))
    assert "_apply_sdk_patches()" in source, "core.py never applies the SDK patches"
    assert source.index("_apply_sdk_patches()") < source.index('mcp = FastMCP("nexus"'), (
        "the patch must be applied before the server is constructed"
    )
