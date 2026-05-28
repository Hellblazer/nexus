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
    """Return collection names registered to a repo, or None for all.

    RDR-137 Phase 3.5 (nexus-tts0d.10): catalog-backed via
    :func:`nexus.repos.read_dual`.  The catalog is authoritative;
    when both catalog and ``repos.json`` carry an entry the dual-read
    shim logs disagreements at DEBUG so cutover-progress is observable.
    This closed the loop on nexus-9iw41 — the bug where a phantom
    ``docs__1-2188`` registered to the nexus repo while the catalog
    and chroma both had the real ``docs__1-1``.

    The ``rdr`` collection follows the existing indexer-name-resolver
    fallback when the catalog has no ``rdr__*`` row for the owner —
    a freshly-indexed repo with only ``code``+``docs`` content is the
    common case and the synthesized 4-segment name (RDR-103 Phase 5)
    is the right shape.
    """
    if repo_path is None:
        return None
    try:
        from nexus.catalog.catalog import Catalog  # noqa: PLC0415
        from nexus.config import catalog_path  # noqa: PLC0415
        from nexus.repos import read_dual  # noqa: PLC0415

        cat_dir = catalog_path()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        reg_path = _ctx_nexus_config_dir() / "repos.json"
        rec = read_dual(repo_path, cat=cat, registry_path=reg_path)
        if rec is None:
            return None
        colls: set[str] = set()
        if rec.code_collection:
            colls.add(rec.code_collection)
        if rec.docs_collection:
            colls.add(rec.docs_collection)
        if rec.rdr_collection:
            colls.add(rec.rdr_collection)
        else:
            # rdr collection not registered in catalog (common for
            # freshly-indexed code+docs-only repos). Fall back to the
            # indexer's name-resolver to synthesise the conformant
            # 4-segment shape (RDR-103 Phase 5).
            try:
                from nexus.indexer import _repo_collection_or_legacy  # noqa: PLC0415

                colls.add(_repo_collection_or_legacy(repo_path, "rdr"))
            except Exception:
                # Recoverable: rdr collection may legitimately not
                # exist for this repo. DEBUG-with-exc_info so the
                # signal is observable without flooding production
                # logs.
                _log.debug(
                    "repo_collection_or_legacy_failed",
                    repo_path=str(repo_path),
                    exc_info=True,
                )
        return colls if colls else None
    except Exception:
        # Outer — degrades the entire L1 context (taxonomy +
        # collections) silently. WARNING because a recurring failure
        # produces wrong LLM prompts with no signal.
        _log.warning(
            "discover_repo_collections_failed",
            exc_info=True,
        )
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

    # nexus-9iw41 defensive dedup: collapse rows with identical
    # (collection, label, doc_count) to one. Production has surfaced
    # degenerate clustering states where N root topics in a single
    # collection share an identical label+count (observed 2026-05-28:
    # 5 rows all "Project knowledge findings content (144)" in a
    # phantom docs__1-2188 collection, IDs 3401/3564/3727/3890/4053).
    # Without dedup those N rows would all show as separate top-N
    # entries in the Knowledge Map. Dedup is keyed on the exact tuple
    # so legitimate distinct labels are unaffected.
    seen: set[tuple[str, str, int]] = set()
    deduped: list[tuple[str, str, int]] = []
    for collection, label, doc_count in rows:
        key = (collection, label, doc_count)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((collection, label, doc_count))

    # Group by collection prefix, filtered by repo if specified
    prefixes: dict[str, list[tuple[str, int]]] = {}
    for collection, label, doc_count in deduped:
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
    from nexus.config import default_db_path
    from nexus.db.t2 import T2Database

    path = db_path or default_db_path()
    with T2Database(path) as db:  # epsilon-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
        return generate_context_l1(
            db.taxonomy, output_path=output_path, repo_path=repo_path,
        )
