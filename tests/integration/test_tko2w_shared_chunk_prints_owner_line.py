# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tko2w: a chunk two documents share prints one owner's file and that owner's lines.

The T3 chunk row is shared by every document that holds its text, so its ``line_start`` and
``line_end`` are the last writer's. Before engine-service-v0.1.170 a search printed the path of one
owner with the line numbers of another. The engine now names the row's owner (``doc_id``, the
collection's first owner by tumbler then position) and writes that owner's manifest span onto the
row; the client resolves the path from the same ``doc_id``. This drives the whole path: catalog
manifest, engine search, client display-path resolution, ``nx search`` output.
"""
from __future__ import annotations

import hashlib
import re

import pytest
from click.testing import CliRunner

from nexus.catalog.factory import make_catalog_writer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from tests._chunk_seed import seed_chunks_direct

pytestmark = [pytest.mark.integration]

_SHARED = (
    "def parse_ttl_expiry(days):\n"
    "    if days <= 0:\n"
    "        return None\n"
    "    return days * 86400\n"
)


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _row(chash: str, position: int, line_start: int, line_end: int) -> dict:
    return {
        "chash": chash, "position": position, "chunk_index": position,
        "section_title": "", "section_type": "", "page_number": None,
        "line_start": line_start, "line_end": line_end,
        "char_start": 0, "char_end": len(_SHARED),
    }


def test_a_shared_chunk_prints_the_engine_named_owners_path_and_lines() -> None:
    collection = "code__tko2w-owner-line__bge-base-en-v15-768__v1"
    chash = _chash(_SHARED)
    with HttpCatalogClient() as cat:
        owner = cat.register_owner("tko2w-owner", "repo", repo_hash="tko2w-hash")
        # Registered first, so the lower tumbler: the collection's first owner.
        first = str(cat.register(
            owner, "first.py", content_type="code", physical_collection=collection,
            chunk_count=1, file_path="src/first.py", source_uri="file:///repo/src/first.py",
            meta={"content_hash": _chash("first-file")},
        ))
        second = str(cat.register(
            owner, "second.py", content_type="code", physical_collection=collection,
            chunk_count=1, file_path="src/second.py", source_uri="file:///repo/src/second.py",
            meta={"content_hash": _chash("second-file")},
        ))
    # The chunk row carries the LAST writer's lines: second.py's, 40-43.
    seed_chunks_direct(
        collection, ids=[chash], documents=[_SHARED], embed=True,
        metadatas=[{
            "chunk_text_hash": chash, "content_hash": _chash("second-file"),
            "line_start": 40, "line_end": 43,
            "chunk_start_char": 0, "chunk_end_char": len(_SHARED),
        }],
    )
    writer = make_catalog_writer()
    writer.write_manifest(first, [_row(chash, 0, 7, 10)], collection=collection)
    writer.write_manifest(second, [_row(chash, 0, 40, 43)], collection=collection)

    result = CliRunner().invoke(
        main, ["search", "parse ttl expiry days", "--corpus", collection, "--n", "5"],
    )

    assert result.exit_code == 0, result.output
    cited = re.findall(r"(src/(?:first|second)\.py|/repo/src/(?:first|second)\.py):(\d+)", result.output)
    assert cited, f"no file:line in the output:\n{result.output}"
    paths = {p.rsplit("/", 1)[-1] for p, _ in cited}
    lines = sorted({int(n) for _, n in cited})
    assert paths == {"first.py"}, f"the first owner's file only:\n{result.output}"
    assert lines[0] == 7 and lines[-1] <= 10, (
        f"first.py's own lines 7-10, not the row's last-writer 40-43:\n{result.output}"
    )
