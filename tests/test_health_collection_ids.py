# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.12 (nexus-p2m1x.32): ``nx doctor``'s three rows for the collection identity.

* ``collection ids``: the tenant's unstamped rows, read from the SAME ``GET /v1/catalog/rdr229_completion_counts`` body the
  RDR-229 completion rows read (no new route). The four history tables keep NULL on old rows by design and are named, not
  counted. ``catalog_collections`` is identity-filled at its DDL, so an unstamped row there is a failure.
* ``collection label clash``: computed on the client from the registry listing (live = ``superseded_by`` empty); a clash
  fails and names both ids.
* ``live-label index``: the engine's ``/v1/status`` boolean ``live_label_index_present`` (catalog-053).

Every way the engine cannot answer is a not-checked row (ok, skipped, "not checked: ..."), never "none".
"""
from __future__ import annotations

import httpx
import pytest

import nexus.db.http_engine_status as http_status
import nexus.health as health
from nexus.catalog.engine_reads import rdr229_completion_counts
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


def _counts(*tables: tuple[str, int, int]) -> dict:
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


def test_unstamped_rows_on_two_tables_and_one_history_table() -> None:
    counts = _counts(("catalog_documents", 0, 3), ("catalog_links", 1, 2), ("relevance_log", 0, 700))
    r = _rows(counts, [])[IDS_LABEL]
    assert r.ok is False and r.warn is True and r.fatal is False and not r.skipped
    assert r.detail.startswith("5 unstamped row(s) on 2 table(s)"), r.detail
    assert "catalog_documents: 3" in r.detail and "catalog_links: 2" in r.detail
    # the history table is named as unstamped by design and its 700 rows are not in the total
    assert "relevance_log" in r.detail and "by design" in r.detail and "700" not in r.detail.split("by design")[0]
    for t in HISTORY_TABLES:
        assert t in r.detail, (t, r.detail)
    assert HISTORY_TABLES == ("relevance_log", "search_telemetry", "hook_failures", "gc_audit")
    assert any("nexus.rdr229_restamp" in s for s in r.fix_suggestions), r.fix_suggestions


def test_nothing_unstamped_is_ok_and_names_the_history_tables_and_the_registry() -> None:
    r = _rows(_counts(("catalog_documents", 2, 0)), [])[IDS_LABEL]
    assert r.ok is True and not r.skipped and not r.warn
    assert r.detail.startswith("0 unstamped row(s)"), r.detail
    assert "catalog_collections" in r.detail and "identity" in r.detail
    for t in HISTORY_TABLES:
        assert t in r.detail


def test_only_a_history_table_unstamped_is_ok() -> None:
    r = _rows(_counts(("gc_audit", 0, 12)), [])[IDS_LABEL]
    assert r.ok is True and not r.warn and r.detail.startswith("0 unstamped row(s)"), r.detail


def test_a_null_id_on_the_registry_itself_is_a_failure() -> None:
    r = _rows(_counts(("catalog_collections", 0, 1), ("catalog_documents", 0, 2)), [])[IDS_LABEL]
    assert r.ok is False and r.warn is False and r.fatal is False, r
    assert "catalog_collections" in r.detail and "identity" in r.detail and r.detail.startswith("3 unstamped row(s)"), r.detail


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


def test_index_absent_is_a_warning_that_says_what_it_rests_on() -> None:
    r = _rows(_counts(), [], {"live_label_index_present": False})[INDEX_LABEL]
    assert r.ok is False and r.warn is True and not r.fatal, r
    assert "catalog-053 has not built" in r.detail and "the next boot retries" in r.detail
    assert "label uniqueness rests on the PK until P4.1" in r.detail


@pytest.mark.parametrize("status", [{}, {"live_label_index_present": "yes"}, {"live_label_index_present": None}, None],
                         ids=["field-absent", "not-a-boolean", "null", "status-unread"])
def test_index_field_absent_is_not_checked(status) -> None:
    r = _rows(_counts(), [], status)[INDEX_LABEL]
    assert _not_checked(r), r


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
