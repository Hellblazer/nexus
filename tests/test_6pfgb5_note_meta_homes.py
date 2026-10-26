# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.5: a note keeps its tags, category, ttl_days and source_agent in its catalog entry.

Two notes with identical text share one chunk row, and the row carries the last writer's keys. The
catalog entry is per note, so each entry's ``meta`` holds its own four values, written in the same
update as ``doc_id`` (``writer.update(meta=...)`` merges, so this works on engine 0.1.164).

Storage-free: a stateful catalog fake stands in at the factory seam, with the engine's merge
semantics for ``meta`` (``jsonb_concat``: a key sent overwrites, a key not sent stays) and live
lookups (chash to documents by ``meta.doc_id``, ghosts by exact title), so every branch of the store
hook (chash dedup, same-source re-put, ghost reconcile, mint) is reachable. The same
claim against the real engine is ``tests/integration/test_6pfgb5_note_meta_homes.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus.catalog import note_write
from nexus.catalog.store_hook import catalog_store_hook_tracked, restore_pre_call_stamp
from nexus.catalog.tumbler import Tumbler

_KEYS = ("tags", "category", "ttl_days", "source_agent")
_COLLECTION = "knowledge__6pfgb5__bge-base-en-v15-768__v1"
_CHASH = "ab" * 32


class _FakeCatalog:
    """Reader and writer in one: documents keyed by tumbler, ``meta`` merged on update."""

    def __init__(self) -> None:
        self.docs: dict[str, SimpleNamespace] = {}
        self.updates: list[tuple[str, dict]] = []
        self.registers: list[dict] = []

    # reads
    def docs_for_chashes(self, chashes):
        """chash -> tumblers of the documents whose ``meta.doc_id`` is that chash (the engine's
        reverse-manifest lookup, as the store hook uses it)."""
        return {c: [t for t, d in self.docs.items() if d.meta.get("doc_id") == c] for c in chashes}

    def resolve(self, tumbler):
        return self.docs.get(str(tumbler))

    def curator_owner_tumbler_by_name(self, name):
        return Tumbler.parse("1.1")

    def by_source_uri(self, uri):
        return next((d for d in self.docs.values() if d.source_uri == uri), None)

    def find_by_title_exact(self, title, content_type=None, owner=None, chunk_count=None):
        return [d for d in self.docs.values() if d.title == title and d.content_type == content_type
                and (chunk_count is None or d.chunk_count == chunk_count)]

    def seed_ghost(self, title: str, meta: dict | None = None) -> str:
        """A pre-existing catalog entry with no manifest and no source_uri (the RDR-145 ghosts)."""
        tumbler = f"1.1.{len(self.docs) + 1}"
        self.docs[tumbler] = SimpleNamespace(
            tumbler=Tumbler.parse(tumbler), title=title, content_type="knowledge", file_path="",
            physical_collection="", source_uri="", chunk_count=0, meta=dict(meta or {}))
        return tumbler

    # writes
    def register_owner(self, name, owner_type):
        return "1.1"

    def register(self, *, owner, title, content_type, physical_collection, meta, source_uri="", with_created=False):
        tumbler = f"1.1.{len(self.docs) + 1}"
        self.registers.append({"title": title, "meta": dict(meta)})
        self.docs[tumbler] = SimpleNamespace(
            tumbler=Tumbler.parse(tumbler), title=title, content_type=content_type, file_path="",
            physical_collection=physical_collection, source_uri=source_uri, chunk_count=1, meta=dict(meta))
        return (tumbler, True) if with_created else tumbler

    def update(self, tumbler, *, physical_collection=None, meta=None, source_uri=None):
        self.updates.append((str(tumbler), dict(meta or {})))
        doc = self.docs[str(tumbler)]
        doc.meta = {**doc.meta, **(meta or {})}

    def close(self):
        pass


@pytest.fixture()
def cat(monkeypatch):
    fake = _FakeCatalog()
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: fake)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: fake)
    return fake


@pytest.fixture()
def no_row_lookup(monkeypatch):
    """put_note sizes its pieces from the collection row's embedding model; that read is storage."""
    monkeypatch.setattr("nexus.corpus.embedding_model_row_preferred", lambda collection: "bge-base-en-v15-768")


def _note_keys(doc) -> dict:
    return {k: doc.meta[k] for k in _KEYS if k in doc.meta}


def test_two_notes_with_identical_text_each_keep_their_own_keys(cat):
    """The bead's pin: one chash, two titles, four different values each."""
    a, created_a = catalog_store_hook_tracked(
        "note-A", _CHASH, _COLLECTION, tags="alpha,one", category="design", ttl_days=7, source_agent="agent-a")
    b, created_b = catalog_store_hook_tracked(
        "note-B", _CHASH, _COLLECTION, tags="beta,two", category="finding", ttl_days=30, source_agent="agent-b")
    assert created_a and created_b and a != b
    assert _note_keys(cat.docs[a]) == {
        "tags": "alpha,one", "category": "design", "ttl_days": 7, "source_agent": "agent-a"}
    assert _note_keys(cat.docs[b]) == {
        "tags": "beta,two", "category": "finding", "ttl_days": 30, "source_agent": "agent-b"}
    assert cat.docs[a].meta["doc_id"] == cat.docs[b].meta["doc_id"] == _CHASH


def test_an_unset_key_is_never_sent(cat):
    """No tags, no category, no ttl (None; the hook also reads 0 as unset, though put_note rejects it), no
    agent: the entry is as it was before."""
    for title, ttl in (("bare", None), ("zero", 0)):
        tumbler, _ = catalog_store_hook_tracked(title, _CHASH, _COLLECTION, ttl_days=ttl)
        assert cat.docs[tumbler].meta == {"doc_id": _CHASH}
    only_tags, _ = catalog_store_hook_tracked("only-tags", _CHASH, _COLLECTION, tags="x")
    assert cat.docs[only_tags].meta == {"doc_id": _CHASH, "tags": "x"}


def test_a_reput_under_the_same_title_replaces_the_keys(cat):
    first, _ = catalog_store_hook_tracked(
        "reput", _CHASH, _COLLECTION, tags="old", category="design", ttl_days=7, source_agent="agent-a")
    second, created = catalog_store_hook_tracked(
        "reput", "cd" * 32, _COLLECTION, tags="new", category="finding", ttl_days=3, source_agent="agent-b")
    assert second == first and not created
    assert _note_keys(cat.docs[first]) == {
        "tags": "new", "category": "finding", "ttl_days": 3, "source_agent": "agent-b"}
    assert cat.docs[first].meta["doc_id"] == "cd" * 32


def test_a_reput_that_leaves_a_key_unset_retracts_it(cat):
    """A ttl_days of 7 that outlived a permanent re-put would expire the note."""
    first, _ = catalog_store_hook_tracked(
        "retract", _CHASH, _COLLECTION, tags="old", category="design", ttl_days=7, source_agent="agent-a")
    catalog_store_hook_tracked("retract", "cd" * 32, _COLLECTION, tags="kept")
    assert _note_keys(cat.docs[first]) == {"tags": "kept", "category": "", "ttl_days": 0, "source_agent": ""}


def test_a_reput_that_changes_nothing_adds_no_unset_keys(cat):
    first, _ = catalog_store_hook_tracked("plain", _CHASH, _COLLECTION)
    catalog_store_hook_tracked("plain", "cd" * 32, _COLLECTION)
    assert cat.docs[first].meta == {"doc_id": "cd" * 32}


def test_note_catalog_meta_returns_the_undo():
    from nexus.catalog.store_hook import note_catalog_meta  # noqa: PLC0415 - absent before nexus-6pfgb.5: import here so the other pins fail by assertion on a tree without it

    write, revert = note_catalog_meta(
        tags="new", ttl_days=3, prior={"tags": "old", "category": "design", "ttl_days": 7})
    assert write == {"tags": "new", "category": "", "ttl_days": 3}
    assert revert == {"tags": "old", "category": "design", "ttl_days": 7}
    write, revert = note_catalog_meta(tags="x", prior=None)
    assert write == {"tags": "x"} and revert == {"tags": ""}


def test_a_failed_reput_restores_the_old_keys_with_the_stamp(cat):
    """restore_pre_call_stamp puts the keys back in the update that puts the stamp back."""
    tumbler, _ = catalog_store_hook_tracked(
        "restore", _CHASH, _COLLECTION, tags="old", category="design", ttl_days=7, source_agent="agent-a")
    before = dict(cat.docs[tumbler].meta)
    pre_call: dict = {}
    again, _ = catalog_store_hook_tracked(
        "restore", "cd" * 32, _COLLECTION, tags="new", ttl_days=3, pre_call_doc_id_out=pre_call)
    assert again == tumbler and cat.docs[tumbler].meta != before

    restore_pre_call_stamp(
        tumbler, pre_call["doc_id"], "cd" * 32, pre_call.get("note_meta"), pre_call.get("note_meta_stamped"))

    assert cat.docs[tumbler].meta == before


def test_a_failed_reput_does_not_overwrite_a_newer_puts_keys(cat):
    """Same content, so the same doc_id: a failed put's restore must also see its own keys still
    in place, or it writes the old tags and ttl over a retag that landed after it."""
    tumbler, _ = catalog_store_hook_tracked(
        "cas", _CHASH, _COLLECTION, tags="old", category="design", ttl_days=7, source_agent="agent-a")
    failing: dict = {}
    catalog_store_hook_tracked(
        "cas", "cd" * 32, _COLLECTION, tags="from-failing", ttl_days=3, pre_call_doc_id_out=failing)
    # a concurrent put of the same content lands its own keys on the same entry
    catalog_store_hook_tracked("cas", "cd" * 32, _COLLECTION, tags="newer", ttl_days=30, source_agent="agent-b")
    newer = dict(cat.docs[tumbler].meta)
    assert newer["tags"] == "newer" and newer["doc_id"] == "cd" * 32

    restore_pre_call_stamp(
        tumbler, failing["doc_id"], "cd" * 32, failing.get("note_meta"), failing.get("note_meta_stamped"))

    assert cat.docs[tumbler].meta == newer


def test_a_same_title_same_text_reput_through_the_chash_dedup_replaces_and_retracts(cat):
    """The commonest edit: `nx store put` of the same text with other tags and no TTL. The live
    chash lookup resolves it onto the note's own entry (the dedup branch, not by_source_uri)."""
    first, _ = catalog_store_hook_tracked(
        "dedup", _CHASH, _COLLECTION, tags="old", category="design", ttl_days=7, source_agent="agent-a")
    assert cat.docs_for_chashes([_CHASH]) == {_CHASH: [first]}
    again, created = catalog_store_hook_tracked("dedup", _CHASH, _COLLECTION, tags="new", source_agent="agent-b")
    assert again == first and not created and len(cat.registers) == 1
    assert _note_keys(cat.docs[first]) == {"tags": "new", "category": "", "ttl_days": 0, "source_agent": "agent-b"}


def test_a_failed_same_text_reput_through_the_chash_dedup_restores_the_old_keys(cat):
    tumbler, _ = catalog_store_hook_tracked(
        "dedup-restore", _CHASH, _COLLECTION, tags="old", category="design", ttl_days=7, source_agent="agent-a")
    before = dict(cat.docs[tumbler].meta)
    pre_call: dict = {}
    catalog_store_hook_tracked("dedup-restore", _CHASH, _COLLECTION, tags="new", pre_call_doc_id_out=pre_call)
    assert cat.docs[tumbler].meta != before
    restore_pre_call_stamp(
        tumbler, pre_call["doc_id"], _CHASH, pre_call.get("note_meta"), pre_call.get("note_meta_stamped"))
    assert cat.docs[tumbler].meta == before


def test_a_ghost_reconcile_writes_the_notes_keys_and_retracts_what_the_ghost_held(cat):
    ghost = cat.seed_ghost("ghosted", meta={"ttl_days": 5, "tags": "stale"})
    out, created = catalog_store_hook_tracked(
        "ghosted", _CHASH, _COLLECTION, tags="fresh", category="design", source_agent="agent-g")
    assert out == ghost and not created and not cat.registers
    assert _note_keys(cat.docs[ghost]) == {
        "tags": "fresh", "category": "design", "ttl_days": 0, "source_agent": "agent-g"}
    assert cat.docs[ghost].meta["doc_id"] == _CHASH


def test_two_titles_with_one_chash_each_keep_their_own_keys_with_the_dedup_lookup_live(cat):
    """The lookup finds the other title's entry for the shared chash; the source_uri guard turns
    it down, and the second title mints its own entry instead of taking the first one's."""
    a, _ = catalog_store_hook_tracked("live-A", _CHASH, _COLLECTION, tags="alpha", ttl_days=7)
    assert cat.docs_for_chashes([_CHASH]) == {_CHASH: [a]}
    b, created = catalog_store_hook_tracked("live-B", _CHASH, _COLLECTION, tags="beta", ttl_days=30)
    assert created and b != a
    assert _note_keys(cat.docs[a]) == {"tags": "alpha", "ttl_days": 7}
    assert _note_keys(cat.docs[b]) == {"tags": "beta", "ttl_days": 30}
    # and a re-put of A with the shared text still lands on A, not on B
    again, _ = catalog_store_hook_tracked("live-A", _CHASH, _COLLECTION, tags="alpha2")
    assert again == a
    assert _note_keys(cat.docs[a]) == {"tags": "alpha2", "ttl_days": 0}
    assert _note_keys(cat.docs[b]) == {"tags": "beta", "ttl_days": 30}


def test_put_note_gives_each_of_two_identical_notes_its_own_keys(cat, no_row_lookup):
    """The bead's pin through the producer protocol (store_put, nx store put, memory promote and the
    recovery import all run put_note): same text, two titles, four different values each."""
    def put(title, **keys):
        with patch("nexus.catalog.note_write.write_note"), \
             patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"):
            out = note_write.put_note(
                content="6pfgb5 identical text under two titles", collection=_COLLECTION, title=title, **keys)
        assert out.catalog_doc_id, out.reason
        return out.catalog_doc_id

    a = put("pn-A", tags="alpha", category="design", ttl_days=7, source_agent="agent-a")
    b = put("pn-B", tags="beta", category="finding", ttl_days=30, source_agent="agent-b")
    assert a != b
    assert _note_keys(cat.docs[a]) == {
        "tags": "alpha", "category": "design", "ttl_days": 7, "source_agent": "agent-a"}
    assert _note_keys(cat.docs[b]) == {
        "tags": "beta", "category": "finding", "ttl_days": 30, "source_agent": "agent-b"}


def test_a_put_note_whose_write_is_refused_leaves_the_old_keys(cat, no_row_lookup):
    def put(content, **keys):
        return note_write.put_note(content=content, collection=_COLLECTION, title="refused", **keys)

    with patch("nexus.catalog.note_write.write_note"), \
         patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"):
        first = put("6pfgb5 refused one", tags="old", ttl_days=7).catalog_doc_id
    before = dict(cat.docs[first].meta)
    refusal = note_write.NoteWriteError(
        catalog_doc_id=first, collection=_COLLECTION, reason="refused", manifest_empty=False)
    with patch("nexus.catalog.note_write.write_note", side_effect=refusal), \
         patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"):
        out = put("6pfgb5 refused two", tags="new", category="x")
    assert out.status == note_write.NOT_LANDED
    meta = cat.docs[first].meta
    assert meta["doc_id"] == before["doc_id"]
    assert (meta["tags"], meta["ttl_days"], meta.get("category", "")) == ("old", 7, "")


def _refused(doc: str) -> note_write.NoteWriteError:
    return note_write.NoteWriteError(catalog_doc_id=doc, collection=_COLLECTION, reason="refused", manifest_empty=True)


def _put(content: str, title: str = "legs", **keys):
    return note_write.put_note(content=content, collection=_COLLECTION, title=title, **keys)


def test_a_minted_row_that_cannot_be_rolled_back_loses_the_failed_puts_keys(cat, no_row_lookup):
    """A refused first put whose rollback also fails leaves the row; it must not keep the keys of a
    note that never landed (the stamp goes back to '' and so do the keys)."""
    with patch("nexus.catalog.store_hook.rollback_minted_catalog_entry", return_value=False), \
         patch("nexus.catalog.note_write.write_note", side_effect=_refused("1.1.1")), \
         patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"):
        out = _put("6pfgb5 minted refused", tags="never", category="x", ttl_days=7, source_agent="agent-m")
    assert out.status == note_write.NOT_LANDED and out.minted
    (doc,) = cat.docs.values()
    assert doc.meta == {"doc_id": "", "tags": "", "category": "", "ttl_days": 0, "source_agent": ""}


def test_a_minted_row_that_cannot_be_rolled_back_after_an_unexpected_error_loses_the_keys(cat, no_row_lookup):
    with patch("nexus.catalog.store_hook.rollback_minted_catalog_entry", return_value=False), \
         patch("nexus.catalog.note_write.write_note", side_effect=RuntimeError("boom")), \
         patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"), \
         pytest.raises(RuntimeError, match="boom"):
        _put("6pfgb5 minted boom", tags="never", ttl_days=7)
    (doc,) = cat.docs.values()
    assert doc.meta == {"doc_id": "", "tags": "", "ttl_days": 0}


def test_a_put_note_that_fails_unexpectedly_leaves_the_old_keys(cat, no_row_lookup):
    """The generic-exception leg of a re-put, as the refusal leg above."""
    with patch("nexus.catalog.note_write.write_note"), \
         patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"):
        first = _put("6pfgb5 boom one", title="boom", tags="old", ttl_days=7).catalog_doc_id
    before = dict(cat.docs[first].meta)
    with patch("nexus.catalog.note_write.write_note", side_effect=RuntimeError("boom")), \
         patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"), \
         pytest.raises(RuntimeError, match="boom"):
        _put("6pfgb5 boom two", title="boom", tags="new", category="x")
    meta = cat.docs[first].meta
    assert meta["doc_id"] == before["doc_id"]
    assert (meta["tags"], meta["ttl_days"], meta.get("category", "")) == ("old", 7, "")
