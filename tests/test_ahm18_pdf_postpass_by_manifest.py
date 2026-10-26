# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ahm18: the streaming PDF post-pass selects its chunks by the document's own manifest.

A chunk row's ``content_hash`` is the last writer's. The post-pass used to select ``where
content_hash=H``, so it missed this PDF's chunks that another document had restamped and hit rows
this PDF does not own. It now reads ``doc_id -> get_manifest -> chash ids`` (one call) and updates
exactly those rows, and judges ``table_page`` by the manifest's own page labels where the engine
records them (``labels_written``), falling back to the chunk row's page for a legacy row.

That selection WRITES rows develop never wrote: a shared chunk another document restamped between this
PDF's upload and its post-pass (a retry or a resume) now takes this PDF's ``title``, ``source_author``
and ``extraction_method``, the last-writer-wins limitation until nexus-6pfgb.6 / nexus-6pfgb.27. The
write there is non-destructive and the tests below pin it: nothing is deleted (the co-owner's
``quality_gate_overridden`` survives, and this PDF's own flag is not set on it), no empty value is
written over a non-empty one, and ``table_page`` is set on such a row only where this document's own
manifest label judges it (``labels_written``), never from the page the co-owner wrote on a legacy row.
A row still carrying this PDF's hash is written in full, as before. A real run whose selection is empty
although chunks were uploaded fails the post-pass. The post-pass against the real engine is
``tests/integration/test_rdr223_pdf_journey.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from nexus.catalog.types import ManifestRow
from nexus.pdf_extractor import ExtractionResult
from nexus.pipeline_stages import _enrich_metadata_from_extraction, _update_chunk_metadata

_COLLECTION = "docs__ahm18__bge-base-en-v15-768__v1"
_H_A = "a" * 64
_H_B = "b" * 64


class _Store:
    """A T3 double that holds rows keyed by chash and applies the engine's merge on update."""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = {k: dict(v) for k, v in rows.items()}
        self.get_calls: list[dict[str, Any]] = []
        self.updates: list[tuple[list[str], list[dict], list[str]]] = []

    # the collection handle
    def get(self, *, ids=None, where=None, include=None, limit=None, offset=0):
        self.get_calls.append({"ids": ids, "where": where, "limit": limit})
        if ids is not None:
            found = [i for i in ids if i in self.rows]
        else:
            (key, value), = where.items()
            found = [i for i, m in self.rows.items() if m.get(key) == value]
            found = found[offset:offset + (limit or len(found))]
        out: dict[str, Any] = {"ids": found}
        if include and "metadatas" in include:
            out["metadatas"] = [dict(self.rows[i]) for i in found]
        return out

    # the vector client
    def update_chunks(self, collection, ids, metadatas, *, delete_keys=None):
        assert collection == _COLLECTION
        self.updates.append((list(ids), [dict(m) for m in metadatas], list(delete_keys or [])))
        for cid, md in zip(ids, metadatas):
            if cid in self.rows:
                self.rows[cid].update(md)
                for k in delete_keys or []:
                    self.rows[cid].pop(k, None)


class _Reader:
    def __init__(self, manifests: dict[str, list[ManifestRow]], calls: list[str], *, boom: bool = False) -> None:
        self._manifests, self._calls, self._boom = manifests, calls, boom

    def get_manifest(self, doc_id: str) -> list[ManifestRow]:
        self._calls.append(doc_id)
        if self._boom:
            raise RuntimeError("catalog down")
        return self._manifests[doc_id]

    def close(self) -> None:
        pass


@pytest.fixture()
def manifests(monkeypatch):
    """doc_id -> manifest rows; ``calls`` records every get_manifest."""
    state: dict[str, Any] = {"by_doc": {}, "calls": [], "boom": False}

    def _make():
        return _Reader(state["by_doc"], state["calls"], boom=state["boom"])

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", _make)
    return state


def _rows(*chashes: str, **labels: Any) -> list[ManifestRow]:
    return [ManifestRow(position=i, chash=c, collection=_COLLECTION, **labels) for i, c in enumerate(chashes)]


def _extraction(**meta: Any) -> ExtractionResult:
    return ExtractionResult(
        text="body", metadata={"page_count": 3, "pdf_author": "Ann A.", "extraction_method": "docling",
                              "docling_title": "A Title", **meta})


def test_the_post_pass_updates_every_chunk_of_the_manifest_and_none_it_does_not_own(manifests) -> None:
    """A owns a1, a2 and the shared s; B owns b1 and s, and wrote s last (the row carries H_B). b_x is a
    row only B owns that carries A's hash (a stale restamp). Selection by content_hash would update
    a1, a2 and b_x, and miss s."""
    store = _Store({
        "a1": {"content_hash": _H_A}, "a2": {"content_hash": _H_A},
        "s": {"content_hash": _H_B, "title": "B Paper"},
        "b1": {"content_hash": _H_B, "title": "B Paper"},
        "b_x": {"content_hash": _H_A, "title": "B Paper"},
    })
    manifests["by_doc"]["doc-a"] = _rows("a1", "a2", "s")

    ok = _enrich_metadata_from_extraction(
        _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION, doc_id="doc-a")

    assert ok is True
    assert manifests["calls"] == ["doc-a"], "one get_manifest for the document"
    assert [c["where"] for c in store.get_calls] == [None], "read by id, never selected by where"
    ids = [i for u in store.updates for i in u[0]]
    metas = [m for u in store.updates for m in u[1]]
    assert sorted(ids) == ["a1", "a2", "s"], "exactly the manifest's chunks, the shared one included"
    assert all(m["title"] == "A Title" and m["extraction_method"] == "docling" for m in metas)
    assert store.rows["b_x"]["title"] == "B Paper", "a row A does not own is untouched"
    assert store.rows["b1"]["title"] == "B Paper"
    assert store.rows["s"]["title"] == "A Title"


def test_a_manifest_row_of_another_collection_is_not_selected(manifests) -> None:
    store = _Store({
        "a1": {"content_hash": _H_A}, "elsewhere": {"content_hash": _H_A}, "no_stamp": {"content_hash": _H_A},
    })
    manifests["by_doc"]["doc-a"] = [
        ManifestRow(position=0, chash="a1", collection=_COLLECTION),
        ManifestRow(position=1, chash="elsewhere", collection="docs__other__bge-base-en-v15-768__v1"),
        ManifestRow(position=2, chash="a1", collection=_COLLECTION),
        ManifestRow(position=3, chash="no_stamp", collection=None),
    ]
    assert _enrich_metadata_from_extraction(
        _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION, doc_id="doc-a") is True
    (ids, _, _), = store.updates
    assert ids == ["a1", "no_stamp"], "other collection excluded; a repeated chash collapses; a collection-less row is this one"


def test_a_catalog_read_failure_fails_the_post_pass_so_the_buffer_is_kept(manifests) -> None:
    store = _Store({"a1": {"content_hash": _H_A}})
    manifests["by_doc"]["doc-a"] = _rows("a1")
    manifests["boom"] = True
    assert _enrich_metadata_from_extraction(
        _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION, doc_id="doc-a") is False
    assert store.updates == []
    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, lambda m: True, doc_id="doc-a") is False


def test_a_dry_run_with_no_document_keeps_the_throwaway_stores_content_hash_selection(manifests) -> None:
    store = _Store({"a1": {"content_hash": _H_A}, "o": {"content_hash": _H_B}})
    assert _enrich_metadata_from_extraction(
        _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION) is True
    assert manifests["calls"] == []
    (ids, _, _), = store.updates
    assert ids == ["a1"]


# ── a row another document restamped is written non-destructively ──────────────────────────────────
#
# A retry of this PDF's post-pass can find a shared chunk another document (B) restamped since this
# PDF's upload: the row's content_hash is B's. Selection by manifest writes it (develop did not), so the
# write must be a replacement of values only: nothing deleted, nothing blanked, no tag judged by B's page.


def _shared_with_b(**extra: Any) -> _Store:
    """A finished co-owner B wrote the shared chunk s last; A also owns ``own`` (its hash)."""
    return _Store({
        "own": {"content_hash": _H_A, "quality_gate_overridden": True, "source_author": "old", "extraction_method": "old",
                "page_number": 3, "chunk_type": "text"},
        "s": {"content_hash": _H_B, "title": "B Paper", "source_author": "B Author",
              "extraction_method": "mineru+docling-degraded", "quality_gate_overridden": True,
              "page_number": 3, "chunk_type": "text", **extra},
    })


def test_a_retry_never_deletes_the_audit_flag_a_co_owner_set(manifests) -> None:
    """B was indexed under --allow-degraded-extraction (its row carries quality_gate_overridden). A is
    healthy, so its enrichment asks for the key's removal; on A's own row that is right, on B's row it is
    a deletion of B's durable audit flag."""
    store = _shared_with_b()
    manifests["by_doc"]["doc-a"] = _rows("own", "s")
    assert _enrich_metadata_from_extraction(
        _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION, doc_id="doc-a") is True
    assert store.rows["s"]["quality_gate_overridden"] is True, "the co-owner's flag survives"
    assert store.rows["s"]["title"] == "A Title", "the value is replaced (the shared row is last-writer-wins)"
    assert "quality_gate_overridden" not in store.rows["own"], "A's own row: the stale flag is removed, as before"
    # one call per class of row, and the call that carries the co-owner's row asks for no deletion
    by_ids = {tuple(ids): dk for ids, _, dk in store.updates}
    assert by_ids[("own",)] == ["quality_gate_overridden"] and by_ids[("s",)] == []


def test_a_retry_never_blanks_a_value_a_co_owner_wrote(manifests) -> None:
    """The extractor reported no author and no method this time. On A's own row the empty string is
    written (as before); on the co-owner's row it would turn a non-empty value into ''."""
    store = _shared_with_b()
    manifests["by_doc"]["doc-a"] = _rows("own", "s")
    assert _enrich_metadata_from_extraction(
        _H_A, _extraction(pdf_author="", extraction_method=""), Path("/x/a.pdf"), store, store, _COLLECTION,
        doc_id="doc-a") is True
    assert store.rows["s"]["source_author"] == "B Author"
    assert store.rows["s"]["extraction_method"] == "mineru+docling-degraded"
    assert store.rows["s"]["title"] == "A Title"
    assert store.rows["own"]["source_author"] == "" and store.rows["own"]["extraction_method"] == ""


def test_a_degraded_retry_does_not_flag_a_co_owners_row(manifests) -> None:
    """A was accepted degraded; the shared row was last written by healthy B. The audit flag is A's
    document-level fact, written on A's own rows; B's row does not get it."""
    store = _Store({"own": {"content_hash": _H_A}, "s": {"content_hash": _H_B}})
    manifests["by_doc"]["doc-a"] = _rows("own", "s")
    assert _enrich_metadata_from_extraction(
        _H_A, _extraction(quality_gate_overridden=True), Path("/x/a.pdf"), store, store, _COLLECTION,
        doc_id="doc-a") is True
    assert store.rows["own"]["quality_gate_overridden"] is True
    assert "quality_gate_overridden" not in store.rows["s"]


def test_a_co_owners_page_does_not_tag_a_legacy_row_table_page(manifests) -> None:
    """engine 0.1.164: no labels, the page on a restamped row is B's. Page 3 is a table page of A, and
    B's chunk on page 3 is not one of B's: it must stay 'text'. A's own legacy row on page 3 is tagged."""
    store = _shared_with_b()
    manifests["by_doc"]["doc-a"] = _rows("own", "s")           # labels_written False
    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a") is True
    assert store.rows["s"]["chunk_type"] == "text"
    assert store.rows["own"]["chunk_type"] == "table_page"
    assert [ids for ids, _, _ in store.updates] == [["own"]]


def test_a_restamped_row_is_tagged_on_the_documents_own_label(manifests) -> None:
    """With labels the judgement is this document's own, whoever last wrote the row."""
    store = _shared_with_b()
    manifests["by_doc"]["doc-a"] = [
        ManifestRow(position=0, chash="s", collection=_COLLECTION, page_number=3, labels_written=True)]
    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a") is True
    assert store.rows["s"]["chunk_type"] == "table_page"
    assert store.rows["s"]["page_number"] == 3


def test_an_empty_value_is_never_written_to_a_restamped_row_by_the_table_pass(manifests) -> None:
    store = _shared_with_b()
    manifests["by_doc"]["doc-a"] = [
        ManifestRow(position=0, chash="s", collection=_COLLECTION, page_number=3, labels_written=True)]

    def _blank(meta: dict) -> bool:
        meta["title"] = ""
        meta["chunk_type"] = "table_page"
        return True

    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _blank, doc_id="doc-a") is True
    assert store.rows["s"]["title"] == "B Paper"
    assert store.rows["s"]["chunk_type"] == "table_page"


def test_a_dry_run_never_reads_the_catalog_even_when_handed_a_doc_id(manifests) -> None:
    """The carve-out keys on dry_run, not on an empty doc_id: a direct caller passing both must not read
    the real catalog from a preview, and selects in the throwaway store by content_hash."""
    store = _Store({"a1": {"content_hash": _H_A}, "o": {"content_hash": _H_B}})
    manifests["by_doc"]["doc-a"] = _rows("o")
    assert _enrich_metadata_from_extraction(
        _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION, doc_id="doc-a", dry_run=True) is True
    assert _update_chunk_metadata(
        store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a", dry_run=True) is True
    assert manifests["calls"] == []
    assert [ids for ids, _, _ in store.updates] == [["a1"]]


# ── an empty selection on a real run is loud ───────────────────────────────────────────────────────


@pytest.mark.parametrize("helper", ["enrich", "table"])
def test_a_selection_of_no_chunk_after_an_upload_fails_loud_and_keeps_the_buffer(manifests, helper) -> None:
    """A manifest of other-collection rows only: nothing this PDF uploaded to this collection is
    selected. Returning True would finish the PDF with no title, author or method and no signal."""
    store = _Store({"elsewhere": {"content_hash": _H_A}})
    manifests["by_doc"]["doc-a"] = [
        ManifestRow(position=0, chash="elsewhere", collection="docs__other__bge-base-en-v15-768__v1")]

    def _run(uploaded: int) -> bool:
        if helper == "enrich":
            return _enrich_metadata_from_extraction(
                _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION,
                doc_id="doc-a", chunks_uploaded=uploaded)
        return _update_chunk_metadata(
            store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a", chunks_uploaded=uploaded)

    with capture_logs() as logs:
        assert _run(5) is False
    (warn,) = [e for e in logs if e["event"] == "post_pass_selected_no_chunks"]
    assert warn["log_level"] == "warning" and warn["doc_id"] == "doc-a" and warn["chunks_uploaded"] == 5
    assert store.updates == []
    # a zero-chunk PDF has nothing to select: that is not this failure
    assert _run(0) is True


@pytest.mark.parametrize("helper", ["enrich", "table"])
def test_a_selection_of_no_live_chunk_after_an_upload_also_fails_loud(manifests, helper) -> None:
    """The manifest names chunks of this collection but no row is live."""
    store = _Store({})
    manifests["by_doc"]["doc-a"] = _rows("gone")
    if helper == "enrich":
        ok = _enrich_metadata_from_extraction(
            _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION, doc_id="doc-a", chunks_uploaded=1)
    else:
        ok = _update_chunk_metadata(
            store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a", chunks_uploaded=1)
    assert ok is False


# ── a failing read writes nothing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("helper", ["enrich", "table"])
def test_a_failing_second_batch_read_writes_nothing(manifests, helper) -> None:
    chashes = [f"c{i:04d}" for i in range(650)]
    store = _Store({c: {"content_hash": _H_A, "page_number": 1, "chunk_type": "text"} for c in chashes})
    manifests["by_doc"]["doc-a"] = _rows(*chashes)
    real_get = store.get

    def _flaky(**kw):
        if len(store.get_calls) >= 1:
            raise RuntimeError("t3 down")
        return real_get(**kw)

    store.get = _flaky  # type: ignore[method-assign]
    if helper == "enrich":
        ok = _enrich_metadata_from_extraction(
            _H_A, _extraction(), Path("/x/a.pdf"), store, store, _COLLECTION, doc_id="doc-a", chunks_uploaded=650)
    else:
        ok = _update_chunk_metadata(
            store, store, _COLLECTION, _H_A, _tag({1}), doc_id="doc-a", chunks_uploaded=650)
    assert ok is False
    assert store.updates == [], "every read precedes every write"


# ── table_page: judged by the document's own labels ────────────────────────────────────────────────


def _tag(table_pages: set[int]):
    def _fn(meta: dict) -> bool:
        if meta.get("page_number", 0) in table_pages and meta.get("chunk_type") != "table_page":
            meta["chunk_type"] = "table_page"
            return True
        return False

    return _fn


def test_table_page_follows_the_documents_own_page_not_the_shared_rows(manifests) -> None:
    """Page 3 is a table page of A. s is shared: A's manifest says page 3 (a table), the row says page 9
    (B's). t is shared: A's manifest says page 7, the row says page 3. Judged by the row, s is missed and
    t is tagged wrongly; judged by A's labels, s is tagged and t is not."""
    store = _Store({
        "s": {"content_hash": _H_B, "page_number": 9, "chunk_type": "text"},
        "t": {"content_hash": _H_B, "page_number": 3, "chunk_type": "text"},
        "own": {"content_hash": _H_A, "page_number": 3, "chunk_type": "text"},
        "b_only": {"content_hash": _H_A, "page_number": 3, "chunk_type": "text"},
    })
    manifests["by_doc"]["doc-a"] = [
        ManifestRow(position=0, chash="s", collection=_COLLECTION, page_number=3, labels_written=True),
        ManifestRow(position=1, chash="t", collection=_COLLECTION, page_number=7, labels_written=True),
        ManifestRow(position=2, chash="own", collection=_COLLECTION, page_number=3, labels_written=True),
    ]

    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a") is True

    (ids, metas, _), = store.updates
    assert sorted(ids) == ["own", "s"]
    assert all(m == {"chunk_type": "table_page"} for m in metas), "only the tag is written, never the overlaid labels"
    assert store.rows["s"]["page_number"] == 9, "the co-owner's page label is not rewritten"
    assert store.rows["t"]["chunk_type"] == "text"
    assert store.rows["b_only"]["chunk_type"] == "text", "a row the manifest does not name is untouched"


def test_table_page_falls_back_to_the_chunk_row_for_a_legacy_manifest_row(manifests) -> None:
    """engine 0.1.164 sends no label columns: labels_written is False and page_number None, so the
    chunk row's own page is the only record and is used."""
    store = _Store({
        "legacy": {"content_hash": _H_A, "page_number": 3, "chunk_type": "text"},
        "legacy_other": {"content_hash": _H_A, "page_number": 5, "chunk_type": "text"},
    })
    manifests["by_doc"]["doc-a"] = [
        ManifestRow(position=0, chash="legacy", collection=_COLLECTION),
        ManifestRow(position=1, chash="legacy_other", collection=_COLLECTION, page_number=None, labels_written=False),
    ]
    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a") is True
    (ids, _, _), = store.updates
    assert ids == ["legacy"]


def test_an_authoritative_unpaged_label_is_not_a_table_page(manifests) -> None:
    """labels_written with a null page means "not paged" (page 0), authoritative: the chunk row's page
    (a co-owner's) is never consulted."""
    store = _Store({"s": {"page_number": 3, "chunk_type": "text"}})
    manifests["by_doc"]["doc-a"] = [
        ManifestRow(position=0, chash="s", collection=_COLLECTION, page_number=None, labels_written=True)]
    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a") is True
    assert store.updates == []


def test_a_chunk_at_two_positions_is_tagged_when_either_is_a_table_page(manifests) -> None:
    for pages in ((1, 3), (3, 1)):  # the table position first and last: a first-wins or last-wins dedupe fails one
        store = _Store({"dup": {"chunk_type": "text"}})
        manifests["by_doc"]["doc-a"] = [
            ManifestRow(position=i, chash="dup", collection=_COLLECTION, page_number=p, labels_written=True)
            for i, p in enumerate(pages)
        ]
        assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _tag({3}), doc_id="doc-a") is True
        (ids, metas, _), = store.updates
        assert ids == ["dup"] and metas == [{"chunk_type": "table_page"}], pages


def test_chunk_rows_are_read_by_id_in_batches_of_at_most_300(manifests) -> None:
    chashes = [f"c{i:04d}" for i in range(650)]
    store = _Store({c: {"content_hash": _H_A, "page_number": 1, "chunk_type": "text"} for c in chashes})
    manifests["by_doc"]["doc-a"] = _rows(*chashes)

    assert _update_chunk_metadata(store, store, _COLLECTION, _H_A, _tag({1}), doc_id="doc-a") is True

    assert manifests["calls"] == ["doc-a"], "get_manifest once however many chunks"
    sizes = [len(c["ids"]) for c in store.get_calls]
    assert sizes == [300, 300, 50], sizes
    assert all(c["where"] is None for c in store.get_calls)
    assert sum(len(i) for i, _, _ in store.updates) == 650
