# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ajpd: staleness, the export witness and the enrich title come from the
CATALOG DOCUMENT, never from ``content_hash`` on a shared T3 chunk row.

A T3 chunk row is one per (collection, chunk text) and its metadata merge is
last-writer-wins (``PgVectorRepository.mergeMetadata``), so ``content_hash`` on a
row is whichever document wrote that text last, not the file hash of the document
being asked about. The per-document hash of record is the RUNFENCE
(``catalog_documents.index_state`` + ``index_content_hash``). These tests drive the
scenarios of the census (T2 nexus/3ajpd-content-hash-census-2026-10-25) with
storage-free doubles: a collection double that FILTERS the way the real store does
(``where`` matches row metadata; ``ids`` selects rows), so a code path that still
asks T3 "which row has this content_hash" gets the real answer and fails here.

Storage-free on purpose: the python_unit tenant ceiling is a hard ratchet.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.doc_indexer import _index_document, index_pdf
from nexus.indexer_utils import (
    StalenessCache,
    apply_catalog_content_hashes,
    build_staleness_cache,
    check_staleness,
)

H_A = "a" * 64          # document A's file hash now
H_A_OLD = "0" * 64      # document A's file hash at its last completed index run
H_B = "b" * 64          # document B's file hash
CHASH = "c" * 64        # the one chunk both documents hold
COLLECTION = "docs__testowner__voyage-context-3__v1"
MODEL = "voyage-context-3"
DOC_A = "1.1.1"


class _SharedCol:
    """A T3 collection whose ``get`` filters like the store: ``where`` equality on row
    metadata, ``ids`` by chunk id. Records every call."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def get(self, where=None, ids=None, include=None, limit=None, **_kw):
        self.calls.append({"where": where, "ids": ids})
        picked = [
            (cid, m) for cid, m in self.rows.items()
            if (ids is None or cid in ids)
            and (where is None or all(m.get(k) == v for k, v in where.items()))
        ]
        if limit:
            picked = picked[:limit]
        return {"ids": [c for c, _ in picked], "metadatas": [m for _, m in picked]}


class _T3:
    def __init__(self, col: _SharedCol) -> None:
        self._col = col

    def get_or_create_collection(self, name: str) -> _SharedCol:
        return self._col


class _Cat:
    """Catalog reader double: document A's fence and its manifest."""

    def __init__(self, state, fence_hash: str, manifest_chashes: list[str]) -> None:
        self.entry = SimpleNamespace(
            tumbler=DOC_A, index_state=state, index_content_hash=fence_hash,
            physical_collection=COLLECTION, index_state_reported=True,
        )
        self.manifest = [SimpleNamespace(chash=c, position=i) for i, c in enumerate(manifest_chashes)]

    def by_source_uri(self, uri: str):
        return self.entry

    def by_file_path(self, owner, fp):
        return self.entry

    def curator_owner_tumbler_by_name(self, name: str):
        return "9.9"

    def by_doc_id(self, doc_id: str):
        return self.entry

    def get_manifest(self, doc_id: str):
        return self.manifest

    def close(self) -> None:
        pass


def _embed(docs, model):
    return ([[0.0]] * len(docs), model)


def _patch_catalog(monkeypatch, cat: _Cat) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: MagicMock(), raising=False)


def _run_gate(gate: str, monkeypatch, tmp_path: Path, cat: _Cat, col: _SharedCol, file_hash: str) -> bool:
    """Drive the real skip gate of ``_index_document`` (prose) or ``index_pdf`` and answer
    whether it went on to chunk (True = judged stale)."""
    monkeypatch.setattr("nexus.doc_indexer._sha256", lambda path: file_hash)
    _patch_catalog(monkeypatch, cat)
    t3 = _T3(col)
    if gate == "prose":
        chunk_fn = MagicMock(return_value=[])
        _index_document(
            tmp_path / "doc.md", "testowner", chunk_fn, t3,
            collection_name=COLLECTION, embed_fn=_embed, force=False,
        )
        return chunk_fn.call_count == 1
    pdf_chunks = MagicMock(return_value=[])
    monkeypatch.setattr("nexus.doc_indexer._pdf_chunks", pdf_chunks)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture for the nexus-3ajpd staleness gate\n")
    index_pdf(
        pdf, "testowner", t3, collection_name=COLLECTION, embed_fn=_embed,
        force=False, streaming="never",
    )
    return pdf_chunks.call_count == 1


GATES = pytest.mark.parametrize("gate", ["prose", "pdf"])


# ── (a) an unchanged document whose only chunk another document wrote last ─────


@GATES
def test_unchanged_document_whose_only_chunk_was_restamped_by_another_document_is_current(
    gate, monkeypatch, tmp_path,
) -> None:
    """Census row 3. A's single chunk is shared with B, and B wrote it last, so the row carries
    B's file hash. A is unchanged (fence 'complete' at A's hash): judged stale by the old
    ``where content_hash=H_A`` probe, which found no row, so A re-indexed on every run."""
    col = _SharedCol({CHASH: {"content_hash": H_B, "embedding_model": MODEL}})
    cat = _Cat("complete", H_A, [CHASH])
    assert _run_gate(gate, monkeypatch, tmp_path, cat, col, H_A) is False, "unchanged A must be skipped"
    assert all(c["where"] is None for c in col.calls), f"T3 was probed by metadata: {col.calls}"


# ── (b) a changed document is never current, whatever a chunk row carries ──────
# REGRESSION GUARDS: with a complete fence at the old hash the parent commit already read a
# changed document as stale, so these pass on the code before nexus-3ajpd. They keep the new
# gate from regressing; the fix itself is pinned by the NULL-fence and restamped-chunk tests.


@GATES
def test_changed_document_is_stale_even_when_a_chunk_row_carries_its_new_hash(
    gate, monkeypatch, tmp_path,
) -> None:
    """Regression guard (passes on the parent commit, which already compared the fence hash).
    A's file changed (fence 'complete' at the OLD hash) and a byte-identical new copy of its
    content, document B, has just stamped the shared chunk with the new hash."""
    col = _SharedCol({CHASH: {"content_hash": H_A, "embedding_model": MODEL}})
    cat = _Cat("complete", H_A_OLD, [CHASH])
    assert _run_gate(gate, monkeypatch, tmp_path, cat, col, H_A) is True


@GATES
def test_document_without_a_fence_is_stale_even_when_a_chunk_row_carries_its_hash(
    gate, monkeypatch, tmp_path,
) -> None:
    """The legacy (NULL fence) bucket. Before nexus-3ajpd a row stamped with the file's hash
    was enough to skip it: a changed A whose new bytes equal another document's was skipped
    with the old chunks in its manifest. With no fence there is no document-level record that
    a run completed at this hash, so it is indexed (and its completion stamps a fence)."""
    col = _SharedCol({CHASH: {"content_hash": H_A, "embedding_model": MODEL}})
    cat = _Cat(None, "", [CHASH])
    assert _run_gate(gate, monkeypatch, tmp_path, cat, col, H_A) is True


@GATES
@pytest.mark.parametrize("state", ["indexing", "failed"])
def test_a_stranded_fence_is_stale_whatever_a_chunk_row_says(gate, state, monkeypatch, tmp_path) -> None:
    """Regression guard (passes on the parent commit: 'indexing' and 'failed' were already stale)."""
    col = _SharedCol({CHASH: {"content_hash": H_A, "embedding_model": MODEL}})
    cat = _Cat(state, H_A, [CHASH])
    assert _run_gate(gate, monkeypatch, tmp_path, cat, col, H_A) is True


# ── the presence-and-model probe ───────────────────────────────────────────────


@GATES
def test_a_complete_document_with_a_stale_model_is_stale(gate, monkeypatch, tmp_path) -> None:
    """Regression guard (passes on the parent commit): the model compare survives the rewrite."""
    col = _SharedCol({CHASH: {"content_hash": H_A, "embedding_model": "an-older-model"}})
    cat = _Cat("complete", H_A, [CHASH])
    assert _run_gate(gate, monkeypatch, tmp_path, cat, col, H_A) is True


@GATES
def test_a_complete_document_with_an_empty_manifest_is_stale(gate, monkeypatch, tmp_path) -> None:
    """Nothing of the document's own to find in T3: it is indexed, not skipped on someone else's rows."""
    col = _SharedCol({CHASH: {"content_hash": H_A, "embedding_model": MODEL}})
    cat = _Cat("complete", H_A, [])
    assert _run_gate(gate, monkeypatch, tmp_path, cat, col, H_A) is True


@GATES
def test_a_complete_document_whose_manifest_chunk_is_not_in_the_collection_is_stale(
    gate, monkeypatch, tmp_path,
) -> None:
    other = "d" * 64
    col = _SharedCol({other: {"content_hash": H_A, "embedding_model": MODEL}})
    cat = _Cat("complete", H_A, [CHASH])
    assert _run_gate(gate, monkeypatch, tmp_path, cat, col, H_A) is True


# ── the repo staleness cache (nx index repo) ───────────────────────────────────


def _cache_col(rows: list[dict]) -> MagicMock:
    col = MagicMock(spec=["get", "get_all_metadata", "name"])
    col.name = "code__x__model-a__v1"
    col.get_all_metadata.return_value = {"ids": [f"c{i}" for i in range(len(rows))], "metadatas": rows}
    return col


def _build(rows: list[dict], owners: dict[str, list[str]]):
    cat = MagicMock()
    cat.docs_for_chashes.return_value = owners
    import nexus.catalog.factory as factory

    orig = factory.make_catalog_reader
    factory.make_catalog_reader = lambda *a, **k: cat
    try:
        return build_staleness_cache(_cache_col(rows))
    finally:
        factory.make_catalog_reader = orig


def test_the_cache_never_records_a_chunk_rows_hash_as_a_documents() -> None:
    """Census row 4. The only chunk of A is shared with B and B wrote it last: the row carries
    B's hash. The cache used to file that under A (``setdefault``), as A's hash."""
    cache = _build(
        [{"chunk_text_hash": CHASH, "content_hash": H_B, "embedding_model": "model-a"}],
        {CHASH: [DOC_A, "1.1.2"]},
    )
    assert cache.by_doc_id[DOC_A] == ("", "model-a")
    assert cache.by_doc_id["1.1.2"] == ("", "model-a")


def test_unchanged_document_with_a_complete_fence_is_current_though_its_only_chunk_was_restamped() -> None:
    """Regression guard (passes on the parent commit, whose overlay already carried the fence
    hash). Scenario (a) on the repo path: the overlay carries A's fence hash, whatever the row says."""
    cache = _build(
        [{"chunk_text_hash": CHASH, "content_hash": H_B, "embedding_model": "model-a"}],
        {CHASH: [DOC_A, "1.1.2"]},
    )
    assert apply_catalog_content_hashes(cache, {DOC_A: H_A}) == 1
    assert check_staleness(MagicMock(), "a.md", H_A, "model-a", doc_id=DOC_A, cache=cache) is True


def test_changed_document_is_stale_when_a_chunk_row_carries_its_new_hash() -> None:
    """Regression guard (passes on the parent commit, whose overlay already carried the fence
    hash). Scenario (b) on the repo path: A changed (fence 'complete' at the OLD hash); the shared
    chunk carries the NEW hash because byte-identical document B wrote it."""
    cache = _build(
        [{"chunk_text_hash": CHASH, "content_hash": H_A, "embedding_model": "model-a"}],
        {CHASH: [DOC_A, "1.1.2"]},
    )
    apply_catalog_content_hashes(cache, {DOC_A: H_A_OLD})
    assert check_staleness(MagicMock(), "a.md", H_A, "model-a", doc_id=DOC_A, cache=cache) is False


def test_changed_document_without_a_complete_fence_is_stale_when_a_chunk_row_carries_its_new_hash() -> None:
    """The case the overlay does not reach (legacy / NULL fence): the chunk row's hash used to
    stand in for the document's, and a changed A whose new bytes another document shares read
    current."""
    cache = _build(
        [{"chunk_text_hash": CHASH, "content_hash": H_A, "embedding_model": "model-a"}],
        {CHASH: [DOC_A]},
    )
    assert apply_catalog_content_hashes(cache, {}) == 0
    assert check_staleness(MagicMock(), "a.md", H_A, "model-a", doc_id=DOC_A, cache=cache) is False


def test_an_empty_content_hash_never_matches_the_cache_s_unknown_hash() -> None:
    """The cache's "no fence hash known" value is the empty string; an empty file hash must not
    read as a match for it."""
    cache = StalenessCache(by_doc_id={DOC_A: ("", "model-a")})
    assert check_staleness(MagicMock(), "a.md", "", "model-a", doc_id=DOC_A, cache=cache) is False


# ── check_staleness without a cache (was a collection-wide content_hash probe) ─


def test_uncached_check_staleness_ignores_other_documents_rows(monkeypatch) -> None:
    """Census row 5, the nexus-o19i0 shape: the uncached branch matched ``content_hash``
    collection-wide and ignored ``doc_id``, so a byte-identical file at a second path read as
    current. It now reads the document's own fence."""
    col = _SharedCol({CHASH: {"content_hash": H_A, "embedding_model": MODEL}})
    _patch_catalog(monkeypatch, _Cat("complete", H_A_OLD, [CHASH]))
    assert check_staleness(col, "a.md", H_A, MODEL, doc_id=DOC_A) is False
    assert all(c["where"] is None for c in col.calls), col.calls


def test_uncached_check_staleness_is_current_from_the_fence_and_an_own_chunk(monkeypatch) -> None:
    col = _SharedCol({CHASH: {"content_hash": H_B, "embedding_model": MODEL}})
    _patch_catalog(monkeypatch, _Cat("complete", H_A, [CHASH]))
    assert check_staleness(col, "a.md", H_A, MODEL, doc_id=DOC_A) is True


# ── (c) the export witness ─────────────────────────────────────────────────────


def _group(
    records: list[tuple[int, str | None]], *, key_registered: bool | None | list[bool | None],
    key: str = H_A, rows: int = 2,
) -> dict:
    """One owner group built record by record the way the import's prepass builds it. A record
    is (position, the chunk row's content_hash or None). ``key_registered`` is the value every
    record's owner carries (None: the field is absent, an old bundle), or a list with one value
    per record for a group whose records disagree."""
    from nexus.exporter import _locate_owner_group, _note_source_evidence

    groups: dict[str, dict] = {}
    per_record = key_registered if isinstance(key_registered, list) else [key_registered] * len(records)
    assert len(per_record) == len(records)
    for (position, content_hash), registered in zip(records, per_record):
        owner = {"source_uri": "file:///w.pdf", "title": "w", "content_type": "pdf",
                 "index_content_hash": key, "manifest_rows": rows}
        if registered is not None:
            owner["key_registered"] = registered
        group, pos = _locate_owner_group(
            groups, {**owner, "position": position}, fallback_source_uri="", fallback_title="",
            fallback_content_type="pdf", target_collection="docs__t__bge-base-en-v15-768__v1")
        _note_source_evidence(group, {} if content_hash is None else {"content_hash": content_hash}, pos)
    return groups["file:///w.pdf"]


def test_export_witness_survives_chunks_another_document_wrote_last() -> None:
    """Census row 9. Every chunk of the exported document was restamped by other documents, so
    no record carries the key as its ``content_hash``; the exporting catalog's own record (the
    document's registration holds the same hash) still witnesses it."""
    from nexus.exporter import _whole_source

    restamped = [(0, H_B), (1, H_B)]
    assert _whole_source(_group(restamped, key_registered=True)) == (H_A, 2)


def test_export_witness_needs_the_catalog_record_and_the_whole_manifest() -> None:
    """Regression guard (passes on the parent commit, which already refused an old bundle and a
    short record count)."""
    from nexus.exporter import _whole_source

    old_bundle = _group([(0, H_B), (1, H_B)], key_registered=None)   # no field at all
    assert _whole_source(old_bundle) is None
    one_short = _group([(0, H_B)], key_registered=True, rows=2)
    assert _whole_source(one_short) is None, "the record count still has to equal the manifest rows"


def test_export_witness_is_withdrawn_by_one_record_without_the_catalog_record() -> None:
    """The rule the comment states: ``key_registered`` counts only when EVERY record of the group
    carries it, whatever order they arrive in. A merged bundle can hold a document whose records
    disagree; the first record registered must not leave a sticky witness behind. Fails on
    596f2249c, where the witness was latched when the first record was seen."""
    from nexus.exporter import _whole_source

    restamped = [(0, H_B), (1, H_B)]
    assert _whole_source(_group(restamped, key_registered=[True, False])) is None
    assert _whole_source(_group(restamped, key_registered=[False, True])) is None
    assert _whole_source(_group(restamped, key_registered=[True, None])) is None
    # The chunk evidence is independent of it: one chunk carrying the key still witnesses.
    assert _whole_source(_group([(0, H_B), (1, H_A)], key_registered=[True, False])) == (H_A, 2)


def test_a_restamped_document_without_catalog_evidence_gets_no_key_never_a_wrong_one() -> None:
    """Regression guard (passes on the parent commit). A chunk row's hash is "some file has this
    hash": a key no row carries is not witnessed, so the document keeps the unique key (no
    same-as) rather than a key nothing vouches for."""
    from nexus.exporter import _whole_source

    assert _whole_source(_group([(0, H_B), (1, H_B)], key_registered=False)) is None
    assert _whole_source(_group([(0, H_A), (1, H_B)], key_registered=False)) == (H_A, 2)


def test_export_ships_key_registered_from_the_catalog_document() -> None:
    from nexus.exporter import _resolve_export_owners

    def entry(index_hash: str, meta_hash: str | None, state: str = "complete"):
        return SimpleNamespace(
            physical_collection=COLLECTION, source_uri="file:///x.pdf", title="x",
            content_type="pdf", index_state=state, index_content_hash=index_hash,
            meta={} if meta_hash is None else {"content_hash": meta_hash},
        )

    def owners_for(e) -> dict:
        reader = MagicMock()
        reader.docs_for_chashes.return_value = {CHASH: ["1.1.1"]}
        reader.get_manifests.return_value = {"1.1.1": [SimpleNamespace(chash=CHASH, position=0)]}
        reader.resolve_many.return_value = {"1.1.1": e}
        return _resolve_export_owners(reader, [CHASH], COLLECTION)[CHASH]

    assert owners_for(entry(H_A, H_A))["key_registered"] is True
    assert owners_for(entry(H_A, None))["key_registered"] is False          # a PDF registers without a file hash
    assert owners_for(entry(H_A, H_B))["key_registered"] is False           # registration moved on
    assert owners_for(entry(H_A, H_A, state="indexing"))["key_registered"] is False
    assert owners_for(entry(H_A, H_A, state="indexing"))["index_content_hash"] == ""


# ── (d) enrich selects chunks by the catalog Document that holds them ─────────
#
# nexus-6pfgb.21 replaced the title-group lookup this section pinned (nexus-g276c,
# nexus-3ajpd): the unit is now the Document, whose catalog title IS the lookup title and whose
# manifest names its chunks. The helper below is the one place enrich asks "who holds these?".

H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64


def _holders_reader(rows, entries, *, manifests=None):
    """A reader whose chash-positions route answers *rows* (``None`` when *manifests* is given:
    an engine without the route), and whose manifests answer the fallback."""
    return SimpleNamespace(
        chash_positions=lambda chashes: (
            None if manifests is not None
            else [r for r in rows if r["chash"] in set(chashes)]
        ),
        docs_and_manifests_for_chashes=lambda chashes: ({}, manifests or {}),
        resolve_many=MagicMock(side_effect=lambda ts: {t: entries[t] for t in ts if t in entries}),
        close=lambda: None,
    )


def _row(chash, doc, collection=COLLECTION):
    return {"chash": chash, "doc_id": doc, "position": 0, "chunk_count": 1, "collection": collection}


def test_enrich_holders_come_from_the_manifest_in_this_collection(monkeypatch) -> None:
    from nexus.commands.enrich import _catalog_documents_for_chunks

    reader = _holders_reader(
        [
            _row(H1, "1.1.1"), _row(H2, "1.1.1"), _row(H2, "1.1.2"),
            _row(H2, "9.9.9", "docs__other__m__v1"),   # same text, another collection
        ],
        {
            "1.1.1": SimpleNamespace(title="A: the full title"),
            "1.1.2": SimpleNamespace(title="B: another paper"),
        },
    )
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader, raising=False)

    owners, entries = _catalog_documents_for_chunks(COLLECTION, [H1, H2, H3])

    assert owners == {H1: ["1.1.1"], H2: ["1.1.1", "1.1.2"]}   # H3 is held by nobody
    assert sorted(entries) == ["1.1.1", "1.1.2"]
    assert entries["1.1.1"].title == "A: the full title"
    reader.resolve_many.assert_called_once_with(["1.1.1", "1.1.2"])


def test_enrich_holders_on_an_engine_whose_rows_carry_no_collection(monkeypatch) -> None:
    """Engine 0.1.164: chash-positions rows carry no collection, so the referencing manifests
    (which do) decide whether a document holds the chunk in THIS collection."""
    from nexus.commands.enrich import _catalog_documents_for_chunks

    here = SimpleNamespace(chash=H1, collection=COLLECTION)
    elsewhere = SimpleNamespace(chash=H1, collection="docs__other__m__v1")
    reader = _holders_reader(
        [], {"1.1.1": SimpleNamespace(title="A")},
        manifests={"1.1.1": [here], "9.9.9": [elsewhere]},
    )
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader, raising=False)

    owners, _entries = _catalog_documents_for_chunks(COLLECTION, [H1])

    assert owners == {H1: ["1.1.1"]}


def test_enrich_holders_never_ask_the_engine_about_an_id_that_is_not_a_chash(monkeypatch) -> None:
    from nexus.commands.enrich import _catalog_documents_for_chunks

    asked: list[list[str]] = []
    reader = _holders_reader([], {})
    reader.chash_positions = lambda chashes: asked.append(list(chashes)) or []
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader, raising=False)

    assert _catalog_documents_for_chunks(COLLECTION, ["chunk-1", "", H1]) == ({}, {})
    assert asked == [[H1]]


def test_enrich_holders_refuse_rather_than_guess_when_the_catalog_cannot_answer(monkeypatch) -> None:
    """The title-group fallback is the defect: no catalog, no enrichment, and nothing written."""
    import click

    from nexus.commands.enrich import _catalog_documents_for_chunks

    reader = _holders_reader([], {})

    def _boom(_chashes):
        raise RuntimeError("engine down")

    reader.chash_positions = _boom
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader, raising=False)
    with pytest.raises(click.ClickException, match="engine down.*nothing was changed"):
        _catalog_documents_for_chunks(COLLECTION, [H1])

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: None, raising=False)
    with pytest.raises(click.ClickException, match="not initialised"):
        _catalog_documents_for_chunks(COLLECTION, [H1])
