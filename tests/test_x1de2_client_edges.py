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


@pytest.fixture
def store(monkeypatch):
    posted: list[dict] = []
    s = HttpDocumentAspectsStore.__new__(HttpDocumentAspectsStore)
    monkeypatch.setattr(s, "_post", lambda path, body=None, **kw: (posted.append(body) or {"written": True}))
    return s, posted


def test_unattributed_upsert_is_loud_but_not_refused(store):
    s, posted = store
    with capture_logs() as cap:
        assert s.upsert(_record()) is True
    assert posted and posted[0]["doc_id"] == ""
    events = [e for e in cap if e.get("event") == "document_aspects_upsert_unattributed"]
    assert len(events) == 1
    assert events[0]["collection"] == "knowledge__x"
    assert events[0]["source_path"] == "/p/a.pdf"


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
