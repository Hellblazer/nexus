# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-086 Phase 4: consumers of ``Catalog.resolve_chash``.

Covers the three deferred items from RDR-083 §v1 Scope Reduction:

  * ``nx doc check-grounding --fail-ungrounded`` — exit 1 on any chash
    that fails to resolve; identify each file:line.
  * ``nx doc check-extensions`` — resolve chash → doc_id BEFORE calling
    ``chunk_grounded_in`` (caller-side fix; chunk_grounded_in signature
    and semantics unchanged). Remove the v1 ``[experimental]`` marker
    and the "all inputs returned no_data" WARNING.
  * ``nx doc render --expand-citations`` — resolve each chash span and
    emit a footnote block with the chunk text. Unresolvable chash
    renders as ``[unresolved chash: <first8>…]`` rather than crashing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from click.testing import CliRunner


# ── Shared seed helper ───────────────────────────────────────────────────────


def _seed_catalog_and_t3(tmp_path: Path):
    """Create a Catalog, T3 EphemeralClient, and T2 ChashIndex. Return
    (cat, t3, chash_index, chash_hex) after seeding one resolvable chunk.

    ``chromadb.EphemeralClient()`` is a process-shared singleton — drop
    the phase4-specific collection before recreating so two tests in
    the same process don't leak chunks between ``knowledge__phase4``
    instances.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.db.t2.chash_index import ChashIndex

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    cat = Catalog(cat_dir, cat_dir / ".catalog.db")

    t3 = chromadb.EphemeralClient()
    try:
        t3.delete_collection("knowledge__phase4")
    except Exception:
        pass
    chash = "a" * 64
    col = t3.get_or_create_collection("knowledge__phase4")
    col.add(
        ids=["doc:0:chunk:0"],
        documents=["Orange foxes and their clever strategies."],
        metadatas=[{"chunk_text_hash": chash, "source_path": "paper.pdf"}],
    )

    chash_index = ChashIndex(tmp_path / "t2.db")
    chash_index.upsert(
        chash=chash, collection="knowledge__phase4", chunk_chroma_id="doc:0:chunk:0",
    )
    return cat, t3, chash_index, chash


# ── 4.1: check-grounding --fail-ungrounded ───────────────────────────────────


class TestCheckGroundingFailUngrounded:
    def test_exits_zero_when_all_chash_resolve(self, tmp_path: Path):
        from nexus.commands.doc import check_grounding_cmd

        cat, t3, chash_index, chash = _seed_catalog_and_t3(tmp_path)

        doc = tmp_path / "grounded.md"
        doc.write_text(f"See [foxes](chash:{chash}).\n")

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ):
            runner = CliRunner()
            result = runner.invoke(
                check_grounding_cmd, [str(doc), "--fail-ungrounded"],
            )

        assert result.exit_code == 0, result.output

    def test_exits_nonzero_on_unresolved_chash(self, tmp_path: Path):
        from nexus.commands.doc import check_grounding_cmd

        cat, t3, chash_index, known = _seed_catalog_and_t3(tmp_path)
        # Use a chash unique to this test — chromadb.EphemeralClient is a
        # process-shared singleton, so a hex literal like "f"*64 could
        # collide with a chunk seeded by another test's fallback scenario
        # and make the "unresolved" assertion silently pass.
        fake_chash = "9" * 63 + "4"

        doc = tmp_path / "mixed.md"
        doc.write_text(
            f"Real: [x](chash:{known}).\n"
            f"Fake: [y](chash:{fake_chash}).\n"
        )

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ):
            runner = CliRunner()
            result = runner.invoke(
                check_grounding_cmd, [str(doc), "--fail-ungrounded"],
            )

        assert result.exit_code == 1, result.output
        # Error output should identify the file + line of the fake chash.
        assert fake_chash[:8] in result.output or "unresolved" in result.output.lower()
        assert str(doc) in result.output or "mixed.md" in result.output

    def test_fail_ungrounded_inactive_without_flag(self, tmp_path: Path):
        """Default behavior unchanged — no resolution + no nonzero exit."""
        from nexus.commands.doc import check_grounding_cmd

        cat, t3, chash_index, _ = _seed_catalog_and_t3(tmp_path)
        fake = "f" * 64

        doc = tmp_path / "unchecked.md"
        doc.write_text(f"Fake: [y](chash:{fake}).\n")

        runner = CliRunner()
        result = runner.invoke(check_grounding_cmd, [str(doc)])
        assert result.exit_code == 0, result.output


# ── 4.2: check-extensions caller-side fix ────────────────────────────────────


class TestCheckExtensionsResolvesChashToDocId:
    def test_chash_resolved_to_doc_id_before_chunk_grounded_in(
        self, tmp_path: Path,
    ):
        """chunk_grounded_in must receive the Chroma-scoped doc_id (from
        resolve_chash), not the raw chash hex.
        """
        from nexus.commands.doc import check_extensions_cmd

        cat, t3, chash_index, chash = _seed_catalog_and_t3(tmp_path)

        doc = tmp_path / "ext.md"
        doc.write_text(f"See [foxes](chash:{chash}).\n")

        # Capture the doc_id chunk_grounded_in is called with.
        captured: list[tuple[str, str, float]] = []

        def fake_cgi(doc_id: str, source_collection: str, *, threshold: float):
            captured.append((doc_id, source_collection, threshold))
            return 0.85  # above threshold → not a candidate, but probed

        fake_taxonomy = MagicMock()
        fake_taxonomy.chunk_grounded_in = fake_cgi

        from contextlib import contextmanager

        @contextmanager
        def _fake_taxonomy_ctx():
            yield fake_taxonomy

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ), patch(
            "nexus.commands.doc._phase4_t2_taxonomy",
            _fake_taxonomy_ctx,
        ):
            runner = CliRunner()
            result = runner.invoke(
                check_extensions_cmd,
                [str(doc), "--primary-source", "knowledge__phase4"],
            )

        assert result.exit_code == 0, result.output
        assert captured, "chunk_grounded_in was never called"
        seen_doc_id, _, _ = captured[0]
        # Critical: the DocId passed must be the resolved doc_id, NOT the chash.
        assert seen_doc_id == "doc:0:chunk:0", (
            f"expected resolved doc_id, got {seen_doc_id!r} "
            f"(this is the RDR-083 v1 inertness bug)"
        )

    def test_experimental_marker_and_warning_removed(self):
        """The docstring no longer carries [experimental] or the inertness WARNING."""
        from nexus.commands.doc import check_extensions_cmd

        docstring = check_extensions_cmd.__doc__ or ""
        assert "[experimental]" not in docstring
        assert "inertness" not in docstring.lower()


# ── 4.3: nx doc render --expand-citations ────────────────────────────────────


class TestRenderExpandCitations:
    def test_expand_citations_emits_footnote_block(self, tmp_path: Path):
        from nexus.commands.doc import render_cmd

        cat, t3, chash_index, chash = _seed_catalog_and_t3(tmp_path)

        doc = tmp_path / "src.md"
        doc.write_text(f"See [foxes](chash:{chash}) for context.\n")

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ):
            runner = CliRunner()
            result = runner.invoke(
                render_cmd,
                [
                    str(doc),
                    "--allow-unresolved",  # no bd/rdr/anchor tokens here
                    "--expand-citations",
                ],
            )

        assert result.exit_code == 0, result.output

        # The rendered sibling should include a footnotes block referencing
        # the chash chunk_text.
        rendered = tmp_path / "src.rendered.md"
        assert rendered.exists(), result.output
        body = rendered.read_text()
        assert "Orange foxes" in body, (
            f"expected chunk text expanded into footnotes; got:\n{body}"
        )

    def test_render_without_flag_preserves_current_behavior(
        self, tmp_path: Path,
    ):
        """Default render (no --expand-citations) leaves chash spans alone."""
        from nexus.commands.doc import render_cmd

        cat, t3, chash_index, chash = _seed_catalog_and_t3(tmp_path)

        doc = tmp_path / "src2.md"
        doc.write_text(f"See [foxes](chash:{chash}).\n")

        runner = CliRunner()
        result = runner.invoke(
            render_cmd, [str(doc), "--allow-unresolved"],
        )
        assert result.exit_code == 0, result.output
        rendered = tmp_path / "src2.rendered.md"
        assert rendered.exists()
        body = rendered.read_text()
        # Without expansion, the chash literal stays verbatim, no footnotes.
        assert f"chash:{chash}" in body
        assert "Orange foxes" not in body  # chunk text NOT inlined

    def test_unresolved_chash_renders_marker_not_crash(self, tmp_path: Path):
        from nexus.commands.doc import render_cmd

        cat, t3, chash_index, _ = _seed_catalog_and_t3(tmp_path)
        fake = "b" * 64

        doc = tmp_path / "bad.md"
        doc.write_text(f"See [x](chash:{fake}).\n")

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ):
            runner = CliRunner()
            result = runner.invoke(
                render_cmd,
                [str(doc), "--allow-unresolved", "--expand-citations"],
            )
        assert result.exit_code == 0, result.output

        rendered = tmp_path / "bad.rendered.md"
        body = rendered.read_text() if rendered.exists() else result.output
        assert (
            "unresolved chash" in body.lower()
            or fake[:8] in body
        ), f"expected unresolved marker, got:\n{body}"
