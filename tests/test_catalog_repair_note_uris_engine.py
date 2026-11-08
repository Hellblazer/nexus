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
    report = json.loads(dry.stdout)
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
