# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-y8qtj client-wave fix: --source-uri identity resolution, the two
fail-loud rules, the end-of-run document-fork warning, and
``nx catalog update --file-path``.

Root defect (see the bead + T2 nexus/y8qtj-design-2026-08-01 for the full
causal chain): a document originally registered from DEVONthink carries
``source_uri=x-devonthink-item://<UUID>``, stamped on AFTER the initial
``file://``-keyed registration. A later path-based re-index (e.g. the gtltb
remediation driver) keys identity on file_path, misses the DT-URI entry, and
silently registers a SECOND catalog Document — leaving the original's
(corrupted) chunks live, searchable, and un-swept.

Tests run against the REAL per-test-tenant engine substrate (autouse
``_pin_t2_substrate`` in tests/conftest.py) via ``tests._catalog_fixture_ops
.ActiveCatalog`` — no catalog mocking below the boundary; this is exactly
the substrate ``by_source_uri`` / ``docs_for_chashes`` / collection-mismatch
detection needs to be exercised against for the fail-loud rules to mean
anything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from structlog.testing import capture_logs

from nexus.cli import main
from tests._catalog_fixture_ops import ActiveCatalog

# Every test in this suite is routed to a real per-test-tenant engine
# catalog by the autouse `_pin_t2_substrate` fixture (tests/conftest.py) —
# no explicit substrate fixture request needed here.

_SEQ = [0]


def _next_seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _chunk(chash: str, position: int) -> dict[str, Any]:
    return {
        "chash": chash, "position": position, "chunk_index": None,
        "line_start": None, "line_end": None, "char_start": None, "char_end": None,
    }


# ── _register_or_lookup_doc_id: --source-uri identity resolution ────────────


class TestSourceUriConvergence:
    def test_converges_on_existing_devonthink_registration_when_indexing_by_path(
        self, tmp_path,
    ) -> None:
        """The exact y8qtj scenario: a document registered under an
        x-devonthink-item:// identity, later re-indexed by a DIFFERENT disk
        path with --source-uri set, must resolve to the SAME document — not
        fork a second one."""
        from nexus.doc_indexer import _register_or_lookup_doc_id

        cat = ActiveCatalog()
        corpus_name = f"dt-papers-{_next_seq()}"
        owner = cat.register_owner(corpus_name, "curator")
        dt_uri = f"x-devonthink-item://{_next_seq():0>8}-CAFE-BABE-0000-000000000000"
        collection = "knowledge__dt-papers__voyage-context-3__v1"
        original_tumbler = cat.register(
            owner, "Zoology Paper",
            content_type="paper",
            file_path="x-devonthink-item-original.pdf",
            physical_collection=collection,
            source_uri=dt_uri,
        )

        # A later re-index of the SAME source, keyed on a fresh disk path
        # (mirrors the gtltb remediation driver's `nx index pdf <path>`).
        new_path = tmp_path / "Zoology-remediated.pdf"
        new_path.write_bytes(b"%PDF-1.4 stub")

        doc_id = _register_or_lookup_doc_id(
            new_path, corpus_name,
            content_type="paper",
            physical_collection=collection,
            source_uri=dt_uri,
        )

        assert doc_id == str(original_tumbler), (
            "must converge on the pre-existing DT-keyed document, not "
            "register a second one under the new path"
        )
        forked = [d for d in cat.all_documents() if d.source_uri == dt_uri]
        assert len(forked) == 1, (
            f"source_uri {dt_uri!r} must resolve to exactly one live "
            f"document; got {len(forked)} — this IS the y8qtj fork defect"
        )

    def test_source_uri_resolves_to_no_live_document_raises_and_registers_nothing(
        self, tmp_path,
    ) -> None:
        from nexus.doc_indexer import _register_or_lookup_doc_id
        from nexus.errors import SourceUriNotFoundError

        cat = ActiveCatalog()
        before = len(list(cat.all_documents()))

        path = tmp_path / "orphan.pdf"
        path.write_bytes(b"%PDF-1.4 stub")

        with pytest.raises(SourceUriNotFoundError):
            _register_or_lookup_doc_id(
                path, "dt-papers-orphan",
                content_type="paper",
                physical_collection="knowledge__dt-papers__voyage-context-3__v1",
                source_uri="x-devonthink-item://NEVER-REGISTERED-0000",
            )

        after = len(list(cat.all_documents()))
        assert after == before, (
            "no live document for --source-uri must NEVER fall back to "
            "registering a new one — that fallback is the y8qtj defect itself"
        )

    def test_source_uri_collection_mismatch_raises(self, tmp_path) -> None:
        from nexus.doc_indexer import _register_or_lookup_doc_id
        from nexus.errors import SourceUriCollectionMismatchError

        cat = ActiveCatalog()
        owner = cat.register_owner(f"dt-papers-mismatch-{_next_seq()}", "curator")
        dt_uri = f"x-devonthink-item://MISMATCH-{_next_seq():0>8}"
        home_collection = "knowledge__dt-papers__voyage-context-3__v1"
        cat.register(
            owner, "Mismatch Paper",
            content_type="paper",
            file_path="mismatch.pdf",
            physical_collection=home_collection,
            source_uri=dt_uri,
        )

        path = tmp_path / "mismatch.pdf"
        path.write_bytes(b"%PDF-1.4 stub")

        with pytest.raises(SourceUriCollectionMismatchError):
            _register_or_lookup_doc_id(
                path, "dt-papers-mismatch",
                content_type="paper",
                physical_collection="docs__default__voyage-context-3__v1",
                source_uri=dt_uri,
            )


# ── nexus-y8qtj reproduced INSIDE its own fix ────────────────────────────────
#
# The NO-GO critical from the y8qtj review: ``_index_document`` (the sole
# path for index_markdown) and ``_index_pdf_incremental`` (the >128-chunk
# PDF path) each called ``_register_or_lookup_doc_id`` internally with a
# bare file_path-only re-derivation — no doc_id/source_uri slot — even
# though their callers (index_markdown, index_pdf) had ALREADY resolved the
# correct doc_id up front, including via source_uri. A path-based re-index
# of a source_uri-registered document minted a FORK Document and threaded
# the fork's tumbler into ``hooks.fire_batch(catalog_doc_id=...)``, landing
# the manifest on the wrong document — reproducing the original y8qtj
# defect for (a) every ``--source-uri`` markdown run and (b) every
# >128-chunk PDF (the flagship case). These tests drive index_pdf() /
# index_markdown() END TO END (not just the internal helper in isolation)
# because that boundary — the caller's resolved doc_id crossing into the
# internal registration call — is exactly the gap unit tests of
# ``_register_or_lookup_doc_id`` alone cannot see.


def _fake_embed(texts: list[str], model: str, **_kwargs: Any) -> tuple[list[list[float]], str]:
    return [[0.1] * 8] * len(texts), model


def _mock_chunk(i: int) -> MagicMock:
    c = MagicMock()
    c.text = f"chunk text {i}" * 20
    c.chunk_index = i
    c.metadata = {
        "chunk_start_char": i * 200, "chunk_end_char": (i + 1) * 200,
        "page_number": i // 5 + 1,
    }
    return c


def _mock_t3() -> MagicMock:
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    t3 = MagicMock()
    t3.get_or_create_collection.return_value = mock_col
    return t3


class TestIndexPdfIncrementalDocIdReuse:
    def test_over_threshold_pdf_converges_on_source_uri_registration(
        self, tmp_path,
    ) -> None:
        """The flagship reproduction: a >128-chunk PDF (the Zoology class)
        re-indexed by a new disk path with --source-uri set must land its
        entire manifest on the PRE-EXISTING document, not fork a second
        one via the incremental path's internal registration call."""
        from nexus.doc_indexer import _INCREMENTAL_THRESHOLD, index_pdf

        cat = ActiveCatalog()
        corpus_name = f"dt-incr-{_next_seq()}"
        owner = cat.register_owner(corpus_name, "curator")
        dt_uri = f"x-devonthink-item://{_next_seq():0>8}-INCR-0000-0000-000000000000"
        collection = f"docs__{corpus_name.replace('_', '-')}__voyage-context-3__v1"
        original_tumbler = cat.register(
            owner, "Zoology Class",
            content_type="paper",
            file_path="zoology-original.pdf",
            physical_collection=collection,
            source_uri=dt_uri,
        )

        new_path = tmp_path / "zoology-remediated.pdf"
        new_path.write_bytes(b"%PDF-1.4 stub")

        n_chunks = _INCREMENTAL_THRESHOLD + 10
        mock_chunks = [_mock_chunk(i) for i in range(n_chunks)]
        t3 = _mock_t3()

        with patch("nexus.doc_indexer.PDFExtractor") as ext_cls, \
             patch("nexus.doc_indexer.PDFChunker") as chk_cls:
            ext_cls.return_value.extract.return_value = MagicMock(
                text="x" * 5000,
                metadata={"extraction_method": "docling", "page_count": 50,
                          "format": "markdown", "page_boundaries": []},
            )
            chk_cls.return_value.chunk.return_value = mock_chunks
            result = index_pdf(
                new_path, corpus=corpus_name, t3=t3,
                collection_name=collection,
                embed_fn=_fake_embed,
                source_uri=dt_uri,
                streaming="never",
            )

        assert result == n_chunks, (
            "the >128-chunk incremental path must still index every chunk"
        )

        docs = cat.by_owner(owner)
        assert len(docs) == 1, (
            f"exactly one live document expected for this owner after a "
            f"source_uri-keyed re-index; got {len(docs)} — this IS the "
            f"y8qtj fork defect reproduced inside its own fix"
        )
        assert str(docs[0].tumbler) == str(original_tumbler), (
            "the re-index must converge on the PRE-EXISTING document, not "
            "register a fresh one"
        )

        manifest = cat.get_manifest(str(original_tumbler))
        assert len(manifest) == n_chunks, (
            f"the full manifest ({n_chunks} chunks) must land on the "
            f"pre-existing document; got {len(manifest)} rows — a fork "
            f"would have stolen the manifest instead"
        )


class TestIndexMarkdownDocIdReuse:
    def test_source_uri_markdown_run_converges_on_existing_registration(
        self, tmp_path,
    ) -> None:
        """The markdown path never had an incremental threshold — EVERY
        --source-uri markdown run went through _index_document's bare
        file_path-only re-derivation, forking a second document for every
        one of them (not just large documents)."""
        from nexus.doc_indexer import index_markdown

        cat = ActiveCatalog()
        corpus_name = f"dt-md-{_next_seq()}"
        owner = cat.register_owner(corpus_name, "curator")
        dt_uri = f"x-devonthink-item://{_next_seq():0>8}-MD00-0000-0000-000000000000"
        collection = f"docs__{corpus_name.replace('_', '-')}__voyage-context-3__v1"
        original_tumbler = cat.register(
            owner, "Zoology Notes",
            content_type="prose",
            file_path="zoology-notes-original.md",
            physical_collection=collection,
            source_uri=dt_uri,
        )

        new_path = tmp_path / "zoology-notes-remediated.md"
        new_path.write_text("---\ntitle: Zoology Notes\n---\n\n# Hello\n\nWorld.\n")

        t3 = _mock_t3()
        chunk = _mock_chunk(0)
        chunk.metadata["header_path"] = "Hello"

        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
            chk_cls.return_value.chunk.return_value = [chunk]
            result = index_markdown(
                new_path, corpus=corpus_name, t3=t3,
                collection_name=collection,
                embed_fn=_fake_embed,
                source_uri=dt_uri,
            )

        assert result == 1

        docs = cat.by_owner(owner)
        assert len(docs) == 1, (
            f"exactly one live document expected for this owner after a "
            f"source_uri-keyed re-index; got {len(docs)} — this IS the "
            f"y8qtj fork defect reproduced inside its own fix"
        )
        assert str(docs[0].tumbler) == str(original_tumbler), (
            "the re-index must converge on the PRE-EXISTING document, not "
            "register a fresh one"
        )

        manifest = cat.get_manifest(str(original_tumbler))
        assert len(manifest) == 1, (
            f"the manifest must land on the pre-existing document; got "
            f"{len(manifest)} rows — a fork would have stolen it instead"
        )


# ── _check_document_fork: end-of-run fork observability ─────────────────────


class TestDocumentForkCheck:
    def test_fires_on_majority_shared_chashes(self) -> None:
        from nexus.doc_indexer import _check_document_fork

        cat = ActiveCatalog()
        owner = cat.register_owner(f"fork-test-{_next_seq()}", "curator")
        coll = "knowledge__fork-test__voyage-context-3__v1"
        old_doc = cat.register(
            owner, "Old Doc", content_type="paper",
            file_path=f"old-{_next_seq()}.pdf", physical_collection=coll,
        )
        new_doc = cat.register(
            owner, "New Doc", content_type="paper",
            file_path=f"new-{_next_seq()}.pdf", physical_collection=coll,
        )

        shared = ["1" * 64, "2" * 64, "3" * 64]
        unique_old = "5" * 64
        unique_new = "4" * 64

        cat.write_manifest(
            str(old_doc), [_chunk(c, i) for i, c in enumerate([*shared, unique_old])],
        )
        cat.write_manifest(
            str(new_doc), [_chunk(c, i) for i, c in enumerate([*shared, unique_new])],
        )

        with capture_logs() as cap:
            forks = _check_document_fork(str(new_doc), coll)

        # 3 of the new doc's 4 chashes (75%) already live in old_doc — above
        # the 0.5 warn fraction.
        assert forks == [(str(old_doc), 3)]

        warnings = [
            e for e in cap
            if e.get("log_level") == "warning" and e.get("event") == "index_possible_document_fork"
        ]
        assert len(warnings) == 1, f"expected exactly one fork warning, got {cap}"
        assert warnings[0]["new"] == str(new_doc)
        assert warnings[0]["existing"] == str(old_doc)
        assert warnings[0]["shared_chunks"] == 3
        assert warnings[0]["total_chunks"] == 4

    def test_stays_quiet_on_disjoint_documents(self) -> None:
        from nexus.doc_indexer import _check_document_fork

        cat = ActiveCatalog()
        owner = cat.register_owner(f"fork-disjoint-{_next_seq()}", "curator")
        coll = "knowledge__fork-disjoint__voyage-context-3__v1"
        doc_a = cat.register(
            owner, "Doc A", content_type="paper",
            file_path=f"a-{_next_seq()}.pdf", physical_collection=coll,
        )
        doc_b = cat.register(
            owner, "Doc B", content_type="paper",
            file_path=f"b-{_next_seq()}.pdf", physical_collection=coll,
        )

        cat.write_manifest(str(doc_a), [_chunk("6" * 64, 0)])
        cat.write_manifest(str(doc_b), [_chunk("7" * 64, 0)])

        with capture_logs() as cap:
            forks = _check_document_fork(str(doc_b), coll)

        assert forks == []
        fork_warnings = [e for e in cap if e.get("event") == "index_possible_document_fork"]
        assert fork_warnings == []

    def test_below_threshold_stays_quiet(self) -> None:
        """20% shared (below the 0.25 warn fraction) must not fire — this
        pins the threshold value itself. Review 2026-08-02 lowered the bar
        from 0.5 to 0.25 so the historical Zoology fork (132/395 = 0.334,
        the only real-world instance) actually fires; a case at 0.334 is
        pinned in test_historical_fork_fraction_fires below."""
        from nexus.doc_indexer import _check_document_fork

        cat = ActiveCatalog()
        owner = cat.register_owner(f"fork-belowthresh-{_next_seq()}", "curator")
        coll = "knowledge__fork-belowthresh__voyage-context-3__v1"
        old_doc = cat.register(
            owner, "Old Doc", content_type="paper",
            file_path=f"old-bt-{_next_seq()}.pdf", physical_collection=coll,
        )
        new_doc = cat.register(
            owner, "New Doc", content_type="paper",
            file_path=f"new-bt-{_next_seq()}.pdf", physical_collection=coll,
        )

        # 1 of 5 shared = 20%, below the >0.25 bar.
        shared = ["8" * 64]
        unique_new = ["9" * 64, "a" * 64, "b" * 64, "c" * 64]

        cat.write_manifest(str(old_doc), [_chunk(c, i) for i, c in enumerate(shared)])
        cat.write_manifest(
            str(new_doc), [_chunk(c, i) for i, c in enumerate([*shared, *unique_new])],
        )

        forks = _check_document_fork(str(new_doc), coll)
        assert forks == []


# ── nx catalog update --file-path ────────────────────────────────────────────


class TestCatalogUpdateFilePath:

    def test_historical_fork_fraction_fires(self) -> None:
        """The Zoology fork — the only real-world instance of this defect
        class — shared 132/395 = 0.334 of its chunks. The 0.5 threshold
        would NOT have fired on it (review 2026-08-02); 0.25 does. This is
        the falsify-by-history pin: if the threshold ever rises past what
        the one known real fork measured, this test goes red."""
        import structlog.testing

        from nexus.doc_indexer import _check_document_fork

        cat = ActiveCatalog()
        owner = cat.register_owner(f"fork-hist-{_next_seq()}", "curator")
        coll = "knowledge__fork-hist__voyage-context-3__v1"
        old_doc = cat.register(
            owner, "Old Doc", content_type="paper",
            file_path=f"old-h-{_next_seq()}.pdf", physical_collection=coll,
        )
        new_doc = cat.register(
            owner, "New Doc", content_type="paper",
            file_path=f"new-h-{_next_seq()}.pdf", physical_collection=coll,
        )
        # 2 of 6 shared = 0.333... — the historical fraction, above >0.25.
        shared = ["8" * 64, "9" * 64]
        unique_new = ["a" * 64, "b" * 64, "c" * 64, "d" * 64]
        cat.write_manifest(str(old_doc), [_chunk(c, i) for i, c in enumerate(shared)])
        cat.write_manifest(
            str(new_doc), [_chunk(c, i) for i, c in enumerate([*shared, *unique_new])],
        )
        with structlog.testing.capture_logs() as logs:
            _check_document_fork(str(new_doc), coll)
        assert any(
            e.get("event") == "index_possible_document_fork" for e in logs
        ), "the historical 0.334 fraction must fire at the 0.25 threshold"
    def test_file_path_updates_and_is_visible_on_resolve(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))
        runner = CliRunner()
        cat = ActiveCatalog()
        owner_name = f"fp-test-{_next_seq()}"
        owner_tumbler = cat.register_owner(owner_name, "repo", repo_hash=f"fp-{_next_seq()}")

        reg = runner.invoke(main, [
            "catalog", "register", "--title", "Repathed Doc",
            "--owner", str(owner_tumbler),
            "--file-path", "/old/dead/path.pdf",
        ])
        assert reg.exit_code == 0, reg.output
        tumbler = reg.output.strip().rsplit(" ", 1)[-1]

        new_path = "/new/live/path.pdf"
        upd = runner.invoke(main, [
            "catalog", "update", tumbler, "--file-path", new_path,
        ])
        assert upd.exit_code == 0, upd.output

        show = runner.invoke(main, ["catalog", "show", tumbler])
        assert show.exit_code == 0, show.output
        file_lines = [ln for ln in show.output.splitlines() if ln.startswith("File:")]
        assert file_lines == [f"File:       {new_path}"], show.output
        # source_uri is a SEPARATE column, deliberately untouched by
        # --file-path (nexus-y8qtj: file_path and source_uri are distinct
        # identities; --file-path repoints the physical location without
        # silently repointing catalog identity too).
        uri_lines = [ln for ln in show.output.splitlines() if ln.startswith("URI:")]
        assert uri_lines == ["URI:        file:///old/dead/path.pdf"], show.output
