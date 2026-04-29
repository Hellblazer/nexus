# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-u7r0: ``nx t3 prune-stale`` subcommand.

RDR-090 P1.4. Tests use a real T3Database backed by chromadb's
EphemeralClient + DefaultEmbeddingFunction so we exercise the full
delete-by-source machinery without Cloud credentials.

Contracts pinned here:

  - ``list_unique_source_paths`` deduplicates across multi-chunk
    same-source documents and skips empty/missing source_path values.
  - ``--dry-run`` (default) reports stale paths and chunk counts but
    does not delete anything.
  - ``--no-dry-run --confirm`` actually deletes; the two-flag dance
    is required.
  - ``--no-dry-run`` alone is treated as report-only (defensive).
  - ``--collection`` scopes to one collection.
  - Live source_paths (file present on disk) are never flagged.
  - Empty source_path (MCP-put chunks) never flagged.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def t3_db():
    """Real T3Database backed by an ephemeral local Chroma."""
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _seed_chunk(
    t3_db: T3Database,
    *,
    collection: str,
    chunk_id: str,
    content: str,
    source_path: str,
) -> None:
    """Insert one chunk into *collection* with the given metadata.

    Uses the underlying chroma collection ``add`` directly so we don't
    invoke the indexing pipeline. The EphemeralClient + DefaultEF
    handles the embedding inline.
    """
    col = t3_db._client.get_or_create_collection(collection)
    col.add(ids=[chunk_id], documents=[content], metadatas=[{"source_path": source_path}])


# ── list_unique_source_paths (db-level) ───────────────────────────────────


def test_list_unique_source_paths_dedupes_by_source(t3_db, tmp_path):
    """Multiple chunks with the same source_path collapse to one entry."""
    coll = "knowledge__test_dedupe"
    src = str(tmp_path / "doc-a.md")
    _seed_chunk(t3_db, collection=coll, chunk_id="c1", content="a1", source_path=src)
    _seed_chunk(t3_db, collection=coll, chunk_id="c2", content="a2", source_path=src)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c3", content="b1",
        source_path=str(tmp_path / "doc-b.md"),
    )
    paths = t3_db.list_unique_source_paths(coll)
    assert len(paths) == 2
    assert sorted(paths) == sorted([
        str(tmp_path / "doc-a.md"),
        str(tmp_path / "doc-b.md"),
    ])


def test_list_unique_source_paths_skips_empty(t3_db, tmp_path):
    """Chunks with empty source_path (MCP-put) are not returned."""
    coll = "knowledge__test_empty"
    _seed_chunk(t3_db, collection=coll, chunk_id="c1", content="x", source_path="")
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c2", content="y",
        source_path=str(tmp_path / "doc-real.md"),
    )
    paths = t3_db.list_unique_source_paths(coll)
    assert paths == [str(tmp_path / "doc-real.md")]


def test_list_unique_source_paths_missing_collection(t3_db):
    assert t3_db.list_unique_source_paths("knowledge__nonexistent") == []


# ── nx t3 prune-stale CLI (integration via patched make_t3) ───────────────


def test_prune_stale_dry_run_reports_stale_only(t3_db, tmp_path, runner):
    """Default dry-run reports stale source_paths + chunk counts; live
    paths and empty source_path chunks are not flagged.
    """
    coll = "knowledge__test_dryrun"
    real = tmp_path / "real.md"
    real.write_text("hello")
    stale = tmp_path / "ghost.md"  # never created
    _seed_chunk(t3_db, collection=coll, chunk_id="r1", content="x", source_path=str(real))
    _seed_chunk(t3_db, collection=coll, chunk_id="s1", content="y", source_path=str(stale))
    _seed_chunk(t3_db, collection=coll, chunk_id="s2", content="z", source_path=str(stale))

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "prune-stale", "-c", coll],
        )
    assert result.exit_code == 0, result.output
    assert str(stale) in result.output
    assert "2 chunk(s)" in result.output
    assert str(real) not in result.output
    assert "would delete" in result.output  # default report-only

    # Live chunk + stale chunks all still present (dry-run)
    col = t3_db._client.get_collection(coll)
    assert col.count() == 3


def test_prune_stale_no_confirm_treated_as_report_only(t3_db, tmp_path, runner):
    """``--no-dry-run`` without ``--confirm`` is the safer middle path:
    print the would-delete report, but do not modify T3.
    """
    coll = "knowledge__test_no_confirm"
    stale = tmp_path / "ghost.md"
    _seed_chunk(t3_db, collection=coll, chunk_id="s1", content="x", source_path=str(stale))

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "prune-stale", "-c", coll, "--no-dry-run"],
        )
    assert result.exit_code == 0
    assert "Add --confirm" in result.output
    assert "would delete" in result.output
    assert t3_db._client.get_collection(coll).count() == 1


def test_prune_stale_no_dry_run_with_confirm_actually_deletes(
    t3_db, tmp_path, runner,
):
    """``--no-dry-run --confirm`` removes the stale chunks; live ones survive."""
    coll = "knowledge__test_delete"
    real = tmp_path / "real.md"
    real.write_text("hi")
    stale = tmp_path / "ghost.md"
    _seed_chunk(t3_db, collection=coll, chunk_id="r1", content="x", source_path=str(real))
    _seed_chunk(t3_db, collection=coll, chunk_id="s1", content="y", source_path=str(stale))
    _seed_chunk(t3_db, collection=coll, chunk_id="s2", content="z", source_path=str(stale))

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["t3", "prune-stale", "-c", coll, "--no-dry-run", "--confirm"],
        )
    assert result.exit_code == 0, result.output
    assert "deleted 2 chunk(s)" in result.output

    col = t3_db._client.get_collection(coll)
    assert col.count() == 1
    surviving = col.get()
    assert surviving["ids"] == ["r1"]


def test_prune_stale_no_stale_paths_emits_clean_summary(
    t3_db, tmp_path, runner,
):
    """When every source_path is live, the summary reads 0/0 cleanly."""
    coll = "knowledge__test_clean"
    real = tmp_path / "doc.md"
    real.write_text("hi")
    _seed_chunk(t3_db, collection=coll, chunk_id="c1", content="x", source_path=str(real))

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(main, ["t3", "prune-stale", "-c", coll])
    assert result.exit_code == 0
    assert "0 chunk(s)" in result.output
    assert "0 stale path(s)" in result.output


def test_prune_stale_no_collections_message(t3_db, runner):
    """Empty Chroma → friendly 'no collections' message.

    chromadb.EphemeralClient is a process-singleton-ish, so other
    tests' collections may still be present. We delete all collections
    at the start so this test exercises the truly-empty path.
    """
    for raw in list(t3_db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            t3_db._client.delete_collection(name)
        except Exception:
            pass

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(main, ["t3", "prune-stale"])
    assert result.exit_code == 0
    assert "No collections" in result.output
