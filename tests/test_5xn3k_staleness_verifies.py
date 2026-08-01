# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k AC2: the staleness gate must verify, not assume.

Root cause (recorded on the bead): both gates ask
``col.get(where={"content_hash": h}, limit=1)`` — "does ANY chunk with this
content hash exist?" — which ONE survivor satisfies. An index that dies
mid-write commits an arbitrary subset, so every later re-index finds a
survivor, returns 0, and reports success. The document is permanently
un-indexable by any supported command.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from nexus.doc_indexer import _manifest_is_fully_present


class _Col:
    """T3 collection stub: get(ids=...) returns only the ids that exist."""

    def __init__(self, present: set[str]) -> None:
        self._present = present
        self.calls: list[list[str]] = []

    def get(self, ids=None, include=None, **kw):
        self.calls.append(list(ids or []))
        return {"ids": [i for i in (ids or []) if i in self._present]}


def _cat(rows):
    cat = MagicMock()
    cat.get_manifest.return_value = rows
    return cat


def _patch_cat(monkeypatch, cat):
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader",
        lambda *a, **k: cat, raising=False,
    )


def test_one_surviving_chunk_does_not_mark_the_document_whole(monkeypatch) -> None:
    """THE BUG. The manifest names 3 chunks; T3 holds 1. Pre-fix the gate saw
    that single survivor and skipped forever."""
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    _patch_cat(monkeypatch, _cat([{"chash": a}, {"chash": b}, {"chash": c}]))
    assert _manifest_is_fully_present(_Col({a}), "1.1.1") is False


def test_fully_present_manifest_still_skips(monkeypatch) -> None:
    """NON-VACUITY: an intact document must NOT be re-embedded every pass."""
    a, b = "a" * 64, "b" * 64
    _patch_cat(monkeypatch, _cat([{"chash": a}, {"chash": b}]))
    assert _manifest_is_fully_present(_Col({a, b}), "1.1.1") is True


def test_empty_manifest_is_left_to_reconcile(monkeypatch) -> None:
    """chunk_count>0 with an EMPTY manifest is the GH #1397 ghost class, which
    `nx catalog reconcile` owns. This gate must not silently claim it."""
    _patch_cat(monkeypatch, _cat([]))
    assert _manifest_is_fully_present(_Col(set()), "1.1.1") is True


def test_unreadable_catalog_fails_open(monkeypatch) -> None:
    """FAIL-OPEN on the hot path: a transient read failure must not force a
    full re-embed of an intact document. The damage is durable and will be
    caught next pass; a false positive here is expensive."""
    cat = MagicMock()
    cat.get_manifest.side_effect = RuntimeError("service down")
    _patch_cat(monkeypatch, cat)
    assert _manifest_is_fully_present(_Col(set()), "1.1.1") is True


def test_presence_check_is_paged_to_the_quota(monkeypatch) -> None:
    """The existence check must respect the 300-id read quota, or a large
    document raises ChromaError instead of verifying."""
    chashes = [f"{i:064x}" for i in range(701)]
    _patch_cat(monkeypatch, _cat([{"chash": c} for c in chashes]))
    col = _Col(set(chashes))
    assert _manifest_is_fully_present(col, "1.1.1") is True
    assert len(col.calls) == 3, f"expected 3 pages of <=300, got {len(col.calls)}"
    assert all(len(batch) <= 300 for batch in col.calls)


# ── the PROSE path's read-only identity resolution ───────────────────────────


def test_prose_identity_lookup_never_registers(monkeypatch) -> None:
    """The prose gate resolves identity READ-ONLY.

    This runs while DECIDING whether to skip, for documents that may be
    untouched. Minting a catalog row as a side effect of that decision would
    be a worse bug than the one being fixed, so only by_source_uri is used —
    never register, never ensure-owner.
    """
    from pathlib import Path

    from nexus.doc_indexer import _doc_id_for_path

    cat = MagicMock()
    cat.by_source_uri.return_value = type("E", (), {"tumbler": "1.2.3"})()
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False,
    )

    assert _doc_id_for_path(Path("/tmp/doc.md")) == "1.2.3"
    assert cat.by_source_uri.call_count == 1
    assert cat.by_source_uri.call_args[0][0].startswith("file://")
    for forbidden in ("register", "register_owner", "ensure_owner_for_repo"):
        assert not getattr(cat, forbidden).called, (
            f"the staleness path must never call {forbidden}"
        )


def test_prose_identity_miss_fails_open(monkeypatch) -> None:
    """An unidentifiable document yields "" -> no expected set -> no evidence
    of damage. A miss must never force a spurious re-embed."""
    from pathlib import Path

    from nexus.doc_indexer import _doc_id_for_path

    cat = MagicMock()
    cat.by_source_uri.return_value = None
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False,
    )
    assert _doc_id_for_path(Path("/tmp/nope.md")) == ""
    assert _manifest_is_fully_present(_Col(set()), "") is True
