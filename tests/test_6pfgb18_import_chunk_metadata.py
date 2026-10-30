# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 nexus-6pfgb.18: an ``.nxexp`` import sends a chunk row's own keys, not the export row's
document keys.

The engine-backed proof (a target holding document X, a file for document Y sharing a chunk with it;
Y's import leaves X's title, tags and line span alone) is in
``test_z0o2p19_nxexp_import_combined_write``. These tests pin the key rule against the key table, so
a key class added to or moved in the table moves the import with it, and the shared-row decision
without an engine.
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus.exporter import _import_chunk_metadata, _OwnerImport, _PageRec
from nexus.tables import chunk_metadata_keys as ckm

_COLL = "code__x__bge-base-en-v15-768__v1"


def _every_key_row() -> dict:
    return {r.key: f"v-{r.key}" for r in ckm.rows() if r.kind == "metadata-key"} | {"made_up_cargo": 1}


def test_an_unshared_row_keeps_chunk_and_position_keys_only():
    got = _import_chunk_metadata(_every_key_row(), shared=False)
    assert set(got) == ckm.keys_of("CHUNK") | ckm.keys_of("POSITION")
    assert len(got) >= 9, "the key table examined too little to mean anything"


def test_a_shared_row_keeps_chunk_keys_only():
    got = _import_chunk_metadata(_every_key_row(), shared=True)
    assert set(got) == ckm.keys_of("CHUNK") == {"chunk_text_hash", "embedding_model"}


def test_no_document_legacy_or_row_key_ever_goes_out():
    for shared in (False, True):
        got = _import_chunk_metadata(_every_key_row(), shared=shared)
        assert not set(got) & (ckm.keys_of("DOCUMENT") | ckm.keys_of("LEGACY") | ckm.keys_of("ROW")), got
        assert "made_up_cargo" not in got


def test_a_none_row_is_an_empty_payload():
    assert _import_chunk_metadata(None, shared=False) == {}  # type: ignore[arg-type]


class _Reader:
    """Catalog reader double: which documents hold which chash, and where each document lives."""

    def __init__(self, held: dict[str, list[str]], collection_of: dict[str, str]) -> None:
        self.held, self.collection_of = held, collection_of
        self.calls: list[str] = []

    def docs_for_chashes(self, chashes):
        self.calls.append("docs_for_chashes")
        return {c: list(self.held.get(c, [])) for c in chashes}

    def resolve_many(self, tumblers):
        self.calls.append("resolve_many")
        return {t: SimpleNamespace(physical_collection=self.collection_of[t]) for t in tumblers}


def _rec(chash: str) -> _PageRec:
    return _PageRec(rec_id=chash, doc="t", meta={}, emb=[0.0], group={}, position=0)


def _importer(reader: _Reader) -> _OwnerImport:
    imp = _OwnerImport(
        db=None, collection_name=_COLL, hooks=None, embedding_model="m", file_hash="h", skip_existing=False)
    imp._reader = reader
    return imp


def test_shared_means_another_live_document_of_this_collection_or_two_documents_of_the_page():
    reader = _Reader(
        held={"c-other": ["1.1.9"], "c-elsewhere": ["1.1.8"], "c-self": ["1.1.1"]},
        collection_of={"1.1.9": _COLL, "1.1.8": "code__other__bge-base-en-v15-768__v1"},
    )
    page = {
        "1.1.1": [_rec("c-other"), _rec("c-elsewhere"), _rec("c-self"), _rec("c-fresh"), _rec("c-both")],
        "1.1.2": [_rec("c-both")],
    }
    got = _importer(reader)._shared_chashes(page)
    assert got == {"c-other", "c-both"}
    # one batched read of each kind for the page, never one per chunk
    assert reader.calls == ["docs_for_chashes", "resolve_many"]


def test_a_page_with_no_other_holder_resolves_nothing():
    reader = _Reader(held={"a": ["1.1.1"]}, collection_of={})
    assert _importer(reader)._shared_chashes({"1.1.1": [_rec("a"), _rec("b")]}) == set()
    assert reader.calls == ["docs_for_chashes"]
