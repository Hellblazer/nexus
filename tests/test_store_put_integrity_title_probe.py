# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``--store-put-integrity`` looks a chash-less note up by TITLE. nexus-1uekf.

The ghost branch promised a VERIFIED T3 lookup and, for a document with no
``meta.doc_id`` -- every legacy store_put note, RDR-145 Gap 1 -- declared a
ghost without looking anything up. 228 rows on 2026-08-27, all chash-less,
none probed. The verdict was right by accident: probed by title, 40/40 had
no chunks and 12/12 controls in the same collection had one each.

A knowledge note's chunks carry its title, so that is the key when there is
no chash. These pin all three outcomes of the title probe and that the
report says which key verified each ghost.
"""
from __future__ import annotations

import json

import pytest

from nexus.commands.catalog_cmds.doctor import doctor_cmd
from nexus.commands.catalog_cmds.integrity import _never_chunked_breakdown
from tests._catalog_fixture_ops import ActiveCatalog
from tests.test_catalog_doctor_new_checks import isolated_nexus, runner  # noqa: F401 — fixtures by name

COLLECTION = "knowledge__seeded__bge-base-en-v15-768__v1"


def _seed_title_only_note(title: str) -> str:
    """A store_put note whose chash is GONE from T3: knowledge collection, no
    file_path, a meta.doc_id (what makes it note-shaped and puts it in the
    check's population) that no chunk carries any more."""
    cat = ActiveCatalog()
    owner = cat.register_owner("knowledge", "curator", repo_hash="")
    t = cat.register(
        owner, title, content_type="knowledge",
        physical_collection=COLLECTION, chunk_count=0,
        meta={"doc_id": "0" * 64},
    )
    return str(t)


class _T3ByTitle:
    """No chunk is ever found by chash; presence is decided by title."""

    def __init__(self, titles_with_chunks: set[str], *, raise_for: set[str] = frozenset()):
        self._titles = titles_with_chunks
        self._raise = raise_for
        self.title_lookups: list[str] = []
        self.chash_lookups: list[str] = []

    def get_by_id(self, collection, doc_id):
        self.chash_lookups.append(doc_id)
        return None

    def find_ids_by_title(self, collection, title):
        self.title_lookups.append(title)
        if title in self._raise:
            raise TimeoutError("simulated T3 timeout")
        return ["chunk-1"] if title in self._titles else []


def _report(runner, monkeypatch, t3):
    monkeypatch.setattr("nexus.db.make_t3", lambda: t3)
    result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
    return result, json.loads(result.stdout)["store_put_integrity"]


def test_a_note_with_chunks_by_title_is_not_a_ghost(isolated_nexus, runner, monkeypatch) -> None:  # noqa: F811
    """The control: before this fix, THIS note was reported as a ghost too."""
    _seed_title_only_note("live-note")
    t3 = _T3ByTitle({"live-note"})

    result, payload = _report(runner, monkeypatch, t3)

    assert payload["checked"] == 1
    assert payload["ghosts"] == [], payload
    assert t3.title_lookups == ["live-note"], "the note was not looked up by title"


def test_a_note_with_no_chunks_by_title_is_a_verified_ghost(isolated_nexus, runner, monkeypatch) -> None:  # noqa: F811
    _seed_title_only_note("gone-note")
    t3 = _T3ByTitle(set())

    result, payload = _report(runner, monkeypatch, t3)

    assert len(payload["ghosts"]) == 1, payload
    ghost = payload["ghosts"][0]
    assert ghost["title"] == "gone-note"
    assert ghost["verified_by"] == "chash+title", "the report must say WHICH keys reached the verdict"
    assert t3.chash_lookups == ["0" * 64], "the chash is tried first"
    assert t3.title_lookups == ["gone-note"], "a chash miss must be followed by the title probe"


def test_a_failed_title_lookup_is_unverifiable_never_a_ghost(isolated_nexus, runner, monkeypatch) -> None:  # noqa: F811
    _seed_title_only_note("flaky-note")
    t3 = _T3ByTitle(set(), raise_for={"flaky-note"})

    result, payload = _report(runner, monkeypatch, t3)

    assert payload["ghosts"] == []
    assert len(payload["unverifiable"]) == 1
    assert "simulated T3 timeout" in payload["unverifiable"][0]["reason"]


def test_the_two_instruments_agree_on_the_same_note(isolated_nexus, runner, monkeypatch) -> None:  # noqa: F811
    """verify's never-chunked breakdown and store-put-integrity's ghost list
    must give the SAME answer for the same note, by the same lookup."""
    _seed_title_only_note("gone-note")
    _seed_title_only_note("live-note")
    t3 = _T3ByTitle({"live-note"})
    entries = ActiveCatalog().list_by_collection(COLLECTION)

    _, spi = _report(runner, monkeypatch, t3)
    breakdown = _never_chunked_breakdown(entries, None, t3=t3)

    assert {g["title"] for g in spi["ghosts"]} == {"gone-note"}
    assert breakdown["rdr145_ghost"]["total"] == 1
    assert breakdown["rdr145_exempt"]["total"] == 1
