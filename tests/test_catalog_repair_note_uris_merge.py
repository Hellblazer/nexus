# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog repair-note-uris --merge`` (nexus-bivz3.24; Sam's ruling "Chunks decide", ask bivz324-merge-winner).

The rule itself is pinned in ``tests/test_note_uri_merge_rule.py``. These tests pin the loop around it, against
a fake writer that records every engine call: the dry run decides and writes nothing, ``--apply`` merges the loser
into the winner (and, when a stale row won, rewrites exactly that row afterwards), a refused pair is reported and
the run goes on, a pair is never retried, an engine error stops the run with the ledger written, and the ledger
says per pair who won, who lost, the loser's URI before and after, and which rule decided.
"""
from __future__ import annotations

import json
import os
import stat

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.note_uri_merge import Candidate, Decision
from nexus.cli import main
from nexus.commands.catalog_cmds import note_uri_repair as cmd

_Y = "knowledge__y__some-model__v1"
_X = "knowledge__x__some-model__v1"


def _entry(tumbler, reason, *, chunks=0, at=None, twin=None, twin_chunks=0, twin_at=None, target="t",
           twin_uri=None):
    e = {
        "tumbler": tumbler, "collection": _Y, "source_uri": f"nx-note://{_X}/{target}",
        "new_uri": f"nx-note://{_Y}/{target}", "reason": reason, "twin": twin,
        "chunk_count": chunks, "indexed_at": at, "has_aspects": False, "has_highlights": False,
    }
    if reason == "twin":
        e.update(twin_chunk_count=twin_chunks, twin_indexed_at=twin_at, twin_has_aspects=False,
                 twin_has_highlights=False, twin_source_uri=twin_uri or f"nx-note://{_Y}/{target}")
    return e


def _census(merge_entries=(), **over):
    n_twin = sum(1 for e in merge_entries if e["reason"] == "twin")
    base = {
        "apply": False, "stale_live": len(merge_entries), "rewrite": 0, "merge": len(merge_entries),
        "merge_twin": n_twin, "merge_sibling": len(merge_entries) - n_twin, "refused": 0,
        "refused_file_routed_collection": 0, "refused_malformed": 0, "tombstoned": 0,
        "export_copies": 0, "export_copies_target_moved": 0,
        "sample": {"rewrite": [], "merge": list(merge_entries), "refused": [], "tombstoned": [],
                   "export_target_moved": []},
    }
    base.update(over)
    return base


def _status(code: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/v1/catalog/merge")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=httpx.Response(code, request=request, text=body))


class _Engine:
    """Scripted writer: censuses come off a queue, merges and rewrites are looked up by what they are asked for."""

    def __init__(self, censuses, merges=None, rewrites=None):
        self.censuses = list(censuses)
        self.merges = merges or {}
        self.rewrites = rewrites or {}
        self.calls: list[tuple] = []
        self.closed = False

    def repair_stale_note_uris(self, *, apply=False, limit=1000, sample=20, tumblers=None):
        if not apply:
            self.calls.append(("census", sample))
            return dict(self.censuses.pop(0) if len(self.censuses) > 1 else self.censuses[0], apply=False)
        self.calls.append(("rewrite", tuple(tumblers or ())))
        out = self.rewrites.get(tumblers[0], "ok")
        if isinstance(out, Exception):
            raise out
        if out == "none":
            return _census(rewritten=0, remaining=0, ledger=[])
        t = tumblers[0]
        return _census(rewritten=1, remaining=0, ledger=[
            {"table": "catalog_documents", "key": t, "old_uri": f"nx-note://{_X}/t", "new_uri": f"nx-note://{_Y}/t"},
            {"table": "document_aspects", "key": "55", "doc_id": t, "old_uri": f"nx-note://{_X}/p",
             "new_uri": f"nx-note://{_Y}/p"},
        ])

    def merge_documents(self, duplicate, canonical):
        self.calls.append(("merge", duplicate, canonical))
        out = self.merges.get((duplicate, canonical), {"links_remapped": 2, "links_collapsed": 0, "links_dropped": 0,
                                                       "source_uri_moved": False})
        if isinstance(out, Exception):
            raise out
        return dict(out, duplicate=duplicate, canonical=canonical)

    def update(self, *a, **k):
        raise AssertionError("the merge pass writes only through merge_documents and the rewrite route")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _patch(monkeypatch, engine):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: engine)


def _run(*args):
    return CliRunner().invoke(main, ["catalog", "repair-note-uris", "--merge", *args])


def _ledger(tmp_path):
    files = list(tmp_path.glob("nx-note-uri-repair-*.jsonl"))
    assert len(files) == 1, files
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def _calls(engine, kind):
    return [c for c in engine.calls if c[0] == kind]


# A: the twin wins (neither holds chunks); B: the stale row holds the only chunks and wins.
TWIN_WINS = _entry("1.1.1", "twin", twin="1.1.2")
STALE_WINS = _entry("1.1.5", "twin", twin="1.1.6", target="u", chunks=4, at="2026-10-01T00:00Z", twin_chunks=0,
                    twin_uri=f"chroma://{_Y}/u")


class TestDryRun:
    def test_it_decides_each_pair_and_says_why_and_writes_nothing(self, monkeypatch, tmp_path):
        e = _Engine([_census([TWIN_WINS, STALE_WINS])])
        _patch(monkeypatch, e)

        result = _run()

        assert result.exit_code == 0, result.output
        assert [c[0] for c in e.calls] == ["census"], "no merge, no rewrite"
        assert e.calls[0] == ("census", 500), "the plan reads as many entries as the engine will list"
        out = result.output
        assert "1.1.2 wins" in out and "no row holds chunks: the twin wins by default" in out
        assert "merge 1.1.1 into 1.1.2" in out
        assert "1.1.5 wins" in out and "the stale row holds the only chunks" in out
        assert "merge 1.1.6 into 1.1.5, then rewrite 1.1.5" in out
        assert "Dry run" in out
        assert list(tmp_path.glob("*.jsonl")) == []

    def test_the_census_is_printed_first(self, monkeypatch):
        _patch(monkeypatch, _Engine([_census([TWIN_WINS])]))
        out = _run().output
        assert out.index("stale note rows (live)") < out.index("1.1.2 wins")

    def test_json_carries_the_census_the_plan_and_what_was_skipped(self, monkeypatch):
        broken = _entry("1.1.9", "twin", twin="1.1.8")
        broken["chunk_count"] = None
        _patch(monkeypatch, _Engine([_census([TWIN_WINS, broken])]))

        result = _run("--json")

        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc["census"]["merge"] == 2
        assert doc["plan"][0]["winner"] == "1.1.2" and doc["plan"][0]["losers"] == ["1.1.1"]
        assert doc["plan"][0]["rule"] == "no row holds chunks: the twin wins by default"
        assert doc["plan"][0]["rewrite_needed"] is False
        assert doc["skipped"][0]["tumbler"] == "1.1.9"

    def test_entries_the_report_did_not_list_are_counted_not_hidden(self, monkeypatch):
        _patch(monkeypatch, _Engine([_census([TWIN_WINS], merge=40, merge_twin=40)]))
        assert "39 more merge candidate(s) are not in this report" in _run().output

    def test_ledger_needs_apply(self):
        result = _run("--ledger", "x.jsonl")
        assert result.exit_code == 2 and "--ledger only applies with --apply" in result.output


class TestApply:
    def test_the_twin_wins_one_merge_call_and_no_rewrite(self, monkeypatch, tmp_path):
        e = _Engine([_census([TWIN_WINS]), _census()])
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert _calls(e, "merge") == [("merge", "1.1.1", "1.1.2")], "merge(S -> W): the stale row is the duplicate"
        assert _calls(e, "rewrite") == []
        assert e.closed

    def test_the_stale_row_wins_merge_the_twin_into_it_then_rewrite_exactly_that_row(self, monkeypatch):
        e = _Engine([_census([STALE_WINS]), _census()])
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        order = [c for c in e.calls if c[0] in ("merge", "rewrite")]
        assert order == [("merge", "1.1.6", "1.1.5"), ("rewrite", ("1.1.5",))], "W is the duplicate; S is rewritten alone"
        assert "window" in result.output.lower(), "the gap between the two calls is named in the report"

    def test_the_census_runs_first_and_again_after(self, monkeypatch):
        e = _Engine([_census([TWIN_WINS]), _census()])
        _patch(monkeypatch, e)

        result = _run("--apply")

        kinds = [c[0] for c in e.calls]
        assert kinds[0] == "census" and kinds[-1] == "census"
        assert result.output.count("stale note rows (live)") == 2
        assert "Census after" in result.output

    def test_two_stale_rows_and_one_twin_merge_into_the_twin_without_chaining(self, monkeypatch):
        a = _entry("1.1.1", "twin", twin="1.1.9")
        b = _entry("1.1.3", "twin", twin="1.1.9", target="t")
        e = _Engine([_census([a, b]), _census()])
        _patch(monkeypatch, e)

        assert _run("--apply").exit_code == 0

        assert _calls(e, "merge") == [("merge", "1.1.1", "1.1.9"), ("merge", "1.1.3", "1.1.9")]

    def test_siblings_merge_into_the_winner_which_is_then_rewritten(self, monkeypatch):
        a = _entry("1.1.1", "sibling", twin="1.1.2")
        b = _entry("1.1.2", "sibling", twin="1.1.1", chunks=3, at="2026-11-01T00:00Z")
        e = _Engine([_census([a, b]), _census()])
        _patch(monkeypatch, e)

        assert _run("--apply").exit_code == 0

        order = [c for c in e.calls if c[0] in ("merge", "rewrite")]
        assert order == [("merge", "1.1.1", "1.1.2"), ("rewrite", ("1.1.2",))]

    def test_a_second_round_picks_up_pairs_the_first_report_did_not_list(self, monkeypatch):
        e = _Engine([_census([TWIN_WINS]), _census([_entry("1.1.7", "twin", twin="1.1.8", target="w")]), _census()])
        _patch(monkeypatch, e)

        assert _run("--apply").exit_code == 0

        assert [c[1:] for c in _calls(e, "merge")] == [("1.1.1", "1.1.2"), ("1.1.7", "1.1.8")]

    def test_a_pair_that_stays_listed_is_not_tried_twice(self, monkeypatch):
        refused = _status(409, json.dumps({"error": "duplicate not found (absent, tombstoned, or not visible)"}))
        e = _Engine([_census([TWIN_WINS])], merges={("1.1.1", "1.1.2"): refused})
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert len(_calls(e, "merge")) == 1, "the census lists it again; the run does not"

    def test_nothing_to_merge_writes_no_ledger_and_makes_no_call(self, monkeypatch, tmp_path):
        e = _Engine([_census()])
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert "Nothing to merge" in result.output
        assert [c[0] for c in e.calls] == ["census"]
        assert list(tmp_path.glob("*.jsonl")) == []


class TestRefusals:
    def test_a_refused_pair_is_reported_with_the_engines_reason_and_the_run_goes_on(self, monkeypatch, tmp_path):
        why = "1.1.1 is already aliased to 1.1.4; refusing to re-point it to 1.1.2"
        e = _Engine([_census([TWIN_WINS, _entry("1.1.3", "twin", twin="1.1.4", target="v")]), _census()],
                    merges={("1.1.1", "1.1.2"): _status(409, json.dumps({"error": why}))})
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert [c[1:] for c in _calls(e, "merge")] == [("1.1.1", "1.1.2"), ("1.1.3", "1.1.4")], "the second pair still ran"
        assert "REFUSED" in result.output and why in result.output
        assert "1 refused" in result.output and "1 merged" in result.output
        lines = [l for l in _ledger(tmp_path) if l["type"] == "merge"]
        assert [(l["loser"], l["outcome"]) for l in lines] == [("1.1.1", "refused"), ("1.1.3", "merged")]
        assert lines[0]["loser_uri_after"] == lines[0]["loser_uri_before"], "a refused loser keeps its URI"
        assert why in lines[0]["detail"]

    def test_a_stale_winner_whose_merge_was_refused_is_not_rewritten(self, monkeypatch):
        e = _Engine([_census([STALE_WINS]), _census()],
                    merges={("1.1.6", "1.1.5"): _status(409, json.dumps({"error": "cycle"}))})
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert _calls(e, "rewrite") == []
        assert "not rewritten" in result.output.lower()

    def test_a_rewrite_refused_in_the_window_is_reported_and_the_run_goes_on(self, monkeypatch, tmp_path):
        other = _entry("1.1.1", "twin", twin="1.1.2")
        taken = _status(409, json.dumps({"error": "repairing the stale note URI of 1.1.5 would rewrite ... 1.1.77"}))
        e = _Engine([_census([STALE_WINS, other]), _census()], rewrites={"1.1.5": taken})
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert ("merge", "1.1.1", "1.1.2") in e.calls, "the next group still ran"
        assert "rewrite refused" in result.output.lower() and "1.1.77" in result.output
        closing = [l for l in _ledger(tmp_path) if l["type"] == "rewrite-result" and l["winner"] == "1.1.5"]
        assert closing[0]["followed"] is False and "1.1.77" in closing[0]["detail"]

    def test_a_stale_winner_the_engine_will_not_rewrite_is_reported_not_forced(self, monkeypatch, tmp_path):
        e = _Engine([_census([STALE_WINS]), _census()], rewrites={"1.1.5": "none"})
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        closing = [l for l in _ledger(tmp_path) if l["type"] == "rewrite-result"]
        assert closing[0]["followed"] is False
        assert "not a rewrite candidate" in closing[0]["detail"]

    def test_the_chunk_guard_refuses_a_pair_whose_data_would_fold_chunks_into_a_chunkless_winner(self, monkeypatch, tmp_path):
        w = Candidate("1.1.2", 0, None, True, f"nx-note://{_Y}/t")
        s = Candidate("1.1.1", 5, "2026-01-01T00:00Z", False, f"nx-note://{_X}/t")
        bad = Decision(winner=w, losers=(s,), rule="forced by a test", target_uri=f"nx-note://{_Y}/t", members=(w, s))
        e = _Engine([_census()])

        outcome = cmd._execute_decision(e, bad, round_no=1, record=lambda line: None)

        assert [p["outcome"] for p in outcome["pairs"]] == ["refused"]
        assert "chunk guard" in outcome["pairs"][0]["detail"]
        assert _calls(e, "merge") == [], "no call was made, and no --force exists to override"


class TestStopsOnErrors:
    def test_a_server_error_stops_the_run_after_writing_what_happened(self, monkeypatch, tmp_path):
        e = _Engine([_census([TWIN_WINS, _entry("1.1.3", "twin", twin="1.1.4", target="v")])],
                    merges={("1.1.1", "1.1.2"): _status(500)})
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 1
        assert [c[1:] for c in _calls(e, "merge")] == [("1.1.1", "1.1.2")], "the second pair was not tried"
        assert [(l["loser"], l["outcome"]) for l in _ledger(tmp_path) if l["type"] == "merge"] == [("1.1.1", "error")]
        assert e.closed

    def test_a_timeout_says_the_merge_may_have_happened(self, monkeypatch, tmp_path):
        e = _Engine([_census([TWIN_WINS])], merges={("1.1.1", "1.1.2"): httpx.ReadTimeout("slow")})
        _patch(monkeypatch, e)

        result = _run("--apply")

        assert result.exit_code == 1
        assert "may have" in result.output.lower()
        line, = [l for l in _ledger(tmp_path) if l["type"] == "merge"]
        assert line["outcome"] == "error" and "may have" in line["detail"]


class TestLedger:
    def test_each_pair_records_winner_loser_uri_before_and_after_rule_and_whether_a_rewrite_follows(
        self, monkeypatch, tmp_path
    ):
        e = _Engine([_census([TWIN_WINS, STALE_WINS]), _census()])
        _patch(monkeypatch, e)

        assert _run("--apply").exit_code == 0

        lines = _ledger(tmp_path)
        pairs = {l["loser"]: l for l in lines if l["type"] == "merge"}
        s_loses = pairs["1.1.1"]
        assert s_loses["winner"] == "1.1.2" and s_loses["round"] == 1
        assert s_loses["loser_uri_before"] == f"nx-note://{_X}/t" and s_loses["loser_uri_after"] == ""
        assert s_loses["rule"] == "no row holds chunks: the twin wins by default"
        assert s_loses["outcome"] == "merged" and s_loses["rewrite_needed"] is False
        assert s_loses["links_remapped"] == 2
        w_loses = pairs["1.1.6"]
        assert w_loses["winner"] == "1.1.5"
        assert w_loses["loser_uri_before"] == f"chroma://{_Y}/u", "the twin's actual spelling, not the canonical one"
        assert w_loses["loser_uri_after"] == ""
        assert w_loses["rule"] == "the stale row holds the only chunks" and w_loses["rewrite_needed"] is True
        rewrite_rows = [l for l in lines if l["type"] == "rewrite"]
        assert [(r["table"], r["key"]) for r in rewrite_rows] == [("catalog_documents", "1.1.5"), ("document_aspects", "55")]
        closing, = [l for l in lines if l["type"] == "rewrite-result"]
        assert closing["winner"] == "1.1.5" and closing["followed"] is True

    def test_the_ledger_is_private_and_named_by_the_option(self, monkeypatch, tmp_path):
        _patch(monkeypatch, _Engine([_census([TWIN_WINS]), _census()]))
        target = tmp_path / "mine.jsonl"

        result = _run("--apply", "--ledger", str(target))

        assert result.exit_code == 0, result.output
        assert target.is_file() and str(target) in result.output
        if os.name == "posix":
            assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0, "owner-only, as the plain --apply ledger is"

    def test_lines_are_on_disk_before_the_next_pair_is_attempted(self, monkeypatch, tmp_path):
        seen = []

        class _Watching(_Engine):
            def merge_documents(self, duplicate, canonical):
                files = list(tmp_path.glob("*.jsonl"))
                seen.append(sum(1 for _ in files[0].read_text().splitlines()) if files else -1)
                return super().merge_documents(duplicate, canonical)

        e = _Watching([_census([TWIN_WINS, _entry("1.1.3", "twin", twin="1.1.4", target="v")]), _census()])
        _patch(monkeypatch, e)

        assert _run("--apply").exit_code == 0

        assert seen == [0, 1], "the first pair's line was durable before the second merge was asked for"

    def test_apply_json_reports_every_pair_and_the_ledger_path(self, monkeypatch, tmp_path):
        _patch(monkeypatch, _Engine([_census([TWIN_WINS, STALE_WINS]), _census()]))

        result = _run("--apply", "--json")

        assert result.exit_code == 0, result.output
        doc = json.loads(result.output[result.output.index("{"):])
        assert {p["loser"] for p in doc["pairs"]} == {"1.1.1", "1.1.6"}
        assert doc["merged"] == 2 and doc["refused"] == 0 and doc["rewrites_followed"] == 1
        assert (tmp_path / doc["ledger"]).is_file()
        assert doc["before"]["merge"] == 2 and doc["after"]["merge"] == 0
