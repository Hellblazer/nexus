# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 Phase F: rdr-frontmatter-v1 deterministic extractor.

The ``rdr__*`` collection prefix routes to a pure-Python parser
(``_parse_rdr_aspects``) rather than the Claude CLI subprocess path.
RDRs carry YAML frontmatter + labelled markdown sections; a
deterministic parser is more reliable and zero-cost compared to
forcing the 5-field LLM extraction shape.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.aspect_extractor import (
    extract_aspects, select_config,
    _parse_rdr_aspects, _parse_rdr_sections,
    _parse_rdr_alternatives, _parse_simple_yaml,
    _split_rdr_frontmatter,
)


# ── select_config routing ───────────────────────────────────────────────────


class TestRouting:
    def test_rdr_prefix_routes_to_rdr_extractor(self) -> None:
        config = select_config("rdr__nexus")
        assert config is not None
        assert config.extractor_name == "rdr-frontmatter-v1"
        assert config.parser_fn is not None

    def test_rdr_prefix_uses_parser_fn_not_subprocess(self) -> None:
        """Calling extract_aspects on rdr__* must not invoke
        subprocess.run — the parser_fn shortcut takes precedence."""
        sample = _SAMPLE_RDR_DOC

        with patch("subprocess.run", side_effect=AssertionError(
            "RDR extractor must not invoke subprocess",
        )):
            record = extract_aspects(
                content=sample,
                source_path="/docs/rdr/rdr-099-test.md",
                collection="rdr__nexus",
            )

        assert record is not None
        assert record.extractor_name == "rdr-frontmatter-v1"
        assert record.problem_formulation
        assert record.proposed_method


# ── Frontmatter splitter ────────────────────────────────────────────────────


class TestFrontmatter:
    def test_splits_frontmatter_and_body(self) -> None:
        fm, body = _split_rdr_frontmatter(_SAMPLE_RDR_DOC)
        assert fm["id"] == "RDR-099"
        assert fm["status"] == "accepted"
        assert "Problem Statement" in body

    def test_no_frontmatter_returns_empty_dict(self) -> None:
        fm, body = _split_rdr_frontmatter("# Just a heading\n\ntext")
        assert fm == {}
        assert "Just a heading" in body

    def test_unterminated_frontmatter_returns_empty_dict(self) -> None:
        fm, body = _split_rdr_frontmatter(
            "---\ntitle: Foo\n# never closed\n\nbody"
        )
        assert fm == {}

    def test_inline_list_parses(self) -> None:
        fm = _parse_simple_yaml('related_issues: [RDR-040, RDR-056]')
        assert fm["related_issues"] == ["RDR-040", "RDR-056"]

    def test_quoted_string_unwrapped(self) -> None:
        fm = _parse_simple_yaml('title: "A Title With Spaces"')
        assert fm["title"] == "A Title With Spaces"

    def test_numeric_scalar_parses_as_int(self) -> None:
        fm = _parse_simple_yaml("priority: 1")
        assert fm["priority"] == 1

    def test_block_scalar_indicator_stored_as_none(self) -> None:
        """Substantive critic Significant #6: previously stored the
        literal ``|`` character (corruption); now stores None and
        skips indented continuation lines."""
        text = (
            "title: Foo\n"
            "note: |\n"
            "  multi-line content\n"
            "  more content\n"
            "status: draft\n"
        )
        fm = _parse_simple_yaml(text)
        assert fm["title"] == "Foo"
        assert fm["note"] is None  # not the literal "|"
        assert fm["status"] == "draft"

    def test_block_list_accumulates_items(self) -> None:
        """Substantive critic Significant #6: previously parsed
        ``related:`` (no inline value) as the empty string and
        silently dropped the ``- foo`` continuation lines. Now
        accumulates them into a list."""
        text = (
            "title: Foo\n"
            "related:\n"
            "  - nexus-abc\n"
            "  - nexus-def\n"
            "status: draft\n"
        )
        fm = _parse_simple_yaml(text)
        assert fm["title"] == "Foo"
        assert fm["related"] == ["nexus-abc", "nexus-def"]
        assert fm["status"] == "draft"

    def test_quoted_block_list_items_stripped(self) -> None:
        text = (
            "tags:\n"
            '  - "first"\n'
            "  - 'second'\n"
            "  - third\n"
        )
        fm = _parse_simple_yaml(text)
        assert fm["tags"] == ["first", "second", "third"]

    def test_block_scalar_indicator_variants(self) -> None:
        """All block scalar indicators (``|``, ``>``, ``|-``, ``>+``)
        store None and skip continuation lines."""
        for indicator in ("|", ">", "|-", ">-", "|+", ">+"):
            text = f"key: {indicator}\n  content\nnext: ok\n"
            fm = _parse_simple_yaml(text)
            assert fm["key"] is None, f"failed for indicator {indicator!r}"
            assert fm["next"] == "ok"


# ── Section splitter ────────────────────────────────────────────────────────


class TestSections:
    def test_splits_h2_sections(self) -> None:
        body = (
            "# Title\n\n"
            "## Problem Statement\n\nThe problem text.\n\n"
            "## Proposed Solution\n\nThe solution text.\n\n"
            "## References\n\nrefs.\n"
        )
        sections = _parse_rdr_sections(body)
        assert "Problem Statement" in sections
        assert "Proposed Solution" in sections
        assert "References" in sections
        assert sections["Problem Statement"].strip() == "The problem text."

    def test_subsections_belong_to_parent(self) -> None:
        body = (
            "## Alternatives Considered\n\n"
            "Intro text.\n\n"
            "### Alternative 1: Foo\n\nFoo description.\n\n"
            "### Alternative 2: Bar\n\nBar description.\n\n"
        )
        sections = _parse_rdr_sections(body)
        text = sections["Alternatives Considered"]
        assert "Foo description" in text
        assert "Bar description" in text


# ── Alternatives parser ─────────────────────────────────────────────────────


class TestAlternatives:
    def test_parses_alternative_n_titles(self) -> None:
        text = (
            "Intro.\n\n"
            "### Alternative 1: Keep it simple\n\nfoo\n\n"
            "### Alternative 2: Use the heavy hammer\n\nbar\n\n"
        )
        titles = _parse_rdr_alternatives(text)
        assert titles == ["Keep it simple", "Use the heavy hammer"]

    def test_parses_plain_alternative_headers(self) -> None:
        """RDRs sometimes use plain `### <title>` without "Alternative N:".
        Still extract the title."""
        text = (
            "### Lock-free queue\n\nfoo\n\n"
            "### Lock-based queue\n\nbar\n\n"
        )
        titles = _parse_rdr_alternatives(text)
        assert titles == ["Lock-free queue", "Lock-based queue"]

    def test_no_alternatives_returns_empty_list(self) -> None:
        assert _parse_rdr_alternatives("Just a paragraph.") == []


# ── End-to-end parse ────────────────────────────────────────────────────────


class TestParseRdrAspects:
    def test_full_rdr_extraction(self) -> None:
        result = _parse_rdr_aspects(_SAMPLE_RDR_DOC)

        assert "consensus" in result["problem_formulation"].lower()
        assert "raft variant" in result["proposed_method"].lower()
        assert result["experimental_datasets"] == []
        assert result["experimental_baselines"] == [
            "Sequential Paxos", "Tendermint",
        ]
        assert "throughput improvement" in result["experimental_results"].lower()
        assert result["confidence"] == 1.0

        # Frontmatter promoted into extras under rdr_ prefix.
        extras = result["extras"]
        assert extras["rdr_id"] == "RDR-099"
        assert extras["rdr_type"] == "Feature"
        assert extras["rdr_status"] == "accepted"
        assert extras["related_issues"] == ["RDR-040", "RDR-056"]

    def test_truncates_long_sections(self) -> None:
        long_text = "x" * 2000
        doc = (
            "---\nid: RDR-001\n---\n"
            "## Problem Statement\n\n" + long_text + "\n\n"
        )
        result = _parse_rdr_aspects(doc)
        assert len(result["problem_formulation"]) <= 803  # 800 + "..."

    def test_missing_sections_yield_empty_strings(self) -> None:
        doc = (
            "---\nid: RDR-001\n---\n"
            "# Title\n\n## References\n\nrefs.\n"
        )
        result = _parse_rdr_aspects(doc)
        assert result["problem_formulation"] == ""
        assert result["proposed_method"] == ""
        assert result["experimental_baselines"] == []

    def test_section_alias_problem_short_form(self) -> None:
        """`## Problem` (without "Statement") still maps to
        problem_formulation."""
        doc = (
            "---\nid: RDR-001\n---\n"
            "## Problem\n\nThe problem.\n"
        )
        result = _parse_rdr_aspects(doc)
        assert result["problem_formulation"] == "The problem."

    def test_no_frontmatter_yields_empty_extras(self) -> None:
        doc = "# Plain markdown\n\n## Problem Statement\n\nx\n"
        result = _parse_rdr_aspects(doc)
        assert result["extras"] == {}
        assert result["problem_formulation"] == "x"


# ── extract_aspects integration ────────────────────────────────────────────


class TestExtractAspectsRdrPath:
    def test_extract_aspects_for_rdr_collection(self) -> None:
        with patch("subprocess.run", side_effect=AssertionError(
            "RDR path must not invoke subprocess",
        )):
            record = extract_aspects(
                content=_SAMPLE_RDR_DOC,
                source_path="/docs/rdr/rdr-099-test.md",
                collection="rdr__nexus",
            )
        assert record is not None
        assert record.extractor_name == "rdr-frontmatter-v1"
        assert record.confidence == 1.0
        assert record.problem_formulation
        assert record.experimental_baselines == [
            "Sequential Paxos", "Tendermint",
        ]
        assert record.extras["rdr_id"] == "RDR-099"

    def test_extract_aspects_handles_unparseable_rdr_gracefully(self) -> None:
        """Garbage input returns null-fields, not an exception."""
        with patch("subprocess.run", side_effect=AssertionError(
            "RDR path must not invoke subprocess",
        )):
            record = extract_aspects(
                content="not an RDR at all",
                source_path="/garbage.md",
                collection="rdr__nexus",
            )
        assert record is not None
        assert record.extractor_name == "rdr-frontmatter-v1"
        # Sections all missing → null-ish fields, but the parser
        # returns successfully (no exception).
        assert record.problem_formulation == ""
        assert record.proposed_method == ""
        assert record.experimental_baselines == []

    def test_parser_fn_exception_yields_null_fields(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a parser_fn raises, the extractor returns a null-fields
        record rather than letting the exception escape."""
        config = select_config("rdr__nexus")

        def boom(content, source_path, collection):
            raise RuntimeError("synthetic parser failure")

        # Patch the config's parser_fn to a raising stub.
        from dataclasses import replace
        bad_config = replace(config, parser_fn=boom)
        from nexus import aspect_extractor as mod
        monkeypatch.setitem(mod._REGISTRY, "rdr__", bad_config)

        with patch("subprocess.run", side_effect=AssertionError(
            "must not invoke subprocess",
        )):
            record = extract_aspects(
                content="anything",
                source_path="/x.md",
                collection="rdr__nexus",
            )
        assert record is not None
        assert record.problem_formulation is None  # null-fields fallback
        assert record.extractor_name == "rdr-frontmatter-v1"


# ── Sample RDR document ─────────────────────────────────────────────────────

_SAMPLE_RDR_DOC = """\
---
title: "Hypothetical RDR for testing"
id: RDR-099
type: Feature
status: accepted
priority: medium
author: Hal Hildebrand
related_issues: [RDR-040, RDR-056]
---

# RDR-099: Hypothetical RDR for testing

## Problem Statement

The consensus protocol family has too many incremental refinements with
overlapping vocabulary, making it hard to reason about which guarantees
are preserved across variants.

## Research Findings

### RF-1: Background

Some background.

## Proposed Solution

A Raft variant with single-leader writes and a coordinator log that
captures cross-variant invariants. Replaces the ad-hoc lookup table
with a typed schema.

## Alternatives Considered

### Alternative 1: Sequential Paxos

Spec-conform but requires more rounds.

### Alternative 2: Tendermint

Different liveness assumptions; incompatible with the target failure model.

## Trade-offs

Some trade-off discussion.

## Validation

Spike measured 30% throughput improvement on the benchmark workload
across three runs.

## References

References here.
"""
