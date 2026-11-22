# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: the subscription set's identity mailbox: ``load`` and ``_token_identity`` read the token file, and
``reset_cache`` removes the listener a set registered. Split from tests/test_subscriptions.py, which is at its
file-size ceiling; engine-free (T1 is a stand-in)."""
from __future__ import annotations

import json
import types
from typing import Any

import pytest

from nexus.mcp.subscriptions import SubscriptionSet


class _T1WithSet:
    """A T1 whose only entry is a persisted subscription set for one session (what ``persist`` writes)."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def list_entries(self) -> list[dict[str, Any]]:
        return self._entries


def _bound_to(monkeypatch: pytest.MonkeyPatch, identity: str | None) -> None:
    import nexus.identity.token_header as token_header  # noqa: PLC0415

    record = None if identity is None else types.SimpleNamespace(identity=identity)
    monkeypatch.setattr(token_header, "current", lambda: record)


def test_token_identity_reads_the_leased_token_file_and_rejects_what_is_not_an_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.mcp import subscriptions  # noqa: PLC0415

    _bound_to(monkeypatch, "i12")
    assert subscriptions._token_identity() == "i12"  # noqa: SLF001
    _bound_to(monkeypatch, "not-an-id")
    assert subscriptions._token_identity() is None  # noqa: SLF001
    _bound_to(monkeypatch, None)
    assert subscriptions._token_identity() is None  # noqa: SLF001

    def boom() -> None:
        raise OSError("unreadable token file")

    monkeypatch.setattr("nexus.identity.token_header.current", boom)
    assert subscriptions._token_identity() is None  # noqa: SLF001 -- no identity delivers no mailbox


def test_load_gives_a_new_set_and_a_restored_one_the_identity_of_the_token_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both load paths deliver the identity mailbox alone: a fresh set (no persisted entry) and one restored from T1."""
    from nexus.mcp import subscriptions  # noqa: PLC0415

    sid = "11111111-2222-4333-8444-555555555555"
    _bound_to(monkeypatch, "i12")
    fresh = subscriptions.load(_T1WithSet([]), sid, store_factory=lambda: None)
    assert [e["subspace"] for e in fresh.entries()] == ["mailbox/i12"]

    stored = {"id": "e1", "tags": subscriptions._TAG, "content": json.dumps(SubscriptionSet(session_id=sid).to_json())}  # noqa: SLF001
    restored = subscriptions.load(_T1WithSet([stored]), sid, store_factory=lambda: None)
    assert [e["subspace"] for e in restored.entries()] == ["mailbox/i12"]

    _bound_to(monkeypatch, None)
    assert subscriptions.load(_T1WithSet([stored]), sid, store_factory=lambda: None).entries() == []


def test_reset_cache_removes_the_identity_listener_the_set_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp import identity_lease, subscriptions  # noqa: PLC0415

    monkeypatch.setattr(identity_lease, "_IDENTITY_LISTENERS", [])
    monkeypatch.setattr(identity_lease, "_LEASE", None)
    _bound_to(monkeypatch, None)
    sid = "11111111-2222-4333-8444-555555555555"
    subscriptions.reset_cache()
    try:
        subs = subscriptions.get_or_load(_T1WithSet([]), sid, store_factory=lambda: None)
        assert len(identity_lease._IDENTITY_LISTENERS) == 1  # noqa: SLF001
        subscriptions.reset_cache(sid)
        assert identity_lease._IDENTITY_LISTENERS == []  # noqa: SLF001 -- a torn-down set is no longer told
        identity_lease._notify_identity("i12")  # noqa: SLF001
        assert subs.identity is None, "and a late identity does not reach it"
    finally:
        subscriptions.reset_cache()
