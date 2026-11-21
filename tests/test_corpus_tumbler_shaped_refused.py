# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-p2m1x.28: a corpus name shaped like a repo owner segment (``1-20``) is refused where it names a docs collection.

``docs__{corpus}`` is built as ``docs__<corpus, underscores as hyphens>__<model>__v1``. A repo whose owner tumbler is
``1.20`` has the owner segment ``1-20`` and so the collection ``docs__1-20__<model>__v1``; a corpus named
``1-20`` (or ``1_20``) builds the same name, and the two merge by name. The refusal is raised by the one builder,
``nexus.corpus.docs_leaf_fallback_collection_name`` (``require_corpus_name``); the CLI front doors call the same
helper only when that builder is what will run, so a ``--collection`` or a PDF to ``knowledge__<corpus>-papers`` is not
refused for a corpus it never turns into a docs name.
"""
from __future__ import annotations

from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from nexus.commands.dt import _resolve_dt_collection, dt
from nexus.commands.index import _index_run_refused_message, index
from nexus.corpus import TumblerShapedCorpusError, docs_leaf_fallback_collection_name, require_corpus_name


@pytest.mark.parametrize("corpus", ["1-20", "1_20", "12-345", "0-1", "7_7", "2024-05"])
def test_an_owner_segment_shaped_corpus_is_refused_naming_the_cause_and_the_exit(corpus):
    with pytest.raises(TumblerShapedCorpusError) as err:
        require_corpus_name(corpus)
    message = str(err.value)
    assert corpus in message
    assert "owner segment" in message
    assert "docs__" in message
    assert "merge" in message
    assert "letter" in message, "the exit: a name with a letter in it (a date-shaped 2024-05 is refused too, on purpose)"


@pytest.mark.parametrize(
    "corpus", ["default", "dt", "delos", "1", "1-20-papers", "2024-05-01", "a-1-20", "1-a", "my_corpus", "1.20", ""],
)
def test_any_other_corpus_name_passes_and_is_returned_unchanged(corpus):
    assert require_corpus_name(corpus) == corpus


def test_the_refusal_is_a_click_usage_error_so_every_command_shows_it_cleanly():
    assert issubclass(TumblerShapedCorpusError, click.UsageError)


def test_the_one_builder_refuses_before_it_derives():
    for corpus in ("1-20", "1_20"):
        with pytest.raises(TumblerShapedCorpusError):
            docs_leaf_fallback_collection_name(corpus)


def test_the_dt_default_docs_name_goes_through_the_one_builder_and_refuses():
    with pytest.raises(TumblerShapedCorpusError):
        _resolve_dt_collection(None, "1-20", ".md")


@pytest.mark.usefixtures("cloud_mode")
def test_the_dt_pdf_default_is_a_knowledge_papers_name_no_owner_segment_can_collide_with():
    with patch("nexus.corpus.resolve_write_embedding_model", return_value="voyage-context-3"):
        assert _resolve_dt_collection(None, "1-20", ".pdf") == "knowledge__1-20-papers__voyage-context-3__v1"


@pytest.mark.usefixtures("cloud_mode")
def test_the_dt_docs_name_for_a_normal_corpus_is_what_it_was():
    with patch("nexus.corpus.resolve_write_embedding_model", return_value="voyage-context-3"):
        assert _resolve_dt_collection(None, "my_notes", ".md") == "docs__my-notes__voyage-context-3__v1"


def test_the_run_refused_message_renders_without_raising_for_a_tumbler_shaped_corpus():
    exc = type("E", (), {"referenced": 1, "present": 0, "missing": 1, "doc_id": "1.2.3"})()
    assert "completion REFUSED" in _index_run_refused_message(exc, corpus="1-20")


@pytest.mark.parametrize("verb", ["md", "pdf"])
def test_nx_index_corpus_refuses_a_tumbler_shaped_name_before_any_work(verb, tmp_path):
    target = tmp_path / ("a.md" if verb == "md" else "a.pdf")
    target.write_bytes(b"# t\n\nbody\n" if verb == "md" else b"%PDF-1.4\n")
    result = CliRunner().invoke(index, [verb, str(target), "--corpus", "1-20"])
    assert result.exit_code == 2, result.output
    assert "owner segment" in result.output
    assert "1-20" in result.output


@pytest.mark.parametrize("verb", ["md", "pdf"])
def test_nx_index_with_a_collection_does_not_refuse_the_corpus_it_never_turns_into_a_docs_name(verb, tmp_path):
    target = tmp_path / ("a.md" if verb == "md" else "a.pdf")
    target.write_bytes(b"# t\n\nbody\n" if verb == "md" else b"%PDF-1.4\n")
    # The run is stopped at the collection resolver, which both commands call right after the corpus check, so
    # nothing past it (storage, an extractor) runs; what is pinned is that the corpus check did not refuse first.
    with patch("nexus.corpus.t3_collection_name", side_effect=click.ClickException("stopped at the resolver")):
        result = CliRunner().invoke(index, [verb, str(target), "--corpus", "1-20", "--collection", "knowledge__x"])
    assert "stopped at the resolver" in result.output, result.output
    assert "owner segment" not in result.output, result.output


def test_nx_index_md_with_a_normal_corpus_proceeds_to_the_indexer(tmp_path):
    target = tmp_path / "a.md"
    target.write_bytes(b"# t\n\nbody\n")
    with patch("nexus.doc_indexer.index_markdown", return_value={"chunks": 3, "sections": 1}) as indexer:
        result = CliRunner().invoke(index, ["md", str(target), "--corpus", "my-notes"])
    assert result.exit_code == 0, result.output
    assert indexer.call_args.kwargs["corpus"] == "my-notes"
    assert "Indexed 3 chunk(s)." in result.output


def test_nx_dt_capture_refuses_a_tumbler_shaped_corpus_for_a_markdown_capture_only():
    for argv in (["capture", "--corpus", "1_20", "--file", "x.md"], ["capture", "--corpus", "1_20", "https://e.org/p"]):
        with patch("nexus.commands.dt._is_darwin", return_value=False):
            result = CliRunner().invoke(dt, argv)
        assert result.exit_code == 2, (argv, result.output)
        assert "owner segment" in result.output
    # A PDF capture is not refused for the corpus; _is_darwin is patched off so the run stops at its own platform
    # check, before DEVONthink is reached (the command is DT-bound).
    with patch("nexus.commands.dt._is_darwin", return_value=False):
        pdf = CliRunner().invoke(dt, ["capture", "--corpus", "1_20", "--file", "x.pdf"])
    assert "owner segment" not in pdf.output and "macOS-only" in pdf.output, pdf.output
    # A markdown capture with a --collection never builds the docs name: not refused either.
    with patch("nexus.commands.dt._is_darwin", return_value=False):
        named = CliRunner().invoke(dt, ["capture", "--corpus", "1_20", "--collection", "knowledge__x", "--file", "x.md"])
    assert "owner segment" not in named.output and "macOS-only" in named.output, named.output


def test_nx_dt_index_refuses_a_tumbler_shaped_corpus_before_it_reads_any_devonthink_record():
    # The corpus is checked up front, so a mixed batch (PDFs, then a markdown note) refuses before its PDFs are indexed.
    with patch("nexus.commands.dt._gather_records") as gather:
        result = CliRunner().invoke(dt, ["index", "--corpus", "1-20", "--uuid", "00000000-0000-0000-0000-000000000000"])
    assert result.exit_code == 2, result.output
    assert "owner segment" in result.output
    gather.assert_not_called()


def test_nx_dt_index_with_a_collection_or_a_normal_corpus_goes_on_to_read_the_records():
    for argv in (["--corpus", "1-20", "--collection", "knowledge__x"], ["--corpus", "my-notes"]):
        with patch("nexus.commands.dt._gather_records", return_value=[]) as gather:
            result = CliRunner().invoke(dt, ["index", *argv, "--uuid", "00000000-0000-0000-0000-000000000000"])
        assert "owner segment" not in result.output, (argv, result.output)
        gather.assert_called_once()
