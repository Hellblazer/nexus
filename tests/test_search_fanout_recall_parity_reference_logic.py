# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fast, non-``integration`` unit coverage for the scoring and floor logic of
``tests/test_search_fanout_recall_parity.py``.

The live-cloud recall-parity gate itself (``-m integration``, skipped by
default) is the only place that runs ``_reference_score`` on real results, so
a change to the scoring would otherwise have no coverage in the default
suite. This module pins the function with plain values: no network.
"""
from __future__ import annotations

import pytest

from nexus.db.limits import QUOTAS
from tests.test_search_fanout_recall_parity import (
    _JACCARD_FLOOR,
    _JACCARD_FLOOR_SPLIT,
    _floor_for,
    _reference_score,
)

_TEN = {f"id{i}" for i in range(10)}


def _swap(ids: set[str], out: str, new: str) -> set[str]:
    return (ids - {out}) | {new}


def test_a_page_identical_to_every_old_run_scores_one():
    assert _reference_score([_TEN] * 5, _TEN) == (1.0, 1.0, 1.0)


def test_the_old_paths_own_tail_swap_does_not_fail_the_new_path():
    """The measured jitter (nexus-ys8a4 item 8): one old run swaps its tenth
    id. Against a single old run that is Jaccard 0.818 and a failure. With the
    swapped id inside the reference, a new page equal to either old page
    passes."""
    jittered = _swap(_TEN, "id9", "tail")
    runs = [_TEN, _TEN, jittered, _TEN, _TEN]
    assert _reference_score(runs, _TEN)[0] == 1.0
    assert _reference_score(runs, jittered)[0] == 1.0


def test_one_id_the_old_path_never_returned_is_at_the_floor_and_two_are_below():
    one = _swap(_TEN, "id9", "new1")
    score, in_union, core_kept = _reference_score([_TEN] * 5, one)
    assert (in_union, core_kept) == (0.9, 0.9)
    assert score >= _JACCARD_FLOOR
    two = _swap(one, "id8", "new2")
    assert _reference_score([_TEN] * 5, two)[0] == pytest.approx(0.8)
    assert _reference_score([_TEN] * 5, two)[0] < _JACCARD_FLOOR


def test_losing_ids_every_old_run_returned_fails_even_when_every_new_id_was_seen():
    """in_union alone would pass this: every new id appeared in some old run.
    The two ids the old path returned every time are gone."""
    wide = [_TEN | {"x"}, _TEN | {"y"}, _TEN, _TEN, _TEN]
    new = (_TEN - {"id0", "id1"}) | {"x", "y"}
    score, in_union, core_kept = _reference_score(wide, new)
    assert in_union == 1.0
    assert core_kept == pytest.approx(0.8)
    assert score < _JACCARD_FLOOR


def test_an_empty_new_page_against_a_non_empty_reference_scores_zero():
    assert _reference_score([_TEN] * 5, set())[0] == 0.0


def test_both_empty_scores_one_and_no_runs_is_an_error():
    assert _reference_score([set()] * 5, set())[0] == 1.0
    with pytest.raises(ValueError, match="at least one run"):
        _reference_score([], _TEN)


def test_floor_for_unsplit_group_is_strict(monkeypatch, cloud_mode):
    """A group whose desired candidate count fits under the engine cap is
    held to the strict floor (nexus-atylb). The three floor tests name
    voyage collections because a multi-collection embedding-model group is
    the shape under test, so they opt into ``cloud_mode`` for the
    mode-declaration lint."""
    monkeypatch.setattr(
        "nexus.search_engine._desired_candidate_count", lambda cols, n: 90,
    )
    cols = [f"rdr__o-{i}__voyage-context-3__v1" for i in range(3)]
    assert _floor_for(cols) == _JACCARD_FLOOR


def test_floor_for_split_group_uses_split_floor(monkeypatch, cloud_mode):
    """A group that would exceed QUOTAS.MAX_QUERY_RESULTS splits into
    sub-batches and is held to the evidence-based split floor (nexus-atylb,
    Sam's ruling 2026-09-07)."""
    monkeypatch.setattr(
        "nexus.search_engine._desired_candidate_count",
        lambda cols, n: QUOTAS.MAX_QUERY_RESULTS + 60,
    )
    cols = [f"rdr__o-{i}__voyage-context-3__v1" for i in range(9)]
    assert _floor_for(cols) == _JACCARD_FLOOR_SPLIT


def test_floor_for_singleton_never_splits(monkeypatch, cloud_mode):
    """A one-collection group is never split, whatever its desired count."""
    monkeypatch.setattr(
        "nexus.search_engine._desired_candidate_count", lambda cols, n: 10_000,
    )
    assert _floor_for(["rdr__o-1__voyage-context-3__v1"]) == _JACCARD_FLOOR
