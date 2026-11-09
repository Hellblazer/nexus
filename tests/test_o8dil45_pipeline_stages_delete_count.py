# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o8dil.45 (RDR-191): ``_force_t3_orphan_cleanup``'s docstring claims
"Fail-loud by design" and "Returns the number of orphan chunks deleted", but
pre-fix it called ``col.delete(orphan_ids)`` and discarded the response,
returning ``len(orphan_ids)`` unconditionally -- a silent over-report once
the server's anti-join (nexus-o8dil.5, RDR-191 F10c) can legitimately refuse
part of the batch (a "orphan" candidate that is, in fact, still referenced
by a live catalog manifest row this function's metadata-only lookup cannot
see).

This function backs the ``--force`` deadlock-break (nexus-9ji): a caller
that believes N orphans were cleared when fewer actually were may find the
deadlock still present on the next re-ingest attempt.
"""
from __future__ import annotations

from typing import Any

import structlog

from nexus.pipeline_stages import _force_t3_orphan_cleanup


class _FakeColFullDelete:
    def __init__(self, ids: list[str]):
        self._ids = list(ids)
        self.delete_calls: list[list[str]] = []

    def get_all_metadata(self, where: dict | None = None, *, include_non_live: bool = False) -> dict:
        return {"ids": list(self._ids)}

    def delete(self, ids: list[str]) -> int:
        self.delete_calls.append(list(ids))
        return len(ids)


class _FakeColPartialDelete:
    """Models the server's anti-join refusing part of the "orphan" batch --
    the metadata-only lookup this function uses cannot see a live manifest
    reference, so some candidates are not actually orphans."""

    def __init__(self, ids: list[str], refuse: set[str]):
        self._ids = list(ids)
        self.refuse = set(refuse)
        self.delete_calls: list[list[str]] = []

    def get_all_metadata(self, where: dict | None = None, *, include_non_live: bool = False) -> dict:
        return {"ids": list(self._ids)}

    def delete(self, ids: list[str]) -> int:
        self.delete_calls.append(list(ids))
        return sum(1 for i in ids if i not in self.refuse)


class _FakeT3:
    def __init__(self, col: Any):
        self._col = col

    def get_or_create_collection(self, name: str) -> Any:
        return self._col


def test_returns_actual_deleted_count_on_full_success():
    col = _FakeColFullDelete(["orphan-1", "orphan-2", "orphan-3"])
    result = _force_t3_orphan_cleanup(_FakeT3(col), "code__test__m__v1", "hash123")
    assert result == 3
    assert col.delete_calls == [["orphan-1", "orphan-2", "orphan-3"]]


def test_returns_actual_deleted_count_not_requested_on_partial_anti_join():
    """Non-vacuity: pre-fix this returned ``len(orphan_ids)`` (3)
    unconditionally, directly contradicting its own docstring's "Returns the
    number of orphan chunks deleted" once the server legitimately refuses
    part of the batch."""
    col = _FakeColPartialDelete(
        ["orphan-1", "orphan-2", "orphan-3"], refuse={"orphan-2"},
    )

    result = _force_t3_orphan_cleanup(_FakeT3(col), "code__test__m__v1", "hash456")

    assert result == 2, (
        f"expected the server's ACTUAL deleted count (2 of 3 requested, "
        f"one anti-join-refused), got {result!r}"
    )


def test_no_orphans_returns_zero_without_calling_delete():
    class _EmptyCol:
        def get_all_metadata(self, where=None, *, include_non_live=False):
            return {"ids": []}

        def delete(self, ids):
            raise AssertionError("delete must not be called when there are no orphans")

    result = _force_t3_orphan_cleanup(_FakeT3(_EmptyCol()), "code__test__m__v1", "hash-none")
    assert result == 0


def test_partial_anti_join_delete_logs_a_warning():
    """A partial refusal here means the ``--force`` deadlock-break may be
    incomplete for this content_hash -- an operator-visible signal, not
    silence, since the caller's next re-ingest may hit the same wall."""
    col = _FakeColPartialDelete(["orphan-1", "orphan-2"], refuse={"orphan-1"})

    with structlog.testing.capture_logs() as logs:
        result = _force_t3_orphan_cleanup(_FakeT3(col), "code__test__m__v1", "hash789")

    assert result == 1
    warnings = [l for l in logs if l.get("log_level") == "warning"]
    assert any("partial" in l["event"] for l in warnings), (
        f"expected a partial-delete warning, got events: {[l['event'] for l in warnings]}"
    )


def test_full_success_delete_logs_no_partial_warning():
    col = _FakeColFullDelete(["orphan-1", "orphan-2"])

    with structlog.testing.capture_logs() as logs:
        result = _force_t3_orphan_cleanup(_FakeT3(col), "code__test__m__v1", "hash-full")

    assert result == 2
    warnings = [l for l in logs if l.get("log_level") == "warning"]
    assert not any("partial" in l["event"] for l in warnings)


# ── RDR-236 Phase 4 Step 1: a chunk row carries no content_hash, so the selector is the manifest's chashes ──


class _SelectorCol:
    """Answers each ``where`` it is asked with the ids that match it, and records the wheres."""

    def __init__(self, by_chash: dict[str, str], by_content_hash: dict[str, list[str]] | None = None):
        self._by_chash = by_chash
        self._by_content_hash = by_content_hash or {}
        self.wheres: list[dict] = []
        self.delete_calls: list[list[str]] = []

    def get_all_metadata(self, where: dict | None = None, *, include_non_live: bool = False) -> dict:
        assert include_non_live, "an orphan has no live owner: the physical scan is the point"
        self.wheres.append(where)
        (key, cond), = where.items()
        if key == "chunk_text_hash":
            return {"ids": [self._by_chash[c] for c in cond["$in"] if c in self._by_chash]}
        return {"ids": list(self._by_content_hash.get(cond, []))}

    def delete(self, ids: list[str]) -> int:
        self.delete_calls.append(list(ids))
        return len(ids)


def _manifest_of(*chashes: str):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    reader = MagicMock()
    reader.get_manifest.return_value = [SimpleNamespace(chash=c) for c in chashes]
    return reader


def test_a_documents_orphans_are_found_by_its_manifests_chashes_not_a_row_key():
    """A row written since Step 1 has no ``content_hash``: the content-hash lookup is blind to it."""
    from unittest.mock import patch

    col = _SelectorCol(by_chash={"c1": "id-1", "c2": "id-2"})
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_manifest_of("c1", "c2")):
        result = _force_t3_orphan_cleanup(_FakeT3(col), "code__test__m__v1", "hashX", doc_id="1.1.5")
    assert result == 2 and col.delete_calls == [["id-1", "id-2"]]
    assert all("content_hash" not in w for w in col.wheres), col.wheres


def test_a_document_with_no_manifest_falls_back_to_the_content_hash_lookup():
    from unittest.mock import patch

    col = _SelectorCol(by_chash={}, by_content_hash={"hashY": ["legacy-1"]})
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_manifest_of()):
        result = _force_t3_orphan_cleanup(_FakeT3(col), "code__test__m__v1", "hashY", doc_id="1.1.5")
    assert result == 1 and col.delete_calls == [["legacy-1"]]
    assert col.wheres == [{"content_hash": "hashY"}]
