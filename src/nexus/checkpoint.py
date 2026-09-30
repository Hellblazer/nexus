# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkpoint files an older client left behind.

No indexing path writes or reads a checkpoint any more (RDR-223, nexus-z0o2p.15). The incremental
PDF path used to record the chunks it had uploaded so a failed run could resume after them; with
the multi-batch writer a chunk is written together with its owner row and the writer keeps no
state across processes, so a resumed run would replace the manifest with only the tail it sent.
Every run now starts at chunk 0 and re-sends what the engine already holds, which costs no
embedding (RDR-181).

What is left is the cleanup of the files an older client wrote: :func:`scan_orphaned_checkpoints`
(``nx doctor``, ``--clean-checkpoints``) and :func:`delete_checkpoint` (the incremental path drops
its predecessor's file for the document it is about to write).
"""
from __future__ import annotations

import json
import os
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
