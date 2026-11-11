# SPDX-License-Identifier: AGPL-3.0-or-later
"""F30 (nexus-f3yqd.6): the doctor's three index-run rows read the engine's census routes (engine 0.1.190).

The rows run in the default sweep and under ``--check-index-runs`` from two bounded requests and no catalog walk
(``catalog_index_run_census_supported``, ``catalog_next_seq_drift_read_supported``). Every way the engine cannot say (a
404 or 503, a bucket that timed out, a malformed body, a failed request, an unlisted flag, no catalog reader) is a
not-checked row: ok, skipped, no warning, never "none"; a missing flag says only that /version does not advertise the routes, and a failed /version probe says so. A
capped census with a zero is "not fully checked", never "none".
"""
from __future__ import annotations

import ast
from collections import Counter

import copy
from pathlib import Path
from typing import Any

import pytest

import nexus.health as h
from nexus.catalog.http_catalog_client import INDEX_RUN_CENSUS_MAX_LIMIT, INDEX_RUN_CENSUS_RULES_DIFFER

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


# ── an engine that judges by another threshold or floor ─────────────────────


def test_a_census_the_engine_computed_by_other_rules_says_so_on_the_census_rows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows(monkeypatch, _Reader(INDEX_RUN_CENSUS_RULES_DIFFER, _DRIFT_NONE))
    assert list(rows) == list(_LABELS)
    for label in (h._STALE_FENCES_LABEL, h._HIDDEN_CHUNKS_LABEL):
        (r,) = rows[label]
        assert _is_not_checked(r) and "different threshold or floor than this client" in r.detail, r.detail
        assert "different releases" in r.detail and "did not answer" not in r.detail and "asks again" not in r.detail
    (drift,) = rows[h._NEXT_SEQ_LABEL]
    assert drift.ok and not drift.skipped and drift.detail == "none (4 owner(s) checked)"


# ── no routes: three not-checked rows that say what they need ───────────────


_ADVERTISE = "does not advertise the census routes (engine 0.1.190 or later; on the managed service, also the edge's /version allowlist)"


class _ProbeReader:
    """A reader whose /version answered without the flags, or whose /version probe itself failed."""

    def __init__(self, *, probe_failed: bool) -> None:
        self._failed = probe_failed

    def index_run_routes_supported(self) -> bool:
        return False

    def version_probe_failed(self) -> bool:
        return self._failed


@pytest.mark.parametrize("reader", [
    _Reader(supported=False), type("R", (), {"index_run_routes_supported": lambda self: "yes"})(),
    _ProbeReader(probe_failed=False),
], ids=["flag-false", "flag-not-true", "probe-answered-without-flags"])
def test_an_engine_that_does_not_advertise_the_routes_is_three_not_checked_rows_that_say_only_that(monkeypatch: pytest.MonkeyPatch, reader: Any) -> None:
    rows = _rows(monkeypatch, reader)
    assert list(rows) == list(_LABELS)
    for label in _LABELS:
        (r,) = rows[label]
        assert _is_not_checked(r) and _ADVERTISE in r.detail and "asks again" not in r.detail, r.detail
    assert "needs engine" not in " ".join(r.detail for v in rows.values() for r in v)
    if isinstance(reader, _Reader):
        assert reader.asked == [] and reader.walks == 0


def test_a_failed_version_probe_is_not_checked_and_asks_again(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows(monkeypatch, _ProbeReader(probe_failed=True))
    assert list(rows) == list(_LABELS)
    for label in _LABELS:
        (r,) = rows[label]
        assert _is_not_checked(r) and "/version could not be read" in r.detail and "the next `nx doctor` asks again" in r.detail, r.detail
        assert "advertise" not in r.detail


def test_a_reader_that_does_not_read_the_routes_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows(monkeypatch, object())
    for label in _LABELS:
        (r,) = rows[label]
        assert _is_not_checked(r) and "this catalog reader does not read the census routes" in r.detail, r.detail
        assert "engine 0.1.190" not in r.detail


@pytest.mark.parametrize("reader", [
    None, type("R", (), {"index_run_routes_supported": lambda self: (_ for _ in ()).throw(RuntimeError("no /version"))})(),
], ids=["no-reader", "probe-raises"])
def test_a_missing_or_unreadable_catalog_is_three_not_checked_rows(monkeypatch: pytest.MonkeyPatch, reader: Any) -> None:
    rows = _rows(monkeypatch, reader)
    assert list(rows) == list(_LABELS) and all(_is_not_checked(r) for v in rows.values() for r in v)


def test_a_reader_that_cannot_be_built_is_three_not_checked_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("no service")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", boom)
    rows = h._check_index_run_routes()
    assert [r.label for r in rows] == list(_LABELS) and all(_is_not_checked(r) for r in rows)


# ── the sweep and --check-index-runs ─────────────────────────────────────────


class _NoCollections:
    def list_collections(self, *a: Any, **k: Any) -> list:
        return []


def _sweep(monkeypatch: pytest.MonkeyPatch, reader: Any) -> dict[str, list[h.HealthResult]]:
    for name in dir(h):
        if name.startswith("_check_") and name != "_check_index_run_routes" and callable(getattr(h, name)):
            monkeypatch.setattr(h, name, lambda *a, **k: [])
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _NoCollections())
    _wire(monkeypatch, reader)
    out: dict[str, list[h.HealthResult]] = {}
    for r in h.run_health_checks()[0]:
        out.setdefault(r.label, []).append(r)
    return out


def test_the_default_sweep_reads_the_three_rows_from_the_routes_without_a_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(_STALE_TWO), _DRIFT_NONE)
    rows = _sweep(monkeypatch, reader)
    assert reader.walks == 0 and len(reader.asked) == 2
    assert any("stranded in index_state='indexing'" in r.detail for r in rows[h._STALE_FENCES_LABEL])
    assert not any(r.skipped for label in _LABELS for r in rows[label])


def test_the_default_sweep_on_an_engine_without_the_flags_says_what_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(), _DRIFT_NONE, supported=False)
    rows = _sweep(monkeypatch, reader)
    for label in _LABELS:
        (r,) = rows[label]
        assert _is_not_checked(r) and _ADVERTISE in r.detail and not r.warn
    assert reader.walks == 0 and reader.asked == []


def test_the_default_sweep_with_unanswered_routes_is_not_checked_and_no_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(None, None)
    rows = _sweep(monkeypatch, reader)
    for label in _LABELS:
        (r,) = rows[label]
        assert _is_not_checked(r) and "advertise" not in r.detail
    assert reader.walks == 0


def test_check_index_runs_is_the_same_three_rows_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(_STALE_TWO), _DRIFT_NONE)
    _wire(monkeypatch, reader)
    rows = h.run_index_run_checks()
    assert [r.label for r in rows][0] == h._STALE_FENCES_LABEL and {r.label for r in rows} == set(_LABELS)
    assert reader.walks == 0 and len(reader.asked) == 2


def test_check_index_runs_on_an_engine_without_the_flags_does_not_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(_census(), _DRIFT_NONE, supported=False)
    _wire(monkeypatch, reader)
    rows = h.run_index_run_checks()
    assert [r.label for r in rows] == list(_LABELS)
    assert all(_is_not_checked(r) and _ADVERTISE in r.detail for r in rows)
    assert reader.walks == 0 and reader.asked == []


def test_the_walk_is_retired() -> None:
    for name in ("_walk_documents_for_doctor", "_WALK_FAILED", "_WalkFailed", "_doc_list", "_index_run_rows_not_checked",
                 "_check_stale_indexing_runs", "_check_failed_runs_hidden_chunks", "_check_next_seq_drift"):
        assert not hasattr(h, name), name


def test_health_defines_no_top_level_function_twice() -> None:
    """A second ``_instant`` once shadowed the engine-status one and crashed the per-collection-index row on a null."""
    tree = ast.parse(Path(h.__file__).read_text(encoding="utf-8"))
    names = Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    assert [n for n, c in names.items() if c > 1] == []
