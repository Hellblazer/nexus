# SPDX-License-Identifier: AGPL-3.0-or-later
"""L1 context cache generator (RDR-072).

Generates a ~200 token knowledge map from taxonomy topic labels,
cached as a flat file for fast SessionStart hook injection (<1ms).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

_log = structlog.get_logger(__name__)

CONTEXT_L1_PATH = Path.home() / ".config" / "nexus" / "context_l1.txt"
_TOPICS_PER_PREFIX = 5


def generate_context_l1(
    taxonomy: "CatalogTaxonomy",
    *,
    output_path: Path | None = None,
) -> Path | None:
    """Generate L1 context cache from taxonomy topic labels.

    Queries root topics (parent_id IS NULL), groups by collection
    prefix (code/docs/knowledge/rdr), takes top 5 per prefix by
    doc_count. Writes atomically via temp file + os.replace.

    Returns the output path, or None if no topics exist.
    """
    output_path = output_path or CONTEXT_L1_PATH

    # Query root topics only (excludes children from split)
    with taxonomy._lock:
        rows = taxonomy.conn.execute(
            "SELECT collection, label, doc_count FROM topics "
            "WHERE parent_id IS NULL "
            "ORDER BY doc_count DESC"
        ).fetchall()

    if not rows:
        return None

    # Group by collection prefix
    prefixes: dict[str, list[tuple[str, int]]] = {}
    for collection, label, doc_count in rows:
        prefix = collection.split("__")[0] if "__" in collection else collection
        if prefix not in prefixes:
            prefixes[prefix] = []
        if len(prefixes[prefix]) < _TOPICS_PER_PREFIX:
            prefixes[prefix].append((label, doc_count))

    # Build text
    lines = ["## Knowledge Map", ""]
    for prefix in sorted(prefixes):
        items = ", ".join(f"{label} ({count})" for label, count in prefixes[prefix])
        lines.append(f"{prefix}: {items}")

    content = "\n".join(lines) + "\n"

    # Atomic write
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=output_path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        os.replace(tmp, output_path)
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        Path(tmp).unlink(missing_ok=True)
        raise

    _log.debug(
        "context_l1_generated",
        topics=len(rows),
        prefixes=len(prefixes),
        chars=len(content),
        path=str(output_path),
    )
    return output_path


def refresh_context_l1(
    *,
    db_path: Path | None = None,
    output_path: Path | None = None,
) -> Path | None:
    """Open T2, generate L1 context cache, close. Convenience wrapper."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    path = db_path or default_db_path()
    with T2Database(path) as db:
        return generate_context_l1(db.taxonomy, output_path=output_path)
