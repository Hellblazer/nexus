# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6lfdi, rewritten for RDR-193 Step 7 (nexus-u5m5g.8): PG-world (service-mode) coverage for the
catalog auto-link generators in ``src/nexus/catalog/link_generator.py``.

The generators send their edges through the engine's ``links_upsert_many`` (the file-path and
dependency linkers) and ``links_same_content`` (the PDF linker), so this suite drives a REAL
``HttpCatalogClient`` over a fake that speaks those routes (``tests/catalog/_link_engine_fake.py``,
wire-faithful to ``CatalogLinksHandler.java``), never a mocked client method. The request COUNTS the
index path is held to are in ``test_link_generator_scoped_reads.py``; this file pins what the
generators produce, the full pass the ``nx catalog generate-links`` verb runs, the index-time PDF
maintenance and the stale-owner source (residual 7).

Wiring mirrors production (``src/nexus/indexer.py`` ``_catalog_hook`` and
``src/nexus/commands/enrich.py`` ``run_bib_enrichment``): reads flow through a real
``HttpCatalogClient`` (the ``reader``/``cat`` arg), writes flow through a SEPARATE
``HttpCatalogClient`` instance wrapped in ``_ServiceCatalogWriter`` (the ``writer`` arg), matching
the reader/writer split ``factory.make_catalog_reader`` / ``make_catalog_writer`` enforce.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.catalog.factory import _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.link_generator import (
    DryRunLinkWriter,
    generate_citation_links,
    generate_pdf_corpus_links,
    generate_prose_filepath_links,
    generate_rdr_filepath_links,
    load_existing_link_keys,
    prune_stale_same_as_links,
    relink_changed_pdfs,
)
from nexus.catalog.tumbler import Tumbler
from tests.catalog._link_engine_fake import State as _State
from tests.catalog._link_engine_fake import start_server as _start_server


@pytest.fixture
def fake_server():
    _State.reset()
    server, url = _start_server()
    yield url
    server.shutdown()


@pytest.fixture
def reader(fake_server: str):
    with HttpCatalogClient(base_url=fake_server, tenant="tenant_abc", _token="test_tok") as c:
        yield c


@pytest.fixture
def writer(fake_server: str):
    # nexus-6lfdi: mirrors production wiring — a SEPARATE HttpCatalogClient instance from the reader,
    # wrapped in _ServiceCatalogWriter, exactly as indexer._catalog_hook and commands/enrich.py wire
    # make_catalog_reader() + make_catalog_writer() as two distinct client objects.
    client = HttpCatalogClient(base_url=fake_server, tenant="tenant_abc", _token="test_tok")
    w = _ServiceCatalogWriter(client)
    yield w
    w.close()


# ── generate_citation_links ──────────────────────────────────────────────────


class TestCitationLinksServiceMode:
    def test_citation_from_ss_id(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssB"]},
        )
        _State.add_document(
            "1.1.2", title="Paper B", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssB"},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 1
        assert len(_State.links) == 1
        lnk = _State.links[0]
        assert lnk["from_tumbler"] == "1.1.1"
        assert lnk["to_tumbler"] == "1.1.2"
        assert lnk["link_type"] == "cites"
        assert lnk["created_by"] == "bib_enricher"

    def test_no_self_citation(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssA"]},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 0
        assert _State.links == []

    def test_no_link_when_target_missing(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssC"]},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 0

    def test_no_duplicate_citations_on_rerun(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssB"]},
        )
        _State.add_document(
            "1.1.2", title="Paper B", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssB"},
        )
        first = generate_citation_links(reader, writer=writer)
        second = generate_citation_links(reader, writer=writer)
        assert first == 1
        assert second == 0  # link_if_absent's pre-flight /link_query hit
        assert len(_State.links) == 1  # no duplicate row created server-side

    def test_openalex_id_space_also_matches(self, reader, writer) -> None:
        """nexus-57mk: bib_openalex_id is indexed alongside
        bib_semantic_scholar_id in the same id_to_tumbler map."""
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_openalex_id": "W1", "references": ["W2"]},
        )
        _State.add_document(
            "1.1.2", title="Paper B", content_type="paper",
            metadata={"bib_openalex_id": "W2"},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 1


# ── generate_rdr_filepath_links ───────────────────────────────────────────────


class TestRdrFilepathLinksServiceMode:
    def test_backtick_path_creates_link(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("We modified `src/nexus/catalog/catalog.py` to fix the bug.")
        _State.add_document(
            "1.1.1", title="catalog.py", content_type="code",
            file_path="src/nexus/catalog/catalog.py",
        )
        _State.add_document(
            "1.1.2", title="Fix Catalog Bug", content_type="rdr",
            file_path=str(rdr_path),
        )
        count = generate_rdr_filepath_links(reader, writer=writer)
        assert count == 1
        lnk = _State.links[0]
        assert lnk["from_tumbler"] == "1.1.2"
        assert lnk["to_tumbler"] == "1.1.1"
        assert lnk["link_type"] == "implements"
        assert lnk["created_by"] == "filepath_extractor"

    def test_no_link_for_unindexed_path(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/missing_file.py` for details.")
        _State.add_document(
            "1.1.1", title="Dangling Ref", content_type="rdr",
            file_path=str(rdr_path),
        )
        count = generate_rdr_filepath_links(reader, writer=writer)
        assert count == 0

    def test_rdr_without_file_on_disk_skipped(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="ghost.py", content_type="code",
            file_path="src/ghost.py",
        )
        _State.add_document(
            "1.1.2", title="Ghost RDR", content_type="rdr",
            file_path="/nonexistent/path/rdr.md",
        )
        count = generate_rdr_filepath_links(reader, writer=writer)
        assert count == 0

    def test_no_duplicate_on_rerun(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("Edit `src/catalog.py`.")
        _State.add_document(
            "1.1.1", title="catalog.py", content_type="code",
            file_path="src/catalog.py",
        )
        _State.add_document(
            "1.1.2", title="Catalog Work", content_type="rdr",
            file_path=str(rdr_path),
        )
        generate_rdr_filepath_links(reader, writer=writer)
        second = generate_rdr_filepath_links(reader, writer=writer)
        assert second == 0
        assert len(_State.links) == 1

    def test_incremental_new_tumblers_scopes_scan(self, reader, writer, tmp_path) -> None:
        a_rdr = tmp_path / "a.md"
        a_rdr.write_text("See `src/nexus/a.py`.")
        b_rdr = tmp_path / "b.md"
        b_rdr.write_text("See `src/nexus/b.py`.")
        _State.add_document("1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py")
        _State.add_document("1.1.2", title="b.py", content_type="code", file_path="src/nexus/b.py")
        _State.add_document("1.1.3", title="RDR A", content_type="rdr", file_path=str(a_rdr))
        _State.add_document("1.1.4", title="RDR B", content_type="rdr", file_path=str(b_rdr))

        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.3")],
        )
        assert count == 1
        assert len(_State.links) == 1
        assert _State.links[0]["from_tumbler"] == "1.1.3"


# ── generate_prose_filepath_links ────────────────────────────────────────────


class TestProseFilepathLinksServiceMode:
    def test_prose_doc_links_to_code(self, reader, writer, tmp_path) -> None:
        prose_path = tmp_path / "runbook.md"
        prose_path.write_text("See ``src/nexus/foo.py`` for the impl.\n")
        _State.add_document(
            "1.1.1", title="Runbook", content_type="prose",
            file_path=str(prose_path),
        )
        _State.add_document(
            "1.1.2", title="foo.py", content_type="code",
            file_path="src/nexus/foo.py",
        )
        count = generate_prose_filepath_links(reader, writer=writer)
        assert count == 1
        lnk = _State.links[0]
        assert lnk["from_tumbler"] == "1.1.1"
        assert lnk["to_tumbler"] == "1.1.2"
        assert lnk["link_type"] == "implements"

    def test_non_source_root_dir_links(self, reader, writer, tmp_path) -> None:
        """nexus-sob9 widening contract: docs/ -> conexus/ (no src/ anchor)
        must link via the wider prose regex."""
        prose_path = tmp_path / "guide.md"
        prose_path.write_text("See ``conexus/hooks/foo.py`` for usage.\n")
        _State.add_document(
            "1.1.1", title="Guide", content_type="prose",
            file_path=str(prose_path),
        )
        _State.add_document(
            "1.1.2", title="foo.py", content_type="code",
            file_path="conexus/hooks/foo.py",
        )
        count = generate_prose_filepath_links(reader, writer=writer)
        assert count == 1

    def test_a_markdown_path_gets_no_edge_and_no_link(self, reader, writer, tmp_path) -> None:
        """The old client matched only ``code`` documents, so a prose file naming ``docs/other.md``
        never linked to it. The engine resolves a path to a document of ANY content type, so the
        client must not send a ``.md`` target at all (RDR-193 declares only the cross-repository
        change). The ``.py`` path in the same file is still sent."""
        prose_path = tmp_path / "guide.md"
        prose_path.write_text("See ``docs/other.md`` and ``src/nexus/foo.py``.\n")
        _State.add_document("1.1.1", title="Guide", content_type="prose", file_path=str(prose_path))
        _State.add_document("1.1.2", title="Other", content_type="prose", file_path="docs/other.md")
        _State.add_document("1.1.3", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.requests.clear()
        assert generate_prose_filepath_links(reader, writer=writer) == 1
        (body,) = _State.posts("/links/upsert_many")
        assert [e["to"]["file_path"] for e in body["edges"]] == ["src/nexus/foo.py"]
        assert [(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in _State.links] == [("1.1.1", "1.1.3")]

    def test_a_file_naming_only_a_markdown_path_sends_nothing(self, reader, writer, tmp_path) -> None:
        prose_path = tmp_path / "guide.md"
        prose_path.write_text("See ``docs/other.md``.\n")
        _State.add_document("1.1.1", title="Guide", content_type="prose", file_path=str(prose_path))
        _State.add_document("1.1.2", title="Other", content_type="rdr", file_path="docs/other.md")
        _State.requests.clear()
        assert generate_prose_filepath_links(reader, writer=writer) == 0
        assert _State.posts("/links/upsert_many") == []
        assert _State.links == []

    def test_bare_filename_does_not_match(self, reader, writer, tmp_path) -> None:
        prose_path = tmp_path / "loose.md"
        prose_path.write_text("Run ``foo.py`` to start.\n")
        _State.add_document(
            "1.1.1", title="Loose", content_type="prose",
            file_path=str(prose_path),
        )
        _State.add_document(
            "1.1.2", title="foo.py", content_type="code", file_path="foo.py",
        )
        count = generate_prose_filepath_links(reader, writer=writer)
        assert count == 0


# ── generate_pdf_corpus_links ────────────────────────────────────────────────


class TestPdfCorpusLinksServiceMode:
    def test_pdfs_sharing_a_commit_but_not_content_are_not_linked(self, reader, writer) -> None:
        """nexus-utkop: head_hash is the git commit for a repo-indexed file,
        so seven distinct PDFs indexed at one commit were joined as same-as."""
        for t, h in (("1.1.1", "3e397d9c"), ("1.1.2", "4fe9dd09"), ("1.1.3", "a2723ab6")):
            _State.add_document(
                t, title=t, content_type="pdf", head_hash="aee32ea0commit",
                index_content_hash=h, physical_collection="docs__1-1",
            )
        assert generate_pdf_corpus_links(reader, writer=writer) == 0
        assert _State.links == []

    def test_legacy_meta_content_hash_is_the_fallback_key(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="P1", content_type="paper", head_hash="c1",
            metadata={"content_hash": "same-bytes"}, physical_collection="knowledge__a",
        )
        _State.add_document(
            "1.1.2", title="P2", content_type="pdf", head_hash="c2",
            index_content_hash="same-bytes", physical_collection="knowledge__b",
        )
        assert generate_pdf_corpus_links(reader, writer=writer) == 1

    def test_two_pdfs_with_same_hash_get_linked(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper1", content_type="paper",
            index_content_hash="abc123", physical_collection="knowledge__delos",
        )
        _State.add_document(
            "1.1.2", title="Paper2", content_type="paper",
            index_content_hash="abc123", physical_collection="knowledge__art-papers",
        )
        count = generate_pdf_corpus_links(reader, writer=writer)
        assert count == 1
        lnk = _State.links[0]
        # anchor = lexicographically-first tumbler ("1.1.1" < "1.1.2")
        assert lnk["from_tumbler"] == "1.1.2"
        assert lnk["to_tumbler"] == "1.1.1"
        assert lnk["link_type"] == "same-as"
        assert lnk["created_by"] == "content_hash_dedup"

    def test_no_link_when_hash_unique(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Unique", content_type="paper",
            index_content_hash="unique-hash", physical_collection="knowledge__delos",
        )
        count = generate_pdf_corpus_links(reader, writer=writer)
        assert count == 0

    def test_idempotent(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="P1", content_type="paper",
            index_content_hash="h1", physical_collection="knowledge__delos",
        )
        _State.add_document(
            "1.1.2", title="P2", content_type="paper",
            index_content_hash="h1", physical_collection="knowledge__art-papers",
        )
        first = generate_pdf_corpus_links(reader, writer=writer)
        second = generate_pdf_corpus_links(reader, writer=writer)
        assert first == 1
        assert second == 0
        assert len(_State.links) == 1

    def test_pdfs_without_content_hash_skipped(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="NoHash1", content_type="paper",
            index_content_hash="", physical_collection="knowledge__delos",
        )
        _State.add_document(
            "1.1.2", title="NoHash2", content_type="paper",
            index_content_hash="", physical_collection="knowledge__art-papers",
        )
        count = generate_pdf_corpus_links(reader, writer=writer)
        assert count == 0


class TestPdfCorpusSameAsPruneServiceMode:
    """nexus-h0uy7: a PDF re-indexed with new bytes must lose the same-as
    link it no longer earns. Pruning is per link, never a bulk delete by
    created_by, and touches only content_hash_dedup links.

    What these tests exercise: the CLIENT's calls and the retained client copy
    (``prune_stale_same_as_links``) against ``tests/catalog/_link_engine_fake.py``. Every
    ``relink_changed_pdfs`` case (the ``links_same_content(prune=True)`` ones below) runs the FAKE's
    copy of the engine's prune and group rule, not the engine's: they pin what the client sends and
    does with the reply, never that the engine retracts or re-joins these links. The engine's own
    rule is pinned by the Java suite and by ``tests/integration/test_rdr193_linking_e2e.py``, whose
    parity case runs the client's decision and the engine's on one fixture."""

    @staticmethod
    def _doc(t: str, h: str, *, content_type: str = "pdf", **fields: Any) -> None:
        _State.add_document(
            t, title=t, content_type=content_type,
            index_content_hash=h, physical_collection="knowledge__a", **fields,
        )

    @staticmethod
    def _dedup_link(src: str, dst: str, **meta: Any) -> None:
        _State.links.append({
            "from_tumbler": src, "to_tumbler": dst, "link_type": "same-as",
            "created_by": "content_hash_dedup", "from_span": "", "to_span": "",
            "metadata": meta,
        })

    def test_link_is_retracted_when_member_content_changes(self, reader, writer) -> None:
        self._doc("1.1.1", "same")
        self._doc("1.1.2", "same")
        assert generate_pdf_corpus_links(reader, writer=writer) == 1
        _State.documents["1.1.2"]["index_content_hash"] = "new-bytes"
        assert prune_stale_same_as_links(reader, writer=writer) == [("1.1.2", "1.1.1")]
        assert _State.links == []

    def test_still_matching_link_is_kept(self, reader, writer) -> None:
        self._doc("1.1.1", "same")
        self._doc("1.1.2", "same")
        generate_pdf_corpus_links(reader, writer=writer)
        assert prune_stale_same_as_links(reader, writer=writer) == []
        assert len(_State.links) == 1

    def test_legacy_meta_content_hash_pair_is_kept(self, reader, writer) -> None:
        self._doc("1.1.1", "", metadata={"content_hash": "same-bytes"})
        self._doc("1.1.2", "same-bytes")
        assert generate_pdf_corpus_links(reader, writer=writer) == 1
        assert prune_stale_same_as_links(reader, writer=writer) == []

    def test_legacy_meta_content_hash_pair_that_differs_is_retracted(self, reader, writer) -> None:
        self._doc("1.1.1", "", metadata={"content_hash": "old-bytes"})
        self._doc("1.1.2", "", metadata={"content_hash": "other-bytes"})
        self._dedup_link("1.1.2", "1.1.1")
        assert prune_stale_same_as_links(reader, writer=writer) == [("1.1.2", "1.1.1")]

    def test_unsettled_key_is_left_alone(self, reader, writer) -> None:
        """Registration moved meta.content_hash but begin_index_run never
        landed, so index_content_hash still names the old bytes."""
        self._doc("1.1.1", "new-bytes", metadata={"content_hash": "new-bytes"})
        self._doc("1.1.2", "old-bytes", metadata={"content_hash": "new-bytes"})
        self._dedup_link("1.1.2", "1.1.1")
        assert prune_stale_same_as_links(reader, writer=writer) == []

    def test_relink_on_a_code_only_change_fetches_no_documents(self, reader, writer) -> None:
        self._doc("1.1.1", "same")
        self._doc("1.1.2", "same")
        generate_pdf_corpus_links(reader, writer=writer)
        _State.add_document("1.1.7", title="code", content_type="code")
        _State.requests.clear()
        assert relink_changed_pdfs(reader, writer=writer, changed={"1.1.7"}) == (0, 0)
        assert _State.lists() == []
        assert _State.count("POST", "/resolve_many") == 0
        assert len(_State.links) == 1

    def test_endpoint_with_no_key_is_left_alone(self, reader, writer) -> None:
        """An unknown key is not a changed one."""
        self._doc("1.1.1", "h1")
        self._doc("1.1.2", "")
        self._dedup_link("1.1.2", "1.1.1")
        assert prune_stale_same_as_links(reader, writer=writer) == []

    def test_scope_covers_either_endpoint_and_nothing_else(self, reader, writer) -> None:
        for t, h in (("1.1.1", "a"), ("1.1.2", "b"), ("1.1.3", "c"), ("1.1.4", "d")):
            self._doc(t, h)
        self._dedup_link("1.1.2", "1.1.1")  # scoped by its from side
        self._dedup_link("1.1.4", "1.1.3")  # scoped by its to side
        self._dedup_link("1.1.4", "1.1.1")  # neither side in scope
        retracted = prune_stale_same_as_links(reader, writer=writer, scope={"1.1.2", "1.1.3"})
        assert sorted(retracted) == [("1.1.2", "1.1.1"), ("1.1.4", "1.1.3")]
        assert [(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in _State.links] == [
            ("1.1.4", "1.1.1"),
        ]

    def test_other_creators_and_co_asserted_links_are_never_pruned(self, reader, writer) -> None:
        self._doc("1.1.1", "h1")
        self._doc("1.1.2", "h2")
        self._doc("1.1.3", "h3")
        _State.links.append({
            "from_tumbler": "1.1.2", "to_tumbler": "1.1.1", "link_type": "same-as",
            "created_by": "user", "from_span": "", "to_span": "",
        })
        self._dedup_link("1.1.3", "1.1.1", co_discovered_by=["user"])
        assert prune_stale_same_as_links(reader, writer=writer) == []
        assert len(_State.links) == 2

    def test_link_to_a_document_no_longer_a_pdf_is_retracted(self, reader, writer) -> None:
        self._doc("1.1.1", "same")
        self._doc("1.1.2", "same")
        generate_pdf_corpus_links(reader, writer=writer)
        _State.documents["1.1.1"]["content_type"] = "prose"
        assert prune_stale_same_as_links(reader, writer=writer) == [("1.1.2", "1.1.1")]

    def test_empty_pdf_fetch_refuses_instead_of_retracting_everything(self, reader, writer) -> None:
        self._dedup_link("1.1.2", "1.1.1")
        assert prune_stale_same_as_links(reader, writer=writer) == []
        assert len(_State.links) == 1

    def test_pages_past_the_first_page(self, reader, writer) -> None:
        self._doc("1.1.1", "anchor")
        for i in range(2, 7):
            self._doc(f"1.1.{i}", f"changed-{i}")
            self._dedup_link(f"1.1.{i}", "1.1.1")
        assert len(prune_stale_same_as_links(reader, writer=writer, page=2)) == 5
        assert _State.links == []

    def test_relink_rejoins_members_when_the_anchor_changes(self, reader, writer) -> None:
        """The anchor re-indexed with new bytes: its members still share a
        key, so they re-join the next anchor in the same pass. (The fake's copy of the engine rule.)"""
        for t in ("1.1.1", "1.1.2", "1.1.3"):
            self._doc(t, "same")
        assert generate_pdf_corpus_links(reader, writer=writer) == 2
        _State.documents["1.1.1"]["index_content_hash"] = "new-bytes"
        assert relink_changed_pdfs(reader, writer=writer, changed={"1.1.1"}) == (2, 1)
        assert [(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in _State.links] == [
            ("1.1.3", "1.1.2"),
        ]

    def test_relink_joins_a_changed_pdf_to_its_new_group(self, reader, writer) -> None:
        """The fake's copy of the engine's prune and group rule; see the class docstring."""
        self._doc("1.1.1", "old")
        self._doc("1.1.2", "old")
        self._doc("1.1.3", "new-bytes")
        generate_pdf_corpus_links(reader, writer=writer)
        _State.documents["1.1.2"]["index_content_hash"] = "new-bytes"
        assert relink_changed_pdfs(reader, writer=writer, changed={"1.1.2"}) == (1, 1)
        assert [(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in _State.links] == [
            ("1.1.3", "1.1.2"),
        ]

    def test_a_new_anchor_gets_every_member_of_its_group(self, reader, writer) -> None:
        """The fake's copy of the engine's group rule; see the class docstring."""
        self._doc("1.1.5", "same")
        self._doc("1.1.6", "same")
        generate_pdf_corpus_links(reader, writer=writer)
        self._doc("1.1.3", "same")
        assert generate_pdf_corpus_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.3")],
        ) == 2
        assert {lnk["to_tumbler"] for lnk in _State.links} == {"1.1.3", "1.1.5"}

    def test_index_time_hook_prunes_the_runs_changed_pdfs(
        self, reader, fake_server: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-h0uy7: the post-run pass in indexer.py, scoped by needs_fence. The retraction is the
        fake's copy of the engine's prune rule; this pins that the hook sends the changed set."""
        from nexus import indexer  # noqa: PLC0415 — keeps this module's import cost unchanged
        from nexus.catalog import factory  # noqa: PLC0415 — patch target

        self._doc("1.1.1", "old")
        self._doc("1.1.2", "new-bytes")
        self._dedup_link("1.1.2", "1.1.1")
        monkeypatch.setattr(factory, "make_catalog_reader", lambda: reader)
        monkeypatch.setattr(
            factory, "make_catalog_writer",
            lambda: _ServiceCatalogWriter(HttpCatalogClient(
                base_url=fake_server, tenant="tenant_abc", _token="test_tok",
            )),
        )
        indexer._relink_changed_pdfs({"needs_fence": {"1.1.2": ("new-bytes", "knowledge__a")}})
        assert _State.links == []

    def test_index_time_hook_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus import indexer  # noqa: PLC0415 — keeps this module's import cost unchanged
        from nexus.catalog import factory  # noqa: PLC0415 — patch target

        def _boom() -> None:
            raise RuntimeError("catalog down")

        monkeypatch.setattr(factory, "make_catalog_reader", _boom)
        indexer._relink_changed_pdfs({"needs_fence": {"1.1.2": ("h", "c")}})

    @staticmethod
    def _cli(reader, writer, monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
        from click.testing import CliRunner  # noqa: PLC0415 — CLI tests only

        from nexus.commands import catalog as cat_cmd  # noqa: PLC0415 — patch target
        from nexus.commands.catalog_cmds.links import generate_links_cmd  # noqa: PLC0415 — CLI tests only

        monkeypatch.setattr(cat_cmd, "_get_catalog", lambda: reader)
        monkeypatch.setattr(cat_cmd, "_get_catalog_writer", lambda: writer)
        result = CliRunner().invoke(
            generate_links_cmd,
            ["--no-citations", "--no-filepath", "--no-prose", "--no-rdr-dependency", *args],
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_cli_write_pass_retracts_then_relinks(self, reader, writer, monkeypatch) -> None:
        self._doc("1.1.1", "a")
        self._doc("1.1.2", "b")
        self._doc("1.1.3", "b")
        self._dedup_link("1.1.2", "1.1.1")
        out = self._cli(reader, writer, monkeypatch)
        assert "stale same-as links retracted: 1" in out
        assert [(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in _State.links] == [
            ("1.1.3", "1.1.2"),
        ]

    def test_cli_dry_run_projects_the_count_once(self, reader, writer, monkeypatch) -> None:
        self._doc("1.1.1", "a")
        self._doc("1.1.2", "b")
        self._dedup_link("1.1.2", "1.1.1")
        out = self._cli(reader, writer, monkeypatch, "--dry-run")
        assert "would retract 1 stale same-as link(s)" in out
        assert "Catalog link count would go 1 -> 0" in out
        assert len(_State.links) == 1

    def test_dry_run_records_the_retraction_and_writes_nothing(self, reader) -> None:
        self._doc("1.1.1", "h1")
        self._doc("1.1.2", "h2")
        self._dedup_link("1.1.2", "1.1.1")
        recorder = DryRunLinkWriter(existing=load_existing_link_keys(reader))
        prune_stale_same_as_links(reader, writer=recorder)
        assert [(r.from_tumbler, r.to_tumbler, r.link_type) for r in recorder.retracted] == [
            ("1.1.2", "1.1.1", "same-as"),
        ]
        assert len(_State.links) == 1


# ── the skip: a generator makes no request when no new document could match its source type ──


class TestLinkGeneratorSeedSkip:
    """Zero REQUESTS, not just zero links: a batch with no document of a generator's source type
    never reaches the engine."""

    def test_rdr_linker_zero_requests_when_no_rdr_seed(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/foo.py`.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="RDR", content_type="rdr", file_path=str(rdr_path))
        _State.requests.clear()
        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.1")], new_content_types={"code"},
        )
        assert count == 0
        assert _State.requests == []

    def test_prose_linker_zero_requests_when_no_prose_seed(self, reader, writer) -> None:
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.requests.clear()
        count = generate_prose_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.1")], new_content_types={"code"},
        )
        assert count == 0
        assert _State.requests == []

    def test_pdf_linker_zero_requests_when_no_pdf_seed(self, reader, writer) -> None:
        _State.add_document("1.1.1", title="RDR", content_type="rdr", file_path="/x/rdr.md")
        _State.requests.clear()
        count = generate_pdf_corpus_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.1")], new_content_types={"rdr"},
        )
        assert count == 0
        assert _State.requests == []

    def test_rdr_linker_still_runs_and_links_when_rdr_seed_present(self, reader, writer, tmp_path) -> None:
        """The skip must not become unconditional: a qualifying seed still produces its links."""
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/foo.py`.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="RDR", content_type="rdr", file_path=str(rdr_path))
        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.2")], new_content_types={"rdr"},
        )
        assert count == 1
        assert _State.lists() == [], "a seeded run reads the seeds (one resolve_many), never lists a type"

    def test_prose_linker_still_runs_and_links_when_docs_seed_present(self, reader, writer, tmp_path) -> None:
        """"docs" is a legitimate (if currently unproduced by indexer.py's _catalog_hook) source type:
        naming it in new_content_types must still run the generator."""
        doc_path = tmp_path / "runbook.md"
        doc_path.write_text("See ``src/nexus/foo.py``.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="Runbook", content_type="docs", file_path=str(doc_path))
        count = generate_prose_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.2")], new_content_types={"docs"},
        )
        assert count == 1

    def test_pdf_linker_still_runs_and_links_when_pdf_seed_present(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="P1", content_type="paper", index_content_hash="h1", physical_collection="knowledge__a",
        )
        _State.add_document(
            "1.1.2", title="P2", content_type="paper", index_content_hash="h1", physical_collection="knowledge__b",
        )
        count = generate_pdf_corpus_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.2")], new_content_types={"paper"},
        )
        assert count == 1
        assert _State.lists() == []

    def test_skip_is_opt_in_omitting_new_content_types_still_runs(self, reader, writer) -> None:
        """Every call site that omits new_content_types (the full pass does) keeps the old behavior:
        the generator runs even when new_tumblers alone names only a non-qualifying type."""
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.requests.clear()
        count = generate_rdr_filepath_links(reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.1")])
        assert count == 0
        assert _State.count("POST", "/resolve_many") == 1, "it looked the seed up, found no rdr, and wrote nothing"
        assert _State.count("POST", "/links/upsert_many") == 0

    def test_citation_linker_still_full_scans(self, reader, writer) -> None:
        """generate_citation_links is deliberately UNCHANGED (bib IDs can live on any content type):
        exactly one unfiltered request."""
        _State.add_document(
            "1.1.1", title="A", content_type="paper", metadata={"bib_semantic_scholar_id": "a", "references": ["b"]},
        )
        _State.add_document("1.1.2", title="B", content_type="paper", metadata={"bib_semantic_scholar_id": "b"})
        _State.requests.clear()
        generate_citation_links(reader, writer=writer)
        assert [p.get("content_type") for p in _State.lists()] == [None]


# ── the full pass: listed sources, path edges, the stale-owner source (residual 7) ──


class TestFullPassOwnerOfTheRepository:
    """RDR-193 Step 7, residual 7: in ``nx catalog generate-links`` a path edge names the owner of
    the repository being linked (the owner whose ``repo_root`` contains the source's ``source_uri``),
    not the source document's own owner. About 150 RDRs sit under a stale owner."""

    @staticmethod
    def _repo(tmp_path):
        root = (tmp_path / "repo").resolve()
        (root / "docs" / "rdr").mkdir(parents=True)
        return root

    def test_a_source_under_a_stale_owner_names_the_repository_owner_and_links(self, reader, writer, tmp_path) -> None:
        root = self._repo(tmp_path)
        _State.set_owner("1.1", repo_root=str(root), name="current")
        # a legacy owner that never recorded a repo_root: its own path lookup can resolve nothing
        _State.set_owner("1.9", repo_root="", name="stale", owner_type="repo")
        rdr = root / "docs" / "rdr" / "rdr-001-x.md"
        rdr.write_text("See `src/nexus/a.py`.\n")
        # the RDR is registered under the stale owner 1.9 (an absolute file_path, so resolve_path still
        # finds the file); the code under the current owner 1.1
        _State.add_document(
            "1.9.4", title="rdr-001", content_type="rdr", file_path=str(rdr), source_uri=f"file://{rdr}",
        )
        _State.add_document(
            "1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py",
            source_uri=f"file://{root / 'src/nexus/a.py'}",
        )
        _State.requests.clear()

        assert generate_rdr_filepath_links(reader, writer=writer) == 1

        (body,) = _State.posts("/links/upsert_many")
        assert body["edges"][0]["to"] == {"owner": "1.1", "file_path": "src/nexus/a.py"}
        assert [(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in _State.links] == [("1.9.4", "1.1.1")]

    def test_the_named_owner_is_the_one_whose_root_contains_the_source(self, reader, writer, tmp_path) -> None:
        mine, other = self._repo(tmp_path), (tmp_path / "other").resolve()
        (other / "docs" / "rdr").mkdir(parents=True)
        _State.set_owner("1.1", repo_root=str(mine), name="mine")
        _State.set_owner("1.2", repo_root=str(other), name="other")
        rdr = mine / "docs" / "rdr" / "rdr-001-x.md"
        rdr.write_text("See `src/nexus/a.py`.\n")
        # registered under the OTHER repository's owner 1.2 (a stale registration of this repo's file;
        # an absolute file_path so resolve_path finds the file without 1.2's own repo_root)
        _State.add_document(
            "1.2.7", title="rdr-001", content_type="rdr", file_path=str(rdr),
            source_uri=f"file://{rdr}",
        )
        _State.add_document(
            "1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py",
            source_uri=f"file://{mine / 'src/nexus/a.py'}",
        )
        _State.add_document(
            "1.2.2", title="a.py", content_type="code", file_path="src/nexus/a.py",
            source_uri=f"file://{other / 'src/nexus/a.py'}",
        )
        _State.requests.clear()

        assert generate_rdr_filepath_links(reader, writer=writer) == 1

        (body,) = _State.posts("/links/upsert_many")
        assert body["edges"][0]["to"]["owner"] == "1.1", "the repository holding the source, not the source's owner 1.2"
        assert [(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in _State.links] == [("1.2.7", "1.1.1")]

    def test_a_source_no_root_contains_keeps_its_own_owner(self, reader, writer, tmp_path) -> None:
        rdr = tmp_path / "rdr.md"
        rdr.write_text("See `src/nexus/a.py`.\n")
        _State.add_document("1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py")
        _State.add_document("1.1.2", title="rdr", content_type="rdr", file_path=str(rdr), source_uri=f"file://{rdr}")
        _State.requests.clear()
        assert generate_rdr_filepath_links(reader, writer=writer) == 1
        (body,) = _State.posts("/links/upsert_many")
        assert body["edges"][0]["to"]["owner"] == "1.1"


class TestGenerateLinksCliDryRunAndWritePass:
    """``nx catalog generate-links`` over the full pass: path edges through ``links_upsert_many``, the same-as
    linking through the engine, and a dry run that says what it cannot preview."""

    @staticmethod
    def _cli(reader, writer, monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
        from click.testing import CliRunner  # noqa: PLC0415 — CLI tests only

        from nexus.commands import catalog as cat_cmd  # noqa: PLC0415 — patch target
        from nexus.commands.catalog_cmds.links import generate_links_cmd  # noqa: PLC0415 — CLI tests only

        monkeypatch.setattr(cat_cmd, "_get_catalog", lambda: reader)
        monkeypatch.setattr(cat_cmd, "_get_catalog_writer", lambda: writer)
        result = CliRunner().invoke(
            generate_links_cmd, ["--no-citations", "--no-prose", "--no-rdr-dependency", *args],
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_write_pass_links_rdr_paths_and_same_content(self, reader, writer, monkeypatch, tmp_path) -> None:
        rdr = tmp_path / "rdr.md"
        rdr.write_text("See `src/nexus/a.py`.\n")
        _State.add_document("1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py")
        _State.add_document("1.1.2", title="rdr", content_type="rdr", file_path=str(rdr))
        _State.add_document("1.1.3", content_type="pdf", index_content_hash="h")
        _State.add_document("1.1.4", content_type="pdf", index_content_hash="h")
        out = self._cli(reader, writer, monkeypatch)
        assert "rdr-filepath links created: 1" in out and "pdf-corpus links created: 1" in out
        assert {(lnk["from_tumbler"], lnk["to_tumbler"], lnk["link_type"]) for lnk in _State.links} == {
            ("1.1.2", "1.1.1", "implements"), ("1.1.4", "1.1.3", "same-as"),
        }

    def test_dry_run_counts_only_paths_that_exist_and_links_not_already_present(
        self, reader, writer, monkeypatch, tmp_path,
    ) -> None:
        """One batch read resolves the paths: ``a.py`` is a new link, ``b.py`` is linked already,
        ``gone.py`` names no document. The same-content linker's links are not previewed."""
        rdr = tmp_path / "rdr.md"
        rdr.write_text("See `src/nexus/a.py`, `src/nexus/b.py` and `src/nexus/gone.py`.\n")
        _State.add_document("1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py")
        _State.add_document("1.1.5", title="b.py", content_type="code", file_path="src/nexus/b.py")
        _State.add_document("1.1.2", title="rdr", content_type="rdr", file_path=str(rdr))
        _State.links.append({
            "from_tumbler": "1.1.2", "to_tumbler": "1.1.5", "link_type": "implements",
            "created_by": "filepath_extractor", "from_span": "", "to_span": "",
        })
        _State.add_document("1.1.3", content_type="pdf", index_content_hash="h")
        _State.add_document("1.1.4", content_type="pdf", index_content_hash="h")
        out = self._cli(reader, writer, monkeypatch, "--dry-run")
        assert "rdr-filepath: would create 1 link(s)" in out, out
        assert "1 path reference(s) name no document at that path under the repository's owner and are not counted" in out
        assert "path edges the engine resolves" not in out and "upper bound" not in out
        assert "1.1.2 -> 1.1.1 (implements) by filepath_extractor" in out
        assert "pdf-corpus: 2 document(s) would go to the engine's same-content linker" in out
        assert "are NOT previewed" in out
        assert len(_State.links) == 1, "a dry run writes nothing"
        assert _State.count("POST", "/links/upsert_many") == 0 and _State.count("POST", "/links/same_content") == 0
        assert _State.count("POST", "/list_by_file_paths") == 1, "the paths are resolved in one batch read"
