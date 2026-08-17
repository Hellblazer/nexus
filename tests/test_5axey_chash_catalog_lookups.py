# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5axey: chash-appropriate catalog lookups.

``by_doc_id`` is a TUMBLER-only lookup on the engine (settled wji11
contract: tumbler is the only document identity) — it always mismatched
the chash-shaped ids passed by three call sites, silently breaking their
intended behavior:

  A1 catalog/store_hook.py    dedup in ``catalog_store_hook_tracked``
  A2 catalog/store_hook.py    ``store_delete_catalog_cleanup`` (delete-path)
  A4 commands/store.py        ``_reap_catalog_for_doc_ids`` (tombstone reap)

This file proves each now works via
``nexus.catalog.store_hook.resolve_knowledge_doc_for_chash`` (the
``docs_for_chashes``-backed replacement), and that the N>1 ambiguity case
is treated conservatively (no match, not an arbitrary pick).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests._catalog_fixture_ops import (
    ActiveCatalog,
    active_reader,
    count_documents,
    seed_manifest_chunks,
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _make_catalog(tmp_path: Path) -> ActiveCatalog:
    return ActiveCatalog()


def _chash(label: str) -> str:
    """A representative full-width (64 hex char) sha256 digest."""
    return hashlib.sha256(label.encode()).hexdigest()


def _populate_knowledge_doc(cat: ActiveCatalog, *, title: str, chash: str,
                             collection: str) -> str:
    """Register a store_put-shaped knowledge document AND write its
    single-chunk manifest row, mirroring the real MCP store_put flow
    (catalog_store_hook_tracked mints the tumbler; store_put_manifest_direct
    writes the (doc_id=tumbler, chash, position=0) manifest row after).
    Returns the tumbler string.
    """
    from nexus.catalog.store_hook import catalog_store_hook_tracked

    tumbler, _created = catalog_store_hook_tracked(
        title=title, doc_id=chash, collection_name=collection,
    )
    seed_manifest_chunks(collection, [chash])
    cat.append_manifest_chunks(
        tumbler, [{"chash": chash, "position": 0}], collection=collection,
    )
    cat.resync_chunk_count_cache(tumbler)
    return tumbler


class TestDedupA1:
    """catalog_store_hook_tracked's chash dedup (store_hook.py A1)."""

    def test_same_chash_different_title_dedups(self, tmp_path, monkeypatch):
        """The previously-broken case: by_doc_id(chash) always mismatched,
        and a differing title means ghost-by-title reconciliation cannot
        mask the bug either -- pre-fix this minted a SECOND document."""
        from nexus.catalog.store_hook import catalog_store_hook_tracked

        cat = _make_catalog(tmp_path)
        chash = _chash("same-content")

        first_tumbler = _populate_knowledge_doc(
            cat, title="Doc A", chash=chash, collection="knowledge__test1__bge-base-en-v15-768__v1",
        )
        assert count_documents() == 1

        second_tumbler, created = catalog_store_hook_tracked(
            title="Doc B",  # deliberately DIFFERENT title
            doc_id=chash,   # SAME chash
            collection_name="knowledge__test2__bge-base-en-v15-768__v1",
        )

        assert created is False, "dedup must fire, not mint a new document"
        assert second_tumbler == first_tumbler
        assert count_documents() == 1, "no duplicate document was minted"

    def test_different_chash_does_not_dedup(self, tmp_path, monkeypatch):
        """Sanity: distinct content must still register distinct documents."""
        from nexus.catalog.store_hook import catalog_store_hook_tracked

        cat = _make_catalog(tmp_path)
        _populate_knowledge_doc(
            cat, title="Doc A", chash=_chash("content-a"),
            collection="knowledge__test1__bge-base-en-v15-768__v1",
        )
        tumbler_b, created = catalog_store_hook_tracked(
            title="Doc B", doc_id=_chash("content-b"),
            collection_name="knowledge__test2__bge-base-en-v15-768__v1",
        )
        assert created is True
        assert count_documents() == 2


class TestDeletePathA2:
    """store_delete_catalog_cleanup (store_hook.py A2)."""

    def test_finds_and_deletes_the_catalog_row(self, tmp_path, monkeypatch):
        from nexus.catalog.store_hook import store_delete_catalog_cleanup

        cat = _make_catalog(tmp_path)
        chash = _chash("delete-me")
        tumbler = _populate_knowledge_doc(
            cat, title="To Delete", chash=chash, collection="knowledge__test__bge-base-en-v15-768__v1",
        )
        assert count_documents() == 1

        deleted_tumbler, error = store_delete_catalog_cleanup(chash, expected_collection=None)

        assert error == ""
        assert deleted_tumbler == tumbler
        assert count_documents() == 0, "the catalog row must be gone"

    def test_no_match_returns_empty_tuple(self, tmp_path, monkeypatch):
        from nexus.catalog.store_hook import store_delete_catalog_cleanup

        _make_catalog(tmp_path)
        tumbler, error = store_delete_catalog_cleanup(_chash("nothing-here"), expected_collection=None)
        assert (tumbler, error) == ("", "")


class TestTombstoneReapA4:
    """_reap_catalog_for_doc_ids (commands/store.py A4)."""

    def test_reaps_the_catalog_row_for_a_deleted_t3_chunk(
        self, tmp_path, monkeypatch,
    ):
        from nexus.commands.store import _reap_catalog_for_doc_ids

        cat = _make_catalog(tmp_path)
        chash = _chash("reap-me")
        _populate_knowledge_doc(
            cat, title="To Reap", chash=chash, collection="knowledge__test__bge-base-en-v15-768__v1",
        )
        assert count_documents() == 1

        _reap_catalog_for_doc_ids([chash], expected_collection=None)

        assert count_documents() == 0

    def test_unmatched_doc_id_is_a_silent_noop(self, tmp_path, monkeypatch):
        from nexus.commands.store import _reap_catalog_for_doc_ids

        _make_catalog(tmp_path)
        _reap_catalog_for_doc_ids([_chash("never-registered")], expected_collection=None)  # must not raise


def _register_knowledge_doc_directly(cat: ActiveCatalog, *, title: str,
                                       chash: str, collection: str) -> str:
    """Register a knowledge document + manifest chunk WITHOUT going through
    ``catalog_store_hook_tracked`` (which now dedups by chash on its own).

    Constructs the pre-existing-duplicate state directly — mirroring what
    the historical bug produced (two catalog documents whose manifests
    both reference the same chash) — so the ambiguity path can be tested
    in isolation from the dedup fix that now prevents it going forward.
    """
    owner = cat.register_owner("knowledge", "curator")
    tumbler = cat.register(
        owner, title, content_type="knowledge", physical_collection=collection,
        meta={"doc_id": chash},
    )
    seed_manifest_chunks(collection, [chash])
    cat.append_manifest_chunks(
        str(tumbler), [{"chash": chash, "position": 0}], collection=collection,
    )
    cat.resync_chunk_count_cache(str(tumbler))
    return str(tumbler)


class TestAmbiguousChash:
    """N>1 candidates: resolve_knowledge_doc_for_chash refuses to guess."""

    def test_two_knowledge_docs_sharing_a_chash_is_treated_as_no_match(
        self, tmp_path, monkeypatch,
    ):
        from nexus.catalog.store_hook import resolve_knowledge_doc_for_chash

        cat = _make_catalog(tmp_path)
        chash = _chash("shared-boilerplate")
        _register_knowledge_doc_directly(
            cat, title="Doc One", chash=chash, collection="knowledge__one__bge-base-en-v15-768__v1",
        )
        _register_knowledge_doc_directly(
            cat, title="Doc Two", chash=chash, collection="knowledge__two__bge-base-en-v15-768__v1",
        )
        assert count_documents() == 2

        reader = active_reader()
        result = resolve_knowledge_doc_for_chash(reader, chash, log_event="test")

        assert result is None, "ambiguous match must not be silently picked"
        # Neither candidate was touched.
        assert count_documents() == 2

    def test_ambiguity_does_not_block_delete_of_an_unrelated_doc(
        self, tmp_path, monkeypatch,
    ):
        """The delete-path lookup returning None on ambiguity means
        store_delete_catalog_cleanup treats it as "nothing to clean" rather
        than raising or guessing."""
        from nexus.catalog.store_hook import store_delete_catalog_cleanup

        cat = _make_catalog(tmp_path)
        chash = _chash("shared-again")
        _register_knowledge_doc_directly(
            cat, title="Doc One", chash=chash, collection="knowledge__one__bge-base-en-v15-768__v1",
        )
        _register_knowledge_doc_directly(
            cat, title="Doc Two", chash=chash, collection="knowledge__two__bge-base-en-v15-768__v1",
        )

        tumbler, error = store_delete_catalog_cleanup(chash, expected_collection=None)

        assert (tumbler, error) == ("", "")
        assert count_documents() == 2, "ambiguous match leaves both docs alone"


class TestFollowAliasAtThisCallSite:
    """nexus-fguo5 call-site pin (review 2026-08-01 Medium finding).

    ``resolve()``'s declared default ``follow_alias=True`` was silently
    never sent on the wire, so every caller that relied on the default —
    including ``resolve_knowledge_doc_for_chash``'s candidate loop —
    actually got the literal-tumbler entry. The fguo5 fix makes the
    declared default real. This test pins the NEW semantics at a real
    call site: a manifest tumbler that is an alias resolves to its
    CANONICAL entry, and the candidate filter judges the canonical row's
    attributes. If a future change flips the polarity back (or a caller
    starts passing follow_alias=False here), this fails loudly instead
    of the behavior drifting silently — the review found ~20 call sites
    inheriting the default with no coverage; this is the representative
    pin for the store-hook family.
    """

    def test_alias_tumbler_resolves_to_canonical_at_dedup_call_site(
        self, tmp_path, monkeypatch,
    ):
        from nexus.catalog.store_hook import resolve_knowledge_doc_for_chash

        cat = _make_catalog(tmp_path)
        chash = _chash("aliased-doc")
        alias_tumbler = _populate_knowledge_doc(
            cat, title="Old Home", chash=chash, collection="knowledge__old__bge-base-en-v15-768__v1",
        )
        canonical_tumbler = _populate_knowledge_doc(
            cat, title="Canonical Home", chash=_chash("other"),
            collection="knowledge__new__bge-base-en-v15-768__v1",
        )
        # set_alias is not on CATALOG_WRITE_OPS (nexus-iltyk) — use the
        # fixture ops' documented escape hatch for un-whitelisted writes.
        from tests._catalog_fixture_ops import unroutable_write_target

        unroutable_write_target().set_alias(alias_tumbler, canonical_tumbler)

        entry = resolve_knowledge_doc_for_chash(
            active_reader(), chash, log_event="test_alias",
        )

        assert entry is not None, (
            "alias-following resolve must still yield a candidate"
        )
        assert str(entry.tumbler) == str(canonical_tumbler), (
            "the declared follow_alias=True default must be honored at the "
            "call site: the manifest names the alias, the caller gets the "
            "canonical entry"
        )
