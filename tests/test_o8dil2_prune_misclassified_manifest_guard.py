# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o8dil.2 (RDR-191 F8c): ``_prune_misclassified_in_collection``'s two
arms must never delete a T3 chunk that is still LIVE-referenced by a
document's catalog manifest without also clearing that reference — doing so
produces a dangling ``document_chunks`` row (a row whose ``chash`` no chunk
anywhere still backs), invisible to every existing sweep (``purge_trash``
gates on a live chunk existing; ``nx t3 gc`` computes orphans in the OTHER
direction).

THE FIX (manifest-FIRST, then delete): before pruning a chash that a doc's
manifest currently lists, the chash's manifest row is removed via
``catalog.atomic_manifest_replace`` FIRST. Only once that write succeeds is
the T3 chunk itself deleted. An abort between the two steps therefore leaves,
at worst, an ORPHAN chunk (recoverable via ``nx t3 gc`` /
``_sweep_superseded_vectors``) — never a dangling manifest row. A chash with
NO live manifest reference (the common "legacy/seeded" shape exercised by
``test_indexer_e2e.py::test_migration_moves_prose_from_code_to_docs``) needs
no manifest action and is deleted exactly as before.

These tests use lightweight in-test doubles (``_FakeCol`` / ``_FakeCatalog``)
that model the T3 "chunk table" and the catalog "manifest table" together, so
the core assertion is a genuine ANTI-JOIN (does any surviving manifest row
reference a chash absent from the chunk table) — not a manifest row-count,
per F17.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.indexer import _prune_misclassified_in_collection


class _FakeCol:
    """Models ``col`` — the T3 "chunk table" for one physical collection."""

    def __init__(self, ids=(), doc_id_by_native_id: dict | None = None):
        self.ids: set[str] = set(ids)
        self.doc_id_by_native_id: dict[str, str] = dict(doc_id_by_native_id or {})
        self.deleted: list[str] = []
        self.delete_raises: Exception | None = None

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
        if self.delete_raises is not None:
            raise self.delete_raises
        for i in ids:
            self.ids.discard(i)
            self.deleted.append(i)


class _FakeCatalog:
    """Models ``catalog`` — the manifest table, keyed by doc_id."""

    def __init__(self, manifests: dict[str, list]):
        self.manifests: dict[str, list] = {k: list(v) for k, v in manifests.items()}
        self.replace_calls: list[tuple[str, list[dict]]] = []
        self.fail_replace_for: set[str] = set()

    def get_manifests(self, doc_ids):
        return {d: self.manifests[d] for d in doc_ids if d in self.manifests}

    def get_manifest(self, doc_id):
        return self.manifests.get(doc_id, [])

    def atomic_manifest_replace(self, doc_id, chunks, **_kw):
        if doc_id in self.fail_replace_for:
            raise RuntimeError("manifest write refused (test)")
        self.replace_calls.append((doc_id, list(chunks)))
        self.manifests[doc_id] = [
            SimpleNamespace(chash=c["chash"], position=c.get("position"))
            for c in chunks
        ]

    def all_chashes(self) -> set[str]:
        return {row.chash for rows in self.manifests.values() for row in rows}


def test_prune_removes_stale_manifest_reference_no_dangling_row(tmp_path: Path):
    """Core non-vacuity case (bead .2 acceptance #1): a reclassified doc's
    stale chash is pruned from the wrong collection AND its manifest
    reference is cleared in the same pass — no re-index required."""
    chash = "a" * 64
    doc_id = "1.1.9"
    file_path = tmp_path / "reclassified.py"
    file_path.write_text("x = 1\n")

    col = _FakeCol(ids=[chash])
    catalog = _FakeCatalog({doc_id: [SimpleNamespace(chash=chash, position=0)]})

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
    )

    assert pruned == 1
    assert chash not in col.ids, "the stale chunk must be gone from the wrong collection"
    # THE ANTI-JOIN (F17): no manifest row may reference a chash absent
    # from the chunk table. Not a row-count — an actual cross-check.
    assert chash not in catalog.all_chashes(), (
        f"a dangling manifest row survives: the manifest still references "
        f"chash {chash} which is no longer present anywhere"
    )


def test_abort_after_manifest_write_leaves_orphan_not_dangling(tmp_path: Path):
    """Acceptance #3: the abort-between-prune-and-write path. The manifest
    write is ordered BEFORE the T3 delete, so an abort mid-delete leaves an
    ORPHAN chunk (safe, GC-recoverable) rather than a dangling manifest row
    (the actual bug)."""
    chash = "b" * 64
    doc_id = "1.1.10"
    file_path = tmp_path / "aborted.py"
    file_path.write_text("y = 2\n")

    col = _FakeCol(ids=[chash])
    col.delete_raises = RuntimeError("simulated process abort mid-delete")
    catalog = _FakeCatalog({doc_id: [SimpleNamespace(chash=chash, position=0)]})

    with pytest.raises(RuntimeError, match="simulated process abort"):
        _prune_misclassified_in_collection(
            col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
        )

    assert chash not in catalog.all_chashes(), (
        "the manifest write must have already committed before the delete "
        "was even attempted"
    )
    assert chash in col.ids, (
        "the aborted delete must leave the chunk in place — an ORPHAN, "
        "which nx t3 gc can reclaim, never a dangling manifest row"
    )


def test_manifest_write_failure_refuses_the_delete(tmp_path: Path):
    """Acceptance #2 ('or refuse'): when the manifest write itself is
    refused, the arm must NOT delete the chunk either — status quo (both
    references intact), not a new orphan or a new dangling row."""
    chash = "c" * 64
    doc_id = "1.1.11"
    file_path = tmp_path / "refuse.py"
    file_path.write_text("z = 3\n")

    col = _FakeCol(ids=[chash])
    catalog = _FakeCatalog({doc_id: [SimpleNamespace(chash=chash, position=0)]})
    catalog.fail_replace_for.add(doc_id)

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
    )

    assert pruned == 0, "a refused manifest write must not be counted as pruned"
    assert chash in col.ids, "refuse means the chunk must NOT be deleted either"
    assert chash in catalog.all_chashes(), "the manifest reference must be untouched"


def test_id_with_no_live_manifest_reference_is_deleted_freely(tmp_path: Path):
    """Regression guard mirroring test_indexer_e2e.py::
    test_migration_moves_prose_from_code_to_docs: a legacy/seeded chunk
    whose native id was NEVER referenced by any manifest row is safe to
    delete outright via the doc_id-keyed (legacy) arm — no manifest action
    needed or attempted."""
    doc_id = "1.1.12"
    file_path = tmp_path / "seed.py"
    file_path.write_text("w = 4\n")
    unreferenced_id = "fake-legacy-chunk"

    col = _FakeCol(ids=[unreferenced_id], doc_id_by_native_id={unreferenced_id: doc_id})
    catalog = _FakeCatalog({doc_id: []})  # no manifest row references this native id

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
    )

    assert pruned == 1
    assert unreferenced_id not in col.ids
    assert catalog.replace_calls == [], (
        "an id with no live manifest reference must be deleted WITHOUT any "
        "manifest write — nothing to guard"
    )


# --- nexus-ch0gk (writer-routing coverage) ------------------------------
#
# Substantive-critique finding (Significant #2, T2
# nexus/nexus-ch0gk-critique-2026-08-11): the four tests above all call
# ``_prune_misclassified_in_collection`` WITHOUT ``catalog_writer=``, so
# they only ever exercise the "catalog_writer is None -> fall back to
# catalog" branch of ``_writer_source = catalog_writer if catalog_writer
# is not None else catalog``. A regression that ignored ``catalog_writer``
# entirely and always wrote through ``catalog`` would pass every test
# above green. ``_FakeWriter`` below is deliberately a DIFFERENT object
# from the reader ``_FakeCatalog`` (not a second reader-shaped double) so
# a misrouted call is visible as "landed on the wrong object", not just
# "landed somewhere".


class _FakeWriter:
    """Minimal double for an explicit catalog WRITER handle — models only
    ``atomic_manifest_replace`` (no ``get_manifests``/``get_manifest``),
    so this object could not stand in for the reader by accident; any
    write reaching it proves the writer path was actually taken."""

    def __init__(self):
        self.replace_calls: list[tuple[str, list[dict]]] = []

    def atomic_manifest_replace(self, doc_id, chunks, **_kw):
        self.replace_calls.append((doc_id, list(chunks)))


def test_manifest_write_routes_through_catalog_writer_when_provided(tmp_path: Path):
    """When ``catalog_writer`` is passed, ``atomic_manifest_replace`` must
    be invoked on the WRITER, never on the reader ``catalog`` handle."""
    chash = "f" * 64
    doc_id = "1.1.15"
    file_path = tmp_path / "writer_routed.py"
    file_path.write_text("v = 5\n")

    col = _FakeCol(ids=[chash])
    catalog = _FakeCatalog({doc_id: [SimpleNamespace(chash=chash, position=0)]})
    writer = _FakeWriter()

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code",
        catalog=catalog, catalog_writer=writer,
    )

    assert pruned == 1
    assert writer.replace_calls, (
        "atomic_manifest_replace must have been invoked on catalog_writer "
        "when one was provided"
    )
    assert catalog.replace_calls == [], (
        "the reader catalog handle must never receive the write when an "
        "explicit catalog_writer is available"
    )


def test_manifest_write_falls_back_to_catalog_when_no_writer_given(tmp_path: Path):
    """Symmetric control: with no ``catalog_writer``, the write must land
    on ``catalog`` itself (pre-existing fallback behaviour, made explicit
    here alongside the writer-routing test above)."""
    chash = "g" * 64
    doc_id = "1.1.16"
    file_path = tmp_path / "fallback_routed.py"
    file_path.write_text("v = 6\n")

    col = _FakeCol(ids=[chash])
    catalog = _FakeCatalog({doc_id: [SimpleNamespace(chash=chash, position=0)]})

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code", catalog=catalog,
    )

    assert pruned == 1
    assert catalog.replace_calls, (
        "with no catalog_writer, atomic_manifest_replace must fall back "
        "to the catalog handle"
    )
