# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ldf2s: ``nx catalog backfill-meta`` reads each collection once per round, paces itself, reports progress
and stops cleanly.

On 2026-10-31 the verb sent one ``/v1/vectors/store-get`` per note and per indexed document (about 35,700 for a
42,000-document catalog) and saturated the cloud edge. These pins hold the grouped reader to the old reader's
plans and counts (``tests/_legacy_meta_backfill.py`` is the old one, verbatim), bound its requests by the
collections a round needs, and cover ``--batch-size``, ``--pause-ms``, the progress line and the Ctrl-C stop.
"""
from __future__ import annotations

import contextlib
import json
import random
import shlex
import signal
import threading
from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from nexus.catalog import meta_backfill as mb
from nexus.cli import main
from nexus.db import request_gate
from nexus.db.http_vector_client import HttpVectorClient
from tests._legacy_meta_backfill import legacy_backfill_meta
from tests.test_6pfgb10_meta_backfill import MINE, PUT, _entry, _invoke, _Manifest, _row, _World, _world_with_one_of_each
from tests._module_seam import setattr_in

A, B, C, K = "docs__x__m__v1", "docs__y__m__v1", "code__x__m__v1", "knowledge__x__minilm__v1"


@dataclass
class _Counting(_World):
    """A ``_World`` that records every read and write in order, so a test can see the requests."""

    events: list = field(default_factory=list)

    def get_manifests(self, ids):
        self.events.append(("manifests", len(ids)))
        return super().get_manifests(ids)

    def chash_positions(self, chashes):
        self.events.append(("owners", len(chashes)))
        return super().chash_positions(chashes)

    def chunk_rows(self, collection, chashes):
        self.events.append(("rows", collection, tuple(chashes)))
        return super().chunk_rows(collection, chashes)

    def update_many(self, payloads):
        self.events.append(("write", len(payloads)))
        return super().update_many(payloads)

    def rows_calls(self) -> list[tuple[str, tuple[str, ...]]]:
        return [(e[1], e[2]) for e in self.events if e[0] == "rows"]


def _catalog(n_docs: int = 110, n_notes: int = 40, *, world: type[_World] = _Counting, seed: int = 7) -> _World:
    """Notes of four kinds and indexed documents in three collections, shuffled: written-row notes, notes
    recovered from a sole-owner row, notes whose row is shared, notes that need nothing; documents with a
    sole-owner row, documents sharing a chunk, documents with a chunk in another collection, documents that
    hold their keys already; notes with a piece in a documents collection, one of them holding the note's only row."""
    w = world()
    entries: list = []
    for j in range(n_notes):
        tumbler = f"1.{j}"
        title = f"note{j}"
        meta = {"put_at": PUT, "ttl_days": 0} if j % 4 == 3 else {}
        entries.append(_entry(meta, title=title, tumbler=tumbler, collection=K))
        pieces = [_Manifest(f"n{j}_0"), _Manifest(f"n{j}_1")]
        if j % 4 == 1:    # a piece in a documents collection; for j % 8 == 5 it holds the only row
            pieces.append(_Manifest(f"n{j}_2", collection=A))
            w.rows[(A, f"n{j}_2")] = _row(tags="elsewhere", category="finding")
            w.owners[f"n{j}_2"] = [tumbler]
        w.manifests[tumbler] = pieces
        for p in pieces[:2]:
            w.owners[p.chash] = [tumbler]
        if j % 4 == 0:
            w.rows[(K, f"n{j}_0")] = _row(catalog_doc_id=tumbler, title=title, tags="a, b", category="c", ttl_days=7)
        elif j % 4 == 1 and j % 8 == 1:
            w.rows[(K, f"n{j}_0")] = _row(tags="legacy", category="finding")
        elif j % 4 == 2:
            w.rows[(K, f"n{j}_0")] = _row(catalog_doc_id="1.999", title="someone else", tags="theirs")
            w.owners[f"n{j}_0"] = [tumbler, f"1.9{j}"]
    for i in range(n_docs):
        tumbler = f"2.{i}"
        coll = (A, B, C)[i % 3]
        ctype, path = ("code", f"f{i}.py") if coll == C else ("paper", f"p{i}.pdf")
        meta = (
            {"extraction_method": "x", "quality_gate_overridden": False, "extraction_source": "file", "frecency_score": 1.0,
             "source_format": "pdf", "tags": ["pdf"], "category": "paper"}
            if i % 10 == 9 else {}
        )
        entries.append(_entry(meta, title=f"doc{i}", tumbler=tumbler, content_type=ctype, file_path=path,
                              collection=coll, author="Ann" if i % 10 == 9 else ""))
        pieces = [_Manifest(f"d{i}_0"), _Manifest(f"d{i}_1"), _Manifest(f"d{i}_2")]
        if i % 11 == 5:
            pieces[2] = _Manifest(f"d{i}_2", collection=B if coll != B else A)
        w.manifests[tumbler] = pieces
        for p in pieces:
            w.owners[p.chash] = [tumbler]
            where = p.collection or coll
            w.rows[(where, p.chash)] = _row(extraction_method="mineru", source_author="Ann", frecency_score=0.5)
        if i % 7 == 6:
            w.owners[f"d{i}_0"] = [tumbler, f"2.{i - 1}"]
    random.Random(seed).shuffle(entries)
    w.entries = entries
    return w


def _write_view(w: _World):
    return [(u["tumbler"], u["meta"], u.get("author")) for u in w.updates]


# ── the grouped read is the old read, with fewer requests ──────────────────────────────────────────

@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("batch", [50, 7, 1, 1000])
@pytest.mark.parametrize("break_", [None, "owners", "rows"])
def test_the_grouped_reader_plans_the_writes_and_counts_of_the_per_document_reader(apply, batch, break_):
    old, new = _catalog(), _catalog()
    for w in (old, new):
        w.fail_owners = break_ == "owners"
        w.fail_rows_for = {B} if break_ == "rows" else set()
    old_report = legacy_backfill_meta(old, old, old if apply else None, apply=apply, batch=batch)
    new_report = mb.backfill_meta(new, new, new if apply else None, apply=apply, batch=batch)
    assert new_report.as_dict() == old_report.as_dict()
    assert _write_view(new) == _write_view(old)
    assert old_report.documents_examined == 150 and old_report.documents_to_stamp > 100, "control: the fixture plans real work"
    if break_ == "rows":
        assert old_report.lookups_failed > 0, "control: the broken collection withheld documents in both"
    if apply:
        assert old.updates, "control: there are writes to compare"


def test_a_round_sends_one_chunk_rows_call_per_collection_and_the_per_document_reader_sent_one_per_document():
    old, new = _catalog(), _catalog()
    legacy_backfill_meta(old, old, None, apply=False, batch=50)
    mb.backfill_meta(new, new, None, apply=False, batch=50)

    old_calls, new_calls = old.rows_calls(), new.rows_calls()
    rounds: list[list[str]] = []
    for e in new.events:
        if e[0] == "manifests":
            rounds.append([])
        elif e[0] == "rows":
            rounds[-1].append(e[1])
    assert len(rounds) == 3, "150 entries at 50 a round"
    for colls in rounds:
        assert len(colls) == len(set(colls)), f"a collection was read twice in one round: {colls}"
        assert set(colls) <= {A, B, C, K}
    for _coll, chashes in new_calls:
        assert len(chashes) == len(set(chashes)), "a chash is asked for once"
    assert len(new_calls) <= 3 * 4, "at most the four collections of the catalog, per round"
    assert len(old_calls) > 100, "control: the per-document reader sent about one call per document"
    assert len(old_calls) >= 8 * len(new_calls)
    # everything the old reader asked for, the new one asked for as well (rows are the same, only fewer requests)
    wanted = {(c, h) for c, hs in old_calls for h in hs}
    assert {(c, h) for c, hs in new_calls for h in hs} == wanted


@pytest.fixture
def wire(monkeypatch):
    """The store-get endpoint: every POST recorded, rows keyed by (collection, chash)."""
    posts: list[dict] = []
    rows: dict = {}

    def _post(path, body, *, tenant=None, **_kw):
        assert path == "/v1/vectors/store-get"
        posts.append(body)
        ids = [i for i in body["ids"] if (body["collection"], i) in rows]
        return {
            "ids": ids, "documents": ["text"] * len(ids), "count": len(ids),
            "metadatas": [rows[(body["collection"], i)] for i in ids],
        }

    monkeypatch.setattr("nexus.db.http_vector_client._post", _post)
    monkeypatch.setattr(HttpVectorClient, "_tenant", "default", raising=False)
    return posts, rows


def test_the_grouped_reader_sends_fewer_store_get_requests_than_the_old_one_through_the_real_client(wire):
    """Old against new on the same catalog, through the real ``HttpVectorClient``: 300 ids a page."""
    posts, rows = wire
    new = _catalog()
    big = _entry({}, title="big", tumbler="1.700", collection=K)
    new.entries.append(big)
    new.manifests["1.700"] = [_Manifest(f"b{i}") for i in range(650)]    # 3 pages of 300, 300, 50
    for i in range(650):
        new.owners[f"b{i}"] = ["1.700"]
    rows.update({k: v for k, v in new.rows.items()})
    for i in range(650):
        rows[(K, f"b{i}")] = _row(catalog_doc_id="1.700", title="big", tags="t")
    old = _catalog()
    old.entries.append(big)
    old.manifests["1.700"] = new.manifests["1.700"]
    old.owners.update(new.owners)

    client = HttpVectorClient.__new__(HttpVectorClient)
    new_report = mb.backfill_meta(new, client, None, apply=False, batch=50)
    new_posts = list(posts)
    posts.clear()
    old_report = legacy_backfill_meta(old, client, None, apply=False, batch=50)

    assert new_report.as_dict() == old_report.as_dict()
    assert max(len(p["ids"]) for p in new_posts) == 300, "paged at 300 ids, and the 650-chunk note needed three pages"
    assert len(posts) > 5 * len(new_posts), f"old {len(posts)} store-gets, new {len(new_posts)}"
    by_collection = sorted({p["collection"] for p in new_posts})
    # three full rounds of 50 (at most one call per collection each) and a fourth round of the 650-chunk note alone: 3 pages
    assert len(new_posts) <= 3 * len(by_collection) + 3


# ── --batch-size and --pause-ms ────────────────────────────────────────────────────────────────────

def test_the_pause_follows_each_rounds_reads_and_its_write_and_not_the_last_write():
    w = _world_with_one_of_each()
    events: list = []
    w.chunk_rows_orig = w.chunk_rows
    w.chunk_rows = lambda c, hs: (events.append("rows"), w.chunk_rows_orig(c, hs))[1]       # type: ignore[method-assign]
    w.update_many_orig = w.update_many
    w.update_many = lambda ps: (events.append("write"), w.update_many_orig(ps))[1]           # type: ignore[method-assign]
    mb.backfill_meta(w, w, w, apply=True, batch=2, pause_ms=250, sleep=lambda s: events.append(("sleep", s)))
    assert events == [
        "rows", "rows", ("sleep", 0.25), "write", ("sleep", 0.25),   # round one: the note's and the paper's rows, a rest, the write, a rest
        ("sleep", 0.25), "write",                                  # round two reads no chunk (shared) but did read the catalog; no rest after the last write
    ]


def test_a_dry_run_pauses_after_the_reads_only_and_a_zero_pause_never_sleeps():
    w = _world_with_one_of_each()
    naps: list[float] = []
    mb.backfill_meta(w, w, None, apply=False, batch=2, pause_ms=40, sleep=naps.append)
    assert naps == [0.04, 0.04]
    naps.clear()
    mb.backfill_meta(w, w, w, apply=True, batch=1, pause_ms=0, sleep=naps.append)
    assert naps == []


def test_a_larger_batch_means_fewer_rounds_and_the_same_result():
    one, two = _catalog(), _catalog()
    assert mb.backfill_meta(one, one, None, apply=False, batch=10).as_dict() == mb.backfill_meta(two, two, None, apply=False, batch=150).as_dict()
    assert len([e for e in one.events if e[0] == "manifests"]) == 15
    assert len([e for e in two.events if e[0] == "manifests"]) == 1


@pytest.mark.parametrize("kwargs", [{"batch": 0}, {"batch": -3}, {"pause_ms": -1}])
def test_a_batch_below_one_or_a_negative_pause_is_refused_before_anything_is_read(kwargs):
    w = _catalog()
    with pytest.raises(ValueError):
        mb.backfill_meta(w, w, None, apply=False, **kwargs)
    assert w.events == []


@pytest.mark.parametrize("flags", [["--batch-size", "0"], ["--batch-size", "-1"], ["--pause-ms", "-5"], ["--batch-size", "x"]])
def test_the_command_refuses_a_bad_batch_size_or_pause(monkeypatch, flags):
    w = _catalog()
    result, _ = _invoke(monkeypatch, w, *flags)
    assert result.exit_code == 2, result.output
    assert w.events == []


def test_the_command_passes_the_batch_size_and_pause_through(monkeypatch):
    w = _catalog()
    naps: list[float] = []
    setattr_in(monkeypatch, mb, "time.sleep", naps.append)
    result, _ = _invoke(monkeypatch, w, "--batch-size", "75", "--pause-ms", "20", "--apply")
    assert result.exit_code == 0, result.output
    assert len([e for e in w.events if e[0] == "manifests"]) == 2, "150 entries at 75 a round"
    assert naps == [0.02, 0.02, 0.02], "a rest after each round's reads, and after the first write"


def test_the_defaults_are_a_round_of_fifty_and_no_pause(monkeypatch):
    w = _catalog()
    naps: list[float] = []
    setattr_in(monkeypatch, mb, "time.sleep", naps.append)
    result, _ = _invoke(monkeypatch, w)
    assert result.exit_code == 0, result.output
    assert len([e for e in w.events if e[0] == "manifests"]) == 3 and naps == []


# ── progress ───────────────────────────────────────────────────────────────────────────────────────

def test_a_progress_report_follows_every_round_of_a_short_run_and_the_line_says_where_it_is():
    w = _catalog()
    ticks = iter(range(0, 1000, 10))
    asked = iter(range(0, 1000, 7))     # the request counter: 7 more requests every time it is read
    seen: list[mb.Progress] = []
    mb.backfill_meta(w, w, None, apply=False, batch=50, progress=seen.append, clock=lambda: next(ticks), requests=lambda: next(asked))
    assert [(p.batch, p.batches, p.documents, p.documents_total) for p in seen] == [(1, 3, 50, 150), (2, 3, 100, 150), (3, 3, 150, 150)]
    # the clock is read at the start (0) and after each round (10, 20, 30); the counter likewise (0, 7, 14, 21), so a round is 7 requests
    assert [p.elapsed for p in seen] == [10, 20, 30] and [p.requests for p in seen] == [7, 14, 21]
    first, last = seen[0], seen[-1]
    assert first.rate_since_start == pytest.approx(0.7) and last.rate_since_start == pytest.approx(0.7)
    assert first.line().startswith("backfill-meta: batch 1/3, 50/150 documents, 7 requests (0.70/s since start, 0.70/s over the last 1 round), ")
    assert last.line().endswith(", 0 lookups failed, 0 writes failed, 30.0s, 5.0 docs/s, at " + last.last_tumbler)
    assert "over the last 3 rounds" in last.line() and last.last_tumbler


def test_the_recent_rate_is_taken_over_the_last_ten_rounds_and_differs_from_the_rate_since_the_start():
    w = _catalog(n_docs=290, n_notes=10)         # 300 entries, 30 rounds of 10
    clock_ticks = iter(range(0, 10_000))
    now = {"t": 0.0}
    reads = {"n": 0}

    def clock() -> float:
        now["t"] = float(next(clock_ticks))
        return now["t"]

    def requests() -> int:
        # one tick a read; the run's first 10 rounds send 1 request a round, the later ones 5 a round
        reads["n"] += 1
        return reads["n"] if reads["n"] <= 11 else 11 + 5 * (reads["n"] - 11)

    seen: list[mb.Progress] = []
    mb.backfill_meta(w, w, None, apply=False, batch=10, progress=seen.append, clock=clock, requests=requests)
    last = seen[-1]
    assert last.recent_rounds == mb.RECENT_ROUNDS == 10
    assert last.recent_requests == 50 and last.recent_elapsed == 10 and last.rate_recent == pytest.approx(5.0)
    assert last.rate_since_start == pytest.approx(110 / 30) and last.rate_recent > last.rate_since_start, "the last ten rounds are the busy ones"


def test_progress_shows_the_lookups_and_writes_that_failed():
    w = _catalog()
    w.fail_rows_for = {B}

    def update_many(payloads):
        w.updates.extend(payloads)
        return [0 if p["tumbler"].endswith("3") else 1 for p in payloads]

    w.update_many = update_many     # type: ignore[method-assign]
    seen: list[mb.Progress] = []
    report = mb.backfill_meta(w, w, w, apply=True, batch=50, progress=seen.append)
    assert report.lookups_failed > 0 and report.writes_failed > 0
    assert (seen[-1].lookups_failed, seen[-1].writes_failed) == (report.lookups_failed, report.writes_failed)
    assert f"{report.lookups_failed} lookups failed, {report.writes_failed} writes failed" in seen[-1].line()


def test_a_long_run_prints_about_three_hundred_lines_and_always_the_last(monkeypatch):
    monkeypatch.setattr(mb, "PROGRESS_LINES", 3)
    w = _catalog()
    seen: list[mb.Progress] = []
    mb.backfill_meta(w, w, None, apply=False, batch=15, progress=seen.append)    # 10 rounds, every 4th
    assert [p.batch for p in seen] == [4, 8, 10]
    assert seen[-1].documents == 150


def test_the_command_counts_requests_through_the_gate_and_prints_them(monkeypatch):
    """A world whose every read and write passes the gate as a transport would: the progress lines carry the count."""
    class _Gated(_Counting):
        def get_manifests(self, ids):
            request_gate.before_request()
            return super().get_manifests(ids)

        def chunk_rows(self, collection, chashes):
            request_gate.before_request()
            return super().chunk_rows(collection, chashes)

        def update_many(self, payloads):
            request_gate.before_request()
            return super().update_many(payloads)

    w = _catalog(world=_Gated)
    result, _ = _invoke(monkeypatch, w, "--apply", "--max-rps", "100000")
    assert result.exit_code == 0, result.output
    last = [line for line in result.stderr.splitlines() if line.startswith("backfill-meta: batch 3/3")][0]
    sent = len([e for e in w.events if e[0] in ("manifests", "rows", "write")])
    assert f", {sent} requests (" in last and sent > 6


def test_progress_goes_to_stderr_and_stdout_keeps_the_json_it_always_had(monkeypatch):
    w = _catalog()
    result, _ = _invoke(monkeypatch, w, "--json", "--batch-size", "50")
    assert result.exit_code == 0, result.output
    assert result.stderr.count("backfill-meta: batch ") == 3 and "3/3, 150/150 documents" in result.stderr
    report = json.loads(result.stdout)
    assert "backfill-meta" not in result.stdout
    assert set(report) == {
        "applied", "documents_examined", "documents_notes", "documents_complete", "documents_all_shared",
        "documents_to_stamp", "documents_written", "lookups_failed", "writes_failed",
        "tags_legacy_strings_left_for_the_array_rung", "keys",
    }, "an uninterrupted run adds nothing to the report"


# ── a clean stop ───────────────────────────────────────────────────────────────────────────────────

class _Interrupting(_Counting):
    """Raises ``KeyboardInterrupt`` from the Nth chunk-row read."""

    stop_at_call: int = 2

    def chunk_rows(self, collection, chashes):
        if len([e for e in self.events if e[0] == "rows"]) + 1 >= self.stop_at_call:
            raise KeyboardInterrupt
        return super().chunk_rows(collection, chashes)


def _first_round_only() -> dict:
    """The report of the same catalog stopped after round one by the polite route."""
    w = _catalog()
    report = mb.backfill_meta(w, w, w, apply=True, batch=50, should_stop=lambda: True)
    assert report.interrupted and report.batches_done == 1
    return report.as_dict()


def test_a_keyboard_interrupt_mid_round_ends_the_run_with_the_finished_rounds_counted_and_nothing_of_the_cut_one():
    # round one reads K, A, B, C at most (4 calls); the interrupt lands in round two
    w = _catalog(world=_Interrupting)
    w.stop_at_call = 6
    report = mb.backfill_meta(w, w, w, apply=True, batch=50)
    assert report.interrupted and report.batches_done == 1 and report.batches_total == 3
    cut = report.as_dict()
    assert cut.pop("cut_batch_may_have_written") == 2, "the report says which batch was cut, and that its write may have landed"
    assert cut == _first_round_only(), "the cut round leaves no count behind, though it had begun"
    assert report.documents_examined == 50
    assert len(w.updates) == report.documents_to_stamp, "no write of the cut round"


def test_a_stop_asked_between_rounds_ends_after_the_round_it_was_asked_in_and_not_after_the_last():
    w = _catalog()
    asks: list[int] = []

    def should_stop() -> bool:
        asks.append(1)
        return len(asks) == 2

    report = mb.backfill_meta(w, w, None, apply=False, batch=50, should_stop=should_stop)
    assert report.interrupted and report.batches_done == 2 and report.documents_examined == 100
    assert asks == [1, 1], "asked after round one and after round two, then stopped"
    last = _catalog()
    only_round = mb.backfill_meta(last, last, None, apply=False, batch=150, should_stop=lambda: True)
    assert not only_round.interrupted, "a stop asked after the last round has nothing left to stop"
    assert "interrupted" not in only_round.as_dict()


def test_the_command_prints_the_partial_report_and_exits_130(monkeypatch):
    w = _catalog(world=_Interrupting)
    w.stop_at_call = 6
    result, closed = _invoke(monkeypatch, w, "--apply", "--json")
    assert result.exit_code == 130, result.output
    report = json.loads(result.stdout)
    assert report["interrupted"] is True and report["batches_done"] == 1 and report["batches_total"] == 3
    assert report["documents_examined"] == 50 and closed == [True], "the writer is closed on the way out"
    assert "Interrupted by SIGINT after 1 of 3 batches (50 documents examined)" in result.stderr
    assert "Batch 2 was cut by a second interrupt: it is NOT in the report above, and its writes may already have landed" in result.stderr
    assert "Resume with: nx catalog backfill-meta --apply --json --batch-size 50 --pause-ms 0 --stop-after-failed-rounds 3 --start-after " in result.stderr


def test_the_command_text_report_covers_the_finished_rounds(monkeypatch):
    w = _catalog(world=_Interrupting)
    w.stop_at_call = 6
    result, _ = _invoke(monkeypatch, w)
    assert result.exit_code == 130
    assert "Examined 50 documents" in result.stdout and "Dry run: nothing written" in result.stdout


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="a signal handler needs the main thread")
def test_the_first_ctrl_c_finishes_the_round_and_stops_and_the_second_aborts_it():
    before = signal.getsignal(signal.SIGINT)

    class _Once(_Counting):
        raised = 0
        times = 1

        def chunk_rows(self, collection, chashes):
            if self.raised < self.times:
                self.raised += 1
                signal.raise_signal(signal.SIGINT)
            return super().chunk_rows(collection, chashes)

    try:
        first = _catalog(world=_Once)
        with mb.stop_after_current_batch() as should_stop:
            assert should_stop() is False
            report = mb.backfill_meta(first, first, first, apply=True, batch=50, should_stop=should_stop)
        assert report.interrupted and report.batches_done == 1, "the round the Ctrl-C landed in was finished"
        assert report.documents_examined == 50 and len(first.updates) == report.documents_to_stamp
        assert signal.getsignal(signal.SIGINT) is before, "the previous handler is back"

        second = _catalog(world=_Once)
        second.times = 2
        with mb.stop_after_current_batch() as should_stop:
            report = mb.backfill_meta(second, second, second, apply=True, batch=50, should_stop=should_stop)
        assert report.interrupted and report.batches_done == 0 and report.documents_examined == 0, \
            "the second Ctrl-C cut the round: it is not counted"
        assert second.updates == []
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        signal.signal(signal.SIGINT, before)


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="a signal handler needs the main thread")
def test_the_command_survives_a_ctrl_c_in_a_round_by_finishing_it(monkeypatch):
    before = signal.getsignal(signal.SIGINT)

    class _Once(_Counting):
        sent = False

        def chunk_rows(self, collection, chashes):
            if not self.sent:
                self.sent = True
                signal.raise_signal(signal.SIGINT)
            return super().chunk_rows(collection, chashes)

    try:
        w = _catalog(world=_Once)
        result, closed = _invoke(monkeypatch, w, "--apply", "--json")
    finally:
        signal.signal(signal.SIGINT, before)
    assert result.exit_code == 130, result.output
    report = json.loads(result.stdout)
    assert report["batches_done"] == 1 and report["documents_examined"] == 50 and report["interrupted"] is True
    assert report["documents_written"] == len(w.updates) and closed == [True]


def test_a_chunk_two_documents_hold_in_one_collection_is_asked_for_once_in_a_round():
    w = _Counting()
    notes = [_entry({}, title=f"n{i}", tumbler=f"1.{i}", collection=K) for i in range(3)]
    w.entries = notes
    for e in notes:
        w.manifests[str(e.tumbler)] = [_Manifest("same"), _Manifest(f"own{e.tumbler}")]
        w.owners[f"own{e.tumbler}"] = [str(e.tumbler)]
    w.owners["same"] = [str(e.tumbler) for e in notes]
    w.rows[(K, "same")] = _row(**MINE, tags="t")
    mb.backfill_meta(w, w, None, apply=False)
    calls = w.rows_calls()
    assert len(calls) == 1 and calls[0][0] == K
    assert sorted(calls[0][1]) == sorted(["same", "own1.0", "own1.1", "own1.2"])


# ── L3: stop after K failed rounds ─────────────────────────────────────────────────────────────────

class _FailingRounds(_Counting):
    """A lookup fails in the rounds named (1-based): the chunk-row read of every collection raises."""

    bad_rounds: frozenset = frozenset()

    def chunk_rows(self, collection, chashes):
        round_ = len([e for e in self.events if e[0] == "manifests"])
        if round_ in self.bad_rounds:
            self.events.append(("rows", collection, tuple(chashes)))
            raise RuntimeError("store-get down")
        return super().chunk_rows(collection, chashes)


def _failing(rounds: set[int]) -> _FailingRounds:
    w = _catalog(world=_FailingRounds)
    w.bad_rounds = frozenset(rounds)
    return w


def test_k_failed_rounds_in_a_row_stop_the_run_cleanly_and_leave_it_resumable():
    w = _failing({1, 2, 3})
    report = mb.backfill_meta(w, w, w, apply=True, batch=10, stop_after_failed_rounds=2)
    assert report.interrupted and report.stop_reason == mb.STOP_FAILED_ROUNDS
    assert report.batches_done == 2 and report.documents_examined == 20 and report.lookups_failed > 0
    # Both finished batches failed, so the resume point stays at the run's start: a rerun from the top, never a
    # --start-after past documents whose keys were withheld (review r2 S1).
    assert report.last_tumbler and report.resume_after == "" and report.as_dict()["resume_after"] == ""
    assert len([e for e in w.events if e[0] == "manifests"]) == 2, "no third round was started"


def test_the_resume_point_stops_before_the_first_failed_round_not_at_the_last_finished_one():
    """A clean batch, then failed ones: the resume command names the clean batch's last document, so the
    failed batches' documents are read again; the progress position (last_tumbler) still moves on."""
    w = _failing({2, 3})
    report = mb.backfill_meta(w, w, w, apply=True, batch=10, stop_after_failed_rounds=2)
    assert report.stop_reason == mb.STOP_FAILED_ROUNDS and report.batches_done == 3
    first_batch_last = str(w.entries[9].tumbler)
    assert report.resume_after == first_batch_last
    assert report.last_tumbler != first_batch_last, "the progress position moved past the failed batches"
    assert report.as_dict()["resume_after"] == first_batch_last


def test_a_good_round_resets_the_count_of_failed_rounds():
    w = _failing({1, 3})     # fail, ok, fail, ok ...: never two in a row
    report = mb.backfill_meta(w, w, None, apply=False, batch=10, stop_after_failed_rounds=2)
    assert not report.interrupted and report.batches_done == report.batches_total == 15


def test_a_write_that_fails_is_a_failed_round_too():
    w = _catalog()
    w.update_many = lambda payloads: [0] * len(payloads)      # type: ignore[method-assign]
    report = mb.backfill_meta(w, w, w, apply=True, batch=10, stop_after_failed_rounds=3)
    assert report.interrupted and report.stop_reason == mb.STOP_FAILED_ROUNDS and report.batches_done == 3
    assert report.writes_failed == report.documents_to_stamp == 25 and report.writes_failed > 0


def test_zero_never_stops_and_the_last_round_has_nothing_to_stop():
    w = _failing(set(range(1, 100)))
    assert not mb.backfill_meta(w, w, None, apply=False, batch=10, stop_after_failed_rounds=0).interrupted
    last = _failing({1})
    done = mb.backfill_meta(last, last, None, apply=False, batch=150, stop_after_failed_rounds=1)
    assert not done.interrupted, "one round, failed: it was also the last, so the run finished"


def test_the_command_stops_after_three_failed_rounds_by_default_and_exits_1(monkeypatch):
    w = _failing({1, 2, 3, 4, 5})
    result, _ = _invoke(monkeypatch, w, "--batch-size", "10", "--json")
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["batches_done"] == 3 and report["stop_reason"] == "failed_rounds" and report["interrupted"] is True
    assert "Stopped after 3 of 15 batches: 3 rounds in a row had a failed lookup or write" in result.stderr
    # Every finished batch failed, so the resume command starts from the top: no --start-after that would skip
    # the documents whose keys were withheld (review r2 S1).
    assert "Resume with: nx catalog backfill-meta --json --batch-size 10 --pause-ms 0 --stop-after-failed-rounds 3\n" in result.stderr
    assert "--start-after" not in result.stderr


def test_the_command_option_can_raise_or_disable_the_guard(monkeypatch):
    w = _failing(set(range(1, 100)))
    result, _ = _invoke(monkeypatch, w, "--batch-size", "10", "--stop-after-failed-rounds", "5", "--json")
    assert result.exit_code == 1 and json.loads(result.stdout)["batches_done"] == 5
    w = _failing(set(range(1, 100)))
    result, _ = _invoke(monkeypatch, w, "--batch-size", "10", "--stop-after-failed-rounds", "0", "--json")
    assert result.exit_code == 0 and json.loads(result.stdout)["lookups_failed"] > 0 and "interrupted" not in json.loads(result.stdout)


# ── L4: resume with --start-after ──────────────────────────────────────────────────────────────────

def test_start_after_skips_the_listing_by_position_and_the_two_halves_make_the_whole():
    whole = _catalog()
    full = mb.backfill_meta(whole, whole, whole, apply=True, batch=50)
    first_half = _catalog()
    stopped = mb.backfill_meta(first_half, first_half, first_half, apply=True, batch=50, should_stop=lambda: True)
    assert stopped.interrupted and stopped.last_tumbler == str(first_half.entries[49].tumbler)
    rest = _catalog()
    second = mb.backfill_meta(rest, rest, rest, apply=True, batch=50, start_after=stopped.last_tumbler)
    assert second.documents_examined == 100 and second.started_after == stopped.last_tumbler
    assert first_half.updates + rest.updates == whole.updates, "the same writes, in the same order, as one uninterrupted pass"
    assert stopped.documents_to_stamp + second.documents_to_stamp == full.documents_to_stamp
    assert stopped.documents_examined + second.documents_examined == full.documents_examined == 150
    seen = {str(u["tumbler"]) for u in rest.updates}
    assert not seen & {str(e.tumbler) for e in rest.entries[:50]}, "nothing at or before the resume point was read or written"


def test_the_skipped_documents_still_count_as_owners_of_their_chunks():
    """X (skipped) and Y (read) hold one chunk, in different collections: each is the sole owner in its own, so
    Y takes the row, exactly as in an uninterrupted pass. Y's judgment needs X in the owner map."""
    def world() -> _World:
        w = _Counting()
        x = _entry({}, title="x", tumbler="2.1", content_type="paper", file_path="x.pdf", collection=A)
        y = _entry({}, title="y", tumbler="2.2", content_type="paper", file_path="y.pdf", collection=B)
        w.entries = [x, y]
        w.manifests = {"2.1": [_Manifest("shared")], "2.2": [_Manifest("shared")]}
        w.owners = {"shared": ["2.1", "2.2"]}
        w.rows[(B, "shared")] = _row(extraction_method="mineru")
        return w

    whole = world()
    mb.backfill_meta(whole, whole, whole, apply=True)
    assert [u["tumbler"] for u in whole.updates if "extraction_method" in u["meta"]] == ["2.2"], "control: Y takes the row"
    resumed = world()
    mb.backfill_meta(resumed, resumed, resumed, apply=True, start_after="2.1")
    assert [u["tumbler"] for u in resumed.updates] == ["2.2"] and "extraction_method" in resumed.updates[0]["meta"]
    assert resumed.updates[0] == next(u for u in whole.updates if u["tumbler"] == "2.2")


def test_start_after_is_a_position_in_the_listing_and_never_a_comparison_of_tumblers():
    """The catalog sorts tumblers as strings in the database's collation: 1.10 comes before 1.2. The skip follows the listing."""
    w = _catalog(n_docs=0, n_notes=0)
    w.entries = [_entry({}, title=t, tumbler=t, collection=K) for t in ("1.1", "1.10", "1.2", "1.3")]
    for e in w.entries:
        w.manifests[str(e.tumbler)] = [_Manifest(f"c{e.tumbler}")]
        w.owners[f"c{e.tumbler}"] = [str(e.tumbler)]
    seen: list[str] = []
    w.get_manifests = lambda ids: (seen.extend(ids), {})[1]       # type: ignore[method-assign]
    mb.backfill_meta(w, w, None, apply=False, batch=10, start_after="1.10")
    assert seen == ["1.2", "1.3"], "after 1.10 in the listing, whatever 1.2 and 1.10 compare as"


def test_a_tumbler_the_listing_does_not_hold_is_refused_before_anything_is_read():
    w = _catalog()
    with pytest.raises(mb.ResumePointError, match="no such document"):
        mb.backfill_meta(w, w, None, apply=False, start_after="9.9.9")
    assert w.events == []


def test_resuming_after_the_last_document_is_a_finished_run_not_a_vacuous_one():
    w = _catalog()
    report = mb.backfill_meta(w, w, None, apply=False, start_after=str(w.entries[-1].tumbler))
    assert report.documents_examined == 0 and report.batches_total == 0 and not report.interrupted and w.events == []


def test_the_command_prints_a_resume_command_that_resumes(monkeypatch):
    # stop it after the first round by the polite route, through the command
    stopper = _catalog()
    monkeypatch.setattr(mb, "stop_after_current_batch", _always_stop)
    result, _ = _invoke(monkeypatch, stopper, "--apply", "--batch-size", "40", "--pause-ms", "5", "--max-rps", "250.5", "--json")
    assert result.exit_code == 130, result.output
    line = [x for x in result.stderr.splitlines() if x.startswith("Resume with: ")][0]
    cmd = line.removeprefix("Resume with: ")
    last = json.loads(result.stdout)["resume_after"]
    assert cmd == f"nx catalog backfill-meta --apply --json --batch-size 40 --pause-ms 5 --max-rps 250.5 --stop-after-failed-rounds 3 --start-after {last}"
    # run exactly that command
    monkeypatch.undo()
    rest = _catalog()
    result, _ = _invoke(monkeypatch, rest, *shlex.split(cmd)[3:])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["started_after"] == last and report["documents_examined"] == 110
    assert rest.updates and str(rest.entries[39].tumbler) not in {u["tumbler"] for u in rest.updates}


def test_the_command_refuses_an_unknown_start_after_and_says_when_there_is_nothing_after_it(monkeypatch):
    w = _catalog()
    result, _ = _invoke(monkeypatch, w, "--start-after", "9.9.9")
    assert result.exit_code == 1 and "the catalog lists no such document" in result.output
    w = _catalog()
    result, _ = _invoke(monkeypatch, w, "--start-after", str(w.entries[-1].tumbler))
    assert result.exit_code == 0 and "Nothing after" in result.stderr


# ── L5: exit codes and signals ─────────────────────────────────────────────────────────────────────

class _StopAfterFirst:
    """Stands in for ``stop_after_current_batch``: the polite stop is asked after the first round."""

    signum = 0

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return True


@pytest.fixture
def _stop_polite(monkeypatch):
    @contextlib.contextmanager
    def cm():
        yield _StopAfterFirst()

    monkeypatch.setattr(mb, "stop_after_current_batch", cm)


def _always_stop():
    @contextlib.contextmanager
    def cm():
        yield _StopAfterFirst()

    return cm()


def test_a_write_that_failed_outranks_the_interrupt_in_the_exit_code(monkeypatch, _stop_polite):
    w = _catalog()
    w.update_many = lambda payloads: [0] * len(payloads)       # type: ignore[method-assign]
    result, _ = _invoke(monkeypatch, w, "--apply", "--json", "--stop-after-failed-rounds", "0")
    assert result.exit_code == 1, "130 must not hide a failed write"
    report = json.loads(result.stdout)
    assert report["interrupted"] is True and report["writes_failed"] > 0
    assert f"{report['writes_failed']} writes failed in the finished batches: exit 1" in result.stderr
    assert "Resume with:" in result.stderr


def test_an_interrupt_with_nothing_failed_is_130(monkeypatch, _stop_polite):
    w = _catalog()
    result, _ = _invoke(monkeypatch, w, "--apply")
    assert result.exit_code == 130 and "Interrupted by SIGINT" in result.stderr


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="a signal handler needs the main thread")
def test_sigterm_is_the_first_sigint_a_polite_stop_and_the_previous_disposition_comes_back():
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    class _Term(_Counting):
        sent = False

        def chunk_rows(self, collection, chashes):
            if not self.sent:
                self.sent = True
                signal.raise_signal(signal.SIGTERM)
            return super().chunk_rows(collection, chashes)

    try:
        w = _catalog(world=_Term)
        with mb.stop_after_current_batch() as stop:
            assert stop() is False and stop.signum == 0
            assert signal.getsignal(signal.SIGTERM) not in (before[signal.SIGTERM], signal.SIG_DFL)
            report = mb.backfill_meta(w, w, w, apply=True, batch=50, should_stop=stop)
            assert signal.getsignal(signal.SIGTERM) == before[signal.SIGTERM], "after the first signal the previous disposition is back, so a second SIGTERM ends the process as it would have"
        assert stop.signum == signal.SIGTERM
        assert report.interrupted and report.batches_done == 1 and report.documents_examined == 50 and len(w.updates) == report.documents_to_stamp
        assert {s: signal.getsignal(s) for s in before} == before
    finally:
        for s, h in before.items():
            signal.signal(s, h)


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="a signal handler needs the main thread")
def test_the_command_exits_143_after_a_sigterm(monkeypatch):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    class _Term(_Counting):
        sent = False

        def chunk_rows(self, collection, chashes):
            if not self.sent:
                self.sent = True
                signal.raise_signal(signal.SIGTERM)
            return super().chunk_rows(collection, chashes)

    try:
        w = _catalog(world=_Term)
        result, closed = _invoke(monkeypatch, w, "--apply")
    finally:
        for s, h in before.items():
            signal.signal(s, h)
    assert result.exit_code == 143, result.output
    assert "Interrupted by SIGTERM after 1 of 3 batches" in result.stderr and closed == [True]


# ── the new options ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flags", [
    ["--max-rps", "0"], ["--max-rps", "-2"], ["--max-rps", "fast"], ["--stop-after-failed-rounds", "-1"],
])
def test_the_command_refuses_a_bad_rate_or_guard(monkeypatch, flags):
    w = _catalog()
    result, _ = _invoke(monkeypatch, w, *flags)
    assert result.exit_code == 2, result.output
    assert w.events == []


def test_the_help_names_the_guard_and_the_runbook_note():
    result = CliRunner().invoke(main, ["catalog", "backfill-meta", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    for needle in ("--max-rps", "--start-after", "--stop-after-failed-rounds", "Keep the catalog's other writers", "for the whole of every pass",
                   "re-reads", "Exit codes:"):
        assert needle in text or needle.replace("re-reads", "reads the chunk rows of most documents again") in text, needle
    assert "a few requests per round" not in text and "2.5 requests/s" not in text
