# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-poigc: ``nx catalog backfill-source-uri`` re-derives chroma://
catalog source_uri values for filesystem-backed (file:// routed)
collections, and leaves the knowledge__ legacy-note population (the engine
backfill rewrites those to nx-note://) alone."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import catalog as _cat_cmd



class _FakeDoc:
    def __init__(self, tumbler: str, physical_collection: str, source_uri: str):
        self.tumbler = tumbler
        self.physical_collection = physical_collection
        self.source_uri = source_uri


class _FakeReader:
    def __init__(self, docs):
        self._docs = docs

    def all_documents(self, *_a, **_kw):
        raise AssertionError("backfill-source-uri must read the chroma:// prefix, not walk the catalog")

    def list_scoped(self, *, source_uri_prefix: str = "", **_kw):
        assert source_uri_prefix == "chroma://", source_uri_prefix
        return [d for d in self._docs if d.source_uri.startswith(source_uri_prefix)]


class _FakeWriter:
    def __init__(self):
        self.update_many_calls: list[list[dict]] = []
        self.closed = False

    def update_many(self, updates):
        self.update_many_calls.append(updates)
        return [1 for _ in updates]

    def close(self):
        self.closed = True


#: The client row cache the verb reads (``mcp_infra.get_collection_row``): collection name -> catalog attributes.
#: Empty by default, so a collection with no row is not file-typed.
_ROWS: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _collection_rows(monkeypatch):
    _ROWS.clear()
    monkeypatch.setattr(
        "nexus.mcp_infra.get_collection_row", lambda name, **_kw: _ROWS.get(name),
    )
    return _ROWS


def _invoke(monkeypatch, docs, args, writer=None):
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
    report = json.loads(result.stdout)
    assert report["total_chroma_rows"] == 2
    assert report["by_collection"]["rdr__x"] == {"count": 1, "candidate": True, "candidate_rows": 1}
    assert report["by_collection"]["knowledge__x"] == {"count": 1, "candidate": False, "candidate_rows": 0}
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
    report = json.loads(result.stdout)
    assert report["rederivable"] == 0
    assert report["refused"] == 1
    assert "not absolute" in report["refusals"][0]["reason"]
    assert writer.update_many_calls == []


def test_knowledge_collections_never_become_candidates(monkeypatch):
    docs = [_FakeDoc("1.1.1", "knowledge__x", "chroma://knowledge__x/Title")]
    result = _invoke(monkeypatch, docs, ["--json"])
    report = json.loads(result.stdout)
    assert report["candidates"] == 0
    assert report["updates"] == []


def test_no_chroma_rows_is_a_clean_no_op(monkeypatch):
    docs = [_FakeDoc("1.1.1", "rdr__x", "file:///abs/already-fine.md")]
    result = _invoke(monkeypatch, docs, [])
    assert result.exit_code == 0, result.output
    assert "No file-routed chroma:// rows to backfill." in result.output


def test_the_census_calls_a_non_file_routed_chroma_row_a_legacy_note_not_a_marker(monkeypatch):
    """RDR-231 (nexus-bivz3.15): those rows are legacy notes the ENGINE backfill rewrites to nx-note://;
    "store_put-origin marker (left alone)" described the retired identity model."""
    _ROWS["knowledge__x"] = {"content_type": "knowledge"}
    docs = [
        _FakeDoc("1.1.1", "rdr__x", "chroma://rdr__x//abs/rdr-1.md"),
        _FakeDoc("1.1.2", "knowledge__x", "chroma://knowledge__x/Some Title"),
    ]
    result = _invoke(monkeypatch, docs, [])
    assert result.exit_code == 0, result.output
    assert "knowledge__x: 1  [legacy note (the engine backfill rewrites it to nx-note://)]" in result.output
    assert "store_put-origin" not in result.output
    assert "rdr__x: 1  [candidate]" in result.output


# ── RDR-229 P2.10 (nexus-p2m1x.15): the check reads the residue's OWN segment ─────────────────────────────────────────
#
# After P2.10 no path moves chroma:// residue, so residue keeps the segment it was written with while its row's
# physical_collection names the collection it is filed under now; and repaired residue carries a <content_type>__S
# segment. Both used to be refused as "malformed" (the URI did not start chroma://<physical_collection>/).


def test_residue_that_moved_keeps_its_old_segment_and_is_re_derived_not_refused(monkeypatch):
    """A re-embed move, a re-home, a rename or a reconcile repoint left ``chroma://rdr__old//abs/x.md`` on a row now
    filed under another collection. Its own segment is file-routed, so it is a candidate, and the path is the text
    after ITS segment."""
    docs = [_FakeDoc("1.1.1", "rdr__new", "chroma://rdr__old//abs/rdr-1.md")]
    report = json.loads(_invoke(monkeypatch, docs, ["--json"]).stdout)
    assert report["refused"] == 0, report["refusals"]
    assert report["updates"] == [
        {"tumbler": "1.1.1", "collection": "rdr__new",
         "was": "chroma://rdr__old//abs/rdr-1.md", "now": "file:///abs/rdr-1.md"}
    ]
    writer = _FakeWriter()
    result = _invoke(monkeypatch, docs, ["--apply"], writer=writer)
    assert result.exit_code == 0, result.output
    assert writer.update_many_calls == [[{"tumbler": "1.1.1", "source_uri": "file:///abs/rdr-1.md"}]]


def test_residue_repaired_to_a_content_type_segment_is_re_derived_not_refused(monkeypatch, _collection_rows):
    """The P2.10 repair move rewrites ``chroma://plain-docs//abs/a.md`` on a docs collection with a prefix-free label to
    ``chroma://docs__plain-docs//abs/a.md``: the segment is file-routed again, whatever the row's collection is named."""
    _collection_rows["plain-docs"] = {"content_type": "docs"}
    docs = [_FakeDoc("1.1.1", "plain-docs", "chroma://docs__plain-docs//abs/a.md")]
    result = _invoke(monkeypatch, docs, ["--json"])
    report = json.loads(result.stdout)
    assert report["refused"] == 0, report["refusals"]
    assert report["updates"] == [
        {"tumbler": "1.1.1", "collection": "plain-docs",
         "was": "chroma://docs__plain-docs//abs/a.md", "now": "file:///abs/a.md"}
    ]


@pytest.mark.parametrize("content_type", ["code", "docs", "rdr"])
def test_residue_still_on_a_prefix_free_segment_is_a_candidate_when_its_collection_is_file_typed(
    monkeypatch, _collection_rows, content_type,
):
    """Residue the repair move has not reached yet (or skipped for a held value): the segment has no prefix, the row's
    collection has content type code, docs or rdr, and the verb re-derives it to its file:// identity all the same."""
    _collection_rows["plain"] = {"content_type": content_type}
    docs = [_FakeDoc("1.1.1", "plain", "chroma://plain//abs/a.md")]
    report = json.loads(_invoke(monkeypatch, docs, ["--json"]).stdout)
    assert report["by_collection"]["plain"] == {"count": 1, "candidate": True, "candidate_rows": 1}
    assert report["updates"] == [
        {"tumbler": "1.1.1", "collection": "plain", "was": "chroma://plain//abs/a.md", "now": "file:///abs/a.md"}
    ]


def test_a_knowledge_collections_chroma_note_on_a_prefix_free_label_is_not_a_candidate(monkeypatch, _collection_rows):
    _collection_rows["plain-know"] = {"content_type": "knowledge"}
    docs = [_FakeDoc("1.1.1", "plain-know", "chroma://plain-know/A title")]
    report = json.loads(_invoke(monkeypatch, docs, ["--json"]).stdout)
    assert report["by_collection"]["plain-know"] == {"count": 1, "candidate": False, "candidate_rows": 0}
    assert report["candidates"] == 0
    assert report["updates"] == []


def test_a_collection_with_no_row_and_no_prefixed_segment_is_not_a_candidate(monkeypatch):
    docs = [_FakeDoc("1.1.1", "unregistered", "chroma://unregistered//abs/a.md")]
    report = json.loads(_invoke(monkeypatch, docs, ["--json"]).stdout)
    assert report["candidates"] == 0


def test_the_path_after_the_own_segment_must_still_be_absolute(monkeypatch, _collection_rows):
    _collection_rows["plain"] = {"content_type": "docs"}
    docs = [_FakeDoc("1.1.1", "plain", "chroma://docs__plain/relative/a.md")]
    writer = _FakeWriter()
    report = json.loads(_invoke(monkeypatch, docs, ["--apply", "--json"], writer=writer).stdout)
    assert report["rederivable"] == 0
    assert "not absolute" in report["refusals"][0]["reason"]
    assert writer.update_many_calls == []


def test_a_chroma_uri_with_no_path_is_refused_as_malformed(monkeypatch):
    docs = [_FakeDoc("1.1.1", "rdr__x", "chroma://rdr__x")]
    report = json.loads(_invoke(monkeypatch, docs, ["--json"]).stdout)
    assert report["refused"] == 1
    assert "malformed" in report["refusals"][0]["reason"]


# ── review r1 (nexus-p2m1x.15): a failed row lookup is never a legacy note ───────────────────────────────────────────


def test_a_failed_collection_row_lookup_fails_loud_and_writes_nothing(monkeypatch):
    """The service flaps while the verb reads a collection's row: the verb must stop, not label every prefix-free
    file-typed row a legacy note (which tells the operator the engine will turn them into notes)."""
    from nexus.mcp_infra import ServiceNotReadyError  # noqa: PLC0415

    def _down(name, *, strict=False, **_kw):
        if strict:
            raise ServiceNotReadyError("collections listing failed")
        return None  # the non-strict read answers "no row" for a failed listing: the verb must not use it

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _down)
    docs = [_FakeDoc("1.1.1", "plain-docs", "chroma://plain-docs//abs/a.md")]
    writer = _FakeWriter()
    result = _invoke(monkeypatch, docs, ["--apply"], writer=writer)
    assert result.exit_code != 0
    assert "legacy note" not in result.output
    assert "collections listing failed" in result.output or "collection row" in result.output
    assert writer.update_many_calls == []


def test_a_collection_with_no_row_is_unclassified_never_a_legacy_note(monkeypatch):
    docs = [_FakeDoc("1.1.1", "unregistered", "chroma://unregistered//abs/a.md")]
    result = _invoke(monkeypatch, docs, [])
    assert result.exit_code == 0, result.output
    assert "legacy note" not in result.output
    assert "no catalog row" in result.output


def test_a_collection_with_candidate_and_non_candidate_rows_does_not_tag_both_as_candidates(monkeypatch):
    """A prefix-free knowledge collection holding one note and one residue value that names a file-routed segment."""
    _ROWS["plain-know"] = {"content_type": "knowledge"}
    docs = [
        _FakeDoc("1.1.1", "plain-know", "chroma://docs__old//abs/a.md"),
        _FakeDoc("1.1.2", "plain-know", "chroma://plain-know/A title"),
    ]
    result = _invoke(monkeypatch, docs, [])
    assert "plain-know: 2  [1 of 2 candidate]" in result.output
    report = json.loads(_invoke(monkeypatch, docs, ["--json"]).stdout)
    assert report["by_collection"]["plain-know"] == {"count": 2, "candidate": True, "candidate_rows": 1}


def test_a_refusal_names_the_function_it_actually_calls(monkeypatch):
    """The relative-path refusal and the no-result refusal speak of file_routed_uri, not uri_for."""
    docs = [_FakeDoc("1.1.1", "rdr__x", "chroma://rdr__x/relative.md")]
    assert "uri_for" not in _invoke(monkeypatch, docs, ["--json"]).stdout
