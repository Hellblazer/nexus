# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog verify`` no longer calls a content-less note "legitimate". nexus-1uekf.

``rdr145_exempt`` was a SHAPE class -- knowledge collection, no file_path, no
source_uri -- rendered as "legitimate by design" with no T3 lookup, while
``--store-put-integrity`` called the identical 226 rows ghosts. Given a T3
handle, the breakdown now probes each exempt candidate by title and splits
it: chunks present -> still exempt (manifest-only gap, backfillable); no
chunks -> ``rdr145_ghost`` (nothing to backfill, title is the only record).
No handle (scoped mode, or T3 unreachable) -> the shape class, unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus.commands.catalog_cmds.integrity import _never_chunked_breakdown

COL = "knowledge__knowledge__voyage-context-3__v1"


def _note(tumbler: str, title: str) -> SimpleNamespace:
    return SimpleNamespace(
        tumbler=tumbler, title=title, physical_collection=COL,
        file_path="", source_uri="", chunk_count=0,
    )


class _T3:
    def __init__(self, titles_with_chunks: set[str], *, raise_for: set[str] = frozenset()):
        self._titles = titles_with_chunks
        self._raise = raise_for
        self.lookups: list[str] = []

    def find_ids_by_title(self, collection, title):
        self.lookups.append(title)
        if title in self._raise:
            raise ConnectionError("simulated")
        return ["c"] if title in self._titles else []


def test_without_a_t3_handle_the_shape_class_is_unchanged() -> None:
    out = _never_chunked_breakdown([_note("1.11.1", "a"), _note("1.11.2", "b")], None)
    assert out["rdr145_exempt"]["total"] == 2
    assert out["rdr145_ghost"]["total"] == 0
    assert out["total"] == 2


def test_with_a_t3_handle_content_less_notes_become_ghosts() -> None:
    t3 = _T3({"has-chunks"})
    out = _never_chunked_breakdown(
        [_note("1.11.1", "has-chunks"), _note("1.11.2", "gone"), _note("1.11.3", "also-gone")],
        None, t3=t3,
    )
    assert out["rdr145_exempt"]["total"] == 1
    assert out["rdr145_ghost"]["total"] == 2
    assert out["rdr145_ghost"]["by_collection"] == [{"physical_collection": COL, "count": 2}]
    assert set(out["rdr145_ghost"]["tumblers"]) == {"1.11.2", "1.11.3"}
    assert out["total"] == 3, "ghosts still count toward never-chunked"
    assert sorted(t3.lookups) == ["also-gone", "gone", "has-chunks"], "every exempt candidate is probed"


def test_a_failed_probe_stays_exempt_and_is_counted_as_unverified() -> None:
    t3 = _T3(set(), raise_for={"flaky"})
    out = _never_chunked_breakdown([_note("1.11.1", "flaky"), _note("1.11.2", "gone")], None, t3=t3)
    assert out["rdr145_exempt"]["total"] == 1, "an unprobeable note is never called a ghost"
    assert out["rdr145_ghost"]["total"] == 1
    assert out["rdr145_unverified"] == 1


def test_the_ghost_note_names_the_disposition() -> None:
    out = _never_chunked_breakdown([_note("1.11.1", "gone")], None, t3=_T3(set()))
    note = out["rdr145_ghost"]["note"]
    assert "title" in note and "backfill" in note
