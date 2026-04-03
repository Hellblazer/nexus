# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus.checkpoint — atomic checkpoint read/write/cleanup for PDF indexing."""
import json
import os
from pathlib import Path

import pytest

from nexus.checkpoint import (
    CheckpointData,
    checkpoint_path,
    delete_checkpoint,
    read_checkpoint,
    write_checkpoint,
)


@pytest.fixture
def ckpt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect checkpoint storage to a temp directory."""
    d = tmp_path / "checkpoints"
    d.mkdir()
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", d)
    return d


# ── CheckpointData construction ──────────────────────────────────────────────


def test_checkpoint_data_fields():
    ck = CheckpointData(
        pdf="/tmp/book.pdf",
        collection="knowledge__art",
        content_hash="abc123",
        chunks_upserted=500,
        total_chunks=2000,
        embedding_model="voyage-context-3",
    )
    assert ck.pdf == "/tmp/book.pdf"
    assert ck.collection == "knowledge__art"
    assert ck.content_hash == "abc123"
    assert ck.chunks_upserted == 500
    assert ck.total_chunks == 2000
    assert ck.embedding_model == "voyage-context-3"
    assert ck.timestamp != ""  # auto-populated


# ── Write + read round-trip ──────────────────────────────────────────────────


def test_write_and_read_roundtrip(ckpt_dir: Path):
    ck = CheckpointData(
        pdf="/data/book.pdf",
        collection="knowledge__art",
        content_hash="deadbeef",
        chunks_upserted=100,
        total_chunks=500,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck)
    loaded = read_checkpoint("deadbeef", "knowledge__art")
    assert loaded is not None
    assert loaded.pdf == "/data/book.pdf"
    assert loaded.collection == "knowledge__art"
    assert loaded.content_hash == "deadbeef"
    assert loaded.chunks_upserted == 100
    assert loaded.total_chunks == 500
    assert loaded.embedding_model == "voyage-context-3"


def test_read_nonexistent_returns_none(ckpt_dir: Path):
    assert read_checkpoint("nonexistent", "knowledge__art") is None


def test_write_overwrites_existing(ckpt_dir: Path):
    ck1 = CheckpointData(
        pdf="/data/book.pdf",
        collection="knowledge__art",
        content_hash="aaa",
        chunks_upserted=50,
        total_chunks=500,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck1)
    ck2 = CheckpointData(
        pdf="/data/book.pdf",
        collection="knowledge__art",
        content_hash="aaa",
        chunks_upserted=200,
        total_chunks=500,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck2)
    loaded = read_checkpoint("aaa", "knowledge__art")
    assert loaded is not None
    assert loaded.chunks_upserted == 200


# ── Atomic write ─────────────────────────────────────────────────────────────


def test_write_is_atomic(ckpt_dir: Path):
    """Checkpoint file should be written atomically (no partial writes)."""
    ck = CheckpointData(
        pdf="/data/book.pdf",
        collection="docs__test",
        content_hash="atomic",
        chunks_upserted=10,
        total_chunks=100,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck)
    p = checkpoint_path("atomic", "docs__test")
    assert p.exists()
    # Verify it's valid JSON
    data = json.loads(p.read_text())
    assert data["content_hash"] == "atomic"
    # No temp files left behind
    temps = list(ckpt_dir.glob("*.tmp"))
    assert temps == []


# ── Delete ───────────────────────────────────────────────────────────────────


def test_delete_checkpoint(ckpt_dir: Path):
    ck = CheckpointData(
        pdf="/data/book.pdf",
        collection="knowledge__art",
        content_hash="deleteme",
        chunks_upserted=10,
        total_chunks=100,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck)
    assert read_checkpoint("deleteme", "knowledge__art") is not None
    delete_checkpoint("deleteme", "knowledge__art")
    assert read_checkpoint("deleteme", "knowledge__art") is None


def test_delete_nonexistent_is_noop(ckpt_dir: Path):
    """Deleting a nonexistent checkpoint should not raise."""
    delete_checkpoint("ghost", "knowledge__art")


# ── Validation ───────────────────────────────────────────────────────────────


def test_read_rejects_hash_mismatch(ckpt_dir: Path):
    """If we ask for hash X but the file contains hash Y, return None."""
    ck = CheckpointData(
        pdf="/data/book.pdf",
        collection="knowledge__art",
        content_hash="original_hash",
        chunks_upserted=50,
        total_chunks=500,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck)
    # Read with a different hash — should return None
    assert read_checkpoint("different_hash", "knowledge__art") is None


def test_read_rejects_collection_mismatch(ckpt_dir: Path):
    """If checkpoint collection doesn't match requested collection, return None."""
    ck = CheckpointData(
        pdf="/data/book.pdf",
        collection="knowledge__art",
        content_hash="hash123",
        chunks_upserted=50,
        total_chunks=500,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck)
    # Read with same hash but different collection — should return None
    assert read_checkpoint("hash123", "docs__test") is None


def test_read_rejects_corrupted_json(ckpt_dir: Path):
    """Corrupted checkpoint file should return None, not raise."""
    p = checkpoint_path("corrupt", "docs__test")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{invalid json")
    assert read_checkpoint("corrupt", "docs__test") is None


# ── Path generation ──────────────────────────────────────────────────────────


def test_checkpoint_path_encodes_collection(ckpt_dir: Path):
    """Collection name with __ separator should be encoded safely in filename."""
    p = checkpoint_path("abc123", "knowledge__art")
    assert "abc123" in p.name
    assert "knowledge__art" in p.name
    assert p.suffix == ".json"


def test_different_collections_different_paths(ckpt_dir: Path):
    """Same hash + different collection = different checkpoint files."""
    p1 = checkpoint_path("abc123", "knowledge__art")
    p2 = checkpoint_path("abc123", "docs__test")
    assert p1 != p2
