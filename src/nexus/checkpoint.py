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

CHECKPOINT_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "nexus" / "checkpoints"


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
