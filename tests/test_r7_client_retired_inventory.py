# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 R7 client half (nexus-p2m1x.25): full-inventory loops skip retired collections, visibly.

The engine answers 409 ``collection_superseded`` to a read by label of a retired collection with no live successor, and
such a row can still hold chunks, so ``list_collections()`` still lists it. A loop over the listing that reads each
collection by name would die on it (or report a false FAIL). Each loop here skips the retired row (``superseded_by`` set
or ``lifecycle_state`` retired), counts it, and logs it (structlog ``retired_collection_skipped``); the live collections
are still read.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from structlog.testing import capture_logs

import nexus.doctor_search as ds
from nexus.commands.catalog_cmds import doctor
from nexus.commands.store import expire_cmd, export_cmd

from nexus.collection_errors import retired_no_successor_error_from_body
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.retired_rows import is_retired_collection_row, skip_retired_rows
from tests.test_6pfgb15_note_ttl import _COLLECTION, _entry, _FakeCatalog, _lapsed_row, _World

RETIRED_NAME = "knowledge__gone__bge-base-en-v15-768__v1"


def _retired_409(name: str):
    err = retired_no_successor_error_from_body(
        409, {"error": "collection_superseded", "reason": "collection_superseded", "collection": name, "collection_id": 41})
    assert err is not None
    return err


# ── the row predicate and the one skip helper ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("row", "retired"), [
    ({"name": "a", "lifecycle_state": "live"}, False),
    ({"name": "a"}, False),
    ({"name": "a", "superseded_by": None}, False),
    ({"name": "a", "superseded_by": ""}, False),
    ({"name": "a", "superseded_by": "b"}, True),
    ({"name": "a", "lifecycle_state": "retired"}, True),
    ({"name": "a", "lifecycle_state": "quarantine"}, False),
    ("bare-name", False),
])
def test_a_row_is_retired_by_superseded_by_or_lifecycle_state(row, retired):
    assert is_retired_collection_row(row) is retired


def test_skip_retired_rows_returns_the_rest_and_names_what_it_left_out_and_logs_it():
    rows = [{"name": "live"}, {"name": "old", "superseded_by": "x"}, {"name": "gone", "lifecycle_state": "retired"}]
    with capture_logs() as logs:
        kept, skipped = skip_retired_rows(rows, site="unit")
    assert [r["name"] for r in kept] == ["live"] and skipped == ("old", "gone")
    [event] = [e for e in logs if e["event"] == "retired_collection_skipped"]
    assert event["site"] == "unit" and event["skipped"] == 2 and event["collections"] == ["old", "gone"]


def test_skip_retired_rows_is_silent_when_nothing_is_retired():
    with capture_logs() as logs:
        kept, skipped = skip_retired_rows([{"name": "live"}], site="unit")
    assert kept == [{"name": "live"}] and skipped == () and logs == []


# ── nx store expire ─────────────────────────────────────────────────────────────────────────────────────────────────

def _expire_world(monkeypatch, *, retired_row):
    """One live knowledge collection with a lapsed chunk, and a retired one whose label read the engine refuses."""
    cat = _FakeCatalog([_entry("1.1.1", meta={"ttl_days": 1, "put_at": "2026-01-01T00:00:00+00:00", "doc_id": "c1"})],
                       {"1.1.1": ["c1"]})
    w = _World(monkeypatch, cat, {"c1": _lapsed_row(1)})
    live_post = __import__("nexus.db.http_vector_client", fromlist=["_post"])._post
    reads: list[str] = []

    def post(path, body, **kw):
        reads.append(body.get("collection", ""))
        if body.get("collection") == RETIRED_NAME:
            raise _retired_409(RETIRED_NAME)
        return live_post(path, body, **kw)

    monkeypatch.setattr(HttpVectorClient, "list_collections", lambda self, *a, **k: [
        {"name": RETIRED_NAME, "count": 3, "content_type": "knowledge", **retired_row},
        {"name": _COLLECTION, "count": 1, "content_type": "knowledge"}])
    monkeypatch.setattr("nexus.db.http_vector_client._post", post)
    return w, reads


@pytest.mark.parametrize("retired_row", [{"superseded_by": RETIRED_NAME}, {"lifecycle_state": "retired"}])
def test_store_expire_skips_a_retired_collection_and_sweeps_the_rest(monkeypatch, retired_row):
    w, reads = _expire_world(monkeypatch, retired_row=retired_row)

    with capture_logs() as logs:
        report = HttpVectorClient().expire_report()

    assert RETIRED_NAME not in reads, "a retired collection is never read by label"
    assert report.retired_collections_skipped == (RETIRED_NAME,)
    assert w.deleted, "the live collection was still swept"
    [event] = [e for e in logs if e["event"] == "retired_collection_skipped"]
    assert event["site"] == "store_expire" and event["collections"] == [RETIRED_NAME]


def test_store_expire_dry_run_reports_the_skipped_retired_collection_too(monkeypatch):
    _, _ = _expire_world(monkeypatch, retired_row={"superseded_by": RETIRED_NAME})
    report = HttpVectorClient().expire_report(dry_run=True)
    assert report.retired_collections_skipped == (RETIRED_NAME,)


def test_nx_store_expire_names_the_retired_collections_it_did_not_read(monkeypatch):


    _expire_world(monkeypatch, retired_row={"superseded_by": RETIRED_NAME})
    monkeypatch.setattr("nexus.commands.store._t3", lambda: HttpVectorClient())

    result = CliRunner().invoke(expire_cmd, [])

    assert result.exit_code == 0, result.output
    assert "1 retired collection was not swept" in result.output and RETIRED_NAME in result.output


# ── nx store export --all ───────────────────────────────────────────────────────────────────────────────────────────

def test_store_export_all_skips_a_retired_collection_and_exports_the_rest(monkeypatch, tmp_path):


    live = "knowledge__live__bge-base-en-v15-768__v1"
    db = MagicMock()
    db.list_collections.return_value = [
        {"name": RETIRED_NAME, "superseded_by": RETIRED_NAME, "count": 3},
        {"name": live, "count": 5},
    ]
    exported: list[str] = []

    def export(*, db, collection_name, output_path, includes, excludes):
        if collection_name == RETIRED_NAME:
            raise _retired_409(RETIRED_NAME)
        exported.append(collection_name)
        return {"exported_count": 5}

    monkeypatch.setattr("nexus.commands.store._t3", lambda: db)
    monkeypatch.setattr("nexus.exporter.export_collection", export)

    with capture_logs() as logs:
        result = CliRunner().invoke(export_cmd, ["--all", "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert exported == [live], "the retired collection is never exported by label"
    assert "ERROR exporting" not in result.output, "a skipped retired collection is not a failure"
    assert "1 retired collection was not exported" in result.output and RETIRED_NAME in result.output
    assert "across 1 collections" in result.output
    assert any(e["event"] == "retired_collection_skipped" and e["site"] == "store_export_all" for e in logs)


# ── nx doctor --check-search ────────────────────────────────────────────────────────────────────────────────────────

def _check_search_world(monkeypatch):

    probed: list[list[str]] = []
    monkeypatch.setattr(ds, "_load_canaries", lambda: [])
    monkeypatch.setattr(ds, "run_name_resolution_probe", lambda *a, **k: [])
    monkeypatch.setattr(ds, "_list_collections", lambda: [
        {"name": RETIRED_NAME, "superseded_by": RETIRED_NAME},
        {"name": "docs__live__bge-base-en-v15-768__v1", "content_type": "docs"}])
    monkeypatch.setattr(ds, "_make_t3", lambda: object())

    def probe(*, collections, **kw):
        probed.append(list(collections))
        return []

    monkeypatch.setattr(ds, "run_retrieval_quality_probe", probe)
    return ds, probed


def test_check_search_does_not_probe_a_retired_collection_and_reports_the_skip_without_failing(monkeypatch, capsys):
    ds, probed = _check_search_world(monkeypatch)

    with capture_logs() as logs:
        ds.run_check_search(json_out=False)  # no SystemExit(2): a skip is not a regression

    assert probed == [["docs__live__bge-base-en-v15-768__v1"]]
    out = capsys.readouterr().out
    assert "retired_skipped" in out and RETIRED_NAME in out and "1 retired_skipped" in out
    assert any(e["event"] == "retired_collection_skipped" and e["site"] == "doctor_check_search" for e in logs)


def test_check_search_json_carries_the_retired_skip(monkeypatch, capsys):

    ds, _ = _check_search_world(monkeypatch)
    with capture_logs():  # the skip's log line would otherwise share stdout with the JSON
        ds.run_check_search(json_out=True)
    retrieval = json.loads(capsys.readouterr().out)["probes"][1]
    assert retrieval["summary"] == {"retired_skipped": 1}
    assert [(r["name"], r["outcome"]) for r in retrieval["results"]] == [(RETIRED_NAME, "retired_skipped")]


# ── nx catalog doctor ───────────────────────────────────────────────────────────────────────────────────────────────

LIVE_NAME = "knowledge__live__bge-base-en-v15-768__v1"


class _FakeCollection:
    def __init__(self, name):
        self.name = name

    def get(self, **kw):
        if self.name == RETIRED_NAME:
            raise _retired_409(RETIRED_NAME)
        return {"ids": ["a" * 64], "documents": ["some text"], "metadatas": [{"chunk_text_hash": "a" * 64}]}


class _FakeT3:
    """A tenant listing one retired collection (it still holds chunks) and one live one; a by-label read of the
    retired one raises what the engine's 409 becomes."""

    def __init__(self):
        self.opened: list[str] = []

    def list_collections(self, *a, **k):
        return [{"name": RETIRED_NAME, "superseded_by": RETIRED_NAME, "count": 3, "content_type": "knowledge"},
                {"name": LIVE_NAME, "count": 1, "content_type": "knowledge", "lifecycle_state": "live"}]

    def get_or_create_collection(self, name):
        self.opened.append(name)
        return _FakeCollection(name)

    def get_embeddings(self, name, ids):
        if name == RETIRED_NAME:
            raise _retired_409(RETIRED_NAME)
        return [[0.0] * 768]

    def manifest_less_census(self, name, **kw):
        if name == RETIRED_NAME:
            raise _retired_409(RETIRED_NAME)
        return {"totals": {"superseded": 0}, "chashes": {"superseded": []}, "owners": {}}


@pytest.mark.parametrize(("runner", "table_key"), [
    ("_run_chunk_size_distribution", "tables"),
    ("_run_chunk_text_dedup", "within"),
])
def test_catalog_doctor_per_collection_checks_skip_a_retired_collection_not_fail_on_it(monkeypatch, runner, table_key):

    t3 = _FakeT3()
    monkeypatch.setattr("nexus.db.make_t3", lambda: t3)

    with capture_logs() as logs:
        report = getattr(doctor, runner)()

    assert report["pass"] is True, report
    assert RETIRED_NAME not in t3.opened and RETIRED_NAME not in report[table_key]
    assert LIVE_NAME in report[table_key], "the live collection is still read"
    assert report["retired_skipped"] == [RETIRED_NAME]
    assert any(e["event"] == "retired_collection_skipped" for e in logs)


def test_catalog_doctor_name_vs_embed_dim_skips_a_retired_collection_not_a_read_error(monkeypatch):

    t3 = _FakeT3()
    monkeypatch.setattr("nexus.db.make_t3", lambda: t3)

    report = doctor._run_name_vs_embed_dim()

    assert report["read_errors"] == [] and report["pass"] is True, report
    assert report["checked"] == 1 and report["retired_skipped"] == [RETIRED_NAME]


def test_catalog_doctor_visible_outside_manifest_skips_a_retired_collection(monkeypatch):

    report = doctor._run_visible_outside_manifest(t3=_FakeT3())

    assert report["check_errors"] == [] and report["pass"] is True, report
    assert report["collections_total"] == 1 and report["retired_skipped"] == [RETIRED_NAME]


@pytest.mark.parametrize(("printer", "report"), [
    ("_print_chunk_size_distribution_text",
     {"pass": True, "micro_chunk_bytes": 100, "max_document_bytes": 16384, "tables": {}, "retired_skipped": ["r1"]}),
    ("_print_chunk_text_dedup_text",
     {"pass": True, "within": {}, "cross_dupe_warn": False, "cross_dupe_chunk_count": 0,
      "cross_dupe_warn_threshold": 100, "retired_skipped": ["r1"]}),
    ("_print_name_vs_embed_dim_text",
     {"pass": True, "checked": 0, "mismatches": [], "empty": [], "skipped_non_conformant": 0, "unknown_token": [],
      "read_errors": [], "retired_skipped": ["r1"]}),
])
def test_catalog_doctor_text_names_the_retired_collections_it_skipped(capsys, printer, report):

    getattr(doctor, printer)(report)

    assert "skipped 1 retired collection" in capsys.readouterr().out
