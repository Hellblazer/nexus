# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkpoint files an older client left behind (RDR-223, nexus-z0o2p.15).

No indexing path writes or reads a checkpoint any more: the incremental PDF path restarts at chunk
0 and re-sends what the engine already holds (which costs no embedding), because the multi-batch
writer keeps no state across processes. What remains of ``nexus.checkpoint`` is the cleanup of the
files an older client wrote, which ``nx doctor`` scans and ``--clean-checkpoints`` deletes, and the
per-document delete the incremental path calls to drop its own predecessor's file. These tests
write the old on-disk shape directly.
"""
import json
from pathlib import Path

import pytest

from nexus.checkpoint import (
    checkpoint_path,
    delete_checkpoint,
    scan_orphaned_checkpoints,
)


def _old_client_checkpoint(dir_: Path, **overrides: object) -> Path:
    """Write a checkpoint file in the shape the removed ``write_checkpoint`` produced."""
    fields = {
        "pdf": "/data/book.pdf",
        "collection": "knowledge__art",
        "content_hash": "deadbeef",
        "chunks_upserted": 10,
        "total_chunks": 100,
        "embedding_model": "model-ctx",
        "timestamp": "2026-01-01T00:00:00+00:00",
        **overrides,
    }
    path = dir_ / f"{fields['content_hash']}-{fields['collection']}.json"
    path.write_text(json.dumps(fields))
    return path


@pytest.fixture
def ckpt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "checkpoints"
    d.mkdir()
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", d)
    return d


# ── Delete ──────────────────────────────────────────────────────────────────

def test_delete_checkpoint(ckpt_dir: Path) -> None:
    f = _old_client_checkpoint(ckpt_dir, content_hash="deleteme")
    assert f.exists() and f == checkpoint_path("deleteme", "knowledge__art")
    delete_checkpoint("deleteme", "knowledge__art")
    assert not f.exists()


def test_delete_nonexistent_is_noop(ckpt_dir: Path) -> None:
    delete_checkpoint("ghost", "knowledge__art")


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
    """An orphaned checkpoint (PDF missing); returns (ckpt_dir, tmp_path)."""
    _old_client_checkpoint(ckpt_dir, pdf=str(tmp_path / "vanished.pdf"), content_hash="orphan1")
    return ckpt_dir, tmp_path


def test_scan_detects_orphan(_orphan_ck) -> None:
    orphans = scan_orphaned_checkpoints()
    assert len(orphans) == 1


def test_scan_does_not_report_live(ckpt_dir: Path, tmp_path: Path) -> None:
    live_pdf = tmp_path / "exists.pdf"
    live_pdf.write_bytes(b"%PDF-1.4")
    _old_client_checkpoint(ckpt_dir, pdf=str(live_pdf), content_hash="live1")
    assert scan_orphaned_checkpoints() == []


def test_scan_mixes_live_and_orphaned(ckpt_dir: Path, tmp_path: Path) -> None:
    live_pdf = tmp_path / "live.pdf"
    live_pdf.write_bytes(b"%PDF-1.4")
    _old_client_checkpoint(ckpt_dir, pdf=str(live_pdf), content_hash="live2")
    _old_client_checkpoint(ckpt_dir, pdf=str(tmp_path / "gone.pdf"), content_hash="dead2")
    orphans = scan_orphaned_checkpoints()
    assert len(orphans) == 1 and "dead2" in orphans[0].name


@pytest.mark.parametrize("delete,should_exist", [(True, False), (False, True)])
def test_scan_delete_flag(ckpt_dir: Path, tmp_path: Path,
                          delete: bool, should_exist: bool) -> None:
    ckpt_file = _old_client_checkpoint(ckpt_dir, pdf=str(tmp_path / "nope.pdf"), content_hash="todel")
    orphans = scan_orphaned_checkpoints(delete=delete)
    assert len(orphans) == 1
    assert ckpt_file.exists() == should_exist


def test_scan_handles_corrupted_checkpoint(ckpt_dir: Path) -> None:
    bad_file = ckpt_dir / "corrupt-orphan.json"
    bad_file.write_text("{invalid json here")
    orphans = scan_orphaned_checkpoints()
    assert len(orphans) == 1 and orphans[0] == bad_file
