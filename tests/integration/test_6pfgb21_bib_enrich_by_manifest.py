# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.21: bib enrichment selects chunks by the catalog Document's manifest.

A T3 chunk row is shared by every Document that holds that text and its ``title`` is the LAST
writer's. ``nx enrich bib`` used to group rows by that title and skip rows that already held an
id field, so a Document's chunks restamped by another writer were enriched with the other's bib,
counted as already enriched, or never reached; ``--backfill-catalog`` copied one representative
chunk's bib into the catalog row matched by title, so a Document could take another's values
(x54wv census rows 10, 11).

Now the unit is the catalog Document: its manifest names its chunks, the catalog row says whether
it is enriched, a chunk row carries the bib only for a chunk exactly one live Document holds, and
``--backfill-catalog`` trusts only such a chunk. ``nx catalog backfill`` registers a paper
collection from its first chunk's row (census row 14), so it reads the Document that holds the
chunk instead, and skips a collection whose first chunk several Documents hold.

These tests drive the real verbs against the engine substrate; only the bib backend (network) is
replaced.
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.factory import make_catalog_writer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.commands.catalog import _backfill_papers, _owner_by_name
from nexus.commands.enrich import enrich
from nexus.db import make_t3
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct
from tests.conftest import fake_credentials

pytestmark = [pytest.mark.integration]

_BIB_A = {
    "bib_year": 2019, "bib_venue": "OSDI", "bib_authors": "Dana",
    "bib_citation_count": 314, "bib_semantic_scholar_id": "ssA",
}
_BIB_B = {
    "bib_year": 2024, "bib_venue": "SOSP", "bib_authors": "Eve",
    "bib_citation_count": 7, "bib_semantic_scholar_id": "ssB",
}
_BACKEND_A = {
    "year": 2019, "venue": "OSDI", "authors": "Dana", "citation_count": 314,
    "semantic_scholar_id": "ssA",
}
_BACKEND_B = {
    "year": 2024, "venue": "SOSP", "authors": "Eve", "citation_count": 7,
    "semantic_scholar_id": "ssB",
}


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _row(chash: str, position: int) -> dict:
    return {
        "chash": chash, "position": position, "chunk_index": position,
        "section_title": "", "section_type": "", "page_number": None,
    }


class _World:
    """Papers A and B in one collection. ``c1`` is held by both, ``c2`` by A alone, ``c3`` by B
    alone. A's manifest is ``a_chunks``, B's ``b_chunks``; ``chunks`` seeds each chunk row with
    its metadata, and B restamps ``c1`` last (title B), as the later indexer of B does. The
    documents' file hashes are ``hash-<tag>-A`` and ``hash-<tag>-B``: the ``content_hash`` a row
    carries when that document's indexer wrote it."""

    def __init__(
        self, tag: str, *, chunks: dict[str, dict],
        a_chunks: tuple[str, ...], b_chunks: tuple[str, ...],
        b_catalog_bib: dict | None = None,
    ) -> None:
        self.collection = f"docs__{tag}__bge-base-en-v15-768__v1"
        self.title_a, self.title_b = f"{tag} Paper A", f"{tag} Paper B"
        self.ch = {n: _chash(f"{tag}-{n}") for n in chunks}
        with HttpCatalogClient() as cat:
            owner = cat.register_owner(f"{tag}-owner", "repo", repo_hash=f"{tag}-hash")
            self.a = str(cat.register(
                owner, self.title_a, content_type="paper",
                physical_collection=self.collection, chunk_count=len(a_chunks),
                meta={"content_hash": f"hash-{tag}-A"},
            ))
            self.b = str(cat.register(
                owner, self.title_b, content_type="paper",
                physical_collection=self.collection, chunk_count=len(b_chunks),
                meta={"content_hash": f"hash-{tag}-B"},
            ))
        names = list(chunks)
        seed_chunks_direct(
            self.collection,
            ids=[self.ch[n] for n in names],
            documents=[f"{tag} {n} text" for n in names],
            embed=True,
            metadatas=[{**chunks[n], "chunk_text_hash": self.ch[n]} for n in names],
        )
        writer = make_catalog_writer()
        try:
            writer.write_manifest(
                self.a, [_row(self.ch[n], i) for i, n in enumerate(a_chunks)],
                collection=self.collection,
            )
            writer.write_manifest(
                self.b, [_row(self.ch[n], i) for i, n in enumerate(b_chunks)],
                collection=self.collection,
            )
            if b_catalog_bib:
                writer.update(self.b, **b_catalog_bib)
        finally:
            writer.close()

    def entry(self, tumbler: str):
        with HttpCatalogClient() as cat:
            return cat.resolve(tumbler)

    def chunk_meta(self, name: str) -> dict:
        col = make_t3().get_or_create_collection(self.collection)
        got = col.get(ids=[self.ch[name]], include=["metadatas"])
        assert got["ids"] == [self.ch[name]], f"{name} is not readable: {got}"
        return got["metadatas"][0]


def _run(world: _World, *args: str):
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        return CliRunner().invoke(enrich, ["bib", world.collection, *args])


def _backend_by_title(world: _World, answers: dict[str, dict]):
    """A bib backend that answers by title and fails on any title it was not told about."""
    def _enrich(title: str) -> dict:
        if title not in answers:
            raise AssertionError(f"the backend was asked about {title!r}")
        return dict(answers[title])
    return _enrich


def test_backfill_catalog_does_not_copy_another_documents_bib_from_a_shared_chunk() -> None:
    """Census row 10. A is enriched; B holds c1 too and wrote it last (its title is now B's, the
    bib it carries is A's). The verb used to take the title-B group's first chunk with a bib (c1)
    and write A's values into B's catalog row. c1 has two live owners, so it is not evidence for
    either; c2 is A's alone and still backfills A."""
    w = _World(
        "6pfgb21-bf",
        chunks={
            "c1": {"title": "6pfgb21-bf Paper B", "content_hash": "hash-6pfgb21-bf-B", **_BIB_A},
            "c2": {"title": "6pfgb21-bf Paper A", "content_hash": "hash-6pfgb21-bf-A", **_BIB_A},
            "c3": {"title": "6pfgb21-bf Paper B", "content_hash": "hash-6pfgb21-bf-B"},
        },
        a_chunks=("c1", "c2"), b_chunks=("c1", "c3"),
    )

    result = _run(w, "--backfill-catalog")

    assert result.exit_code == 0, result.output
    a, b = w.entry(w.a), w.entry(w.b)
    assert a.bib_semantic_scholar_id == "ssA" and a.bib_year == 2019, result.output
    assert b.bib_semantic_scholar_id == "" and b.bib_year == 0 and b.bib_authors == "", (
        f"B took A's bib from the shared chunk: {b}\n{result.output}"
    )
    assert "Backfilled 1 documents" in result.output, result.output
    assert "1 enriched chunks skipped" in result.output, result.output


def test_backfill_catalog_ignores_a_chunk_a_former_holder_wrote_and_fills_only() -> None:
    """Census row 10, the stale former holder. B was enriched (c1 carries B's bib and B's file
    hash), then B dropped c1: A is c1's only live holder, yet the row names B as its last indexer. A's
    catalog row stays untouched. B's own sole-held chunk c3 backfills B; and a catalog row that
    already holds an id (B after that run) is left as it is on the next run."""
    w = _World(
        "6pfgb21-stale",
        chunks={
            "c1": {"title": "6pfgb21-stale Paper B", "content_hash": "hash-6pfgb21-stale-B", **_BIB_B},
            "c2": {"title": "6pfgb21-stale Paper A", "content_hash": "hash-6pfgb21-stale-A"},
            "c3": {"title": "6pfgb21-stale Paper B", "content_hash": "hash-6pfgb21-stale-B", **_BIB_B},
        },
        a_chunks=("c1", "c2"), b_chunks=("c3",),
    )

    first = _run(w, "--backfill-catalog")

    assert first.exit_code == 0, first.output
    a, b = w.entry(w.a), w.entry(w.b)
    assert a.bib_semantic_scholar_id == "" and a.bib_year == 0 and a.bib_authors == "", (
        f"A took the former holder's bib: {a}\n{first.output}"
    )
    assert b.bib_semantic_scholar_id == "ssB" and b.bib_year == 2024, first.output
    assert "Backfilled 1 documents" in first.output, first.output
    assert "1 enriched chunks skipped (the row does not name the document that holds it as its last indexer)" in first.output

    second = _run(w, "--backfill-catalog")

    assert second.exit_code == 0, second.output
    assert "Backfilled 0 documents" in second.output, second.output
    assert "1 documents already hold the id" in second.output, second.output


def test_enrich_auto_links_two_enriched_papers_that_cite_each_other() -> None:
    """nexus-7mcev. Enrichment writes the ids to catalog columns and ``references`` to meta; the
    post-run pass used to read ids from meta alone and so linked nothing."""
    w = _World(
        "6pfgb21-cites",
        chunks={"c1": {"title": "6pfgb21-cites Paper A"}, "c2": {"title": "6pfgb21-cites Paper B"}},
        a_chunks=("c1",), b_chunks=("c2",),
    )

    with patch(
        "nexus.bib_enricher.enrich",
        side_effect=_backend_by_title(w, {
            w.title_a: {**_BACKEND_A, "references": ["ssB"]},
            w.title_b: {**_BACKEND_B, "references": ["ssA"]},
        }),
    ):
        result = _run(w, "--source", "s2", "--delay", "0")

    assert result.exit_code == 0, result.output
    assert "Auto-generated 2 citation links" in result.output, result.output
    with HttpCatalogClient() as cat:
        assert [str(link.to_tumbler) for link in cat.links_from(w.a, link_type="cites")] == [w.b]
        assert [str(link.to_tumbler) for link in cat.links_from(w.b, link_type="cites")] == [w.a]


def test_enrich_reaches_a_document_whose_only_chunk_is_shared_and_carries_the_others_bib() -> None:
    """Census row 11. A holds only c1; B holds c1 and c3 and is enriched. c1 was restamped with
    A's title and still carries B's id, so the old pass counted it already enriched, found no
    title-A group, and A was never enriched. The catalog says A has no id: A is looked up, and
    its own bib goes on its catalog row. c1, which B also holds, keeps B's values."""
    w = _World(
        "6pfgb21-only-shared",
        chunks={
            "c1": {"title": "6pfgb21-only-shared Paper A", **_BIB_B},
            "c3": {"title": "6pfgb21-only-shared Paper B", **_BIB_B},
        },
        a_chunks=("c1",), b_chunks=("c1", "c3"),
        b_catalog_bib={
            "bib_semantic_scholar_id": "ssB", "bib_year": 2024, "bib_authors": "Eve",
        },
    )

    with patch(
        "nexus.bib_enricher.enrich",
        side_effect=_backend_by_title(w, {w.title_a: _BACKEND_A}),
    ) as backend:
        result = _run(w, "--source", "s2", "--delay", "0")

    assert result.exit_code == 0, result.output
    assert [c.args for c in backend.call_args_list] == [(w.title_a,)], result.output
    a, b = w.entry(w.a), w.entry(w.b)
    assert a.bib_semantic_scholar_id == "ssA" and a.bib_year == 2019, result.output
    assert b.bib_semantic_scholar_id == "ssB" and b.bib_year == 2024, "B was already enriched"
    assert w.chunk_meta("c1")["bib_semantic_scholar_id"] == "ssB", (
        "A's bib must not be written onto a chunk B also holds"
    )


def test_enrich_writes_a_documents_bib_only_on_chunks_no_other_document_holds() -> None:
    """Census row 11. Neither paper is enriched; c1 is shared and B wrote it last (title B). The
    title-B group was {c1, c3}, so A's chunk row c1 took B's bib. Each document is enriched by
    its own catalog title; its chunk rows change only where it is the sole holder, and c1 is left
    as it was."""
    w = _World(
        "6pfgb21-nobleed",
        chunks={
            "c1": {"title": "6pfgb21-nobleed Paper B"},
            "c2": {"title": "6pfgb21-nobleed Paper A"},
            "c3": {"title": "6pfgb21-nobleed Paper B"},
        },
        a_chunks=("c1", "c2"), b_chunks=("c1", "c3"),
    )

    with patch(
        "nexus.bib_enricher.enrich",
        side_effect=_backend_by_title(
            w, {w.title_a: _BACKEND_A, w.title_b: _BACKEND_B},
        ),
    ):
        result = _run(w, "--source", "s2", "--delay", "0")

    assert result.exit_code == 0, result.output
    a, b = w.entry(w.a), w.entry(w.b)
    assert a.bib_semantic_scholar_id == "ssA" and a.bib_authors == "Dana", result.output
    assert b.bib_semantic_scholar_id == "ssB" and b.bib_authors == "Eve", result.output
    assert w.chunk_meta("c2")["bib_semantic_scholar_id"] == "ssA"
    assert w.chunk_meta("c3")["bib_semantic_scholar_id"] == "ssB"
    assert not w.chunk_meta("c1").get("bib_semantic_scholar_id"), (
        "a chunk two documents hold carries neither document's bib"
    )
    assert "enriched 2 chunks across 2 documents" in result.output, result.output


def _paper_world(tag: str, holders: int):
    """A ``docs__`` collection whose one chunk is held by ``holders`` live Documents, none of
    them under the ``papers`` curator, and whose chunk row carries a stranger's title, authors
    and year (the last writer's). Returns ``(collection, chash, [tumbler, ...])``."""
    collection = f"docs__{tag}__bge-base-en-v15-768__v1"
    chash = _chash(f"{tag}-chunk")
    seed_chunks_direct(
        collection, ids=[chash], documents=[f"{tag} text"], embed=True,
        metadatas=[{
            "chunk_text_hash": chash, "title": "The Stranger Paper",
            "bib_authors": "Stranger Author", "bib_year": 1999,
        }],
    )
    tumblers: list[str] = []
    with HttpCatalogClient() as cat:
        owner = cat.register_owner(f"{tag}-owner", "repo", repo_hash=f"{tag}-hash")
        for i in range(holders):
            tumblers.append(str(cat.register(
                owner, f"The Real Paper {i}", content_type="paper", author=f"Real Author {i}",
                year=2020, physical_collection=collection, chunk_count=1,
            )))
    writer = make_catalog_writer()
    try:
        for t in tumblers:
            writer.write_manifest(t, [_row(chash, 0)], collection=collection)
        writer.update(tumblers[0], bib_authors="Real Bib Authors", bib_year=2021)
    finally:
        writer.close()
    return collection, chash, tumblers


def _backfill_papers_run():
    cat = ActiveCatalog()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        count = _backfill_papers(cat, make_t3(), dry_run=False)
    papers = _owner_by_name(cat, "papers")
    registered = list(cat.by_owner(papers)) if papers is not None else []
    return count, registered


def test_paper_backfill_reads_the_document_that_holds_the_first_chunk(capsys) -> None:
    """Census row 14. The collection's first chunk is held by one live Document. The row's
    title and bib are the last writer's (a stranger's here); the Document's own catalog row
    says what this paper is."""
    collection, _chash_, _tumblers = _paper_world("6pfgb21-paper-one", holders=1)

    count, registered = _backfill_papers_run()

    assert count == 1, capsys.readouterr().out
    assert [(e.title, e.author, e.year, e.physical_collection) for e in registered] == [
        ("The Real Paper 0", "Real Bib Authors", 2021, collection),
    ]


def test_paper_backfill_skips_a_collection_whose_first_chunk_several_documents_hold(capsys) -> None:
    """Census row 14. Two live Documents hold the first chunk, so its row names neither:
    the collection is skipped and reported, not registered under the stranger's title."""
    _paper_world("6pfgb21-paper-two", holders=2)

    count, registered = _backfill_papers_run()

    out = capsys.readouterr().out
    assert count == 0 and registered == [], out
    assert "its first chunk is held by 2 documents" in out, out


def test_paper_backfill_reads_no_holders_for_a_collection_already_registered(capsys) -> None:
    """A collection the papers curator already holds is not a gap to register, so its first
    chunk's holders are neither read nor warned about, however many documents hold that chunk
    (before: the shared-first-chunk skip ran first, so it warned on every re-run and every
    collection paid the holder lookup)."""
    from nexus.commands import catalog as catalog_cmd

    collection, _chash_, _tumblers = _paper_world("6pfgb21-paper-registered", holders=2)
    cat = ActiveCatalog()
    writer = make_catalog_writer()
    try:
        curator = catalog_cmd._get_or_create_curator(cat, "papers", writer=writer)
        writer.register(
            owner=curator, title="Registered by hand", content_type="paper",
            physical_collection=collection,
        )
    finally:
        writer.close()
    asked: list[str] = []
    real = catalog_cmd._live_holders_of_chunk

    def _recording(cat_, col, chunk_id):
        asked.append(col)
        return real(cat_, col, chunk_id)

    with patch.object(catalog_cmd, "_live_holders_of_chunk", _recording):
        _backfill_papers_run()

    out = capsys.readouterr().out
    assert collection not in asked, f"holders were read for a registered collection: {asked}"
    assert f"{collection} skipped" not in out, out
