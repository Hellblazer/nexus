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

        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
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

    def test_h2_inside_fenced_code_block_is_not_a_section_boundary(
        self,
    ) -> None:
        """Round-3 review Critical #2: shell snippets and quoted markdown
        inside ``` fences must not be matched as ``## section`` headings.

        Without code-fence stripping the shell-comment line below
        truncates ``Problem Statement`` early, leaving
        ``"## This is a shell comment"`` as a phantom section.
        """
        body = (
            "## Problem Statement\n\n"
            "Quorum invariants matter. See the shell example:\n\n"
            "```bash\n"
            "## This is a shell comment\n"
            "echo hello\n"
            "```\n\n"
            "More problem text.\n\n"
            "## Proposed Solution\n\nThe solution.\n"
        )
        sections = _parse_rdr_sections(body)
        assert "Problem Statement" in sections
        assert "Proposed Solution" in sections
        assert "This is a shell comment" not in sections
        # The full Problem Statement body is preserved through the fence.
        text = sections["Problem Statement"]
        assert "Quorum invariants matter" in text
        assert "More problem text" in text


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
        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
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
        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
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

        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
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


# ── The frontmatter reaches the parser (nexus-xho6c) ────────────────────────
#
# The indexers strip the frontmatter before chunking, so the text the extractor reassembles from T3
# chunks has none. Live, every rdr-frontmatter-v1 row (377 of 377) had empty extras, RDR-232's own
# included. These tests drive the entry points the aspect worker calls, with the chunk read replaced by
# the frontmatter-less body and a real file on disk.

_RDR_BODY = _SAMPLE_RDR_DOC.split("---\n", 2)[2]  # what chunks hold: the body, no header
_RDR_COLLECTION = "rdr__nexus-test"


def _write_rdr(tmp_path, name: str = "rdr-099-test.md", text: str = _SAMPLE_RDR_DOC):
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(text)
    return path


@pytest.fixture
def chunk_text(monkeypatch: pytest.MonkeyPatch):
    """The chunk read returns ``chunks['text']`` (the body, as the indexers store it); no T3 is reached."""
    from unittest.mock import MagicMock

    from nexus.aspect_readers import ReadOk

    chunks = {"text": _RDR_BODY}
    monkeypatch.setattr("nexus.aspect_extractor.get_t3", lambda: MagicMock())
    monkeypatch.setattr(
        "nexus.aspect_extractor.read_source",
        lambda uri, **_kw: ReadOk(text=chunks["text"], metadata={"scheme": "chroma"}),
    )
    monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("the RDR parser never calls the model")))
    return chunks


class TestRdrFrontmatterReachesTheParser:
    def test_the_header_of_the_source_file_is_put_back_in_front_of_the_chunk_text(
        self, tmp_path, chunk_text,
    ) -> None:
        path = _write_rdr(tmp_path)
        record = extract_aspects(content="", source_path=str(path), collection=_RDR_COLLECTION)
        assert record is not None
        assert record.extras["rdr_id"] == "RDR-099", "the id the lookup of sibling files reads"
        assert record.extras["rdr_status"] == "accepted"
        assert record.confidence == 1.0
        assert record.problem_formulation and record.experimental_baselines == ["Sequential Paxos", "Tendermint"], (
            "the body still comes from the indexed chunks")

    def test_an_absolute_lookup_path_finds_the_file_when_the_storage_key_is_relative(
        self, tmp_path, chunk_text,
    ) -> None:
        path = _write_rdr(tmp_path)
        record = extract_aspects(
            content="", source_path="docs/rdr/rdr-099-test.md", collection=_RDR_COLLECTION,
            lookup_path=str(path),
        )
        assert record is not None and record.extras["rdr_id"] == "RDR-099"

    def test_a_relative_source_path_finds_the_file_through_the_catalogs_source_uri(
        self, tmp_path, chunk_text, monkeypatch,
    ) -> None:
        path = _write_rdr(tmp_path)
        asked: list[tuple[str, str]] = []

        def source_uri(collection: str, doc_id: str) -> str:
            asked.append((collection, doc_id))
            return path.as_uri()

        monkeypatch.setattr("nexus.aspect_worker._resolve_source_uri_from_doc_id", source_uri)
        record = extract_aspects(
            content="", source_path="docs/rdr/rdr-099-test.md", collection=_RDR_COLLECTION,
            doc_id_lookup=lambda collection, sid: "1.2.20",
        )
        assert record is not None and record.extras["rdr_id"] == "RDR-099"
        assert asked == [(_RDR_COLLECTION, "1.2.20")]

    def test_the_batch_path_does_the_same_for_a_queue_row_with_a_doc_id(
        self, tmp_path, chunk_text, monkeypatch,
    ) -> None:
        from nexus.aspect_extractor import extract_aspects_batch

        path = _write_rdr(tmp_path)
        monkeypatch.setattr("nexus.aspect_worker._resolve_source_uri_from_doc_id", lambda c, d: path.as_uri())
        records = extract_aspects_batch([
            (_RDR_COLLECTION, "docs/rdr/rdr-099-test.md", "", "1.2.20"),
            (_RDR_COLLECTION, str(path), "", ""),
        ])
        assert [r.extras["rdr_id"] for r in records] == ["RDR-099", "RDR-099"]

    def test_without_a_readable_file_the_row_is_as_it_was_and_nothing_raises(
        self, tmp_path, chunk_text, monkeypatch,
    ) -> None:
        monkeypatch.setattr("nexus.aspect_worker._resolve_source_uri_from_doc_id", lambda c, d: "")
        for source_path in (str(tmp_path / "docs" / "rdr" / "gone.md"), "docs/rdr/rdr-099-test.md"):
            record = extract_aspects(
                content="", source_path=source_path, collection=_RDR_COLLECTION,
                doc_id_lookup=lambda collection, sid: "1.2.20",
            )
            assert record is not None and record.extras == {}, "no header to put back: the old, honest empty row"
            assert record.problem_formulation, "the sections still parse from the chunks"

    def test_only_a_md_file_is_read_and_a_header_in_the_chunks_is_not_doubled(
        self, tmp_path, chunk_text,
    ) -> None:
        txt = _write_rdr(tmp_path, "rdr-099-test.txt")
        record = extract_aspects(content="", source_path=str(txt), collection=_RDR_COLLECTION)
        assert record is not None and record.extras == {}, "a non-.md source is never read for its header"
        md = _write_rdr(tmp_path, "rdr-098-other.md", "---\nid: RDR-098\n---\nfile body\n")
        chunk_text["text"] = _SAMPLE_RDR_DOC  # a chunk set that kept its header
        record = extract_aspects(content="", source_path=str(md), collection=_RDR_COLLECTION)
        assert record is not None and record.extras["rdr_id"] == "RDR-099", "the chunk's own header wins"

    def test_a_file_with_no_header_adds_none(self, tmp_path, chunk_text) -> None:
        path = _write_rdr(tmp_path, "rdr-099-plain.md", "# no frontmatter here\n")
        record = extract_aspects(content="", source_path=str(path), collection=_RDR_COLLECTION)
        assert record is not None and record.extras == {}

    def test_the_content_the_caller_hands_over_is_parsed_as_given(self, tmp_path, chunk_text) -> None:
        path = _write_rdr(tmp_path, "rdr-099-test.md", "---\nid: RDR-777\n---\n")
        record = extract_aspects(content=_SAMPLE_RDR_DOC, source_path=str(path), collection=_RDR_COLLECTION)
        assert record is not None and record.extras["rdr_id"] == "RDR-099", "only sourced content gets a header"

    def test_other_prefixes_never_read_a_header_from_disk(self, tmp_path, chunk_text, monkeypatch) -> None:
        """A ``knowledge__`` row whose source file has a header: the text the model is prompted with stays
        header-free (the model call is replaced by a recorder; non-vacuous only if it was reached)."""
        import subprocess

        marker = "HEADER-ONLY-MARKER-7731"
        path = _write_rdr(tmp_path, "note-099.md", f"---\nid: {marker}\nstatus: accepted\n---\nfile body\n")
        prompts: list[str] = []

        def record(prompt: str, *_a, **_kw):
            prompts.append(prompt)
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="recorded")

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", record)
        extract_aspects(content="", source_path=str(path), collection="knowledge__nexus-test")
        assert prompts, "the model was prompted: the check saw the text it would have changed"
        assert marker not in "".join(prompts), "a non-RDR row never gets the file's header put in front"

    def test_a_symlink_that_leaves_docs_rdr_is_not_read(self, tmp_path, chunk_text) -> None:
        outside = tmp_path / "private" / "secret.md"
        outside.parent.mkdir()
        outside.write_text("---\nid: RDR-666\n---\nsecret\n")
        d = tmp_path / "docs" / "rdr"
        d.mkdir(parents=True)
        link = d / "rdr-099-link.md"
        link.symlink_to(outside)
        record = extract_aspects(content="", source_path=str(link), collection=_RDR_COLLECTION)
        assert record is not None and record.extras == {}, "the link is resolved first; its target is not under docs/rdr"

    def test_an_md_file_outside_any_docs_rdr_is_not_read(self, tmp_path, chunk_text) -> None:
        stray = tmp_path / "notes" / "rdr-099-stray.md"
        stray.parent.mkdir()
        stray.write_text(_SAMPLE_RDR_DOC)
        record = extract_aspects(content="", source_path=str(stray), collection=_RDR_COLLECTION)
        assert record is not None and record.extras == {}

    def test_a_malformed_source_uri_is_skipped_and_never_raises_into_the_batch(
        self, tmp_path, chunk_text, monkeypatch,
    ) -> None:
        from nexus.aspect_extractor import extract_aspects_batch

        path = _write_rdr(tmp_path)
        uris = {"1.2.20": "file://[x/y.md", "1.2.21": path.as_uri()}
        monkeypatch.setattr("nexus.aspect_worker._resolve_source_uri_from_doc_id", lambda c, d: uris[d])
        records = extract_aspects_batch([
            (_RDR_COLLECTION, "docs/rdr/rdr-099-test.md", "", "1.2.20"),
            (_RDR_COLLECTION, "docs/rdr/rdr-099-test.md", "", "1.2.21"),
        ])
        assert [r.extras.get("rdr_id") for r in records] == [None, "RDR-099"], (
            "the malformed row keeps its honest empty extras; its neighbour still gets its header")

    def test_the_catalogs_source_uri_wins_over_an_absolute_lookup_path(
        self, tmp_path, chunk_text, monkeypatch,
    ) -> None:
        catalog = _write_rdr(tmp_path, "rdr-099-test.md")
        stale = _write_rdr(tmp_path, "rdr-098-stale.md", "---\nid: RDR-098\n---\n")
        monkeypatch.setattr("nexus.aspect_worker._resolve_source_uri_from_doc_id", lambda c, d: catalog.as_uri())
        record = extract_aspects(
            content="", source_path="docs/rdr/rdr-099-test.md", collection=_RDR_COLLECTION,
            lookup_path=str(stale), doc_id_lookup=lambda collection, sid: "1.2.20",
        )
        assert record is not None and record.extras["rdr_id"] == "RDR-099", (
            "the doc_id-keyed catalog source is the document's own file; a path in the queue row may be stale")

    def test_the_aspect_worker_persists_the_rdr_id_for_a_queued_row(
        self, tmp_path, chunk_text, monkeypatch,
    ) -> None:
        """Through ``AspectExtractionWorker._process_row``: the row the worker writes (``complete_aspect``)
        carries the id."""
        import types

        import nexus.commands.enrich as enrich
        import nexus.mcp_infra as infra
        from nexus.aspect_worker import AspectExtractionWorker

        path = _write_rdr(tmp_path)

        class _Db:
            def __init__(self) -> None:
                self.completed: list[dict] = []

            def complete_aspect(self, fields: dict) -> None:
                self.completed.append(fields)

        db = _Db()
        monkeypatch.setattr(infra, "t2_index_write", lambda fn: fn(db))
        monkeypatch.setattr(enrich, "_build_catalog_manifest_lookup", lambda: None)
        monkeypatch.setattr("nexus.aspect_worker._resolve_source_uri_from_doc_id", lambda c, d: path.as_uri())
        row = types.SimpleNamespace(
            collection=_RDR_COLLECTION, source_path="docs/rdr/rdr-099-test.md", content="",
            doc_id="1.2.20", retry_count=0,
        )
        AspectExtractionWorker(poll_interval=10.0)._process_row(row)
        assert [d["extras"]["rdr_id"] for d in db.completed] == ["RDR-099"]
        assert db.completed[0]["extractor_name"] == "rdr-frontmatter-v1" and db.completed[0]["confidence"] == 1.0
