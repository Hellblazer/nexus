# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-x1de2 client-edge pins: (51) the plans-service-unavailable branch,
(52) aspect doc_id attribution, (53) malformed engine response bodies."""

from __future__ import annotations

import dataclasses
import inspect

import httpx
import pytest
from click.testing import CliRunner
from structlog.testing import capture_logs

from nexus import aspect_worker
from nexus.cli import main
from nexus.commands import enrich
from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore, _body_to_record
from nexus.db.t2.records import AspectRecord, with_doc_id


# ── (51) _open_plan_library: plans service unavailable ─────────────────────


class _UnreachablePlanLibrary:
    def __init__(self, *a, **kw):
        raise RuntimeError("no plans endpoint could be resolved")


def test_plan_list_reports_plans_service_unavailable_cleanly(monkeypatch):
    """A constructor failure surfaces as a one-line ClickException naming
    the cause, not a traceback."""
    monkeypatch.setattr(
        "nexus.db.t2.http_plan_library.HttpPlanLibrary", _UnreachablePlanLibrary,
    )
    result = CliRunner().invoke(main, ["plan", "list"])
    assert result.exit_code != 0
    assert "plans service unavailable: no plans endpoint could be resolved" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


# ── (52) doc_id attribution ────────────────────────────────────────────────


def _record(**kw) -> AspectRecord:
    base = dict(
        collection="knowledge__x", source_path="/p/a.pdf", problem_formulation="p",
        proposed_method="m", extracted_at="2026-08-28T00:00:00Z", model_version="v",
        extractor_name="e",
    )
    base.update(kw)
    return AspectRecord(**base)


def test_with_doc_id_stamps_only_a_missing_identity():
    rec = _record()
    assert with_doc_id(rec, "1.2.3").doc_id == "1.2.3"
    assert with_doc_id(rec, "") is rec                      # nothing to stamp
    kept = _record(doc_id="9.9.9")
    assert with_doc_id(kept, "1.2.3") is kept               # never overwrites
    assert with_doc_id(kept, "1.2.3").doc_id == "9.9.9"


def test_worker_completion_paths_attribute_the_queue_row():
    """Both worker completion paths go through ``_attributed`` — a wiring
    pin, so neither can quietly revert to the bare asdict(record)."""
    # Scoped to the two completion methods, not the whole module, so an
    # unrelated edit elsewhere cannot red this pin (substantive-critic).
    batch = inspect.getsource(aspect_worker.AspectExtractionWorker._process_batch)
    single = inspect.getsource(aspect_worker.AspectExtractionWorker._process_row)
    for name, src in (("_process_batch", batch), ("_process_row", single)):
        assert "_attributed(record, row)" in src, f"{name} must stamp the queue row's doc_id"
        assert "asdict(record))" not in src, f"{name} completes a bare, unattributed record"
    row = dataclasses.make_dataclass("Row", [("doc_id", str)])("4.5.6")
    assert aspect_worker._attributed(_record(), row).doc_id == "4.5.6"
    assert aspect_worker._attributed(_record(), object()).doc_id == ""


def test_enrich_gap_fill_attributes_the_catalog_entry():
    src = inspect.getsource(enrich._run_extraction)
    assert "with_doc_id(record," in src, "the gap-fill must stamp the catalog entry's tumbler"
    assert "asdict(_rec)" in src  # the stamped record is what gets completed


# ── nexus-r0kum: resolve-or-refuse a file-routed row with no source_uri ────


class _FakeEntry:
    def __init__(self, physical_collection: str, source_uri: str):
        self.physical_collection = physical_collection
        self.source_uri = source_uri


class _FakeCatalog:
    def __init__(self, entries: dict[str, _FakeEntry]):
        self._entries = entries

    def by_doc_id(self, doc_id: str):
        return self._entries.get(doc_id)


def test_attributed_resolves_source_uri_for_file_routed_row_via_doc_id(monkeypatch):
    """The rdr-frontmatter-v1 writer path: source_path is a T3 chash (not
    a filesystem path), so uri_for correctly returns None for it. When
    the queue row carries a resolvable catalog tumbler, its OWN
    source_uri is the real identity."""
    monkeypatch.setattr(
        aspect_worker, "_resolve_catalog_reader",
        lambda: _FakeCatalog({"1.2.3": _FakeEntry("rdr__x", "file:///abs/rdr-1.md")}),
    )
    record = _record(
        collection="rdr__x", source_path="7e00d7d567d326ec6a6b595a4fbf12fc",
        source_uri=None,
    )
    row = dataclasses.make_dataclass("Row", [("doc_id", str)])("1.2.3")
    attributed = aspect_worker._attributed(record, row)
    assert attributed.doc_id == "1.2.3"
    assert attributed.source_uri == "file:///abs/rdr-1.md"


def test_attributed_leaves_non_file_routed_collection_uri_alone(monkeypatch):
    """knowledge__ rows are attributed by title, not source_uri — a
    missing source_uri there is not this bead's defect class and must
    never trigger a catalog lookup."""
    calls: list[str] = []

    def _boom_reader():
        calls.append("called")
        raise AssertionError("must not resolve the catalog for a non-file-routed collection")

    monkeypatch.setattr(aspect_worker, "_resolve_catalog_reader", _boom_reader)
    record = _record(collection="knowledge__x", source_uri=None)
    row = dataclasses.make_dataclass("Row", [("doc_id", str)])("1.2.3")
    attributed = aspect_worker._attributed(record, row)
    assert attributed.source_uri is None
    assert calls == []


def test_attributed_cross_collection_mismatch_is_refused(monkeypatch):
    """A resolvable tumbler belonging to a DIFFERENT collection must
    never re-point this row's source_uri at it."""
    monkeypatch.setattr(
        aspect_worker, "_resolve_catalog_reader",
        lambda: _FakeCatalog({"1.2.3": _FakeEntry("rdr__other", "file:///abs/other.md")}),
    )
    record = _record(collection="rdr__x", source_uri=None)
    row = dataclasses.make_dataclass("Row", [("doc_id", str)])("1.2.3")
    attributed = aspect_worker._attributed(record, row)
    assert attributed.source_uri is None


def test_is_unattributable_true_for_file_routed_missing_uri():
    rec = _record(collection="rdr__x", source_uri=None, doc_id="")
    assert aspect_worker._is_unattributable(rec) is True


def test_is_unattributable_false_once_source_uri_resolved():
    rec = _record(collection="rdr__x", source_uri="file:///abs/rdr-1.md", doc_id="1.2.3")
    assert aspect_worker._is_unattributable(rec) is False


def test_is_unattributable_false_for_non_file_routed_collection():
    rec = _record(collection="knowledge__x", source_uri=None, doc_id="")
    assert aspect_worker._is_unattributable(rec) is False


def test_worker_completion_paths_refuse_unattributable_rows():
    """Wiring pin, mirroring test_worker_completion_paths_attribute_the_
    queue_row above: both completion paths must check _is_unattributable
    and route a positive to a terminal mark_failed(..., error=
    "unattributable_identity") instead of completing an unfindable row."""
    batch = inspect.getsource(aspect_worker.AspectExtractionWorker._process_batch)
    single = inspect.getsource(aspect_worker.AspectExtractionWorker._process_row)
    for name, src in (("_process_batch", batch), ("_process_row", single)):
        assert "_is_unattributable(attributed)" in src, f"{name} must refuse an unattributable row"
        assert "unattributable_identity" in src, f"{name} must mark_failed with the named reason"


@pytest.fixture
def store(monkeypatch):
    posted: list[dict] = []
    s = HttpDocumentAspectsStore.__new__(HttpDocumentAspectsStore)
    monkeypatch.setattr(s, "_post", lambda path, body=None, **kw: (posted.append(body) or {"written": True}))
    return s, posted


def test_unattributed_upsert_is_refused(store):
    # hygiene-001 (nexus-tk070.p6a follow-on) SUPERSEDES nexus-x1de2's
    # original warn-and-proceed contract: document_aspects.doc_id is NOT
    # NULL now and the engine refuses a blank doc_id (Sam decision: every
    # aspect row must attribute to a live catalog document). The client
    # raises before ever posting, one call earlier than the engine's own
    # 400 "doc_id required".
    s, posted = store
    with pytest.raises(ValueError, match="doc_id required"):
        s.upsert(_record())
    assert not posted


def test_attributed_upsert_is_quiet(store):
    s, posted = store
    with capture_logs() as cap:
        assert s.upsert(_record(doc_id="1.2.3")) is True
    assert posted[0]["doc_id"] == "1.2.3"
    assert not [e for e in cap if e.get("event") == "document_aspects_upsert_unattributed"]


def test_upsert_rejected_by_the_engine_propagates_not_swallows(store, monkeypatch):
    """A stamped doc_id the catalog no longer has trips fk-001 engine-side;
    the store must let that surface (the worker routes it to retry/fail,
    never a silently unattributed row)."""
    s, _ = store
    request = httpx.Request("POST", "http://test/v1/aspects/upsert")
    exc = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request),
    )

    def _raise(path, body=None, **kw):
        raise exc

    monkeypatch.setattr(s, "_post", _raise)
    with pytest.raises(httpx.HTTPStatusError):
        s.upsert(_record(doc_id="9.9.9-gone"))


# ── (53) malformed engine response bodies ──────────────────────────────────


def test_body_to_record_tolerates_null_columns_and_garbage_json():
    rec = _body_to_record({
        "collection": "knowledge__x", "source_path": "/p/a.pdf",
        "experimental_datasets": None,          # NULL column
        "experimental_baselines": "not json[",  # garbage
        "extras": '["a list, not an object"]',  # wrong JSON shape
        "salient_sentences": "{",                # truncated JSON
        "confidence": None,
        # doc_id / extracted_at / model_version / extractor_name absent
    })
    assert rec.experimental_datasets == []
    assert rec.experimental_baselines == []
    assert rec.extras == {}
    assert rec.salient_sentences == []
    assert rec.confidence is None
    assert rec.doc_id == ""
    assert rec.extracted_at == "" and rec.model_version == "" and rec.extractor_name == ""


def test_body_to_record_passes_well_formed_json_strings_through():
    rec = _body_to_record({
        "collection": "c", "source_path": "s",
        "experimental_datasets": '["d1", "d2"]',
        "extras": '{"k": 1}',
        "salient_sentences": '["one", "two"]',
        "doc_id": "1.2.3",
    })
    assert rec.experimental_datasets == ["d1", "d2"]
    assert rec.extras == {"k": 1}
    assert rec.salient_sentences == ["one", "two"]
    assert rec.doc_id == "1.2.3"


def test_get_returns_none_on_404_and_on_an_empty_body(monkeypatch):
    s = HttpDocumentAspectsStore.__new__(HttpDocumentAspectsStore)
    request = httpx.Request("GET", "http://test/v1/aspects/get")
    exc = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    def _raise(path, params=None, **kw):
        raise exc

    monkeypatch.setattr(s, "_get", _raise)
    assert s.get("c", "s") is None
    monkeypatch.setattr(s, "_get", lambda path, params=None, **kw: {})
    assert s.get("c", "s") is None
