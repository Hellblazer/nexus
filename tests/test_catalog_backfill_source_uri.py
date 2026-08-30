# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-poigc: ``nx catalog backfill-source-uri`` re-derives chroma://
catalog source_uri values for filesystem-backed (file:// routed)
collections, and leaves the knowledge__ store_put-origin marker
population alone."""
from __future__ import annotations

import json

from click.testing import CliRunner

from nexus.cli import main


class _FakeDoc:
    def __init__(self, tumbler: str, physical_collection: str, source_uri: str):
        self.tumbler = tumbler
        self.physical_collection = physical_collection
        self.source_uri = source_uri


class _FakeReader:
    def __init__(self, docs):
        self._docs = docs

    def all_documents(self):
        return self._docs


class _FakeWriter:
    def __init__(self):
        self.update_many_calls: list[list[dict]] = []
        self.closed = False

    def update_many(self, updates):
        self.update_many_calls.append(updates)
        return [1 for _ in updates]

    def close(self):
        self.closed = True


def _invoke(monkeypatch, docs, args, writer=None):
    from nexus.commands import catalog as _cat_cmd

    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: _FakeReader(docs))
    if writer is not None:
        monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", lambda: writer)
    return CliRunner().invoke(main, ["catalog", "backfill-source-uri", *args])


def test_dry_run_reports_candidates_and_markers_without_writing(monkeypatch):
    docs = [
        _FakeDoc("1.1.1", "rdr__x", "chroma://rdr__x//abs/rdr-1.md"),
        _FakeDoc("1.1.2", "knowledge__x", "chroma://knowledge__x/Some Title"),
        _FakeDoc("1.1.3", "rdr__x", "file:///abs/already-fine.md"),
    ]
    result = _invoke(monkeypatch, docs, ["--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["total_chroma_rows"] == 2
    assert report["by_collection"]["rdr__x"] == {"count": 1, "candidate": True}
    assert report["by_collection"]["knowledge__x"] == {"count": 1, "candidate": False}
    assert report["candidates"] == 1
    assert report["rederivable"] == 1
    assert report["apply"] is False
    assert report["updates"] == [
        {"tumbler": "1.1.1", "collection": "rdr__x",
         "was": "chroma://rdr__x//abs/rdr-1.md", "now": "file:///abs/rdr-1.md"}
    ]


def test_apply_rewrites_candidates_via_update_many(monkeypatch):
    docs = [_FakeDoc("1.1.1", "rdr__x", "chroma://rdr__x//abs/rdr-1.md")]
    writer = _FakeWriter()
    result = _invoke(monkeypatch, docs, ["--apply"], writer=writer)
    assert result.exit_code == 0, result.output
    assert writer.update_many_calls == [
        [{"tumbler": "1.1.1", "source_uri": "file:///abs/rdr-1.md"}]
    ]
    assert writer.closed is True


def test_dry_run_never_constructs_a_writer(monkeypatch):
    from nexus.commands import catalog as _cat_cmd

    def _boom():
        raise AssertionError("dry-run must never open a catalog writer")

    docs = [_FakeDoc("1.1.1", "rdr__x", "chroma://rdr__x//abs/rdr-1.md")]
    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: _FakeReader(docs))
    monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", _boom)
    result = CliRunner().invoke(main, ["catalog", "backfill-source-uri"])
    assert result.exit_code == 0, result.output


def test_refuses_relative_path_without_writing(monkeypatch):
    """A row whose chroma:// path component is relative (the actual
    poigc bug shape — an older uri_for that routed rdr__ to chroma://
    with a relative source_path) is reported, never guessed at."""
    docs = [_FakeDoc("1.1.1", "rdr__x", "chroma://rdr__x/relative/path.md")]
    writer = _FakeWriter()
    result = _invoke(monkeypatch, docs, ["--apply", "--json"], writer=writer)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["rederivable"] == 0
    assert report["refused"] == 1
    assert "not absolute" in report["refusals"][0]["reason"]
    assert writer.update_many_calls == []


def test_knowledge_collections_never_become_candidates(monkeypatch):
    docs = [_FakeDoc("1.1.1", "knowledge__x", "chroma://knowledge__x/Title")]
    result = _invoke(monkeypatch, docs, ["--json"])
    report = json.loads(result.output)
    assert report["candidates"] == 0
    assert report["updates"] == []


def test_no_chroma_rows_is_a_clean_no_op(monkeypatch):
    docs = [_FakeDoc("1.1.1", "rdr__x", "file:///abs/already-fine.md")]
    result = _invoke(monkeypatch, docs, [])
    assert result.exit_code == 0, result.output
    assert "No file-routed chroma:// rows to backfill." in result.output
