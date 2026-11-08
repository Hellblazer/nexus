# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog repair-note-uris`` against a real engine (nexus-bivz3.24).

One test, one tenant: the verb, the HTTP client and the engine's census and rewrite, end to end. The census
numbers and the three-table rewrite are pinned exhaustively in Java (``NoteUriRepairTest``); this proves the
wiring: the dry run writes nothing, ``--apply`` rewrites the rewrite candidate and only it, a merge candidate
and a tombstoned stale row are counted and left alone, and a re-put under the note's collection finds the
repaired row.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus import corpus
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.cli import main

_MODEL = "bge-base-en-v15-768"


@pytest.fixture(autouse=True)
def _clear_registration_cache():
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.mark.integration
def test_dry_run_writes_nothing_and_apply_rewrites_only_the_rewrite_candidate(t2_service_env, tmp_path):
    reader = make_catalog_reader()
    writer = make_catalog_writer()
    curator = writer.register_owner("knowledge", "curator")
    live = f"knowledge__repair-live__{_MODEL}__v1"
    left = f"knowledge__repair-left__{_MODEL}__v1"
    for name, subject in ((live, "repair-live"), (left, "repair-left")):
        writer.register_collection(name, content_type="knowledge", owner_id=subject, embedding_model=_MODEL)

    def note(title: str, uri: str) -> str:
        return str(writer.register(
            owner=curator, title=title, content_type="knowledge", physical_collection=live, source_uri=uri))

    rewrite = note("alpha", f"nx-note://{left}/alpha")                # no twin: rewrite candidate
    merge = note("gamma", f"nx-note://{left}/gamma")                  # a live twin holds the target
    twin = note("gamma-twin", f"nx-note://{live}/gamma")
    dead = note("dead", f"nx-note://{left}/dead")                     # stale, then tombstoned
    assert writer.delete_document(dead)
    note("fine", f"nx-note://{live}/fine")                            # not stale

    runner = CliRunner()
    dry = runner.invoke(main, ["catalog", "repair-note-uris", "--json"])
    assert dry.exit_code == 0, dry.output
    report = json.loads(dry.output)
    assert report["apply"] is False
    assert (report["stale_live"], report["rewrite"], report["merge"], report["merge_twin"],
            report["refused"], report["tombstoned"]) == (2, 1, 1, 1, 0, 1)
    assert [r["tumbler"] for r in report["sample"]["merge"]] == [merge]
    assert report["sample"]["merge"][0]["twin"] == twin
    assert reader.by_source_uri(f"nx-note://{left}/alpha") is not None, "a dry run writes nothing"
    assert reader.by_source_uri(f"nx-note://{live}/alpha") is None

    undo = tmp_path / "undo.jsonl"
    applied = runner.invoke(main, ["catalog", "repair-note-uris", "--apply", "--json", "--ledger", str(undo)])
    assert applied.exit_code == 0, applied.output
    # the first write of a process logs the production-write guard's opt-in notice to stdout ahead of the JSON
    done = json.loads(applied.output[applied.output.index("{"):])
    assert done["rewritten"] == 1 and done["batches"] == 1
    assert done["after"]["rewrite"] == 0 and done["after"]["merge"] == 1 and done["after"]["tombstoned"] == 1
    assert done["ledger"] == str(undo)
    lines = [json.loads(line) for line in undo.read_text().splitlines()]
    assert lines == [{"batch": 1, "table": "catalog_documents", "key": rewrite,
                      "old_uri": f"nx-note://{left}/alpha", "new_uri": f"nx-note://{live}/alpha"}], \
        "the ledger names the one row changed, with the URI it had and the one it has"

    found = reader.by_source_uri(f"nx-note://{live}/alpha")
    assert found is not None and str(found.tumbler) == rewrite, "a re-put under the note's collection finds it"
    assert reader.by_source_uri(f"nx-note://{left}/alpha") is None
    still = reader.by_source_uri(f"nx-note://{left}/gamma")
    assert still is not None and str(still.tumbler) == merge, "a merge candidate is not touched"
    assert not getattr(still, "alias_of", ""), "and nothing was merged"


@pytest.mark.integration
def test_merge_pass_chunks_decide_the_twin_by_default_the_stale_row_when_it_holds_the_only_chunks(
    t2_service_env, tmp_path
):
    """``--merge`` end to end (Sam's ruling "Chunks decide"): a dry run decides and writes nothing; ``--apply``
    merges the loser into the winner, rewrites a stale winner, and the ledger says what happened to each pair."""
    reader = make_catalog_reader()
    writer = make_catalog_writer()
    curator = writer.register_owner("knowledge", "curator")
    live = f"knowledge__merge-live__{_MODEL}__v1"
    left = f"knowledge__merge-left__{_MODEL}__v1"
    for name, subject in ((live, "merge-live"), (left, "merge-left")):
        writer.register_collection(name, content_type="knowledge", owner_id=subject, embedding_model=_MODEL)

    def note(title: str, uri: str, chunks: int = 0) -> str:
        return str(writer.register(
            owner=curator, title=title, content_type="knowledge", physical_collection=live, source_uri=uri,
            chunk_count=chunks))

    s1 = note("p1-stale", f"nx-note://{left}/p1")                 # neither holds chunks: the twin wins
    w1 = note("p1-twin", f"nx-note://{live}/p1")
    s2 = note("p2-stale", f"nx-note://{left}/p2", chunks=3)       # the stale row holds the only chunks: it wins
    w2 = note("p2-twin", f"nx-note://{live}/p2")

    runner = CliRunner()
    dry = runner.invoke(main, ["catalog", "repair-note-uris", "--merge", "--json"])
    assert dry.exit_code == 0, dry.output
    plan = {d["winner"]: d for d in json.loads(dry.output)["plan"]}
    assert set(plan) == {w1, s2}
    assert plan[w1]["losers"] == [s1] and plan[w1]["rewrite_needed"] is False
    assert plan[s2]["losers"] == [w2] and plan[s2]["rewrite_needed"] is True
    assert plan[s2]["rule"] == "the stale row holds the only chunks"
    assert reader.by_source_uri(f"nx-note://{left}/p1") is not None, "a dry run writes nothing"
    assert reader.by_source_uri(f"nx-note://{left}/p2") is not None

    undo = tmp_path / "merge.jsonl"
    done = runner.invoke(main, ["catalog", "repair-note-uris", "--merge", "--apply", "--json", "--ledger", str(undo)])
    assert done.exit_code == 0, done.output
    report = json.loads(done.output[done.output.index("{"):])
    assert (report["merged"], report["refused"], report["errors"], report["rewrites_followed"]) == (2, 0, 0, 1)
    assert report["after"]["merge"] == 0

    assert str(reader.by_source_uri(f"nx-note://{live}/p1").tumbler) == w1, "the twin keeps its URI"
    assert reader.by_source_uri(f"nx-note://{left}/p1") is None, "the loser's URI is cleared"
    assert str(reader.by_source_uri(f"nx-note://{live}/p2").tumbler) == s2, "the stale winner was rewritten"
    assert reader.by_source_uri(f"nx-note://{left}/p2") is None
    shown = runner.invoke(main, ["catalog", "show", w2, "--json"])
    assert json.loads(shown.stdout)["tumbler"] == s2, "the twin that lost is an alias of the stale winner"

    lines = [json.loads(line) for line in undo.read_text().splitlines()]
    pairs = {l["loser"]: l for l in lines if l["type"] == "merge"}
    assert pairs[s1]["winner"] == w1 and pairs[s1]["loser_uri_before"] == f"nx-note://{left}/p1"
    assert pairs[s1]["loser_uri_after"] == "" and pairs[s1]["outcome"] == "merged"
    assert pairs[w2]["winner"] == s2 and pairs[w2]["loser_uri_before"] == f"nx-note://{live}/p2"
    assert [l for l in lines if l["type"] == "rewrite-result"][0]["followed"] is True
    assert any(l["type"] == "rewrite" and l["table"] == "catalog_documents" and l["key"] == s2 for l in lines)
