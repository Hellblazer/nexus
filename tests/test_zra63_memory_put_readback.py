# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""memory_put verifies the row is readable before claiming it stored (nexus-zra63).

nexus-piqm5 Layer 2. Layer 1 reads a finished subagent's transcript and catches
writes that REPORTED failure. It structurally cannot see the other half: a store
that returns a success string while landing nothing, because the transcript
faithfully records the success string. Only a read-back separates those.

THE FALSIFIER IS A SILENT NO-OP, NOT A RAISE, and that choice is the whole
point of the suite. A raising store only proves the tool propagates errors it
was handed -- which the pre-existing ``except`` already did, so such a test
would pass with the read-back deleted. A no-op store (``put`` returns an id,
``resolve_title`` finds nothing) proves the check READS STATE BACK rather than
trusting a return value. ``test_the_falsifier_is_reached_through_the_readback``
pins that distinction so the suite cannot quietly degrade into the weaker one.

The parent bead's bar, quoted: any fix "must be falsifiable by breaking the
store. A check that passes when persistence is unavailable is the same defect
one level up." Hence the third state -- an unreachable verifier is reported as
UNVERIFIED, never as stored.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """_record_tier_write is best-effort and reaches the real store.

    It resolves ``nexus.mcp_infra.t2_ctx`` lazily, so patching
    ``core._t2_ctx`` does not cover it. Left live it would attempt a real
    network write on every case here (swallowing the failure, but slowly).
    """
    import nexus.mcp.core as core
    monkeypatch.setattr(core, "_record_tier_write", lambda **kw: None)


def _fake_t2(*, put_id: object, resolve):
    """A stand-in T2 whose write and read-back can disagree.

    ``resolve`` is called as ``resolve(project, title)`` and either returns the
    ``(entry, candidates)`` pair ``resolve_title`` yields, or raises to
    simulate an unreachable verifier.
    """
    class _DB:
        def put(self, **_kw):
            return put_id

        def resolve_title(self, *, project, title):
            return resolve(project, title)

    db = _DB()

    @contextmanager
    def _ctx():
        yield db

    return _ctx


def _entry(content: str, row_id: object = 42) -> dict:
    return {
        "id": row_id, "project": "p", "title": "t",
        "content": content, "tags": "", "timestamp": "",
    }


def _call(monkeypatch: pytest.MonkeyPatch, *, put_id, resolve, content="findings"):
    import nexus.mcp.core as core
    monkeypatch.setattr(core, "_t2_ctx", _fake_t2(put_id=put_id, resolve=resolve))
    return core.memory_put(content=content, project="p", title="t")


# ── the falsifier: break the store SILENTLY, the check must trip ────────────

def test_falsifier_silent_noop_store_is_caught(monkeypatch):
    """put() hands back an id; nothing ever lands. The 2026-08-25 blind spot."""
    out = _call(monkeypatch, put_id=42, resolve=lambda p, t: (None, []))
    assert out.startswith("Error:"), out
    assert "did not land" in out


def test_the_falsifier_is_reached_through_the_readback(monkeypatch):
    """Pin that the no-op is caught by READING BACK, not by error propagation.

    ``missing`` is a verdict only ``_verify_t2_write_landed`` can produce. If
    this assertion ever has to be relaxed to a bare "Error:", the suite has
    decayed into testing the pre-existing except branch instead.
    """
    out = _call(monkeypatch, put_id=42, resolve=lambda p, t: (None, []))
    assert "(missing)" in out, out


def test_error_carries_the_prefix_layer_1_scans_for(monkeypatch):
    """Cross-layer composition: a silent no-op becomes visible to Layer 1 too.

    Layer 1's transcript scan keys on the shared "Error: " prefix, so routing
    this through _mcp_tool_error means the SubagentStop hook catches a silent
    no-op as well, not just this caller.
    """
    out = _call(monkeypatch, put_id=42, resolve=lambda p, t: (None, []))
    assert out.lstrip().startswith("Error:")


def test_content_mismatch_is_caught(monkeypatch):
    out = _call(
        monkeypatch, put_id=42,
        resolve=lambda p, t: (_entry("something else entirely"), []),
        content="findings",
    )
    assert out.startswith("Error:"), out
    assert "(mismatch)" in out


# ── the third state: unreachable is NOT success and NOT failure ─────────────

def test_unreachable_verifier_is_unverified_not_stored(monkeypatch):
    """A check that reports success when persistence is unavailable is the
    defect one level up. It must not claim the write landed."""
    def _boom(p, t):
        raise ConnectionError("T2 unreachable")

    out = _call(monkeypatch, put_id=42, resolve=_boom)
    assert out.startswith("Stored (UNVERIFIED):"), out
    assert "ConnectionError" in out
    # Neither a plain success...
    assert not out.startswith("Stored: ")
    # ...nor a confirmed failure.
    assert not out.startswith("Error:")


def test_unverified_says_both_directions_are_open(monkeypatch):
    def _boom(p, t):
        raise TimeoutError("read timed out")

    out = _call(monkeypatch, put_id=42, resolve=_boom)
    assert "neither a confirmed success nor a confirmed failure" in out.lower()


# ── the happy path's existing contract is unchanged ────────────────────────

def test_verified_write_returns_the_original_string_exactly(monkeypatch):
    """Existing callers parse this line; verification must not reword it."""
    out = _call(
        monkeypatch, put_id=42,
        resolve=lambda p, t: (_entry("findings"), []),
        content="findings",
    )
    assert out == "Stored: [42] p/t"


def test_empty_content_is_still_rejected_before_any_write(monkeypatch):
    """The pre-existing guard runs ahead of the write and the read-back."""
    import nexus.mcp.core as core

    def _unreachable(p, t):  # pragma: no cover — must never be consulted
        raise AssertionError("read-back ran despite empty content")

    monkeypatch.setattr(
        core, "_t2_ctx", _fake_t2(put_id=1, resolve=_unreachable),
    )
    assert core.memory_put(content="", project="p", title="t") == (
        "Error: content is required"
    )
