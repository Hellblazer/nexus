# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.22: ``nx catalog orphan-backfill link-existing`` links only manifest-less rows.

``--by title`` and ``--by content_hash`` used to append a whole title or hash group to one
document. A T3 chunk row is shared by every document that holds that text and its ``title`` and
``content_hash`` are the LAST writer's, so the group held chunks the target already owns (a second
manifest row for the same chunk at a new position) and chunks another document owns (appended to a
document that does not hold them), and it missed a chunk its own document owns but another writer
restamped. Now the verb considers only rows no live manifest names, skips a chash the target
already holds, and says what it skipped. A chunk whose only owner is a document in the trash is
manifest-less (the engine's reverse lookup sees live manifests only) and IS linked; that
disclosed behaviour is pinned here too, as is the append position when the target's manifest has
a position gap (an append upserts by position).

These tests drive the real verb against the engine substrate.
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from tests._chunk_seed import seed_chunks_direct
from tests.conftest import fake_credentials

pytestmark = [pytest.mark.integration]


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _row(chash: str, position: int) -> dict:
    return {
        "chash": chash, "position": position, "chunk_index": position,
        "section_title": "", "section_type": "", "page_number": None,
    }


class _World:
    """Documents D (target) and E (another owner) sharing one collection.

    ``c1`` is D's own chunk, ``c2`` is ownerless, ``c3`` is E's chunk. Every one is stamped with
    the group key (D's title / D's file hash), as a chunk another writer restamped is.
    ``d_rows`` is what D's manifest holds, ``(name, position)`` (positions may have a gap);
    ``trashed_owner`` adds ``c4``, held only by a document F that is then moved to the trash.
    """

    def __init__(
        self, tag: str, *, key: str,
        d_rows: tuple[tuple[str, int], ...] = (("c1", 0),), trashed_owner: bool = False,
    ) -> None:
        self.collection = f"docs__{tag}__bge-base-en-v15-768__v1"
        self.title_d = f"{tag} Document D"
        self.title_e = f"{tag} Document E"
        self.h_d, self.h_e = _chash(f"{tag}-file-d"), _chash(f"{tag}-file-e")
        names = ["c1", "c2", "c3"]
        names += [n for n, _ in d_rows if n not in names]
        if trashed_owner:
            names.append("c4")
        self.ch = {n: _chash(f"{tag}-{n}") for n in names}
        with HttpCatalogClient() as cat:
            owner = cat.register_owner(f"{tag}-owner", "repo", repo_hash=f"{tag}-hash")
            self.d = str(cat.register(
                owner, self.title_d, content_type="pdf", physical_collection=self.collection,
                chunk_count=1, meta={"content_hash": self.h_d},
            ))
            self.e = str(cat.register(
                owner, self.title_e, content_type="pdf", physical_collection=self.collection,
                chunk_count=1, meta={"content_hash": self.h_e},
            ))
            self.f = ""
            if trashed_owner:
                self.f = str(cat.register(
                    owner, f"{tag} Document F", content_type="pdf",
                    physical_collection=self.collection, chunk_count=1,
                    meta={"content_hash": _chash(f"{tag}-file-f")},
                ))
        stamp = {"title": self.title_d} if key == "title" else {"content_hash": self.h_d}
        seed_chunks_direct(
            self.collection,
            ids=list(self.ch.values()),
            documents=[f"{tag} {n} text" for n in self.ch],
            embed=True,
            metadatas=[
                {**stamp, "chunk_text_hash": self.ch[n], "chunk_index": i}
                for i, n in enumerate(self.ch)
            ],
        )
        self.reader = make_catalog_reader()
        writer = make_catalog_writer()
        writer.write_manifest(
            self.d, [_row(self.ch[n], pos) for n, pos in d_rows], collection=self.collection,
        )
        writer.write_manifest(self.e, [_row(self.ch["c3"], 0)], collection=self.collection)
        if trashed_owner:
            writer.write_manifest(self.f, [_row(self.ch["c4"], 0)], collection=self.collection)
            with HttpCatalogClient() as cat:
                assert cat.delete_document(self.f), "F must be in the trash before the verb runs"

    def manifest(self, tumbler: str) -> list[tuple[str, int]]:
        return [(r.chash, r.position) for r in self.reader.get_manifest(tumbler)]


def _link(w: _World, by: str, *, dry_run: bool):
    flag = "--dry-run" if dry_run else "--no-dry-run"
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        return CliRunner().invoke(
            main, ["catalog", "orphan-backfill", "link-existing", w.collection, "--by", by, flag],
        )


@pytest.mark.parametrize("by", ["title", "content_hash"])
def test_link_existing_appends_only_the_manifest_less_row(by: str) -> None:
    """D holds c1; c2 is ownerless; c3 is E's. All three carry D's key. Only c2 is appended, at
    the one new position after D's rows; c1 is not doubled and E's c3 is not taken."""
    w = _World(f"6pfgb22-{by.replace('_', '')}", key=by)

    result = _link(w, by, dry_run=False)

    assert result.exit_code == 0, result.output
    assert w.manifest(w.d) == [(w.ch["c1"], 0), (w.ch["c2"], 1)], result.output
    assert w.manifest(w.e) == [(w.ch["c3"], 0)], "another document's row is never taken"
    assert "Linked 1 chunks across 1 existing Documents" in result.output, result.output


@pytest.mark.parametrize("by", ["title", "content_hash"])
def test_dry_run_reports_the_rows_it_would_link_and_those_it_would_skip(by: str) -> None:
    w = _World(f"6pfgb22-dry-{by.replace('_', '')}", key=by)

    result = _link(w, by, dry_run=True)

    assert result.exit_code == 0, result.output
    out = result.output
    assert "Would link 1 chunks across 1 existing Documents" in out, out
    assert "Skipped 1 chunks the target Document already holds" in out, out
    assert "Skipped 1 chunks another live Document of this collection owns" in out, out
    assert "head_hash" not in out, "the join key is the file-content hash, never head_hash"
    assert w.manifest(w.d) == [(w.ch["c1"], 0)], "a dry run writes nothing"


@pytest.mark.parametrize("by", ["title", "content_hash"])
def test_a_chunk_owned_only_by_a_trashed_document_is_linked(by: str) -> None:
    """The disclosed boundary of "owned": the engine's reverse lookup counts LIVE manifests only,
    so c4, whose only holder F is in the trash, is manifest-less and goes to D beside c2. E's c3
    is still skipped. If this behaviour changes, change it on purpose."""
    w = _World(f"6pfgb22-trash-{by.replace('_', '')}", key=by, trashed_owner=True)

    result = _link(w, by, dry_run=False)

    assert result.exit_code == 0, result.output
    assert w.manifest(w.d) == [
        (w.ch["c1"], 0), (w.ch["c2"], 1), (w.ch["c4"], 2),
    ], result.output
    assert w.manifest(w.e) == [(w.ch["c3"], 0)]
    assert "Linked 2 chunks across 1 existing Documents" in result.output, result.output


@pytest.mark.parametrize("by", ["title", "content_hash"])
def test_a_gap_in_the_targets_positions_is_never_overwritten(by: str) -> None:
    """D holds rows at positions 0, 1 and 3. An append upserts by position, so a start at the row
    count (3) would replace the row at 3. The new row goes to 4."""
    w = _World(
        f"6pfgb22-gap-{by.replace('_', '')}", key=by,
        d_rows=(("c1", 0), ("c5", 1), ("c6", 3)),
    )

    result = _link(w, by, dry_run=False)

    assert result.exit_code == 0, result.output
    assert w.manifest(w.d) == [
        (w.ch["c1"], 0), (w.ch["c5"], 1), (w.ch["c6"], 3), (w.ch["c2"], 4),
    ], result.output
