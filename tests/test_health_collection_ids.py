# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.12 (nexus-p2m1x.32): ``nx doctor``'s three rows for the collection identity.

* ``Collection ids``: the tenant's unstamped and orphan rows, read from the SAME ``GET /v1/catalog/rdr229_completion_counts``
  body the RDR-229 completion rows read (no new route). Fixtures hold only what the engine emits: ``tables[]`` carries the
  tables of ``p31-checks.tsv`` whose CHECK is still NOT VALID and that have at least one violating row, never a history table.
  ``catalog_collections``' ``unstamped`` is tombstones awaiting ``superseded_by_id``, a warning like any other table; a real
  failure is a registry row with a NULL ``collection_id`` in the listing the clash row reads.
* ``Collection label clash``: computed on the client from the registry listing (live = ``superseded_by`` empty); a clash
  fails and names both ids.
* ``Live-label index``: the engine's ``/v1/status`` boolean ``live_label_index_present`` (catalog-053).

Every way the engine cannot answer is a not-checked row (ok, skipped, "not checked: ..."), never "none".
"""
from __future__ import annotations

import httpx
import pytest

import nexus.db.http_engine_status as http_status
import nexus.health as health
from nexus.catalog.engine_reads import rdr229_completion_counts
from nexus.catalog.engine_reads.collection_rows import read_collection_rows
from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable
from nexus.collection_id_health import (
    CLASH_LABEL,
    HISTORY_TABLES,
    IDS_LABEL,
    INDEX_LABEL,
    check_collection_ids,
)
from nexus.health import HealthResult

_NO_DANGLING = {"total": 0, "by_content_type": {}, "self_id": {"total": 0, "by_content_type": {}}}


#: The tables of ``service/src/main/resources/db/rdr229/p31-checks.tsv`` that carry a CHECK. The engine lists one only while
#: that CHECK is NOT VALID and a row of the caller's tenant violates it (``CompletionTenantCounts.readIn``).
_P31_TABLES = frozenset({
    "catalog_collections", "chunks", "taxonomy_centroids", "catalog_document_chunks", "chunk_orphaned_at", "topic_assignments",
    "document_aspects", "topics", "taxonomy_meta", "document_highlights", "canary_query_ring", "aspect_extraction_queue",
    "catalog_documents", "chash_remap", "pdf_pipeline", "reembed_jobs",
})


def _counts(*tables: tuple[str, int, int]) -> dict:
    for t, o, u in tables:
        assert t in _P31_TABLES, f"{t} has no CHECK in p31-checks.tsv, so the engine never lists it"
        assert o + u > 0, f"the engine omits a table with no violating row: {t}"
    return {
        "registry_rows": 10, "live_collections": 9, "superseded_by_waiting": 0, "dangling_tombstones": _NO_DANGLING,
        "tables": [{"table": t, "orphans": o, "unstamped": u} for t, o, u in tables],
    }


def _live(name: str, cid: int | None, **extra) -> dict:
    return {"name": name, "collection_id": cid, "superseded_by": "", **extra}


_STATUS_OK = {"live_label_index_present": True}


def _rows(counts=None, rows=None, status=_STATUS_OK) -> dict[str, HealthResult]:
    out = check_collection_ids(
        status,
        counts_reader=(lambda: counts if not isinstance(counts, Exception) else (_ for _ in ()).throw(counts)),
        rows_reader=(lambda: rows if not isinstance(rows, Exception) else (_ for _ in ()).throw(rows)),
    )
    assert [r.label for r in out] == [IDS_LABEL, CLASH_LABEL, INDEX_LABEL], [r.label for r in out]
    return {r.label: r for r in out}


def _not_checked(r: HealthResult) -> bool:
    return r.ok is True and r.skipped is True and not r.warn and not r.fatal and r.detail.startswith("not checked")


# ── collection ids ───────────────────────────────────────────────────────────


def _ids(counts, rows=None) -> HealthResult:
    """The ids row over a registry listing with an id on every row unless the caller gives one."""
    return _rows(counts, [_live("code__1-1__voyage-code-3__v1", 1)] if rows is None else rows)[IDS_LABEL]


def _fix_text(r: HealthResult) -> str:
    return " ".join(r.fix_suggestions)


def test_the_labels_are_capitalised_like_their_neighbours() -> None:
    assert (IDS_LABEL, CLASH_LABEL, INDEX_LABEL) == ("Collection ids", "Collection label clash", "Live-label index")


def test_unstamped_rows_on_two_tables_warn_and_the_stamp_move_clears_them() -> None:
    r = _ids(_counts(("catalog_documents", 0, 3), ("chunks", 0, 2)))
    assert r.ok is False and r.warn is True and r.fatal is False and not r.skipped
    assert r.detail.startswith("5 unstamped row(s) on 2 table(s)"), r.detail
    assert "catalog_documents: 3" in r.detail and "chunks: 2" in r.detail
    fix = _fix_text(r)
    # the stamp move clears them on its own: this row recommends nothing to run, and points at the rows that say more
    assert "on its own" in fix and "completion" in fix and "VALIDATE" in fix, fix
    assert "rdr229_restamp" not in fix and "nexus_admin" not in fix, fix


def test_the_history_tables_are_one_static_sentence_never_counted() -> None:
    assert HISTORY_TABLES == ("relevance_log", "search_telemetry", "hook_failures", "gc_audit")
    clean = _ids(_counts())
    waiting = _ids(_counts(("catalog_documents", 0, 3)))
    sentence = "Unstamped by design and not counted (old rows keep NULL, no CHECK): " + ", ".join(HISTORY_TABLES) + "."
    assert sentence in clean.detail and sentence in waiting.detail
    assert waiting.detail.startswith("3 unstamped row(s) on 1 table(s)"), waiting.detail


def test_registry_tombstones_are_a_warning_awaiting_superseded_by_id_never_a_failure() -> None:
    # catalog_collections is kind SUPERSEDED: its unstamped is the tombstones whose superseded_by_id is NULL, orphans always 0
    r = _ids(_counts(("catalog_collections", 0, 4), ("catalog_documents", 0, 2)))
    assert r.ok is False and r.warn is True and r.fatal is False, r
    assert "4 tombstone(s) in catalog_collections awaiting superseded_by_id" in r.detail, r.detail
    assert "2 unstamped row(s) on 1 table(s)" in r.detail
    assert "catalog_collections: " not in r.detail, "the registry is not one of the per-table unstamped counts"
    fix = _fix_text(r)
    assert "identity" not in r.detail and "identity" not in fix and "restore" not in fix, (r.detail, fix)
    assert "rdr229_restamp" not in fix


def test_only_registry_tombstones_still_warn() -> None:
    r = _ids(_counts(("catalog_collections", 0, 1)))
    assert r.warn is True and r.ok is False and not r.fatal
    assert "1 tombstone(s) in catalog_collections awaiting superseded_by_id" in r.detail
    assert "unstamped row(s) on" not in r.detail


def test_orphan_rows_are_named_per_table_as_write_frozen_and_point_at_the_validate_rows() -> None:
    r = _ids(_counts(("catalog_documents", 2, 0), ("chunks", 1, 4)))
    assert r.ok is False and r.warn is True and r.fatal is False, r
    assert "3 orphan row(s)" in r.detail and "write-frozen" in r.detail, r.detail
    assert "catalog_documents: 2" in r.detail and "chunks: 1" in r.detail
    assert "4 unstamped row(s) on 1 table(s)" in r.detail, r.detail
    fix = _fix_text(r)
    assert "no registry row" in fix and "23514" in fix and "VALIDATE" in fix and "completion" in fix, fix


def test_a_table_with_only_orphans_is_not_ok() -> None:
    r = _ids(_counts(("document_aspects", 5, 0)))
    assert r.ok is False and r.warn is True, r
    assert "0 unstamped" not in r.detail and "5 orphan row(s)" in r.detail, r.detail
    assert "unstamped row(s) on" not in r.detail


def test_nothing_waiting_is_ok_and_says_what_it_checked() -> None:
    r = _ids(_counts(), [_live("a", 1), _live("b", 2)])
    assert r.ok is True and not r.skipped and not r.warn and not r.fatal and not r.fix_suggestions, r
    assert r.detail.startswith("0 unstamped and 0 orphan row(s)"), r.detail
    assert "2 registry row(s) checked for a null collection_id" in r.detail, r.detail
    for t in HISTORY_TABLES:
        assert t in r.detail


def test_a_registry_row_with_a_null_collection_id_fails_and_names_its_label() -> None:
    rows = [_live("docs__1-1__m__v1", 41), _live("docs__1-1__m__v2", None),
            {"name": "docs__1-1__m__v0", "collection_id": None, "superseded_by": "docs__1-1__m__v1"}]
    r = _ids(_counts(), rows)
    assert r.ok is False and r.warn is False and r.fatal is False and not r.skipped, r
    assert r.detail.startswith("2 registry row(s) with no collection_id: "), r.detail
    assert "'docs__1-1__m__v2'" in r.detail and "'docs__1-1__m__v0'" in r.detail and "'docs__1-1__m__v1'" not in r.detail
    assert "identity" in _fix_text(r) and "by hand" in _fix_text(r)


def test_a_null_registry_id_outranks_a_wait_and_survives_unread_counts() -> None:
    rows = [_live("x", None), _live("y", 5)]  # an engine that lists ids (one row carries one), with one null
    waiting = _ids(_counts(("catalog_documents", 0, 3)), rows)
    assert waiting.ok is False and waiting.warn is False, waiting
    assert "'x'" in waiting.detail and "3 unstamped row(s)" in waiting.detail
    unread = _ids(EngineRouteUnavailable("/rdr229_completion_counts", "f", EngineRouteUnavailable.TIMEOUT), rows)
    assert unread.ok is False and unread.warn is False and not unread.skipped, unread
    assert "'x'" in unread.detail and "counts not read" in unread.detail, unread.detail


def _listed(raw_rows: list[dict]) -> list[dict]:
    """Rows as the doctor reads them: the real uncoerced reader over HttpCatalogClient with a stubbed GET."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — test-local

    client = HttpCatalogClient(base_url="http://127.0.0.1:9", _token="t")
    client._get = lambda path, **_kw: {"collections": raw_rows}  # type: ignore[method-assign]
    return read_collection_rows(client)


def test_an_engine_that_lists_no_ids_is_not_checked_never_a_failure() -> None:
    # every released engine before P3.5a's read half: no collection_id key; the client reads it as None on every row
    rows = _listed([{"name": "docs__a__m__v1", "superseded_by": ""}, {"name": "c9", "superseded_by": ""}])
    assert all("collection_id" not in r for r in rows), "the uncoerced read keeps the key absent"
    r = _ids(_counts(), rows)
    assert r.ok is True and not r.warn, r
    assert "no collection_id:" not in r.detail and "this engine does not list collection_id" in r.detail, r.detail


def test_a_listing_with_ids_and_one_null_fails_naming_it() -> None:
    rows = _listed([{"name": "a", "superseded_by": "", "collection_id": 7}, {"name": "b", "superseded_by": "", "collection_id": None}])
    r = _ids(_counts(), rows)
    assert r.ok is False and not r.warn and "1 registry row(s) with no collection_id: 'b'" in r.detail, r.detail
    assert "2 registry row(s) checked for a null collection_id" in r.detail, r.detail


def test_an_unread_listing_is_said_in_the_ids_row_and_the_counts_still_report() -> None:
    r = _rows(_counts(("catalog_documents", 0, 1)), RuntimeError("engine down"))[IDS_LABEL]
    assert r.warn is True and "registry ids not checked: the registry listing could not be read" in r.detail, r.detail
    ok = _rows(_counts(), RuntimeError("engine down"))[IDS_LABEL]
    assert ok.ok is True and "registry ids not checked" in ok.detail, ok


class _Client:
    """The catalog client's slice ``read_rdr229_completion_counts`` uses."""

    def __init__(self, flags: dict, failure: Exception | None = None, probe_failed: bool = False) -> None:
        self._flags, self._failure, self._version_probe_failed = flags, failure, probe_failed

    def _version_flags(self) -> dict:
        return self._flags

    def _get(self, route, **_kw):
        assert self._failure is not None, "the route must not be asked"
        raise self._failure


def _http(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://engine/v1/catalog/rdr229_completion_counts")
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=httpx.Response(status, request=req))


_FLAG = {rdr229_completion_counts.FLAG: True}


@pytest.mark.parametrize(
    "client",
    [
        pytest.param(_Client(_FLAG, _http(404)), id="404"),
        pytest.param(_Client(_FLAG, _http(503)), id="503"),
        pytest.param(_Client(_FLAG, _http(500)), id="failed"),
        pytest.param(_Client(_FLAG, RuntimeError("boom")), id="transport-error"),
        pytest.param(_Client({}), id="flag-absent"),
        pytest.param(_Client({}, probe_failed=True), id="version-probe-failed"),
    ],
)
def test_every_way_the_engine_cannot_say_is_not_checked(client) -> None:
    out = check_collection_ids(
        _STATUS_OK, counts_reader=lambda: rdr229_completion_counts.read_rdr229_completion_counts(client), rows_reader=lambda: [],
    )
    ids = next(r for r in out if r.label == IDS_LABEL)
    assert _not_checked(ids), ids
    assert "none" not in ids.detail.lower().replace("nonexistent", "")


@pytest.mark.parametrize(
    ("client", "asks_again"),
    [
        pytest.param(_Client({}), False, id="flag-absent"),
        pytest.param(_Client(_FLAG, _http(404)), False, id="404-unsupported"),
        pytest.param(_Client({}, probe_failed=True), True, id="version-probe-failed"),
        pytest.param(_Client(_FLAG, _http(503)), True, id="503"),
        pytest.param(_Client(_FLAG, _http(500)), True, id="failed"),
    ],
)
def test_an_engine_without_the_route_is_not_asked_again_but_a_failed_read_is(client, asks_again: bool) -> None:
    """A flag the engine does not advertise (or a 404) changes only when the engine does, so the row does not say the next
    ``nx doctor`` asks again (retry=False, as health.py's census rows); a failed probe or read does."""
    out = check_collection_ids(
        _STATUS_OK, counts_reader=lambda: rdr229_completion_counts.read_rdr229_completion_counts(client), rows_reader=lambda: [],
    )
    ids = next(r for r in out if r.label == IDS_LABEL)
    assert _not_checked(ids), ids
    assert ("asks again" in ids.detail) is asks_again, ids.detail


def test_a_malformed_body_is_not_checked() -> None:
    class Bad(_Client):
        def _get(self, route, **_kw):
            return {"tables": "nope"}

    err = None
    try:
        rdr229_completion_counts.read_rdr229_completion_counts(Bad(_FLAG))
    except EngineRouteUnavailable as exc:
        err = exc
    assert err is not None and err.reason == EngineRouteUnavailable.MALFORMED
    r = _rows(err, [])[IDS_LABEL]
    assert _not_checked(r), r


def test_no_catalog_reader_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: None)
    out = check_collection_ids(_STATUS_OK)
    assert [r.label for r in out] == [IDS_LABEL, CLASH_LABEL, INDEX_LABEL]
    assert _not_checked(out[0]) and _not_checked(out[1]), out
    assert out[2].ok and not out[2].skipped  # the index row reads the status alone


# ── label clash ──────────────────────────────────────────────────────────────


def test_two_live_rows_on_one_label_fail_and_name_both_ids() -> None:
    rows = [_live("docs__1-1__m__v1", 41), _live("docs__1-1__m__v1", 77), _live("c9001", 9001)]
    r = _rows(_counts(), rows)[CLASH_LABEL]
    assert r.ok is False and r.warn is False and not r.skipped, r
    assert "docs__1-1__m__v1" in r.detail and "41" in r.detail and "77" in r.detail, r.detail
    assert "c9001" not in r.detail
    fix = " ".join(r.fix_suggestions)
    assert "P3.5a" in fix and "write half" in fix and "P4.1" in fix and "no command does it by id yet" in fix, fix


def test_a_tombstone_sharing_a_label_with_a_live_row_is_not_a_clash() -> None:
    rows = [_live("docs__1-1__m__v1", 41),
            {"name": "docs__1-1__m__v1", "collection_id": 40, "superseded_by": "docs__1-1__m__v2"}]
    r = _rows(_counts(), rows)[CLASH_LABEL]
    assert r.ok is True and not r.skipped and not r.warn, r
    assert r.detail.startswith("none"), r.detail


def test_a_clash_on_a_row_without_an_id_still_names_the_label() -> None:
    r = _rows(_counts(), [_live("x", None), _live("x", 5)])[CLASH_LABEL]
    assert r.ok is False and "x" in r.detail and "5" in r.detail and "no id" in r.detail, r.detail


@pytest.mark.parametrize("rows", [RuntimeError("engine down"), None], ids=["listing-raises", "listing-not-a-list"])
def test_an_unreadable_listing_is_not_checked(rows) -> None:
    r = _rows(_counts(), rows)[CLASH_LABEL]
    assert _not_checked(r), r


# ── live-label index ─────────────────────────────────────────────────────────


def test_index_present_is_ok() -> None:
    r = _rows(_counts(), [], {"live_label_index_present": True})[INDEX_LABEL]
    assert r.ok is True and not r.skipped and not r.warn, r


def test_index_absent_is_a_warning_that_is_harmless_until_p4_1_and_names_both_causes() -> None:
    r = _rows(_counts(), [], {"live_label_index_present": False})[INDEX_LABEL]
    assert r.ok is False and r.warn is True and not r.fatal, r
    assert "Harmless until P4.1" in r.detail and "primary key" in r.detail, r.detail
    fix = " ".join(r.fix_suggestions)
    # catalog-053-live-label-unique.xml: a lock timeout (retried at the next boot) or a migration role that no longer owns the table
    assert "lock timeout" in fix and "retries it by itself" in fix, fix
    assert "no longer owns nexus.catalog_collections" in fix and "a restart does not fix that" in fix, fix
    assert "catalog-053-1" in fix
    assert not fix.lower().startswith("restart"), "restarting the engine is not the primary remedy"


def test_index_field_absent_says_predates_or_probe_could_not_run_and_asks_again() -> None:
    r = _rows(_counts(), [], {})[INDEX_LABEL]
    assert _not_checked(r), r
    assert "predates live_label_index_present" in r.detail and "probe could not run" in r.detail, r.detail
    assert "the next `nx doctor` asks again" in r.detail, r.detail


@pytest.mark.parametrize("status", [{"live_label_index_present": "yes"}, {"live_label_index_present": None}, None],
                         ids=["not-a-boolean", "null", "status-unread"])
def test_index_field_unreadable_is_not_checked(status) -> None:
    r = _rows(_counts(), [], status)[INDEX_LABEL]
    assert _not_checked(r), r
    assert "asks again" in r.detail, r.detail


# ── wiring ───────────────────────────────────────────────────────────────────


def test_the_sweep_runs_the_rows_on_the_status_it_was_handed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_health_checks`` is called for real with a canned status and every other check stepped over: the rows are in the
    sweep under their ``_check_collection_ids`` name and are handed the caller's status."""
    ran: list[str] = []

    def stepper(_progress):
        def step(name, fn):
            ran.append(name)
            return list(fn()) if name == "_check_collection_ids" else []
        return step

    monkeypatch.setattr(health, "_row_stepper", stepper)
    monkeypatch.setattr(http_status, "fetch_engine_status", lambda *a, **k: pytest.fail("the sweep fetched the status"))
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: None)

    results, _local = health.run_health_checks(engine_status={"live_label_index_present": False})

    assert "_check_collection_ids" in ran
    by_label = {r.label: r for r in results if r.label in (IDS_LABEL, CLASH_LABEL, INDEX_LABEL)}
    assert set(by_label) == {IDS_LABEL, CLASH_LABEL, INDEX_LABEL}
    assert by_label[INDEX_LABEL].warn is True


def test_an_id_listing_engine_whose_every_row_is_null_fails_naming_them() -> None:
    # linda's pre-review: the capability is the key's presence, never "some row holds an int"
    rows = _listed([{"name": "a", "superseded_by": "", "collection_id": None},
                    {"name": "b", "superseded_by": "", "collection_id": None}])
    r = _ids(_counts(), rows)
    assert r.ok is False and not r.warn and "2 registry row(s) with no collection_id: 'a', 'b'" in r.detail, r.detail


def test_the_doctor_reads_the_uncoerced_listing_when_the_reader_offers_it(monkeypatch) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — test-local

    client = HttpCatalogClient(base_url="http://127.0.0.1:9", _token="t")
    # an engine before P3.5a's read half: no collection_id key. Read through the coerced list_collections, the key would
    # be filled with None and every row would fail; the uncoerced read keeps it absent, so the row is "not checked".
    client._get = lambda path, **_kw: {"collections": [{"name": "a", "superseded_by": ""}, {"name": "b", "superseded_by": ""}]}  # type: ignore[method-assign]
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: client)
    counts = _counts()
    out = {x.label: x for x in check_collection_ids(_STATUS_OK, counts_reader=lambda: counts, rows_reader=None)}
    r = out[IDS_LABEL]
    assert r.ok is True and not r.warn and "this engine does not list collection_id" in r.detail, r.detail
