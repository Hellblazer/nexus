# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path

import pytest

from nexus.checkpoint import (
    CheckpointData,
    checkpoint_path,
    delete_checkpoint,
    read_checkpoint,
    scan_orphaned_checkpoints,
    write_checkpoint,
)

_CK_DEFAULTS = dict(
    pdf="/data/book.pdf",
    collection="knowledge__art",
    embedding_model="voyage-context-3",
    total_chunks=500,
)


def _ck(**overrides: object) -> CheckpointData:
    return CheckpointData(**{**_CK_DEFAULTS, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def ckpt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "checkpoints"
    d.mkdir()
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", d)
    return d


# ── Construction ────────────────────────────────────────────────────────────

def test_checkpoint_data_fields() -> None:
    ck = _ck(content_hash="abc123", chunks_upserted=500)
    assert (ck.pdf, ck.collection, ck.content_hash) == ("/data/book.pdf", "knowledge__art", "abc123")
    assert (ck.chunks_upserted, ck.total_chunks, ck.embedding_model) == (500, 500, "voyage-context-3")
    assert ck.timestamp != ""


# ── Write + read round-trip ─────────────────────────────────────────────────

def test_write_and_read_roundtrip(ckpt_dir: Path) -> None:
    write_checkpoint(_ck(content_hash="deadbeef", chunks_upserted=100))
    loaded = read_checkpoint("deadbeef", "knowledge__art")
    assert loaded is not None
    assert (loaded.content_hash, loaded.chunks_upserted) == ("deadbeef", 100)


def test_read_nonexistent_returns_none(ckpt_dir: Path) -> None:
    assert read_checkpoint("nonexistent", "knowledge__art") is None


def test_write_overwrites_existing(ckpt_dir: Path) -> None:
    write_checkpoint(_ck(content_hash="aaa", chunks_upserted=50))
    write_checkpoint(_ck(content_hash="aaa", chunks_upserted=200))
    loaded = read_checkpoint("aaa", "knowledge__art")
    assert loaded is not None and loaded.chunks_upserted == 200


# ── Atomic write ────────────────────────────────────────────────────────────

def test_write_is_atomic(ckpt_dir: Path) -> None:
    write_checkpoint(_ck(collection="docs__test", content_hash="atomic", chunks_upserted=10, total_chunks=100))
    p = checkpoint_path("atomic", "docs__test")
    assert p.exists()
    assert json.loads(p.read_text())["content_hash"] == "atomic"
    assert list(ckpt_dir.glob("*.tmp")) == []


# ── Delete ──────────────────────────────────────────────────────────────────

def test_delete_checkpoint(ckpt_dir: Path) -> None:
    write_checkpoint(_ck(content_hash="deleteme", chunks_upserted=10, total_chunks=100))
    assert read_checkpoint("deleteme", "knowledge__art") is not None
    delete_checkpoint("deleteme", "knowledge__art")
    assert read_checkpoint("deleteme", "knowledge__art") is None


def test_delete_nonexistent_is_noop(ckpt_dir: Path) -> None:
    delete_checkpoint("ghost", "knowledge__art")


# ── Validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hash_arg,coll_arg", [
    ("different_hash", "knowledge__art"),
    ("hash123", "docs__test"),
])
def test_read_rejects_mismatch(ckpt_dir: Path, hash_arg: str, coll_arg: str) -> None:
    h = "original_hash" if hash_arg == "different_hash" else "hash123"
    write_checkpoint(_ck(content_hash=h, chunks_upserted=50))
    assert read_checkpoint(hash_arg, coll_arg) is None


def test_read_rejects_corrupted_json(ckpt_dir: Path) -> None:
    p = checkpoint_path("corrupt", "docs__test")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{invalid json")
    assert read_checkpoint("corrupt", "docs__test") is None


# ── Path generation ─────────────────────────────────────────────────────────

def test_checkpoint_path_encodes_collection(ckpt_dir: Path) -> None:
    p = checkpoint_path("abc123", "knowledge__art")
    assert "abc123" in p.name and "knowledge__art" in p.name and p.suffix == ".json"


def test_different_collections_different_paths(ckpt_dir: Path) -> None:
    assert checkpoint_path("abc123", "knowledge__art") != checkpoint_path("abc123", "docs__test")


# ── scan_orphaned_checkpoints ───────────────────────────────────────────────

def test_scan_empty_when_no_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", tmp_path / "nonexistent")
    assert scan_orphaned_checkpoints() == []


def test_scan_empty_when_no_checkpoints(ckpt_dir: Path) -> None:
    assert scan_orphaned_checkpoints() == []


@pytest.fixture
def _orphan_ck(ckpt_dir: Path, tmp_path: Path):
    """Write an orphaned checkpoint (PDF missing) and return (ckpt_dir, tmp_path)."""
    write_checkpoint(_ck(pdf=str(tmp_path / "vanished.pdf"), content_hash="orphan1",
                         chunks_upserted=10, total_chunks=100))
    return ckpt_dir, tmp_path


def test_scan_detects_orphan(_orphan_ck) -> None:
    orphans = scan_orphaned_checkpoints()
    assert len(orphans) == 1


def test_scan_does_not_report_live(ckpt_dir: Path, tmp_path: Path) -> None:
    live_pdf = tmp_path / "exists.pdf"
    live_pdf.write_bytes(b"%PDF-1.4")
    write_checkpoint(_ck(pdf=str(live_pdf), content_hash="live1", chunks_upserted=50, total_chunks=200))
    assert scan_orphaned_checkpoints() == []


def test_scan_mixes_live_and_orphaned(ckpt_dir: Path, tmp_path: Path) -> None:
    live_pdf = tmp_path / "live.pdf"
    live_pdf.write_bytes(b"%PDF-1.4")
    write_checkpoint(_ck(pdf=str(live_pdf), content_hash="live2", chunks_upserted=10, total_chunks=100))
    write_checkpoint(_ck(pdf=str(tmp_path / "gone.pdf"), content_hash="dead2",
                         chunks_upserted=5, total_chunks=50))
    orphans = scan_orphaned_checkpoints()
    assert len(orphans) == 1 and "dead2" in orphans[0].name


@pytest.mark.parametrize("delete,should_exist", [(True, False), (False, True)])
def test_scan_delete_flag(ckpt_dir: Path, tmp_path: Path,
                          delete: bool, should_exist: bool) -> None:
    write_checkpoint(_ck(pdf=str(tmp_path / "nope.pdf"), content_hash="todel",
                         chunks_upserted=10, total_chunks=100))
    ckpt_file = checkpoint_path("todel", "knowledge__art")
    orphans = scan_orphaned_checkpoints(delete=delete)
    assert len(orphans) == 1
    assert ckpt_file.exists() == should_exist


def test_scan_handles_corrupted_checkpoint(ckpt_dir: Path) -> None:
    bad_file = ckpt_dir / "corrupt-orphan.json"
    bad_file.write_text("{invalid json here")
    orphans = scan_orphaned_checkpoints()
    assert len(orphans) == 1 and orphans[0] == bad_file
