# SPDX-License-Identifier: AGPL-3.0-or-later
"""The merge pass's rule (nexus-bivz3.24; Sam's ruling "Chunks decide", ask bivz324-merge-winner).

For a stale row S and the live twin W that holds its target URI: W wins by default. S wins only if S holds
chunks and W holds none, or both hold chunks and S's ``indexed_at`` is strictly newer. Ties go to W. Stale rows
that would rewrite onto one URI (siblings) are ranked by the same key and a tie goes to the lowest tumbler.
Pure functions over the census's merge entries: no engine, no tenant.
"""
from __future__ import annotations

import itertools

import pytest

from nexus.catalog.note_uri_merge import Candidate, decide_group, plan

_Y = "knowledge__y__m__v1"
_X = "knowledge__x__m__v1"


def _s(tumbler: str, chunks: int, at: str | None, uri: str | None = None) -> Candidate:
    return Candidate(tumbler, chunks, at, False, uri or f"nx-note://{_X}/t")


def _w(tumbler: str, chunks: int, at: str | None, uri: str | None = None) -> Candidate:
    return Candidate(tumbler, chunks, at, True, uri or f"nx-note://{_Y}/t")


class TestTwinPairs:
    def test_the_twin_wins_by_default_when_neither_holds_chunks(self):
        d = decide_group([_s("1.1.1", 0, None), _w("1.1.2", 0, None)])
        assert d.winner.tumbler == "1.1.2" and [x.tumbler for x in d.losers] == ["1.1.1"]
        assert d.rule == "no row holds chunks: the twin wins by default"
        assert d.rewrite_needed is False

    def test_the_twin_wins_when_it_holds_the_only_chunks(self):
        d = decide_group([_s("1.1.1", 0, "2026-10-01T00:00Z"), _w("1.1.2", 4, "2026-09-01T00:00Z")])
        assert d.winner.tumbler == "1.1.2"
        assert d.rule == "the twin holds the only chunks"

    def test_the_stale_row_wins_when_it_holds_the_only_chunks(self):
        d = decide_group([_s("1.1.1", 7, "2026-01-01T00:00Z"), _w("1.1.2", 0, "2026-11-01T00:00Z")])
        assert d.winner.tumbler == "1.1.1" and [x.tumbler for x in d.losers] == ["1.1.2"]
        assert d.rule == "the stale row holds the only chunks"
        assert d.rewrite_needed is True, "the stale winner keeps its stale URI after the merge, so it is rewritten"

    def test_both_with_chunks_the_newer_indexed_at_wins_stale_newer(self):
        d = decide_group([_s("1.1.1", 3, "2026-11-02T00:00Z"), _w("1.1.2", 9, "2026-11-01T00:00Z")])
        assert d.winner.tumbler == "1.1.1"
        assert d.rule == "both hold chunks and the stale row is newer"

    def test_both_with_chunks_the_newer_indexed_at_wins_twin_newer(self):
        d = decide_group([_s("1.1.1", 3, "2026-10-01T00:00Z"), _w("1.1.2", 1, "2026-11-01T00:00Z")])
        assert d.winner.tumbler == "1.1.2"
        assert d.rule == "both hold chunks and the twin is newer"

    def test_both_with_chunks_equal_indexed_at_is_a_tie_and_the_twin_wins(self):
        d = decide_group([_s("1.1.1", 3, "2026-11-01T00:00:00Z"), _w("1.1.2", 3, "2026-11-01T00:00Z")])
        assert d.winner.tumbler == "1.1.2"
        assert d.rule == "both hold chunks at the same time: ties go to the twin"

    @pytest.mark.parametrize("stale_at,twin_at,winner", [
        ("2026-11-01T00:00Z", None, "1.1.1"),    # a dated row beats an undated one
        (None, "2026-11-01T00:00Z", "1.1.2"),
        (None, None, "1.1.2"),                   # neither dated: a tie, the twin
        ("not a time", "2026-11-01T00:00Z", "1.1.2"),   # unreadable counts as undated, never as newest
    ])
    def test_a_missing_or_unreadable_indexed_at_is_the_oldest(self, stale_at, twin_at, winner):
        d = decide_group([_s("1.1.1", 2, stale_at), _w("1.1.2", 2, twin_at)])
        assert d.winner.tumbler == winner

    def test_offsets_are_compared_as_instants(self):
        # 01:00+02:00 is 23:00Z the day before: the twin (00:00Z) is the newer one.
        d = decide_group([_s("1.1.1", 1, "2026-11-01T01:00+02:00"), _w("1.1.2", 1, "2026-11-01T00:00Z")])
        assert d.winner.tumbler == "1.1.2"

    def test_the_rule_never_folds_a_chunk_holder_into_a_chunkless_winner(self):
        # Every branch: the loser has chunks only if the winner has them too (nexus-bivz3.27's refusal never applies).
        for sc in (0, 1, 5):
            for wc in (0, 1, 5):
                for sat, wat in ((None, None), ("2026-01-01T00:00Z", "2026-02-01T00:00Z"),
                                 ("2026-02-01T00:00Z", "2026-01-01T00:00Z")):
                    d = decide_group([_s("1.1.1", sc, sat), _w("1.1.2", wc, wat)])
                    assert not (d.losers[0].chunk_count > 0 and d.winner.chunk_count == 0), (sc, wc, sat, wat)


class TestGroups:
    def test_two_stale_rows_with_one_twin_are_one_group_and_one_winner(self):
        d = decide_group([_s("1.1.1", 0, None), _s("1.1.3", 0, None), _w("1.1.2", 0, None)])
        assert d.winner.tumbler == "1.1.2"
        assert sorted(x.tumbler for x in d.losers) == ["1.1.1", "1.1.3"]

    def test_a_stale_row_can_beat_the_twin_and_its_stale_sibling(self):
        d = decide_group([_s("1.1.1", 0, None), _s("1.1.3", 5, "2026-11-01T00:00Z"), _w("1.1.2", 0, None)])
        assert d.winner.tumbler == "1.1.3"
        assert sorted(x.tumbler for x in d.losers) == ["1.1.1", "1.1.2"]
        assert d.rewrite_needed is True

    def test_siblings_the_one_with_the_only_chunks_wins(self):
        d = decide_group([_s("1.1.1", 0, None), _s("1.1.2", 2, "2026-11-01T00:00Z")])
        assert d.winner.tumbler == "1.1.2"
        assert d.rule == "the row holds the only chunks"
        assert d.rewrite_needed is True

    def test_siblings_with_chunks_the_newer_wins(self):
        d = decide_group([_s("1.1.1", 2, "2026-10-01T00:00Z"), _s("1.1.2", 9, "2026-11-01T00:00Z"),
                          _s("1.1.3", 1, "2026-09-01T00:00Z")])
        assert d.winner.tumbler == "1.1.2"
        assert d.rule == "the newest of the rows that hold chunks"

    def test_siblings_with_no_chunks_the_lowest_tumbler_wins(self):
        d = decide_group([_s("1.1.10", 0, None), _s("1.1.2", 0, None), _s("1.1.9", 0, None)])
        assert d.winner.tumbler == "1.1.2", "tumblers compare as numbers, not as text"
        assert d.rule == "no row holds chunks: the lowest tumbler wins"

    def test_siblings_equal_time_and_chunks_the_lowest_tumbler_wins(self):
        d = decide_group([_s("1.1.5", 1, "2026-11-01T00:00Z"), _s("1.1.4", 3, "2026-11-01T00:00:00Z")])
        assert d.winner.tumbler == "1.1.4"

    def test_the_winner_does_not_depend_on_the_order_the_census_listed_the_rows(self):
        rows = [_s("1.1.1", 0, None), _s("1.1.2", 2, "2026-10-01T00:00Z"), _s("1.1.3", 2, "2026-11-01T00:00Z"),
                _w("1.1.4", 2, "2026-11-01T00:00Z")]
        winners = {decide_group(list(p)).winner.tumbler for p in itertools.permutations(rows)}
        assert winners == {"1.1.4"}, "ties between 1.1.3 and the twin go to the twin"

    def test_a_group_needs_two_rows(self):
        with pytest.raises(ValueError):
            decide_group([_s("1.1.1", 0, None)])


def _entry(t, reason, **kw):
    base = {"tumbler": t, "collection": _Y, "source_uri": f"nx-note://{_X}/t", "new_uri": f"nx-note://{_Y}/t",
            "reason": reason, "chunk_count": 0, "indexed_at": None}
    base.update(kw)
    return base


class TestPlan:
    def test_twin_entries_group_by_their_twin_and_use_the_twins_own_fields(self):
        entries = [
            _entry("1.1.1", "twin", twin="1.1.9", twin_chunk_count=2, twin_indexed_at="2026-11-01T00:00Z",
                   twin_source_uri=f"chroma://{_Y}/t"),
            _entry("1.1.2", "twin", twin="1.1.9", twin_chunk_count=2, twin_indexed_at="2026-11-01T00:00Z",
                   twin_source_uri=f"chroma://{_Y}/t"),
            _entry("1.1.5", "twin", twin="1.1.8", twin_chunk_count=0, twin_indexed_at=None,
                   twin_source_uri=f"nx-note://{_Y}/u", chunk_count=3, indexed_at="2026-10-01T00:00Z"),
        ]
        p = plan(entries)
        assert not p.skipped
        by_twin = {next(c for c in d.members if c.is_twin).tumbler: d for d in p.decisions}
        assert sorted(x.tumbler for x in by_twin["1.1.9"].losers) == ["1.1.1", "1.1.2"]
        assert by_twin["1.1.9"].winner.uri == f"chroma://{_Y}/t", "the twin's actual spelling is carried"
        assert by_twin["1.1.8"].winner.tumbler == "1.1.5"

    def test_sibling_entries_group_by_target_and_a_lone_visible_sibling_is_left_for_the_next_round(self):
        entries = [
            _entry("1.1.1", "sibling", twin="1.1.2"),
            _entry("1.1.2", "sibling", twin="1.1.1"),
            _entry("1.1.7", "sibling", twin="1.1.8", new_uri=f"nx-note://{_Y}/other"),
        ]
        p = plan(entries)
        assert [sorted(c.tumbler for c in d.members) for d in p.decisions] == [["1.1.1", "1.1.2"]]
        assert [s["tumbler"] for s in p.skipped] == ["1.1.7"]
        assert "not in the report" in p.skipped[0]["why"] or "alone" in p.skipped[0]["why"]

    @pytest.mark.parametrize("broken", [
        {"chunk_count": "3"}, {"chunk_count": None}, {"twin_chunk_count": None}, {"twin": None},
    ])
    def test_an_entry_without_usable_fields_is_skipped_with_its_reason_never_guessed_at(self, broken):
        e = _entry("1.1.1", "twin", twin="1.1.9", twin_chunk_count=1, twin_indexed_at=None,
                   twin_source_uri=f"nx-note://{_Y}/t")
        e.update(broken)
        p = plan([e])
        assert p.decisions == []
        assert [s["tumbler"] for s in p.skipped] == ["1.1.1"]
        assert p.skipped[0]["why"]
