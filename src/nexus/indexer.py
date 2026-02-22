# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code repository indexing pipeline."""
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.registry import RepoRegistry


def index_repository(repo: Path, registry: "RepoRegistry") -> None:
    """Index all files in *repo* into the T3 code__ collection.

    Marks status as 'indexing' while running, 'ready' on success.
    This is a placeholder — full implementation follows frecency + chunking pipeline.
    """
    registry.update(repo, status="indexing")
    try:
        _run_index(repo, registry)
        info = registry.get(repo)
        collection = info["collection"] if info else f"code__{repo.name}"
        registry.update(repo, status="ready")
    except Exception:
        registry.update(repo, status="error")
        raise


def _run_index(repo: Path, registry: "RepoRegistry") -> None:
    """Full indexing pipeline: frecency → chunking → embedding → T3 upsert."""
    import os

    from nexus.frecency import compute_frecency
    from nexus.chunker import chunk_file
    from nexus.ripgrep_cache import MAX_CACHE_SIZE, build_cache

    info = registry.get(repo)
    if info is None:
        return

    collection_name = info["collection"]

    # Gather all text files with frecency scores
    scored: list[tuple[float, Path]] = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.parts):
            continue  # Skip hidden dirs/files
        score = compute_frecency(repo, path)
        scored.append((score, path))

    # Sort descending by frecency
    scored.sort(key=lambda x: x[0], reverse=True)

    # Update ripgrep cache
    cache_path = Path.home() / ".config" / "nexus" / f"{repo.name}.cache"
    build_cache(repo, cache_path, scored)

    # Chunk and index (requires T3 credentials)
    voyage_key = os.environ.get("VOYAGE_API_KEY", "")
    chroma_key = os.environ.get("CHROMA_API_KEY", "")
    if not voyage_key or not chroma_key:
        return  # Skip embedding without credentials

    from nexus.db.t3 import T3Database

    tenant = os.environ.get("CHROMA_TENANT", "")
    database = os.environ.get("CHROMA_DATABASE", "default")
    db = T3Database(tenant=tenant, database=database, api_key=chroma_key, voyage_api_key=voyage_key)

    for _score, file in scored:
        try:
            content = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        chunks = chunk_file(file, content)
        for chunk in chunks:
            db.put(
                collection=collection_name,
                content=chunk["text"],
                title=f"{file.relative_to(repo)}:{chunk['line_start']}-{chunk['line_end']}",
                tags=[file.suffix.lstrip(".")],
                category="code",
                session_id="",
                source_agent="nexus-indexer",
                store_type="code",
                ttl_days=0,
            )
