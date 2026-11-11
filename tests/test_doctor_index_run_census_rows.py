# SPDX-License-Identifier: AGPL-3.0-or-later
"""F30 (nexus-f3yqd.6): the doctor's three index-run rows read the engine's census routes (engine 0.1.190).

With both flags (``catalog_index_run_census_supported``, ``catalog_next_seq_drift_read_supported``) the rows run in the
default sweep from two bounded requests and no catalog walk. Every way the engine cannot say (a 404 or 503, a bucket
that timed out, a malformed body, a failed request, an unlisted flag) is a not-checked row: ok, skipped, no warning,
never "none". A capped census with a zero is "not fully checked", never "none". Without the flags the rows are what they
were: "not checked" in the default sweep, a catalog walk under ``--check-index-runs``.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

import nexus.health as h
from nexus.catalog.http_catalog_client import INDEX_RUN_CENSUS_MAX_LIMIT

_LABELS = (h._STALE_FENCES_LABEL, h._HIDDEN_CHUNKS_LABEL, h._NEXT_SEQ_LABEL)

_FENCE_ANCHORED = {
    "checked": 5, "reported_null": 6, "undated_reported_null": 1, "floor_candidates": 3,
    "anchor": {"kind": "proven", "at": "2026-08-20T10:00:00Z", "earliest_stamped_at": "2026-08-09T09:00:00Z",
               "proven_at": "2026-08-20T10:00:00Z"},
    "post_anchor": {"count": 1, "capped": False, "examples": [
        {"tumbler": "2.1.6", "source_uri": "file:///r/a.py", "file_path": "a.py", "indexed_at": "2026-09-01T00:00:00Z"}]},
}
_FENCE_CLEAN = {
    "checked": 5, "reported_null": 2, "undated_reported_null": 0, "floor_candidates": 0,
    "anchor": {"kind": "earliest", "at": "2026-08-12T00:00:00Z", "earliest_stamped_at": "2026-08-12T00:00:00Z",
               "proven_at": None},
    "post_anchor": {"count": 0, "capped": False, "examples": []},
}
_STALE_NONE = {"count": 0, "capped": False, "notes": 0, "examples": []}
_STALE_TWO = {"count": 2, "capped": False, "notes": 1, "examples": [
    {"tumbler": "1.1.1", "source_uri": "file:///r/s.py", "started_at": "2026-11-10T00:00:00Z", "age_s": 90000, "note": False},
    {"tumbler": "1.1.2", "source_uri": None, "started_at": "2026-11-10T00:00:00Z", "age_s": 108000, "note": True}]}
_FAILED_NONE = {"failed": {"count": 0, "capped": False}, "count": 0, "capped": False, "examples": []}
_DRIFT_NONE = {"checked": 4, "drifted": 0, "capped": False, "examples": []}


def _census(stale=_STALE_NONE, fence=_FENCE_CLEAN, failed=_FAILED_NONE) -> dict:
    return copy.deepcopy({"stale": stale, "fence": fence, "failed_hidden": failed})


class _Reader:
    """A catalog reader with the census routes: what each answers, and what it was asked."""

    def __init__(self, census: Any = None, drift: Any = None, *, supported: bool = True) -> None:
        self._census, self._drift, self._supported = census, drift, supported
        self.asked: list[tuple[str, dict]] = []
        self.walks = 0

    def index_run_routes_supported(self) -> bool:
        return self._supported

    def index_run_census(self, limit: int = 0) -> Any:
        self.asked.append(("census", {"limit": limit}))
        if isinstance(self._census, Exception):
            raise self._census
        return self._census

    def next_seq_drift(self, limit: int = 0) -> Any:
        self.asked.append(("drift", {"limit": limit}))
        if isinstance(self._drift, Exception):
            raise self._drift
        return self._drift

    def all_documents(self, limit: int = 0, **_kw: Any) -> list:
        self.walks += 1
        return []

    def list_owners(self, include_deactivated: bool = False) -> list:
        return []

    def owners_with_roots(self) -> dict:
        return {}


def _wire(monkeypatch: pytest.MonkeyPatch, reader: Any) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader)


def _rows(monkeypatch: pytest.MonkeyPatch, reader: Any) -> dict[str, list[h.HealthResult]]:
    _wire(monkeypatch, reader)
    out: dict[str, list[h.HealthResult]] = {}
    for r in h._check_index_run_routes():
        out.setdefault(r.label, []).append(r)
    return out


def _is_not_checked(r: h.HealthResult) -> bool:
    return r.ok is True and r.skipped is True and not r.warn and not r.fatal and r.detail.startswith("not checked")


# ── the rows the routes answer ───────────────────────────────────────────────


def test_a_healthy_census_is_three_clean_rows_from_two_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(), _DRIFT_NONE)
    rows = _rows(monkeypatch, reader)
    assert list(rows) == list(_LABELS) and all(len(v) == 1 for v in rows.values())
    stale, hidden, drift = (rows[label][0] for label in _LABELS)
    assert stale.ok and not stale.skipped and stale.detail.startswith("none (5 fenced document(s) checked)"), stale.detail
    assert hidden.ok and not hidden.skipped and hidden.detail == "not applicable (no failed index runs)"
    assert drift.ok and not drift.skipped and drift.detail == "none (4 owner(s) checked)"
    assert reader.asked == [("census", {"limit": INDEX_RUN_CENSUS_MAX_LIMIT}), ("drift", {"limit": INDEX_RUN_CENSUS_MAX_LIMIT})]
    assert INDEX_RUN_CENSUS_MAX_LIMIT == 10_000 and reader.walks == 0


def test_findings_are_the_walk_rows_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = {"failed": {"count": 5, "capped": False}, "count": 2, "capped": False, "examples": []}
    drift = {"checked": 4, "drifted": 1, "capped": False,
             "examples": [{"owner": "1.12", "next_seq": 3, "highest_child_seq": 9}]}
    rows = _rows(monkeypatch, _Reader(_census(_STALE_TWO, _FENCE_ANCHORED, failed), drift))
    stale = rows[h._STALE_FENCES_LABEL]
    assert [r.warn for r in stale] == [True, True]
    assert any(r.detail.startswith("1 document(s) report index_state but carry no stamp") and "file:///r/a.py" in r.detail for r in stale)
    stranded = next(r for r in stale if "stranded in index_state='indexing'" in r.detail)
    assert stranded.detail.startswith("2 document(s) stranded") and "file:///r/s.py (25.0h)" in stranded.detail
    assert "1.1.2 (30.0h, note)" in stranded.detail and "1 of them are notes" in stranded.detail
    (hidden,) = rows[h._HIDDEN_CHUNKS_LABEL]
    assert hidden.warn and hidden.detail.startswith("2 of 5 failed document(s) have chunks stored with no manifest owner")
    (d,) = rows[h._NEXT_SEQ_LABEL]
    assert d.warn and d.detail.startswith("1 owner(s) whose allocator is at or below") and "1.12 (next_seq=3, highest child=9)" in d.detail


def test_a_capped_stale_count_is_at_least(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = {**_STALE_TWO, "count": 10_000, "capped": True, "notes": 1}
    rows = _rows(monkeypatch, _Reader(_census(stale), _DRIFT_NONE))
    row = next(r for r in rows[h._STALE_FENCES_LABEL] if "stranded" in r.detail)
    assert row.detail.startswith("at least 10000 document(s) stranded"), row.detail
    assert "; +9998 more" in row.detail


def test_drift_beyond_the_examples_is_counted_and_named_as_more(monkeypatch: pytest.MonkeyPatch) -> None:
    drift = {"checked": 9, "drifted": 5, "capped": True, "examples": [{"owner": "1.1", "next_seq": 1, "highest_child_seq": 4}]}
    (row,) = _rows(monkeypatch, _Reader(_census(), drift))[h._NEXT_SEQ_LABEL]
    assert row.detail.startswith("5 owner(s) whose allocator") and "; +4 more" in row.detail, row.detail


def test_no_owners_is_a_skip_the_walk_rows_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    (row,) = _rows(monkeypatch, _Reader(_census(), {"checked": 0, "drifted": 0, "capped": False, "examples": []}))[h._NEXT_SEQ_LABEL]
    assert row.ok and row.detail == "skipped (no owners)"


# ── a capped zero is never "none" ────────────────────────────────────────────


def test_a_capped_failed_census_with_nothing_hidden_is_not_fully_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = {"failed": {"count": 10_000, "capped": True}, "count": 0, "capped": True, "examples": []}
    (row,) = _rows(monkeypatch, _Reader(_census(failed=failed), _DRIFT_NONE))[h._HIDDEN_CHUNKS_LABEL]
    assert row.ok is True and row.skipped is True and not row.warn
    assert row.detail.startswith("not fully checked"), row.detail
    assert "none" not in row.detail.lower().replace("none with", "")


def test_a_capped_failed_census_with_some_hidden_is_at_least(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = {"failed": {"count": 10_000, "capped": True}, "count": 3, "capped": True, "examples": []}
    (row,) = _rows(monkeypatch, _Reader(_census(failed=failed), _DRIFT_NONE))[h._HIDDEN_CHUNKS_LABEL]
    assert row.warn and row.detail.startswith("at least 3 of 10000+ failed document(s)"), row.detail


def test_an_uncapped_failed_census_with_nothing_hidden_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = {"failed": {"count": 7, "capped": False}, "count": 0, "capped": False, "examples": []}
    (row,) = _rows(monkeypatch, _Reader(_census(failed=failed), _DRIFT_NONE))[h._HIDDEN_CHUNKS_LABEL]
    assert row.ok and not row.skipped and row.detail == "none (7 failed document(s), none with stored chunks hidden)"


# ── every cannot-say path is skipped, ok, no warning ─────────────────────────


@pytest.mark.parametrize("census", [None, RuntimeError("503"), "x", []], ids=["none", "raises", "string", "list"])
def test_no_census_answer_skips_the_census_rows_and_the_drift_row_still_reads(monkeypatch: pytest.MonkeyPatch, census: Any) -> None:
    rows = _rows(monkeypatch, _Reader(census, _DRIFT_NONE))
    assert list(rows) == list(_LABELS)
    assert _is_not_checked(rows[h._STALE_FENCES_LABEL][0]) and _is_not_checked(rows[h._HIDDEN_CHUNKS_LABEL][0])
    assert not rows[h._NEXT_SEQ_LABEL][0].skipped and rows[h._NEXT_SEQ_LABEL][0].ok


@pytest.mark.parametrize("drift", [None, RuntimeError("404"), "x"], ids=["none", "raises", "string"])
def test_no_drift_answer_skips_the_drift_row_and_the_census_rows_still_read(monkeypatch: pytest.MonkeyPatch, drift: Any) -> None:
    rows = _rows(monkeypatch, _Reader(_census(), drift))
    assert _is_not_checked(rows[h._NEXT_SEQ_LABEL][0])
    assert not rows[h._STALE_FENCES_LABEL][0].skipped and not rows[h._HIDDEN_CHUNKS_LABEL][0].skipped


def test_every_unanswered_row_never_warns_whatever_the_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows(monkeypatch, _Reader(ValueError("bad"), ConnectionError("refused")))
    assert all(_is_not_checked(r) for v in rows.values() for r in v) and len(rows) == 3


@pytest.mark.parametrize(("bucket", "labels"), [
    ("stale", (h._STALE_FENCES_LABEL,)), ("fence", (h._STALE_FENCES_LABEL,)), ("failed_hidden", (h._HIDDEN_CHUNKS_LABEL,)),
])
def test_a_bucket_that_timed_out_is_not_checked_and_never_none(monkeypatch: pytest.MonkeyPatch, bucket: str, labels: tuple) -> None:
    census = _census()
    census[bucket] = {"timed_out": True}
    rows = _rows(monkeypatch, _Reader(census, _DRIFT_NONE))
    for label in labels:
        assert [_is_not_checked(r) for r in rows[label]] == [True], [r.detail for r in rows[label]]
        assert "none" not in rows[label][0].detail
    # The buckets that answered still read.
    for label in set(_LABELS) - set(labels) - {h._NEXT_SEQ_LABEL}:
        assert not rows[label][0].skipped, rows[label][0].detail


def test_a_stale_bucket_that_timed_out_does_not_hide_the_fence_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    census = _census(fence=_FENCE_ANCHORED)
    census["stale"] = {"timed_out": True}
    rows = _rows(monkeypatch, _Reader(census, _DRIFT_NONE))[h._STALE_FENCES_LABEL]
    assert [bool(r.warn) for r in rows] == [True, False]
    assert rows[0].detail.startswith("1 document(s) report index_state but carry no stamp")
    assert _is_not_checked(rows[1]) and "stale fences" in rows[1].detail


def test_a_fence_bucket_that_timed_out_does_not_hide_the_stale_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    census = _census(stale=_STALE_TWO)
    census["fence"] = {"timed_out": True}
    rows = _rows(monkeypatch, _Reader(census, _DRIFT_NONE))[h._STALE_FENCES_LABEL]
    assert rows[0].warn and rows[0].detail.startswith("2 document(s) stranded")
    assert _is_not_checked(rows[-1]) and "unstamped documents" in rows[-1].detail and len(rows) == 2


def test_both_census_buckets_timing_out_is_one_not_checked_row(monkeypatch: pytest.MonkeyPatch) -> None:
    census = _census()
    census["stale"] = census["fence"] = {"timed_out": True}
    (row,) = _rows(monkeypatch, _Reader(census, _DRIFT_NONE))[h._STALE_FENCES_LABEL]
    assert _is_not_checked(row)


def test_a_body_the_client_accepted_but_the_row_cannot_render_is_three_not_checked_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    census = _census(fence=_FENCE_ANCHORED)
    census["fence"]["post_anchor"]["examples"][0]["indexed_at"] = "not a time"  # the client validates this; a fake reader does not
    rows = _rows(monkeypatch, _Reader(census, _DRIFT_NONE))
    assert all(_is_not_checked(r) for v in rows.values() for r in v) and len(rows) == 3


# ── no routes: the rows are what they were ───────────────────────────────────


@pytest.mark.parametrize("reader", [
    None, object(), _Reader(supported=False), type("R", (), {"index_run_routes_supported": lambda self: "yes"})(),
    type("R", (), {"index_run_routes_supported": lambda self: (_ for _ in ()).throw(RuntimeError("no /version"))})(),
], ids=["no-reader", "no-method", "flag-false", "flag-not-true", "probe-raises"])
def test_an_engine_without_both_routes_yields_no_route_rows(monkeypatch: pytest.MonkeyPatch, reader: Any) -> None:
    _wire(monkeypatch, reader)
    assert h._check_index_run_routes() == []
    if isinstance(reader, _Reader):
        assert reader.asked == []


def test_a_reader_that_cannot_be_built_yields_no_route_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("no service")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", boom)
    assert h._check_index_run_routes() == []


# ── the sweep and --check-index-runs ─────────────────────────────────────────


class _NoCollections:
    def list_collections(self, *a: Any, **k: Any) -> list:
        return []


def _sweep(monkeypatch: pytest.MonkeyPatch, reader: Any) -> dict[str, list[h.HealthResult]]:
    for name in dir(h):
        if name.startswith("_check_") and name != "_check_index_run_routes" and callable(getattr(h, name)):
            monkeypatch.setattr(h, name, lambda *a, **k: [])
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _NoCollections())
    walked: list[int] = []
    monkeypatch.setattr(h, "_walk_documents_for_doctor", lambda: (walked.append(1), None)[1])
    _wire(monkeypatch, reader)
    assert not walked
    out: dict[str, list[h.HealthResult]] = {}
    for r in h.run_health_checks()[0]:
        out.setdefault(r.label, []).append(r)
    assert not walked, "the default sweep walked the catalog"
    return out


def test_the_default_sweep_reads_the_three_rows_from_the_routes_without_a_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(_STALE_TWO), _DRIFT_NONE)
    rows = _sweep(monkeypatch, reader)
    assert reader.walks == 0 and len(reader.asked) == 2
    assert any("stranded in index_state='indexing'" in r.detail for r in rows[h._STALE_FENCES_LABEL])
    assert not any(r.skipped for label in _LABELS for r in rows[label])
    assert not any("--check-index-runs" in r.detail for label in _LABELS for r in rows[label])


def test_the_default_sweep_on_an_engine_without_the_routes_is_what_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _sweep(monkeypatch, _Reader(_census(), _DRIFT_NONE, supported=False))
    for label in _LABELS:
        (r,) = rows[label]
        assert r.ok is True and r.skipped is True and not r.warn
        assert r.detail == ("not checked in the default sweep (it reads every catalog document); "
                            "run `nx doctor --check-index-runs`")


def test_the_default_sweep_with_unanswered_routes_is_not_checked_and_still_no_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _sweep(monkeypatch, _Reader(None, None))
    for label in _LABELS:
        (r,) = rows[label]
        assert _is_not_checked(r) and "--check-index-runs" not in r.detail


def test_check_index_runs_reads_the_routes_and_does_not_walk_when_the_engine_has_them(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(), _DRIFT_NONE)
    _wire(monkeypatch, reader)
    walked: list[int] = []
    monkeypatch.setattr(h, "_walk_documents_for_doctor", lambda: (walked.append(1), [])[1])
    rows = h.run_index_run_checks()
    assert [r.label for r in rows] == list(_LABELS) and not walked and reader.walks == 0


def test_check_index_runs_walks_as_before_on_an_engine_without_the_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(), _DRIFT_NONE, supported=False)
    _wire(monkeypatch, reader)
    walks: list[int] = []
    docs: list = []
    monkeypatch.setattr(h, "_walk_documents_for_doctor", lambda: (walks.append(1), docs)[1])
    seen: list[object] = []
    for name, label in zip(("_check_stale_indexing_runs", "_check_failed_runs_hidden_chunks", "_check_next_seq_drift"), _LABELS):
        monkeypatch.setattr(h, name, lambda d, label=label: (seen.append(d), [h.HealthResult(label=label, ok=True, detail="none")])[1])
    rows = h.run_index_run_checks()
    assert walks == [1] and seen == [docs, docs, docs] and reader.asked == []
    assert [r.label for r in rows] == list(_LABELS)
