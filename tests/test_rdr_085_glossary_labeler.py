# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for RDR-085 Glossary-Aware Topic Labeler.

Pins three contracts:

  * Glossary resolver load order: ``.nexus.yml#taxonomy.glossary``
    wins over ``docs/glossary.md``; both are absent → empty dict.
  * Labeler migrates from ``subprocess.run(['claude', '-p'])`` to
    ``claude_dispatch(prompt, schema)``. Glossary text, when present,
    is prepended to the prompt so the LLM sees project vocabulary
    before the numbered topics.
  * Invariant: ``len(results) == len(items)``. Missing or schema-
    rejected labels become ``None`` in their slot; the caller's
    c-TF-IDF fallback fills the gap.

These tests do not exercise claude_dispatch itself — that substrate
already has its own test coverage (shipped with RDR-080).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ── Glossary resolver ────────────────────────────────────────────────────────


class TestLoadGlossary:

    def test_config_glossary_wins(self, tmp_path: Path) -> None:
        from nexus.glossary import load_glossary

        (tmp_path / ".nexus.yml").write_text(
            "taxonomy:\n"
            "  glossary:\n"
            "    SSMF: SelfSimilarMaskingField\n"
            "    CCE: Contextualized Chunk Embedding\n"
        )
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "glossary.md").write_text(
            "# Glossary\n\n- SSMF: OVERRIDDEN  (should NOT win)\n"
        )

        g = load_glossary(tmp_path)
        assert g.get("SSMF") == "SelfSimilarMaskingField"
        assert g.get("CCE") == "Contextualized Chunk Embedding"

    def test_markdown_glossary_fallback(self, tmp_path: Path) -> None:
        from nexus.glossary import load_glossary

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "glossary.md").write_text(
            "# Project Glossary\n\n"
            "- **SSMF**: SelfSimilarMaskingField\n"
            "- CCE: Contextualized Chunk Embedding\n"
            "- chash: content-addressed chunk hash\n"
        )

        g = load_glossary(tmp_path)
        assert g.get("SSMF") == "SelfSimilarMaskingField"
        assert g.get("CCE") == "Contextualized Chunk Embedding"
        assert g.get("chash") == "content-addressed chunk hash"

    def test_no_glossary_anywhere_returns_empty(self, tmp_path: Path) -> None:
        from nexus.glossary import load_glossary

        assert load_glossary(tmp_path) == {}

    def test_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        """A busted .nexus.yml must not crash the labeler — empty falls
        through to the c-TF-IDF path."""
        from nexus.glossary import load_glossary

        (tmp_path / ".nexus.yml").write_text(": ::: not valid yaml\n")
        assert load_glossary(tmp_path) == {}


class TestFormatForPrompt:

    def test_formats_as_bulleted_vocabulary(self) -> None:
        from nexus.glossary import format_for_prompt

        text = format_for_prompt({"SSMF": "SelfSimilarMaskingField", "CCE": "Contextualized Chunk Embedding"})
        assert "Project vocabulary" in text
        assert "SSMF: SelfSimilarMaskingField" in text
        assert "CCE: Contextualized Chunk Embedding" in text

    def test_empty_glossary_returns_empty_string(self) -> None:
        from nexus.glossary import format_for_prompt

        assert format_for_prompt({}) == ""

    def test_truncates_past_max_tokens(self) -> None:
        """At max_tokens=50 (generous for tests), only a handful of
        vocabulary entries fit; the rest are silently dropped rather
        than dominating the prompt."""
        from nexus.glossary import format_for_prompt

        big = {f"TERM{i}": f"expansion number {i} padded " * 10 for i in range(50)}
        text = format_for_prompt(big, max_tokens=50)
        # Very rough token proxy: 1 token ≈ 4 chars.  We assert the
        # output stays under ~200 chars (== ~50 tokens).
        assert len(text) < 400


# ── Labeler migration ────────────────────────────────────────────────────────


class TestLabelerDispatch:

    @pytest.mark.asyncio
    async def test_calls_claude_dispatch_not_subprocess(self) -> None:
        """The migrated labeler must go through claude_dispatch — not
        raw subprocess.run — so the unified auth / schema / unwrap-fix
        surface applies to labeling."""
        from nexus.commands import taxonomy_cmd

        dispatched = AsyncMock(return_value={
            "labels": [
                {"idx": 1, "label": "Pattern Matching"},
                {"idx": 2, "label": "Vector Search"},
            ],
        })
        items = [
            (["ART", "masking"], ["paper1.pdf:0", "paper2.pdf:3"]),
            (["cosine", "ANN"], ["paper3.pdf:1"]),
        ]

        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_labels_batch(items)

        assert dispatched.called, "labeler must dispatch via claude_dispatch"
        assert results == ["Pattern Matching", "Vector Search"]

    @pytest.mark.asyncio
    async def test_glossary_text_appears_in_prompt(self) -> None:
        """When glossary_text is supplied, the dispatcher receives a
        prompt whose head is the glossary preamble."""
        from nexus.commands import taxonomy_cmd

        captured: dict[str, str] = {}

        async def fake_dispatch(prompt: str, schema: dict, **kw) -> dict:
            captured["prompt"] = prompt
            return {"labels": [{"idx": 1, "label": "A Topic"}]}

        items = [(["term"], ["doc.pdf:0"])]
        with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch):
            await taxonomy_cmd._generate_labels_batch(
                items, glossary_text="Project vocabulary:\n- SSMF: SelfSimilarMaskingField\n",
            )

        assert "Project vocabulary" in captured["prompt"]
        assert "SSMF: SelfSimilarMaskingField" in captured["prompt"]
        # Glossary precedes the numbered topic list
        vocab_pos = captured["prompt"].find("Project vocabulary")
        topic_pos = captured["prompt"].find("1.")
        assert 0 <= vocab_pos < topic_pos, (
            "glossary must precede the topic list in the prompt"
        )

    @pytest.mark.asyncio
    async def test_no_glossary_no_preamble(self) -> None:
        from nexus.commands import taxonomy_cmd

        captured: dict[str, str] = {}

        async def fake_dispatch(prompt: str, schema: dict, **kw) -> dict:
            captured["prompt"] = prompt
            return {"labels": [{"idx": 1, "label": "A Topic"}]}

        items = [(["term"], ["doc.pdf:0"])]
        with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch):
            await taxonomy_cmd._generate_labels_batch(items)

        assert "Project vocabulary" not in captured["prompt"]

    @pytest.mark.asyncio
    async def test_length_invariant_on_partial_response(self) -> None:
        """If claude_dispatch returns fewer labels than items, the
        result list is still len(items); missing slots are None."""
        from nexus.commands import taxonomy_cmd

        items = [
            (["a"], ["p.pdf:0"]),
            (["b"], ["p.pdf:1"]),
            (["c"], ["p.pdf:2"]),
        ]
        # Only returns 2 labels for 3 topics, AND skips idx 2
        dispatched = AsyncMock(return_value={
            "labels": [
                {"idx": 1, "label": "First"},
                {"idx": 3, "label": "Third"},
            ],
        })
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_labels_batch(items)

        assert len(results) == 3
        assert results == ["First", None, "Third"]

    @pytest.mark.asyncio
    async def test_dispatch_exception_returns_all_none(self) -> None:
        """claude_dispatch raising (timeout, schema reject, etc.) returns
        a None-filled list — caller's c-TF-IDF fallback takes over."""
        from nexus.commands import taxonomy_cmd

        items = [(["a"], ["p.pdf:0"]), (["b"], ["p.pdf:1"])]
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            AsyncMock(side_effect=RuntimeError("schema reject")),
        ):
            results = await taxonomy_cmd._generate_labels_batch(items)

        assert results == [None, None]

    @pytest.mark.asyncio
    async def test_label_length_bounds_respected(self) -> None:
        """3 <= len(label) <= 60. Labels outside that window become None."""
        from nexus.commands import taxonomy_cmd

        items = [
            (["a"], ["p.pdf:0"]),
            (["b"], ["p.pdf:1"]),
            (["c"], ["p.pdf:2"]),
        ]
        dispatched = AsyncMock(return_value={
            "labels": [
                {"idx": 1, "label": "OK Label"},
                {"idx": 2, "label": "x"},  # too short
                {"idx": 3, "label": "Z" * 100},  # too long
            ],
        })
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_labels_batch(items)

        assert results == ["OK Label", None, None]

    @pytest.mark.asyncio
    async def test_empty_items_returns_empty_without_dispatch(self) -> None:
        """Empty batch is a fast path — no LLM subprocess spawned."""
        from nexus.commands import taxonomy_cmd

        dispatched = AsyncMock()
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_labels_batch([])

        assert results == []
        assert not dispatched.called
