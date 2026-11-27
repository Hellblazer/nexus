# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 R7 client half (nexus-p2m1x.25): full-inventory loops skip retired collections, visibly.

The engine answers 409 ``collection_superseded`` to a read by label of a retired collection with no live successor, and
such a row can still hold chunks, so ``list_collections()`` still lists it. A loop over the listing that reads each
collection by name would die on it (or report a false FAIL). Each loop here skips the retired row (``superseded_by`` set
or ``lifecycle_state`` retired), counts it, and logs it (structlog ``retired_collection_skipped``); the live collections
are still read.
"""
from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import click
import httpx
import pytest
from click.testing import CliRunner
from structlog.testing import capture_logs

from nexus import health, search_engine
from nexus.catalog.engine_writes.collection_delete import SupersededCollectionDeleteError, refusal_of
from nexus.commands.collection import _reembed_job_error
from nexus.db.t2._refreshable_client import _RetiredNoSuccessorHttpError
from nexus.db.t2.http_document_aspects_store import _raise_fenced_refusal
from nexus.search_engine import search_cross_corpus
from tests.test_retired_no_successor_client import _BatchingT3
from nexus.cli import main
from nexus.collection_errors import RetiredNoSuccessorError
from nexus.commands.catalog_cmds.integrity import note_chunks_present
from nexus.db.http_vector_client import VectorServiceError
from nexus.catalog.recovery_bundle import _collection_rows
from nexus.commands import enrich
from nexus.commands.enrich import aspects_backfill_uri_cmd
from nexus.upgrade_ladder.rungs.manifest_section_labels import _chunk_metadatas
import nexus.doctor_assignments as da
import nexus.doctor_embeddings as de
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


# ── nx doctor --check-embeddings / --check-assignments ──────────────────────────────────────────────────────────────

class _ListingT3:
    def list_collections(self, *a, **k):
        return [{"name": RETIRED_NAME, "superseded_by": RETIRED_NAME, "count": 3},
                {"name": LIVE_NAME, "count": 4}]


def test_check_embeddings_probes_only_live_collections_and_names_the_retired_skip(monkeypatch, capsys):
    probed: list[dict] = []
    monkeypatch.setattr("nexus.db.make_t3", lambda: _ListingT3())
    monkeypatch.setattr(de, "probe_collections", lambda t3, sizes, **kw: probed.append(dict(sizes)) or [])
    monkeypatch.setattr(de, "format_report", lambda results, **kw: ([], True))

    de.run_check_embeddings(sample=5, collections=(), seed=1)

    assert probed == [{LIVE_NAME: 4}]
    assert "skipped 1 retired collection" in capsys.readouterr().out


def test_check_embeddings_says_a_named_retired_collection_is_retired_not_that_it_does_not_exist(monkeypatch, capsys):
    monkeypatch.setattr("nexus.db.make_t3", lambda: _ListingT3())
    with pytest.raises(SystemExit) as exc:
        de.run_check_embeddings(sample=5, collections=(RETIRED_NAME,), seed=1)
    out = capsys.readouterr().out
    assert exc.value.code == 1 and "retired" in out and "no such collection" not in out


def test_check_assignments_probes_only_live_collections_and_names_the_retired_skip(monkeypatch, capsys):
    probed: list[dict] = []
    monkeypatch.setattr("nexus.db.make_t3", lambda: _ListingT3())
    monkeypatch.setattr(da, "probe_collections", lambda taxo, t3, sizes, **kw: probed.append(dict(sizes)) or [])
    monkeypatch.setattr(da, "format_report", lambda results, **kw: ([], True))

    class _Taxo:
        def close(self):
            pass

    monkeypatch.setattr("nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore", _Taxo)

    da.run_check_assignments(sample=5, collections=(), seed=1)

    assert probed == [{LIVE_NAME: 4}]
    assert "skipped 1 retired collection" in capsys.readouterr().out


# ── nx doctor: the PDF chunk metadata scan ──────────────────────────────────────────────────────────────────────────

def test_doctor_pdf_stub_scan_does_not_read_a_retired_collection(monkeypatch):
    scanned: list[str] = []

    class _Listing:
        def list_collections(self, *a, **k):
            return [{"name": RETIRED_NAME, "content_type": "knowledge", "superseded_by": RETIRED_NAME},
                    {"name": LIVE_NAME, "content_type": "knowledge"}]

    def scan(t3, name, limit):
        scanned.append(name)
        return {}, health._StubScanCoverage()

    monkeypatch.setattr("nexus.db.make_t3", lambda: _Listing())
    monkeypatch.setattr(health, "_pdf_stub_route", lambda: None)
    monkeypatch.setattr(health, "_pdf_stubs_in_collection", scan)

    with capture_logs() as logs:
        [result] = health._check_pdf_stub_metadata()

    assert scanned == [LIVE_NAME], "the retired collection is not read by label"
    assert "could not be read" not in result.detail
    assert any(e["event"] == "retired_collection_skipped" and e["site"] == "doctor_pdf_stub_metadata" for e in logs)


# ── recovery bundle export ──────────────────────────────────────────────────────────────────────────────────────────

def test_recovery_export_reads_a_retired_collection_as_holding_nothing_and_logs_it():
    class _T3:
        def chunk_rows(self, collection, chashes, sensitivity="include"):
            raise _retired_409(collection)

    with capture_logs() as logs:
        rows = _collection_rows(_T3(), RETIRED_NAME, ["a" * 64])

    assert rows == {}, "its notes count as ghosts, as for an unregistered collection, rather than stopping the export"
    assert any(e["event"] == "recovery_export_collection_retired" and e["collection"] == RETIRED_NAME for e in logs)


# ── the manifest-section-labels and span-fill rungs' chunk read ─────────────────────────────────────────────────────

def test_the_rung_chunk_read_of_a_retired_collection_finds_no_chunk_and_logs_instead_of_aborting_the_scan():
    class _T3:
        def get_collection(self, name):
            raise _retired_409(name)

    with capture_logs() as logs:
        got = _chunk_metadatas(_T3(), RETIRED_NAME, ["a" * 64])

    assert got == {}, "the rows stay legacy as 'chunk missing'; a partial scan must not abort the whole rung"
    assert any(e["event"] == "retired_collection_skipped" and e["site"] == "upgrade_chunk_metadatas" for e in logs)


# ── nx enrich aspects backfill-uri ──────────────────────────────────────────────────────────────────────────────────

def test_enrich_aspects_backfill_uri_skips_a_retired_collection_and_reports_it(monkeypatch):
    class _Aspects:
        def list_by_collection(self, coll):
            if coll == RETIRED_NAME:
                raise _retired_409(coll)
            return []

    @contextlib.contextmanager
    def _t2(_path):
        yield SimpleNamespace(document_aspects=_Aspects())

    monkeypatch.setattr("nexus.db.t2.T2Database", _t2)
    monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: "x")
    monkeypatch.setattr(enrich, "_catalog_collections", lambda db: [RETIRED_NAME, LIVE_NAME])

    with capture_logs() as logs:
        result = CliRunner().invoke(aspects_backfill_uri_cmd, ["--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output[result.output.index("{"):])
    assert report["retired_skipped"] == [RETIRED_NAME]
    assert [c["collection"] for c in report["collections"]] == [LIVE_NAME]
    assert any(e["event"] == "retired_collection_skipped" and e["site"] == "enrich_aspects_backfill_uri" for e in logs)


# ── get_by_id and its callers ───────────────────────────────────────────────────────────────────────────────────────

def _get_by_id_world(monkeypatch):
    def post(path, body, **kw):
        raise _retired_409(body["collection"])

    monkeypatch.setattr("nexus.db.http_vector_client._post", post)


def test_get_by_id_surfaces_the_retired_error_and_still_reads_other_service_errors_as_a_miss(monkeypatch):
    _get_by_id_world(monkeypatch)
    with pytest.raises(RetiredNoSuccessorError):
        HttpVectorClient().get_by_id(RETIRED_NAME, "a" * 64, sensitivity="include")

    def other(path, body, **kw):
        raise VectorServiceError("collection gone", code=404)

    monkeypatch.setattr("nexus.db.http_vector_client._post", other)
    assert HttpVectorClient().get_by_id(RETIRED_NAME, "a" * 64, sensitivity="include") is None


def test_nx_store_get_of_a_retired_collection_is_the_one_message_not_not_found(monkeypatch):
    _get_by_id_world(monkeypatch)
    monkeypatch.setattr("nexus.commands.store._t3", lambda: HttpVectorClient())
    monkeypatch.setattr("nexus.commands.store._store_scope", lambda collection, db, **k: ([RETIRED_NAME], []))

    result = CliRunner().invoke(main, ["store", "get", "a" * 64, "-c", RETIRED_NAME])

    assert result.exit_code != 0
    assert "was retired and has no successor" in result.output and "not found" not in result.output


def test_a_retired_collection_is_unverifiable_never_a_ghost_to_the_note_presence_lookup(monkeypatch):
    _get_by_id_world(monkeypatch)
    with pytest.raises(RetiredNoSuccessorError):  # its callers read a raise as unverifiable, never "content is gone"
        note_chunks_present(HttpVectorClient(), RETIRED_NAME, "a" * 64, "a title")


# ── guards the 0e696a2211 confirmation review found no test could fail (T2 nexus/confirm-0e696a2211-nexus-p2m1x-2026-11-24) ──

def test_a_search_naming_a_retired_collection_memoizes_nothing_as_poisoned():
    live, dead = "code__nexus", "knowledge__gone__bge-base-en-v15-768__v1"
    before = set(search_engine._poisoned_collections)
    t3 = _BatchingT3({live: [{"id": "a", "content": "hit", "distance": 0.3}]}, retired={dead})
    search_cross_corpus("q", [live, dead], 10, t3, sensitivity="include")
    assert set(search_engine._poisoned_collections) == before, "a retired collection is not a permanent poisoning"


def test_the_cli_boundary_does_not_prefix_the_retired_message_as_a_generic_service_error():
    @click.command("zz-read-r7")
    def zz_read() -> None:
        raise _retired_409(RETIRED_NAME)

    main.add_command(zz_read)
    try:
        result = CliRunner().invoke(main, ["zz-read-r7"])
    finally:
        main.commands.pop("zz-read-r7", None)
    assert result.exit_code == 1
    assert "the nexus service returned an error" not in result.output and "was retired and has no successor" in result.output


def test_reembed_job_error_gives_the_typed_retired_error_its_own_message():
    err = _reembed_job_error(_retired_409(RETIRED_NAME))
    assert "was retired and has no successor" in err.message and "collection_superseded" not in err.message


def test_the_aspect_upsert_and_taxonomy_persist_do_not_flatten_the_retired_error_to_a_generic_conflict():
    request = httpx.Request("POST", "http://x/upsert")
    response = httpx.Response(409, request=request, json={"error": "collection_superseded", "reason": "collection_superseded", "collection": RETIRED_NAME})
    typed = _RetiredNoSuccessorHttpError(_retired_409(RETIRED_NAME), httpx.HTTPStatusError("409", request=request, response=response))

    with pytest.raises(RetiredNoSuccessorError):
        _raise_fenced_refusal(typed)


# ── the engine's explicit reason on the delete route (RETIRED_NO_SUCCESSOR_REASON, engine 8595519d90) ───────────────

@pytest.mark.parametrize("reason", ["collection_superseded", "retired_no_successor"])
def test_a_delete_refused_for_a_retired_row_with_no_successor_is_the_typed_refusal_under_either_reason(reason):
    request = httpx.Request("POST", "http://mock.test/delete")
    body = {"error": "collection_superseded", "reason": reason, "collection": RETIRED_NAME, "collection_id": 41}
    exc = httpx.HTTPStatusError("x", request=request, response=httpx.Response(409, json=body, request=request))

    refusal = refusal_of(exc)

    assert isinstance(refusal, SupersededCollectionDeleteError) and refusal.successor == ""
