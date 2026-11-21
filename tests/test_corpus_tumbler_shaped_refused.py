# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-p2m1x.28: a corpus name shaped like a repo owner segment (``1-20``) is refused.

``docs__{corpus}`` is built as ``docs__<corpus, underscores as hyphens>__<model>__v1``. A repo whose owner tumbler is
``1.20`` has the owner segment ``1-20`` and so the collection ``docs__1-20__<model>__v1``; a corpus named
``1-20`` (or ``1_20``) builds the same name, and the two merge by name. Every entry point that turns a corpus
name into a collection name goes through ``nexus.corpus.require_corpus_name``.
"""
from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from nexus.commands.dt import _resolve_dt_collection, dt
from nexus.commands.index import _index_run_refused_message, index
from nexus.corpus import TumblerShapedCorpusError, docs_leaf_fallback_collection_name, require_corpus_name


@pytest.mark.parametrize("corpus", ["1-20", "1_20", "12-345", "0-1", "7_7"])
def test_an_owner_segment_shaped_corpus_is_refused_naming_the_cause(corpus):
    with pytest.raises(TumblerShapedCorpusError) as err:
        require_corpus_name(corpus)
    message = str(err.value)
    assert corpus in message
    assert "owner segment" in message
    assert "docs__" in message
    assert "merge" in message


@pytest.mark.parametrize(
    "corpus", ["default", "dt", "delos", "1", "1-20-papers", "2024-05-01", "a-1-20", "1-a", "my_corpus", "1.20", ""],
)
def test_any_other_corpus_name_passes_and_is_returned_unchanged(corpus):
    assert require_corpus_name(corpus) == corpus


def test_the_refusal_is_a_click_usage_error_so_every_command_shows_it_cleanly():
    assert issubclass(TumblerShapedCorpusError, click.UsageError)


def test_the_leaf_fallback_derivation_refuses_before_it_derives():
    # Covers doc_indexer's three fallbacks and commands/index.py's dry-run: all call this one function.
    with pytest.raises(TumblerShapedCorpusError):
        docs_leaf_fallback_collection_name("1-20")
    with pytest.raises(TumblerShapedCorpusError):
        docs_leaf_fallback_collection_name("1_20")


def test_the_dt_default_collection_refuses_a_tumbler_shaped_corpus_for_markdown_and_pdf():
    for ext in (".md", ".pdf"):
        with pytest.raises(TumblerShapedCorpusError):
            _resolve_dt_collection(None, "1-20", ext)


def test_the_run_refused_message_renders_without_raising_for_a_tumbler_shaped_corpus():
    exc = type("E", (), {"referenced": 1, "present": 0, "missing": 1, "doc_id": "1.2.3"})()
    message = _index_run_refused_message(exc, corpus="1-20")
    assert "completion REFUSED" in message


@pytest.mark.parametrize("verb", ["md", "pdf"])
def test_nx_index_corpus_refuses_a_tumbler_shaped_name_before_any_work(verb, tmp_path):
    target = tmp_path / ("a.md" if verb == "md" else "a.pdf")
    target.write_bytes(b"# t\n\nbody\n" if verb == "md" else b"%PDF-1.4\n")
    result = CliRunner().invoke(index, [verb, str(target), "--corpus", "1-20"])
    assert result.exit_code == 2, result.output
    assert "owner segment" in result.output
    assert "1-20" in result.output


def test_nx_dt_index_and_capture_refuse_a_tumbler_shaped_corpus():
    for argv in (
        ["index", "--corpus", "1-20", "--uuid", "00000000-0000-0000-0000-000000000000"],
        ["capture", "--corpus", "1_20", "--file", "x.pdf"],
    ):
        result = CliRunner().invoke(dt, argv)
        assert result.exit_code == 2, (argv, result.output)
        assert "owner segment" in result.output
