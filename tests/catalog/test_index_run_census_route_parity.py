# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.6: the doctor's route-fed rows say what the Python oracle's rows say, over the engine's shared cases.

``test_index_run_census_cases.py`` and ``test_next_seq_drift_cases.py`` hold the Python oracle (``_stale_fence_rows_over``,
``_next_seq_drift_rows_over``, and ``heal_manifest_gaps(dry_run=True).reconciled`` behind ``_failed_hidden_rows_over``) to
the tables the engine's Java tests hold its SQL to, so the SQL and the oracle agree. This module holds the third leg: for
each case the engine's answer is rebuilt from the table's expected numbers (what the SQL is proven to return), fed through
the rows a default ``nx doctor`` runs against engine 0.1.190, and the rows must equal the oracle's, text and marks. A
change to the rendering of either path that the other does not follow, or to the table that the route rows do not follow,
fails here.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from nexus import health
from nexus.catalog.manifest_heal import heal_manifest_gaps
from tests.catalog.test_index_run_census_cases import (  # noqa: F401 — clock is the fixture the oracle rows need
    _NOW,
    _Catalog,
    _scenarios,
    _T3,
    _walk,
    _wire,
    clock,
)
from tests.catalog.test_next_seq_drift_cases import _cases, _owners

_NEUTRAL_STALE = {"count": 0, "capped": False, "notes": 0, "examples": []}
_NEUTRAL_FENCE = {
    "checked": 1, "reported_null": 0, "undated_reported_null": 0, "floor_candidates": 0,
    "anchor": {"kind": "earliest", "at": "2026-08-12T00:00:00Z", "earliest_stamped_at": "2026-08-12T00:00:00Z", "proven_at": None},
    "post_anchor": {"count": 0, "capped": False, "examples": []},
}
_NEUTRAL_FAILED = {"failed": {"count": 0, "capped": False}, "count": 0, "capped": False, "examples": []}


def _name(d: dict) -> str:
    return d["source_uri"] or d["tumbler"]


def _by_name(scenario: dict) -> dict[str, dict]:
    return {_name(d): d for d in scenario["documents"] if not d["deleted"]}


def _stale_bucket(scenario: dict) -> dict:
    want, docs = scenario["expect"]["stale"], _by_name(scenario)
    examples = []
    for name in want["names"]:
        d = docs[name]
        started = datetime.fromisoformat(d["index_started_at"].replace("Z", "+00:00"))
        examples.append({"tumbler": d["tumbler"], "source_uri": d["source_uri"] or None, "started_at": d["index_started_at"],
                         "age_s": int((_NOW - started).total_seconds()), "note": d["content_type"] == "knowledge"})
    return {"count": want["count"], "capped": False, "notes": want["notes"], "examples": examples}


def _fence_bucket(scenario: dict) -> dict:
    want, docs = scenario["expect"]["fence"], _by_name(scenario)
    examples = [{"tumbler": docs[n]["tumbler"], "source_uri": docs[n]["source_uri"] or None,
                 "file_path": docs[n]["file_path"] or None, "indexed_at": docs[n]["indexed_at"]} for n in want["post_anchor"]]
    return {
        "checked": want["checked"], "reported_null": want["reported_null"], "undated_reported_null": want["undated_reported_null"],
        "floor_candidates": want["floor_candidates"],
        "anchor": {"kind": want["anchor_kind"], "at": want["anchor_at"], "earliest_stamped_at": want["earliest_stamped_at"],
                   "proven_at": want["proven_at"]},
        "post_anchor": {"count": len(examples), "capped": False, "examples": examples},
    }


def _failed_bucket(scenario: dict) -> dict:
    want, docs = scenario["expect"]["failed_hidden"], {d["tumbler"]: d for d in scenario["documents"]}
    examples = [{"tumbler": t, "source_uri": docs[t]["source_uri"] or None, "chunk_count": docs[t]["chunk_count"], "chunks_stored": 1}
                for t in want["hidden"]]
    return {"failed": {"count": want["failed"], "capped": False}, "count": want["count"], "capped": False, "examples": examples}


def _same(oracle_rows: list[health.HealthResult], route_rows: list[health.HealthResult]) -> None:
    assert [(r.ok, r.warn, r.skipped, r.fatal, r.detail, r.fix_suggestions) for r in route_rows] == \
           [(r.ok, r.warn, r.skipped, r.fatal, r.detail, r.fix_suggestions) for r in oracle_rows]


def _cat(scenario: dict, monkeypatch: pytest.MonkeyPatch) -> Any:
    _wire(monkeypatch, scenario)  # the fake T3 the heal reads
    return _Catalog(scenario)


@pytest.mark.parametrize("scenario", _scenarios("stale"), ids=lambda s: s["id"])
def test_the_stale_row_from_the_route_is_the_oracle_row(scenario: dict, monkeypatch: pytest.MonkeyPatch, clock: None) -> None:
    cat = _cat(scenario, monkeypatch)
    census = {"stale": _stale_bucket(scenario), "fence": _NEUTRAL_FENCE, "failed_hidden": _NEUTRAL_FAILED}
    route = health._stale_fence_rows_from_census(cat, health._STALE_FENCES_LABEL, census)
    oracle = health._stale_fence_rows_over(_walk(scenario), cat)
    _same([r for r in oracle if "stranded in index_state='indexing'" in r.detail],
          [r for r in route if "stranded in index_state='indexing'" in r.detail])
    assert any("stranded" in r.detail for r in route) == (scenario["expect"]["stale"]["count"] > 0)


@pytest.mark.parametrize("scenario", _scenarios("fence"), ids=lambda s: s["id"])
def test_the_fence_row_from_the_route_is_the_oracle_row(scenario: dict, monkeypatch: pytest.MonkeyPatch, clock: None) -> None:
    cat = _cat(scenario, monkeypatch)
    census = {"stale": _NEUTRAL_STALE, "fence": _fence_bucket(scenario), "failed_hidden": _NEUTRAL_FAILED}
    route = health._stale_fence_rows_from_census(cat, health._STALE_FENCES_LABEL, census)
    oracle = health._stale_fence_rows_over(_walk(scenario), cat)
    _same([r for r in oracle if "stranded in index_state='indexing'" not in r.detail], route)


@pytest.mark.parametrize("scenario", _scenarios("failed_hidden"), ids=lambda s: s["id"])
def test_the_failed_runs_row_from_the_route_is_the_oracle_row(scenario: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _cat(scenario, monkeypatch)
    route = health._failed_hidden_rows_from_census(health._HIDDEN_CHUNKS_LABEL, _failed_bucket(scenario))
    oracle = health._failed_hidden_rows_over(_walk(scenario), cat)
    _same(oracle, route)


@pytest.mark.parametrize("scenario", _scenarios("failed_hidden"), ids=lambda s: s["id"])
def test_the_failed_count_the_route_answers_is_what_the_heal_dry_run_reconciles(scenario: dict) -> None:
    """The oracle, called directly: ``heal_manifest_gaps(dry_run=True).reconciled`` over the failed documents, the
    number the engine's ``failed_hidden.count`` equals."""
    failed = [e for e in _walk(scenario) if e.index_state == "failed"]
    reconciled = heal_manifest_gaps(failed, _Catalog(scenario), lambda *a, **k: _T3(scenario), None, dry_run=True).reconciled
    assert reconciled == scenario["expect"]["failed_hidden"]["count"] == _failed_bucket(scenario)["count"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_the_drift_row_from_the_route_is_the_oracle_row(case: dict) -> None:
    oracle = health._next_seq_drift_rows_over(
        _owners(case), [SimpleNamespace(tumbler=t) for t in case["live"]], case["tombstoned"])
    drifted = sorted(case["expect"]["drifted"], key=lambda d: d["owner"])
    body = {"checked": len(case["owners"]), "drifted": len(drifted), "capped": False, "examples": drifted}
    route = health._next_seq_drift_rows_from_route(health._NEXT_SEQ_LABEL, body)
    # The oracle names owners in the catalog's order, the engine's route in prefix order: compare the order-free text.
    if len(drifted) > 1:
        assert sorted(route[0].detail.split("highest existing tumbler: ")[1].split(". The engine floors")[0].split("; ")) == \
               sorted(oracle[0].detail.split("highest existing tumbler: ")[1].split(". The engine floors")[0].split("; "))
        assert (route[0].ok, route[0].warn) == (oracle[0].ok, oracle[0].warn)
    else:
        _same(oracle, route)


def test_the_route_rows_are_held_to_every_branch_of_the_fence_row(monkeypatch: pytest.MonkeyPatch, clock: None) -> None:
    """Non-vacuity: the table reaches the post-baseline, no-baseline, clean and quiet outcomes, so the parametrized
    comparison above is not passing over a row that only ever says one thing."""
    seen: set[str] = set()
    for scenario in _scenarios("fence"):
        cat = _cat(scenario, monkeypatch)
        census = {"stale": _NEUTRAL_STALE, "fence": _fence_bucket(scenario), "failed_hidden": _NEUTRAL_FAILED}
        (row, *_rest) = health._stale_fence_rows_from_census(cat, health._STALE_FENCES_LABEL, census)
        d = row.detail
        seen.add("post_anchor" if "carry no stamp, despite" in d else "no_baseline" if d.startswith("cannot attribute")
                 else "clean" if d.startswith("none (") else "quiet" if d.startswith("fence live") else d)
    assert seen == {"post_anchor", "no_baseline", "clean", "quiet"}


def test_an_empty_catalog_reads_the_same_on_both_paths() -> None:
    """The one place the nothing-to-assess text lives is ``_stale_fence_rows``: the oracle over no documents and the
    route over an empty census say the same, and a pre-fence engine (documents that never carried the key) still says so."""
    empty_fence = {"checked": 0, "reported_null": 0, "undated_reported_null": 0, "floor_candidates": 0,
                   "anchor": {"kind": None, "at": None, "earliest_stamped_at": None, "proven_at": None},
                   "post_anchor": {"count": 0, "capped": False, "examples": []}}
    route = health._stale_fence_rows_from_census(
        None, health._STALE_FENCES_LABEL, {"stale": _NEUTRAL_STALE, "fence": empty_fence, "failed_hidden": _NEUTRAL_FAILED})
    _same(health._stale_fence_rows_over([]), route)
    assert route[0].detail == "none (no catalog document)"
    pre_fence = SimpleNamespace(index_state=None, index_state_reported=False, tumbler="1.1.1")
    (row,) = health._stale_fence_rows_over([pre_fence])
    assert row.detail.startswith("skipped (engine does not report index_state")
