# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-086 Phase 5: ``nx doc cite`` authoring CLI.

Closes Gap 6 (no one-shot "cite this claim" command). Composes
``search(structured=True)`` with the ``chunk_text_hash`` field surfaced
in Phase 3, emits a ready-to-paste ``[excerpt](chash:<hex>)`` markdown
link, and provides a ``--json`` schema for pipeline scripting.

Exit contract (RDR §Phase 5 Failure Modes):
  * 0 — cite emitted (JSON threshold_met flag reflects --min-similarity)
  * 1 — top result above --min-similarity; warning on stderr, stdout empty
        (JSON still returns candidates with threshold_met=false)
  * 2 — usage errors: empty chash_index (fresh install), empty collection
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from click.testing import CliRunner


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def cite_env(tmp_path: Path):
    """Catalog + T3 + ChashIndex with one resolvable chunk."""
    from nexus.catalog.catalog import Catalog
    from nexus.db.t2.chash_index import ChashIndex

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    cat = Catalog(cat_dir, cat_dir / ".catalog.db")

    t3 = chromadb.EphemeralClient()
    chash_hex = "c" * 64
    col = t3.get_or_create_collection("knowledge__cite")
    col.add(
        ids=["doc:0:chunk:0"],
        documents=["Orange foxes are clever and patient."],
        metadatas=[{"chunk_text_hash": chash_hex, "source_path": "p.pdf"}],
    )

    chash_index = ChashIndex(tmp_path / "t2.db")
    chash_index.upsert(
        chash=chash_hex, collection="knowledge__cite", chunk_chroma_id="doc:0:chunk:0",
    )
    yield cat, t3, chash_index, chash_hex
    chash_index.close()


# ── 5.1: cite emits markdown ─────────────────────────────────────────────────


class TestCiteEmitsMarkdownLink:
    def test_cite_emits_markdown_link_on_match(self, cite_env):
        from nexus.commands.doc import cite_cmd

        cat, t3, chash_index, chash = cite_env

        def _fake_search(**kwargs):
            return {
                "ids": ["doc:0:chunk:0"],
                "tumblers": [""],
                "distances": [0.12],
                "collections": ["knowledge__cite"],
                "chunk_text_hash": [chash],
            }

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ), patch("nexus.commands.doc._phase5_search", _fake_search):
            runner = CliRunner()
            result = runner.invoke(
                cite_cmd, ["orange foxes", "--against", "knowledge__cite"],
            )

        assert result.exit_code == 0, result.output
        # Markdown link shape: [excerpt](chash:<hex>)
        assert "](chash:" in result.output
        assert chash in result.output


# ── 5.2: --min-similarity gate ───────────────────────────────────────────────


class TestCiteMinSimilarityGate:
    def test_above_threshold_exits_1_stdout_empty(self, cite_env):
        from nexus.commands.doc import cite_cmd

        cat, t3, chash_index, chash = cite_env

        # Distance far above the default threshold.
        def _fake_search(**kwargs):
            return {
                "ids": ["doc:0:chunk:0"],
                "tumblers": [""],
                "distances": [0.99],  # very poor match
                "collections": ["knowledge__cite"],
                "chunk_text_hash": [chash],
            }

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ), patch("nexus.commands.doc._phase5_search", _fake_search):
            runner = CliRunner()
            result = runner.invoke(
                cite_cmd,
                [
                    "orange foxes", "--against", "knowledge__cite",
                    "--min-similarity", "0.30",
                ],
            )

        assert result.exit_code == 1, result.output
        # stdout should be empty for the markdown path — use separate stderr.
        # Click's CliRunner folds both; check the "above threshold" message.
        assert "threshold" in result.output.lower() or "above" in result.output.lower()


# ── 5.3: --json schema ──────────────────────────────────────────────────────


class TestCiteJsonSchema:
    def test_json_schema_matches_spec(self, cite_env):
        from nexus.commands.doc import cite_cmd

        cat, t3, chash_index, chash = cite_env

        def _fake_search(**kwargs):
            return {
                "ids": ["doc:0:chunk:0"],
                "tumblers": [""],
                "distances": [0.12],
                "collections": ["knowledge__cite"],
                "chunk_text_hash": [chash],
            }

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ), patch("nexus.commands.doc._phase5_search", _fake_search):
            runner = CliRunner()
            result = runner.invoke(
                cite_cmd,
                ["orange foxes", "--against", "knowledge__cite", "--json"],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "candidates" in payload
        assert "query" in payload
        assert "threshold_met" in payload
        assert len(payload["candidates"]) >= 1
        cand = payload["candidates"][0]
        for key in ("chash", "distance", "collection", "chunk_excerpt", "markdown_link"):
            assert key in cand, f"missing key {key!r} in candidate"
        assert cand["chash"] == chash
        assert cand["markdown_link"].endswith(f"](chash:{chash})")


# ── 5.4: empty-index short-circuit ──────────────────────────────────────────


class TestCiteEmptyIndexShortCircuit:
    def test_empty_chash_index_exits_2_with_actionable_message(
        self, tmp_path: Path,
    ):
        from nexus.catalog.catalog import Catalog
        from nexus.commands.doc import cite_cmd
        from nexus.db.t2.chash_index import ChashIndex

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        # Empty chash_index (no rows).
        chash_index = ChashIndex(tmp_path / "t2.db")
        t3 = chromadb.EphemeralClient()
        try:
            with patch(
                "nexus.commands.doc._phase4_catalog_t3_chash",
                return_value=(cat, t3, chash_index),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cite_cmd,
                    ["any claim", "--against", "knowledge__nope"],
                )
            assert result.exit_code == 2, result.output
            assert "backfill-hash" in result.output
        finally:
            chash_index.close()


# ── 5.5: tied candidates note ───────────────────────────────────────────────


class TestCiteTiedCandidatesNote:
    def test_tied_candidates_within_0_01_emit_note(self, cite_env):
        from nexus.commands.doc import cite_cmd

        cat, t3, chash_index, _ = cite_env
        c1, c2 = "a" * 64, "b" * 64

        # Register a second row so the empty-index guard passes.
        chash_index.upsert(
            chash=c1, collection="knowledge__cite", chunk_chroma_id="doc:0:chunk:0",
        )
        chash_index.upsert(
            chash=c2, collection="knowledge__cite", chunk_chroma_id="doc:0:chunk:0",
        )

        def _fake_search(**kwargs):
            return {
                "ids": ["id-a", "id-b"],
                "tumblers": ["", ""],
                "distances": [0.200, 0.205],  # within 0.01
                "collections": ["knowledge__cite"],
                "chunk_text_hash": [c1, c2],
            }

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ), patch("nexus.commands.doc._phase5_search", _fake_search):
            runner = CliRunner()
            result = runner.invoke(
                cite_cmd,
                ["claim", "--against", "knowledge__cite", "--limit", "5"],
            )

        assert result.exit_code == 0, result.output
        assert "tied" in result.output.lower()

    def test_tied_candidates_json_returns_all(self, cite_env):
        from nexus.commands.doc import cite_cmd

        cat, t3, chash_index, _ = cite_env
        c1, c2 = "a" * 64, "b" * 64
        chash_index.upsert(
            chash=c1, collection="knowledge__cite", chunk_chroma_id="doc:0:chunk:0",
        )
        chash_index.upsert(
            chash=c2, collection="knowledge__cite", chunk_chroma_id="doc:0:chunk:0",
        )

        def _fake_search(**kwargs):
            return {
                "ids": ["id-a", "id-b"],
                "tumblers": ["", ""],
                "distances": [0.200, 0.205],
                "collections": ["knowledge__cite"],
                "chunk_text_hash": [c1, c2],
            }

        with patch(
            "nexus.commands.doc._phase4_catalog_t3_chash",
            return_value=(cat, t3, chash_index),
        ), patch("nexus.commands.doc._phase5_search", _fake_search):
            runner = CliRunner()
            result = runner.invoke(
                cite_cmd,
                [
                    "claim", "--against", "knowledge__cite",
                    "--limit", "5", "--json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert len(payload["candidates"]) == 2
