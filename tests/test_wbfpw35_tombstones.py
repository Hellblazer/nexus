# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus.catalog.tombstones, directly (nexus-wbfpw.35 fix round 3).

The module is what keeps the maintenance verbs from reviving a document somebody
deleted on purpose, and nothing tested it except through those verbs: four
mutants (no owner filter, no collection filter, no paging, no 404 handling)
survived the round-2 suite. Each test below names the mutation it exists to kill.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from nexus.catalog.tombstones import (
    TombstonedDocument,
    TombstoneGuardUnavailable,
    Tombstones,
    deleted_only_sources,
    has_live_document,
    read_tombstones,
)


def _row(i: int, **over: Any) -> dict[str, Any]:
    row = {
        "tumbler": f"1.2.{i}", "title": f"t{i}", "physical_collection": "docs__x",
        "content_type": "prose", "file_path": f"d/{i}.md", "deleted_at": "2026-10-01",
    }
    row.update(over)
    return row


class _Cat:
    """A trash lister over fixed rows with the engine's offset/limit paging."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    def list_trash(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        self.calls.append((limit, offset))
        return self.rows[offset:offset + limit]


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://engine/v1/catalog/trash")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))


class TestReadTombstones:
    def test_pages_the_whole_listing(self) -> None:
        # Kills: dropping the paging loop (only the first 300 rows come back).
        cat = _Cat([_row(i) for i in range(650)])

        tombs = read_tombstones(cat)

        assert len(tombs.docs) == 650
        assert [off for _lim, off in cat.calls] == [0, 300, 600]
        assert tombs.docs[-1].file_path == "d/649.md"

    def test_a_full_last_page_asks_for_one_more(self) -> None:
        cat = _Cat([_row(i) for i in range(600)])

        assert len(read_tombstones(cat).docs) == 600
        assert [off for _lim, off in cat.calls] == [0, 300, 600]

    def test_a_404_refuses_and_names_the_engine(self) -> None:
        # Kills: swallowing the 404 into an empty set (the verb would then run
        # as if nothing had been deleted).
        class _Gone:
            def list_trash(self, **_kw: Any) -> list:
                raise _http_error(404)

        with pytest.raises(TombstoneGuardUnavailable) as exc:
            read_tombstones(_Gone())

        assert "engine-service-v0.1.142" in exc.value.message

    def test_another_http_error_propagates_unchanged(self) -> None:
        class _Down:
            def list_trash(self, **_kw: Any) -> list:
                raise _http_error(503)

        with pytest.raises(httpx.HTTPStatusError):
            read_tombstones(_Down())

    def test_an_entry_without_the_file_path_key_refuses(self) -> None:
        # An engine that predates the field. Kills: reading a missing key as "no
        # path" and carrying on, which makes every path look undeleted.
        rows = [_row(0), {k: v for k, v in _row(1).items() if k != "file_path"}]

        with pytest.raises(TombstoneGuardUnavailable) as exc:
            read_tombstones(_Cat(rows))

        assert "file_path" in exc.value.message
        assert "engine-service-v0.1.142" in exc.value.message

    def test_a_null_file_path_is_a_paper_or_note_not_an_old_engine(self) -> None:
        # The new engine sends null for a document that never had a path. Kills:
        # treating null like an absent key, which refused every catalog holding
        # one tombstoned paper.
        tombs = read_tombstones(_Cat([_row(0, file_path=None), _row(1)]))

        assert [d.file_path for d in tombs.docs] == ["", "d/1.md"]

    def test_a_reader_with_no_trash_listing_refuses(self) -> None:
        with pytest.raises(TombstoneGuardUnavailable):
            read_tombstones(object())

    def test_owner_is_the_tumbler_minus_its_last_segment(self) -> None:
        tombs = read_tombstones(_Cat([_row(7)]))

        assert tombs.docs[0].owner == "1.2"
        assert tombs.docs[0].tumbler == "1.2.7"


def _doc(owner: str, path: str, collection: str = "docs__x") -> TombstonedDocument:
    return TombstonedDocument(
        tumbler=f"{owner}.9", owner=owner, file_path=path,
        physical_collection=collection, content_type="prose",
    )


class TestCoversPath:
    def test_matches_on_owner(self) -> None:
        # Kills: removing the owner filter.
        tombs = Tombstones([_doc("1.2", "a.md")])

        assert tombs.covers_path(("a.md",), owner="1.2")
        assert not tombs.covers_path(("a.md",), owner="1.3")

    def test_matches_on_collection(self) -> None:
        # Kills: removing the collection filter.
        tombs = Tombstones([_doc("1.2", "a.md", collection="docs__x")])

        assert tombs.covers_path(("a.md",), collection="docs__x")
        assert not tombs.covers_path(("a.md",), collection="docs__y")

    def test_a_basename_is_not_a_suffix_match(self) -> None:
        # Kills: matching a repo-relative file_path as a suffix of the chunk's
        # absolute source_path, which let a deleted README.md claim every other
        # README.md in the collection.
        tombs = Tombstones([_doc("1.2", "README.md")])

        assert tombs.covers_path(("README.md",))
        assert not tombs.covers_path(("/repo/pkg/sub/README.md",))

    def test_any_of_the_given_forms_may_match(self) -> None:
        tombs = Tombstones([_doc("1.2", "src/a.py")])

        assert tombs.covers_path(("/repo/src/a.py", "src/a.py"))

    def test_a_tombstone_with_no_path_covers_nothing(self) -> None:
        tombs = Tombstones([_doc("1.2", "")])

        assert not tombs.covers_path(("",))


class _LiveCat:
    """by_file_path over live rows keyed (owner, file_path); owner roots."""

    def __init__(self, live: set[tuple[str, str]], roots: dict[str, str] | None = None) -> None:
        self.live = live
        self.roots = roots or {}

    def by_file_path(self, owner: str, file_path: str) -> Any:
        return object() if (str(owner), file_path) in self.live else None

    def get_owner_by_prefix(self, prefix: str) -> dict:
        return {"repo_root": self.roots.get(prefix, "")}


class TestDeletedOnlySources:
    def test_a_path_with_a_live_document_too_is_kept(self) -> None:
        # A re-indexed file: tombstone AND live document at one (owner, path).
        # Kills: dropping the live check (the reindex then purged live chunks and
        # rebuilt none of them).
        cat = _LiveCat(live={("1.2", "a.md")})
        tombs = Tombstones([_doc("1.2", "a.md")])

        assert deleted_only_sources(cat, tombs, {"a.md"}, collection="docs__x") == []
        assert has_live_document(cat, tombs, ("a.md",))

    def test_a_tombstone_only_path_is_returned(self) -> None:
        cat = _LiveCat(live=set())
        tombs = Tombstones([_doc("1.2", "a.md")])

        assert deleted_only_sources(cat, tombs, {"a.md", "b.md"}, collection="docs__x") == ["a.md"]

    def test_an_absolute_source_matches_through_the_owners_repo_root(self) -> None:
        cat = _LiveCat(live=set(), roots={"1.2": "/repo"})
        tombs = Tombstones([_doc("1.2", "docs/a.md")])

        assert deleted_only_sources(
            cat, tombs, {"/repo/docs/a.md", "/other/docs/a.md"}, collection="docs__x",
        ) == ["/repo/docs/a.md"]

    def test_a_deleted_root_file_does_not_claim_a_same_named_file_deeper_in(self) -> None:
        cat = _LiveCat(live=set(), roots={"1.2": "/repo"})
        tombs = Tombstones([_doc("1.2", "README.md")])

        assert deleted_only_sources(
            cat, tombs, {"/repo/pkg/sub/README.md"}, collection="docs__x",
        ) == []

    def test_a_tombstone_in_another_collection_does_not_count(self) -> None:
        cat = _LiveCat(live=set())
        tombs = Tombstones([_doc("1.2", "a.md", collection="docs__other")])

        assert deleted_only_sources(cat, tombs, {"a.md"}, collection="docs__x") == []
