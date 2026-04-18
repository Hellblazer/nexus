# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkpoint support for incremental PDF indexing.

Tracks embed/upsert progress so a failed extraction can resume from the
last successfully upserted batch rather than re-processing the entire
document.  Checkpoints are written atomically (tempfile + os.rename) to
avoid partial writes on crash.

Design note (RDR-047, S2 resolution): The full document is always extracted
and chunked in one pass.  Only the embed/upsert phase is batched with
checkpoints.  This means there are no cross-batch chunk boundary issues —
chunking always operates on the complete text.

Design note (RDR-047, S1 resolution): If the process crashes between a
successful upsert and the checkpoint write, the next run will re-embed and
re-upsert the same chunks.  This is safe because ChromaDB upsert is
idempotent (put-if-absent-or-update).  The cost is at most one batch of
re-embedding (~32 chunks, ~2 min).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

def _checkpoint_dir_at_import() -> Path:
    """Resolve at import time — honours NEXUS_CONFIG_DIR, then XDG, then home."""
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override) / "checkpoints"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "nexus" / "checkpoints"


CHECKPOINT_DIR = _checkpoint_dir_at_import()


def checkpoint_path(content_hash: str, collection: str) -> Path:
    """Return the filesystem path for a checkpoint file."""
    return CHECKPOINT_DIR / f"{content_hash}-{collection}.json"


@dataclass
class CheckpointData:
    """State of an incremental PDF embed/upsert operation."""

    pdf: str
    collection: str
    content_hash: str
    chunks_upserted: int
    total_chunks: int
    embedding_model: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def write_checkpoint(data: CheckpointData) -> None:
    """Atomically write a checkpoint to disk."""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    target = checkpoint_path(data.content_hash, data.collection)
    payload = json.dumps(
        {
            "pdf": data.pdf,
            "collection": data.collection,
            "content_hash": data.content_hash,
            "chunks_upserted": data.chunks_upserted,
            "total_chunks": data.total_chunks,
            "embedding_model": data.embedding_model,
            "timestamp": data.timestamp,
        },
        indent=2,
    )
    # Atomic write: write to temp file in same directory, then rename.
    fd, tmp = tempfile.mkstemp(dir=CHECKPOINT_DIR, suffix=".tmp")
    try:
        os.write(fd, payload.encode())
        os.close(fd)
        os.rename(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_checkpoint(content_hash: str, collection: str) -> CheckpointData | None:
    """Read and validate a checkpoint.  Returns None if missing, corrupt, or mismatched."""
    target = checkpoint_path(content_hash, collection)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        _log.warning("checkpoint_corrupt", path=str(target))
        return None
    # Validate hash and collection match
    if data.get("content_hash") != content_hash:
        return None
    if data.get("collection") != collection:
        return None
    return CheckpointData(
        pdf=data["pdf"],
        collection=data["collection"],
        content_hash=data["content_hash"],
        chunks_upserted=data["chunks_upserted"],
        total_chunks=data["total_chunks"],
        embedding_model=data["embedding_model"],
        timestamp=data.get("timestamp", ""),
    )


def delete_checkpoint(content_hash: str, collection: str) -> None:
    """Delete a checkpoint file if it exists."""
    target = checkpoint_path(content_hash, collection)
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def scan_orphaned_checkpoints(
    *,
    delete: bool = False,
) -> list[Path]:
    """Scan the checkpoint directory for orphaned checkpoint files.

    A checkpoint is considered orphaned when the PDF it references no longer
    exists on disk.  This covers two cases:
    - The PDF was moved or deleted after indexing started.
    - The checkpoint was written for a path that was later cleaned up.

    Content-hash verification is intentionally skipped here: we only check
    file existence because re-hashing every PDF just for a doctor check would
    be prohibitively expensive.

    Parameters
    ----------
    delete:
        When True, delete each orphaned checkpoint file from disk.
        When False (default), return the list without modifying anything.

    Returns
    -------
    list[Path]
        Paths of checkpoint files that are orphaned.
    """
    if not CHECKPOINT_DIR.exists():
        return []

    orphans: list[Path] = []
    for ckpt_file in CHECKPOINT_DIR.glob("*.json"):
        try:
            raw = json.loads(ckpt_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            _log.debug("checkpoint_scan_unreadable", path=str(ckpt_file), error=str(exc))
            # Unreadable checkpoint — treat as orphaned
            orphans.append(ckpt_file)
            if delete:
                try:
                    ckpt_file.unlink()
                    _log.info("orphaned_checkpoint_deleted", path=str(ckpt_file), reason="unreadable")
                except FileNotFoundError:
                    pass
            continue

        pdf_path_str = raw.get("pdf", "")
        if not pdf_path_str or not Path(pdf_path_str).exists():
            orphans.append(ckpt_file)
            _log.debug(
                "orphaned_checkpoint_detected",
                path=str(ckpt_file),
                pdf=pdf_path_str or "(missing key)",
            )
            if delete:
                try:
                    ckpt_file.unlink()
                    _log.info("orphaned_checkpoint_deleted", path=str(ckpt_file), pdf=pdf_path_str)
                except FileNotFoundError:
                    pass

    return orphans
