# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline proof that the live recall-parity gate's harness meets the required
``sensitivity`` contract (nexus-tu8wp.3; contract: ab7ac46e6, nexus-rfvyy.7.8).

``tests/test_search_fanout_recall_parity.py`` is ``integration`` and runs only
in an announced cloud window, so it went 11 of 23 red on a missing keyword
(``search_cross_corpus() missing 1 required keyword-only argument:
'sensitivity'``) and nothing in the default loop noticed. These tests drive the
harness's own call paths, ``_run_floor`` and the frozen pre-fix module behind
``_load_old_search_cross_corpus``, against a client whose reads REQUIRE
``sensitivity`` (no default), exactly as ``HttpVectorClient.search`` does. No
network.
"""
from __future__ import annotations

import subprocess

import pytest

from nexus.db.http_vector_client import HttpVectorClient
from nexus.search_engine import search_cross_corpus as new_search_cross_corpus
from tests.test_search_fanout_recall_parity import (
    _PRE_FIX_SHA,
    _READ_SENSITIVITY,
    _REPO_ROOT,
    _load_old_search_cross_corpus,
    _run_floor,
    _SensitivityClient,
    _with_read_sensitivity,
)

_COLS = ["knowledge__alpha", "knowledge__beta", "code__gamma"]


class _StrictClient:
    """A client whose ``search`` has no default for ``sensitivity``."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def search(
        self, query, collection_names, n_results=10, where=None, *,
        sensitivity, rerank=False, rerank_meta_out=None,
    ):
        self.seen.append(sensitivity)
        col = collection_names[0]
        return [
            {"id": f"{col}-{i}", "content": f"text {i}", "distance": 0.1 + 0.01 * i,
             "chunk_text_hash": f"{i:064x}"}
            for i in range(3)
        ]


def test_the_harness_reads_with_the_sensitivity_the_measurement_script_uses():
    assert _READ_SENSITIVITY == "include"


@pytest.mark.parametrize("floor", [None, "old"])
def test_run_floor_passes_sensitivity_through_search_cross_corpus(floor):
    client = _StrictClient()
    results, calls = _run_floor(
        new_search_cross_corpus, "q", _COLS, 10, client,
        floor=floor, threshold=float("inf"), rerank=False,
    )
    assert results, "the strict client returned rows, so an empty page means the search never ran"
    assert calls > 0
    assert client.seen
    assert set(client.seen) == {_READ_SENSITIVITY}


def test_wrapper_supplies_sensitivity_and_leaves_the_old_signature_alone():
    seen: dict = {}

    def frozen_old(query, collections, n_results, t3, *, threshold_override=None):
        seen["rows"] = t3.search(query, collections[:1], n_results=n_results)
        seen["threshold_override"] = threshold_override
        return seen["rows"]

    client = _StrictClient()
    out = _with_read_sensitivity(frozen_old)("q", _COLS, 5, client, threshold_override=1.5)
    assert out
    assert client.seen == [_READ_SENSITIVITY]
    assert seen["threshold_override"] == 1.5


def test_wrapper_without_the_fix_would_fail_the_strict_client():
    """Control: the bare frozen function against the strict client is the
    failure the live run hit, so the tests above are not vacuous."""

    def frozen_old(query, collections, n_results, t3):
        return t3.search(query, collections[:1], n_results=n_results)

    with pytest.raises(TypeError, match="sensitivity"):
        frozen_old("q", _COLS, 5, _StrictClient())


def test_proxy_resolves_search_at_call_time_so_a_call_counter_still_counts():
    client = _StrictClient()
    proxy = _SensitivityClient(client)
    counted: list[int] = []
    real = client.search

    def counting(*a, **kw):
        counted.append(1)
        return real(*a, **kw)

    client.search = counting  # what tests._route_parity.CallLog does
    proxy.search("q", ["knowledge__alpha"], n_results=3)
    assert counted == [1]


def test_proxy_keeps_the_wrapped_clients_class_for_the_old_modules_isinstance_check():
    """The frozen module's ``_voyage_thresholds_active`` is
    ``isinstance(t3, HttpVectorClient)``; a proxy that failed it would drop
    the default per-collection thresholds on the reference path only."""
    real = HttpVectorClient.__new__(HttpVectorClient)
    assert isinstance(_SensitivityClient(real), HttpVectorClient)
    assert not isinstance(_SensitivityClient(_StrictClient()), HttpVectorClient)


def test_the_frozen_pre_fix_module_runs_against_the_strict_client():
    """The real loader: the module that ``git show`` yields calls the CURRENT
    client contract. Needs the pre-fix commit in the clone's history."""
    have_sha = subprocess.run(
        ["git", "cat-file", "-e", f"{_PRE_FIX_SHA}:src/nexus/search_engine.py"],
        cwd=_REPO_ROOT, capture_output=True, check=False,
    ).returncode == 0
    if not have_sha:
        pytest.skip(f"{_PRE_FIX_SHA} is not in this clone's history (shallow checkout)")
    old = _load_old_search_cross_corpus()
    client = _StrictClient()
    results = old(
        "q", _COLS, 10, client, cluster_by=None, threshold_override=float("inf"),
    )
    assert results
    assert len(client.seen) == len(_COLS), "the old path is one search per collection"
    assert set(client.seen) == {_READ_SENSITIVITY}
