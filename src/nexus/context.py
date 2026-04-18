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

def _ctx_nexus_config_dir() -> Path:
    """Resolve the config dir at import time, honouring NEXUS_CONFIG_DIR."""
    import os as _os

    override = _os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


CONTEXT_L1_DIR = _ctx_nexus_config_dir() / "context"
# Legacy single-file path (kept for backward compat)
CONTEXT_L1_PATH = _ctx_nexus_config_dir() / "context_l1.txt"


def _context_path_for_repo(repo_path: Path | None) -> Path:
    """Return per-repo cache file path, or global fallback."""
    if repo_path is None:
        return CONTEXT_L1_PATH
    import hashlib
    repo_hash = hashlib.sha1(str(repo_path.resolve()).encode()).hexdigest()[:8]
    return CONTEXT_L1_DIR / f"{repo_path.name}-{repo_hash}.txt"
_TOPICS_PER_PREFIX = 5


def _repo_collections(repo_path: Path | None) -> set[str] | None:
    """Return collection names registered to a repo, or None for all."""
    if repo_path is None:
        return None
    try:
        from nexus.registry import RepoRegistry

        reg_path = _ctx_nexus_config_dir() / "repos.json"
        if not reg_path.exists():
            return None
        reg = RepoRegistry(reg_path)
        entry = reg.get(repo_path)
        if not entry:
            return None
        colls = set()
        for key in ("collection", "docs_collection"):
            if entry.get(key):
                colls.add(entry[key])
        # Also include rdr__ and knowledge__ for this repo
        # (they use the same hash suffix)
        for coll_name in colls.copy():
            suffix = coll_name.split("__", 1)[1] if "__" in coll_name else ""
            if suffix:
                colls.add(f"rdr__{suffix}")
        return colls if colls else None
    except Exception:
        return None


def generate_context_l1(
    taxonomy: "CatalogTaxonomy",
    *,
    output_path: Path | None = None,
    repo_path: Path | None = None,
) -> Path | None:
    """Generate L1 context cache from taxonomy topic labels.

    Queries root topics (parent_id IS NULL), groups by collection
    prefix (code/docs/knowledge/rdr), takes top 5 per prefix by
    doc_count. Writes atomically via temp file + os.replace.

    When ``repo_path`` is provided, only topics from collections
    registered to that repo are included. Otherwise all collections.

    Returns the output path, or None if no topics exist.
    """
    output_path = output_path or _context_path_for_repo(repo_path)
    allowed = _repo_collections(repo_path)

    # Query root topics only (excludes children from split)
    with taxonomy._lock:
        rows = taxonomy.conn.execute(
            "SELECT collection, label, doc_count FROM topics "
            "WHERE parent_id IS NULL "
            "ORDER BY doc_count DESC"
        ).fetchall()

    if not rows:
        return None

    # Group by collection prefix, filtered by repo if specified
    prefixes: dict[str, list[tuple[str, int]]] = {}
    for collection, label, doc_count in rows:
        if allowed is not None and collection not in allowed:
            continue
        prefix = collection.split("__")[0] if "__" in collection else collection
        if prefix not in prefixes:
            prefixes[prefix] = []
        if len(prefixes[prefix]) < _TOPICS_PER_PREFIX:
            prefixes[prefix].append((label, doc_count))

    if not prefixes:
        return None

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


def read_context_l1(repo_path: Path | None = None) -> str:
    """Read the cached L1 context for a repo. Returns empty string if missing."""
    path = _context_path_for_repo(repo_path)
    if path.exists():
        return path.read_text().strip()
    # Fallback to legacy global file
    if CONTEXT_L1_PATH.exists():
        return CONTEXT_L1_PATH.read_text().strip()
    return ""


def refresh_context_l1(
    *,
    db_path: Path | None = None,
    output_path: Path | None = None,
    repo_path: Path | None = None,
) -> Path | None:
    """Open T2, generate L1 context cache, close. Convenience wrapper."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    path = db_path or default_db_path()
    with T2Database(path) as db:
        return generate_context_l1(
            db.taxonomy, output_path=output_path, repo_path=repo_path,
        )
