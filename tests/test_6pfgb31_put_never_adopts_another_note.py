# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.31: a put never adopts, re-sources or re-keys another note.

The store hook's chash dedup reconciled a put onto any document in the collection holding the
chunk, unless the put had a title AND that document had a different non-empty ``source_uri``.
So an untitled put of a titled note's text adopted the titled note (the guard needs a title to
compare), and a put under a second title adopted a legacy note whose ``source_uri`` was empty.
Either way the note's ``source_uri`` was rewritten and the put's ``ttl_days``/``put_at`` stamped
on it, and ``nx store expire`` then retired a note its owner had made permanent.

A note's identity is (collection, title). Identical text under another title, or under none, is a
second document whose manifest shares the chunk (RDR-108). An untitled put reconciles only onto
an untitled document with the same text.

Storage-free: a stateful catalog fake at the factory seam, with the engine's lookups (chash to
documents by ``meta.doc_id``, exact source_uri, ghosts by exact title) and merge semantics for
``meta``. The same claims against the real engine are
``tests/integration/test_6pfgb31_put_never_adopts_another_note.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog.store_hook import catalog_store_hook_tracked
from nexus.catalog.tumbler import Tumbler

_COLLECTION = "knowledge__6pfgb31__bge-base-en-v15-768__v1"
_CHASH = "cd" * 32


class _FakeCatalog:
    """Reader and writer in one. ``update`` honours ``source_uri`` and ``meta`` like the engine."""

    def __init__(self) -> None:
        self.docs: dict[str, SimpleNamespace] = {}
        self.updates: list[tuple[str, dict]] = []

    def docs_for_chashes(self, chashes):
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

    def register_owner(self, name, owner_type):
        return "1.1"

    def register(self, *, owner, title, content_type, physical_collection, meta, source_uri="", with_created=False):
        tumbler = f"1.1.{len(self.docs) + 1}"
        self.docs[tumbler] = SimpleNamespace(
            tumbler=Tumbler.parse(tumbler), title=title, content_type=content_type, file_path="",
            physical_collection=physical_collection, source_uri=source_uri, chunk_count=1, meta=dict(meta))
        return (tumbler, True) if with_created else tumbler

    def update(self, tumbler, *, physical_collection=None, meta=None, source_uri=None):
        self.updates.append((str(tumbler), {"meta": dict(meta or {}), "source_uri": source_uri}))
        doc = self.docs[str(tumbler)]
        doc.meta = {**doc.meta, **(meta or {})}
        if source_uri is not None:
            doc.source_uri = source_uri

    def seed(self, title: str, *, source_uri: str, meta: dict | None = None, tumbler: str | None = None) -> str:
        """A live note holding ``_CHASH``; ``source_uri=""`` is a pre-nexus-sdp0u (legacy) row."""
        tumbler = tumbler or f"1.1.{len(self.docs) + 1}"
        self.docs[tumbler] = SimpleNamespace(
            tumbler=Tumbler.parse(tumbler), title=title, content_type="knowledge", file_path="",
            physical_collection=_COLLECTION, source_uri=source_uri, chunk_count=1,
            meta={"doc_id": _CHASH, **(meta or {})})
        return tumbler

    def close(self):
        pass


@pytest.fixture()
def cat(monkeypatch):
    fake = _FakeCatalog()
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: fake)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: fake)
    return fake


def _put(title: str, **keys):
    return catalog_store_hook_tracked(title, _CHASH, _COLLECTION, **keys)


def _unchanged(doc, *, title: str, source_uri: str, meta: dict) -> None:
    assert doc.title == title
    assert doc.source_uri == source_uri
    assert doc.meta == meta


def test_an_untitled_put_of_a_titled_notes_text_mints_its_own_document(cat):
    """Repro 1: the titled permanent note stays whole; the untitled put gets a document of its own."""
    uri = uri_for(_COLLECTION, "A")
    a = cat.seed("A", source_uri=uri, meta={"put_at": "2026-01-01T00:00:00+00:00"})
    before = dict(cat.docs[a].meta)

    tumbler, created = _put("", ttl_days=1)

    assert created and tumbler != a
    assert not [u for u in cat.updates if u[0] == a], "the titled note is never written"
    _unchanged(cat.docs[a], title="A", source_uri=uri, meta=before)
    assert "ttl_days" not in cat.docs[a].meta
    assert cat.docs[tumbler].title == "" and cat.docs[tumbler].meta["ttl_days"] == 1


def test_a_put_under_a_second_title_never_adopts_a_legacy_note_with_no_source_uri(cat):
    """Repro 2: a legacy note (empty source_uri, its own title) is not re-sourced or given the TTL."""
    a = cat.seed("legacy-A", source_uri="")
    before = dict(cat.docs[a].meta)

    tumbler, created = _put("other-title", ttl_days=1)

    assert created and tumbler != a
    assert not [u for u in cat.updates if u[0] == a]
    _unchanged(cat.docs[a], title="legacy-A", source_uri="", meta=before)
    assert cat.docs[tumbler].title == "other-title"
    assert cat.docs[tumbler].source_uri == uri_for(_COLLECTION, "other-title")


def test_a_titled_put_never_adopts_an_untitled_note(cat):
    """The other direction: an untitled note is not adopted by a titled put of its text."""
    a = cat.seed("", source_uri="")
    before = dict(cat.docs[a].meta)

    tumbler, created = _put("named", ttl_days=1)

    assert created and tumbler != a
    _unchanged(cat.docs[a], title="", source_uri="", meta=before)


def test_a_second_title_still_makes_a_second_document(cat):
    """nexus-enej7 stays: identical content under a second title is a second document."""
    uri_a = uri_for(_COLLECTION, "shared-A")
    a = cat.seed("shared-A", source_uri=uri_a)

    b, created = _put("shared-B")

    assert created and b != a
    assert cat.docs[a].source_uri == uri_a and cat.docs[a].title == "shared-A"
    assert cat.docs[b].source_uri == uri_for(_COLLECTION, "shared-B")


def test_a_same_title_reput_reconciles_onto_its_own_note(cat):
    uri = uri_for(_COLLECTION, "same")
    a = cat.seed("same", source_uri=uri)

    tumbler, created = _put("same", ttl_days=3)

    assert not created and tumbler == a
    assert cat.docs[a].meta["ttl_days"] == 3 and len(cat.docs) == 1


def test_a_same_title_reput_heals_a_legacy_note_with_no_source_uri(cat):
    """nexus-n90xg: the legacy note under THIS title is the same note; it gets its source_uri."""
    a = cat.seed("legacy", source_uri="")

    tumbler, created = _put("legacy")

    assert not created and tumbler == a
    assert cat.docs[a].source_uri == uri_for(_COLLECTION, "legacy") and len(cat.docs) == 1


def test_a_same_title_reput_finds_its_note_among_notes_sharing_the_text(cat):
    """Two titled notes share the chunk. The chash lookup is ambiguous, so it must still be
    the same note that is reached (by source_uri), never the other."""
    a = cat.seed("one", source_uri=uri_for(_COLLECTION, "one"))
    b = cat.seed("two", source_uri=uri_for(_COLLECTION, "two"))

    tumbler, created = _put("two", ttl_days=2)

    assert not created and tumbler == b
    assert "ttl_days" not in cat.docs[a].meta


def test_an_untitled_reput_reconciles_onto_the_untitled_note_not_a_titled_one(cat):
    """A titled note and an untitled one share the text. A second untitled put finds the
    untitled one (the titled note does not make the chash lookup ambiguous), so untitled
    puts of one text do not multiply documents."""
    titled = cat.seed("A", source_uri=uri_for(_COLLECTION, "A"))
    untitled = cat.seed("", source_uri="")

    tumbler, created = _put("", ttl_days=1)

    assert not created and tumbler == untitled
    assert "ttl_days" not in cat.docs[titled].meta
    assert len(cat.docs) == 2


def test_an_untitled_put_then_another_makes_one_untitled_document(cat):
    """With a titled note holding the text: the first untitled put mints, the second reuses it."""
    cat.seed("A", source_uri=uri_for(_COLLECTION, "A"))

    first, created_first = _put("")
    second, created_second = _put("")

    assert created_first and not created_second and first == second
    assert len(cat.docs) == 2


def test_two_untitled_documents_sharing_the_text_do_not_multiply_with_every_untitled_put(cat):
    """nexus-6pfgb.31 round 2: two untitled documents (concurrent first puts leave them) made every later
    untitled put ambiguous, so each minted another (2 -> 5 after 3 puts). A put reconciles onto the
    LOWEST tumbler instead, however the catalog orders the matches."""
    higher = cat.seed("", source_uri="", tumbler="1.1.10")
    lower = cat.seed("", source_uri="", tumbler="1.1.9")

    results = [_put("", ttl_days=1) for _ in range(3)]

    assert [t for t, _ in results] == [lower] * 3 and [c for _, c in results] == [False] * 3
    assert len(cat.docs) == 2
    assert cat.docs[lower].meta["ttl_days"] == 1 and "ttl_days" not in cat.docs[higher].meta


def test_a_titled_put_keeps_the_ambiguity_and_reaches_its_note_by_source_uri(cat):
    """Lowest-tumbler is for the untitled put only: a titled put that picked the lowest of two same-titled
    documents could stamp a source_uri the other already holds. It stays ambiguous and finds its note by URI."""
    uri = uri_for(_COLLECTION, "dup")
    legacy = cat.seed("dup", source_uri="", tumbler="1.1.9")
    keyed = cat.seed("dup", source_uri=uri, tumbler="1.1.10")

    tumbler, created = _put("dup", ttl_days=2)

    assert (tumbler, created) == (keyed, False)
    assert cat.docs[legacy].source_uri == "" and "ttl_days" not in cat.docs[legacy].meta


def test_an_untitled_put_never_blanks_the_source_uri_of_the_untitled_document_it_reconciles_onto(cat):
    """A non-empty source_uri on an untitled document (a path-keyed import) is not overwritten with ''."""
    a = cat.seed("", source_uri="chroma://elsewhere/doc")

    tumbler, created = _put("", ttl_days=1)

    assert (tumbler, created) == (a, False)
    assert cat.docs[a].source_uri == "chroma://elsewhere/doc"
    assert cat.docs[a].meta["ttl_days"] == 1
    assert all(u[1]["source_uri"] != "" for u in cat.updates), "no update wrote an empty source_uri"


def test_the_store_put_wire_text_states_the_untitled_contract():
    """The MCP tool text is what a caller reads: an empty title does NOT always make a new document."""
    from nexus.mcp import core  # noqa: PLC0415 — heavy import, only this test needs it

    tool = core.mcp._tool_manager._tools["store_put"]
    title_text = tool.parameters["properties"]["title"]["description"]
    claim = ("Empty registers an untitled document; a repeated untitled put of the same text reuses it, "
             "and it never adopts a titled note.")
    assert claim in " ".join(title_text.split())
    assert "always" not in " ".join(title_text.split()).lower().split("empty")[-1]
    doc = " ".join((tool.description or "").split())
    assert "always creates a new document" not in doc
    assert "never adopts a titled note" in doc
