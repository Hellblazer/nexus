# SPDX-License-Identifier: AGPL-3.0-or-later
"""The allocator-drift row counts tombstoned children, as its docstring says (nexus-f3yqd.6).

``catalog_documents``' unique key is ``(tenant_id, tumbler)`` and covers soft-deleted rows, so a tombstoned
document's tumbler is still taken, and the engine's own ``highestChildSeq`` counts it. The doctor row read the live
walk only (``/list`` filters ``deleted_at IS NULL``), so an owner whose highest child had been deleted read as clean.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import nexus.health as h


def _cat(owners: list[dict], live: list[str], trash: list[str] | Exception) -> MagicMock:
    cat = MagicMock()
    cat.list_owners.return_value = owners
    cat.all_documents.return_value = [MagicMock(tumbler=t) for t in live]
    if isinstance(trash, Exception):
        cat.list_trash.side_effect = trash
    else:
        cat.list_trash.side_effect = lambda *, limit, offset: [{"tumbler": t} for t in trash[offset:offset + limit]]
    return cat


def _row(monkeypatch: pytest.MonkeyPatch, cat: MagicMock) -> h.HealthResult:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False)
    return h._check_next_seq_drift()[0]


def test_a_tombstoned_high_child_is_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _cat([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.1", "1.12.7"], ["1.12.9"])
    r = _row(monkeypatch, cat)
    assert r.ok is False and r.warn is True, r.detail
    assert "1.12" in r.detail and "highest child=9" in r.detail


def test_the_trash_is_paged_to_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_TRASH_PAGE", 2)
    trash = ["1.12.2", "1.12.3", "1.12.4", "1.12.5", "1.12.11"]
    cat = _cat([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], trash)
    r = _row(monkeypatch, cat)
    assert "highest child=11" in r.detail, r.detail


def test_an_unreadable_trash_is_said_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _cat([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], RuntimeError("404 Not Found"))
    r = _row(monkeypatch, cat)
    assert r.ok is True and r.detail.startswith("none"), r.detail
    assert "tombstoned children not read" in r.detail, r.detail


def test_a_clean_owner_with_a_lower_tombstone_stays_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _cat([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], ["1.12.3"])
    r = _row(monkeypatch, cat)
    assert r.ok is True and r.detail == "none (1 owner(s) checked)", r.detail


def test_a_lower_tombstone_never_lowers_the_high_water(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _cat([{"tumbler_prefix": "1.12", "next_seq": 5}], ["1.12.9"], ["1.12.3"])
    r = _row(monkeypatch, cat)
    assert r.ok is False and "highest child=9" in r.detail, r.detail


def test_odd_trash_entries_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _cat([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], [])
    cat.list_trash.side_effect = lambda *, limit, offset: [{"tumbler": "1.12.x"}, "junk", {"tumbler": "1.12.8.1"}] if offset == 0 else []
    r = _row(monkeypatch, cat)
    assert r.ok is True and r.detail == "none (1 owner(s) checked)", r.detail


def test_an_engine_that_ignores_offset_does_not_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_TRASH_PAGE", 2)
    cat = _cat([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], [])
    cat.list_trash.side_effect = lambda *, limit, offset: [{"tumbler": "1.12.9"}, {"tumbler": "1.12.10"}]
    r = _row(monkeypatch, cat)
    assert "highest child=10" in r.detail and cat.list_trash.call_count == 2, (r.detail, cat.list_trash.call_count)
