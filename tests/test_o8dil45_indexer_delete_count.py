# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o8dil.45 (RDR-191): indexer.py's delete-count call sites must report
the server's ACTUAL deleted count, not the requested/candidate size.

Post-nexus-o8dil.5, ``PgVectorRepository.delete``'s anti-join can legitimately
delete fewer chunks than requested (a chash another LIVE document's manifest
still references is correctly retained). Two sites here discarded the
server's response and reported the requested size unconditionally:

  - ``_batched_delete``: summed ``len(batch)`` per batch instead of the
    server's actual per-batch ``deleted`` count.
  - ``_prune_misclassified_in_collection``'s ``_guarded_delete`` closure
    (shared by both the manifest-chash arm and the legacy doc_id-keyed arm):
    returned ``len(deletable)`` -- the count the F8c manifest guard (sibling
    bead nexus-o8dil.2) approved as safe to send -- rather than what the
    server actually removed. The F8c guard's ``chash_to_docs`` ground truth
    is scoped to the doc_ids being pruned in this call, not catalog-wide, so
    a candidate it clears can still be legitimately anti-join-refused by the
    server for a live reference outside that scope.

This bead is about VISIBILITY of the count -- not about extending the F8c
manifest-completeness guard (nexus-o8dil.2's scope). The tests below use a
fake collection whose ``.delete()`` mirrors the real server contract (an
``int`` actually-deleted count, potentially less than requested), the same
contract ``_ServiceCollectionStub.delete()`` now has (see
``tests/db/test_o8dil45_delete_count_visibility.py``).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import structlog

from nexus.indexer import _batched_delete, _prune_misclassified_in_collection


# ── _batched_delete ──────────────────────────────────────────────────────────


class _FakeColReportsActualCount:
    """Models a real anti-join-scoped backend: ``delete()`` returns the
    ACTUAL number removed, which may be less than requested."""

    def __init__(self, refuse: set[str] | None = None):
        self.refuse = set(refuse or ())
        self.calls: list[list[str]] = []

    def delete(self, ids):
        self.calls.append(list(ids))
        return sum(1 for i in ids if i not in self.refuse)


class _FakeColReturnsNone:
    """Models a backend with no counting contract (e.g. the in-memory test
    double): ``delete()`` returns ``None``. Must fall back to ``len(batch)``
    -- unchanged legacy behavior -- since these backends never partially
    refuse a delete."""

    def delete(self, ids):
        return None


def test_batched_delete_sums_the_actual_reported_count_not_len_batch():
    """Non-vacuity: pre-fix, ``_batched_delete`` did ``deleted += len(batch)``
    unconditionally regardless of what ``col.delete()`` returned -- this
    would report 5, not 4."""
    col = _FakeColReportsActualCount(refuse={"id2"})
    ids = [f"id{i}" for i in range(5)]

    result = _batched_delete(col, ids)

    assert result == 4, (
        f"expected the actual count (4 of 5 requested, one anti-join-"
        f"refused), got {result!r}"
    )


def test_batched_delete_falls_back_to_len_batch_when_col_reports_no_count():
    """Backends without the counting contract (returns None) must not
    silently report 0 -- fall back to len(batch), matching every backend's
    behavior before this bead."""
    result = _batched_delete(_FakeColReturnsNone(), ["a", "b", "c"])
    assert result == 3


def test_batched_delete_sums_across_multiple_batches(monkeypatch):
    """The docstring's explicit requirement: batch_delete-shaped sum, not a
    single-batch capture."""
    monkeypatch.setattr("nexus.indexer._CHROMA_PAGE_SIZE", 2)
    col = _FakeColReportsActualCount(refuse={"id1", "id4"})
    ids = [f"id{i}" for i in range(5)]  # batches: [0,1] [2,3] [4]

    result = _batched_delete(col, ids)

    assert result == 3, f"5 requested, 2 refused (id1, id4) -> 3 actual, got {result!r}"
    assert col.calls == [["id0", "id1"], ["id2", "id3"], ["id4"]]


# ── _prune_misclassified_in_collection (_guarded_delete) ────────────────────


class _FakeCatalog:
    """Minimal manifest-table double (mirrors test_o8dil2's, kept local to
    this file so this bead's tests do not depend on a sibling bead's test
    module)."""

    def __init__(self, manifests: dict[str, list]):
        self.manifests: dict[str, list] = {k: list(v) for k, v in manifests.items()}
        self.replace_calls: list[tuple[str, list[dict]]] = []

    def get_manifests(self, doc_ids):
        return {d: self.manifests[d] for d in doc_ids if d in self.manifests}

    def get_manifest(self, doc_id):
        return self.manifests.get(doc_id, [])

    def atomic_manifest_replace(self, doc_id, chunks, **_kw):
        self.replace_calls.append((doc_id, list(chunks)))
        self.manifests[doc_id] = [
            SimpleNamespace(chash=c["chash"], position=c.get("position"))
            for c in chunks
        ]


class _FakeColAntiJoin:
    """Models the T3 chunk table AND the server's authoritative anti-join:
    ``.delete(ids)`` may legitimately refuse some ids (server-side, outside
    what the F8c manifest guard can see) and returns the ACTUAL count
    removed -- the real ``_ServiceCollectionStub`` contract post-fix."""

    def __init__(self, ids=(), doc_id_by_native_id: dict | None = None, refuse=()):
        self.ids: set[str] = set(ids)
        self.doc_id_by_native_id: dict[str, str] = dict(doc_id_by_native_id or {})
        self.refuse: set[str] = set(refuse)

    def get(self, ids=None, include=None, where=None, limit=None, offset=None):
        if ids is not None:
            return {"ids": [i for i in ids if i in self.ids]}
        if where is not None:
            wanted_docs = set(where.get("doc_id", {}).get("$in", []))
            matched = [
                i for i in self.ids
                if self.doc_id_by_native_id.get(i) in wanted_docs
            ]
            return {"ids": matched}
        return {"ids": sorted(self.ids)}

    def delete(self, ids):
        actual = 0
        for i in ids:
            if i in self.refuse:
                continue
            self.ids.discard(i)
            actual += 1
        return actual


def test_prune_misclassified_manifest_arm_reports_actual_server_count(tmp_path: Path):
    """Core non-vacuity case: the F8c guard clears BOTH chashes as safe to
    delete (their only manifest reference is the one being pruned), but the
    server's anti-join -- authoritative, and scoped catalog-wide, unlike the
    guard's per-call ``chash_to_docs`` -- still refuses one. Pre-fix, this
    returned 2 (``len(deletable)``); must now return 1 (the actual count)."""
    kept_chash = "d" * 64
    gone_chash = "e" * 64
    doc_id = "1.1.20"
    file_path = tmp_path / "partial.py"
    file_path.write_text("q = 1\n")

    col = _FakeColAntiJoin(ids=[kept_chash, gone_chash], refuse={kept_chash})
    catalog = _FakeCatalog({doc_id: [
        SimpleNamespace(chash=kept_chash, position=0),
        SimpleNamespace(chash=gone_chash, position=1),
    ]})

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
    )

    assert pruned == 1, (
        f"pruned must equal the server's ACTUAL deleted count (1), not the "
        f"2 chashes the F8c manifest guard approved as deletable -- got {pruned!r}"
    )
    assert gone_chash not in col.ids
    assert kept_chash in col.ids, "the server-refused id must still be present"


def test_prune_misclassified_legacy_arm_reports_actual_server_count(tmp_path: Path):
    """Same anti-join-visibility fix, exercised through the legacy
    doc_id-keyed (where-filter) arm rather than the manifest-chash arm --
    both share the ``_guarded_delete`` closure this bead fixes."""
    doc_id = "1.1.21"
    file_path = tmp_path / "legacy.py"
    file_path.write_text("r = 1\n")
    kept_id = "legacy-kept"
    gone_id = "legacy-gone"

    col = _FakeColAntiJoin(
        ids=[kept_id, gone_id],
        doc_id_by_native_id={kept_id: doc_id, gone_id: doc_id},
        refuse={kept_id},
    )
    catalog = _FakeCatalog({doc_id: []})  # no manifest rows -> legacy arm only

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
    )

    assert pruned == 1, f"expected the actual server count (1), got {pruned!r}"
    assert gone_id not in col.ids
    assert kept_id in col.ids


def test_prune_misclassified_partial_delete_logs_a_warning(tmp_path: Path):
    """A server-side anti-join refusal the F8c guard did not anticipate is a
    signal worth an operator's attention (it means the guard's per-call
    ``chash_to_docs`` ground truth missed a live reference) -- not silence."""
    kept_chash = "f" * 64
    gone_chash = "g" * 64
    doc_id = "1.1.22"
    file_path = tmp_path / "warn.py"
    file_path.write_text("s = 1\n")

    col = _FakeColAntiJoin(ids=[kept_chash, gone_chash], refuse={kept_chash})
    catalog = _FakeCatalog({doc_id: [
        SimpleNamespace(chash=kept_chash, position=0),
        SimpleNamespace(chash=gone_chash, position=1),
    ]})

    with structlog.testing.capture_logs() as logs:
        _prune_misclassified_in_collection(
            col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
        )

    warnings = [l for l in logs if l.get("log_level") == "warning"]
    assert any(
        "partial" in l["event"] and "anti_join" in l["event"] for l in warnings
    ), f"expected an anti-join partial-delete warning, got events: {[l['event'] for l in warnings]}"


def test_prune_misclassified_full_success_no_warning(tmp_path: Path):
    """Control: when the server deletes everything the guard approved (the
    common case), no partial-delete warning fires."""
    chash = "h" * 64
    doc_id = "1.1.23"
    file_path = tmp_path / "full.py"
    file_path.write_text("t = 1\n")

    col = _FakeColAntiJoin(ids=[chash])
    catalog = _FakeCatalog({doc_id: [SimpleNamespace(chash=chash, position=0)]})

    with structlog.testing.capture_logs() as logs:
        pruned = _prune_misclassified_in_collection(
            col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
        )

    assert pruned == 1
    warnings = [l for l in logs if l.get("log_level") == "warning"]
    assert not any("partial" in l["event"] and "anti_join" in l["event"] for l in warnings)
