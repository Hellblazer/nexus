# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code repository indexing pipeline."""
from pathlib import Path
from typing import TYPE_CHECKING


class CredentialsMissingError(RuntimeError):
    """Raised when T3 credentials are absent; prevents marking repo as ready."""

if TYPE_CHECKING:
    from nexus.registry import RepoRegistry


def index_repository(repo: Path, registry: "RepoRegistry") -> None:
    """Index all files in *repo* into the T3 code__ collection.

    Marks status as 'indexing' while running, 'ready' on success,
    'pending_credentials' when T3 credentials are absent.
    """
    registry.update(repo, status="indexing")
    try:
        _run_index(repo, registry)
        registry.update(repo, status="ready")
    except CredentialsMissingError:
        registry.update(repo, status="pending_credentials")
        # Do NOT re-raise: callers (polling) treat non-exception return as success;
        # raising here lets polling avoid recording head_hash (see polling.py).
        raise
    except Exception:
        registry.update(repo, status="error")
        raise


def _run_index(repo: Path, registry: "RepoRegistry") -> None:
    """Full indexing pipeline: frecency → chunking → embedding → T3 upsert."""
    import os

    from nexus.chunker import chunk_file
    from nexus.frecency import compute_frecency
    from nexus.ripgrep_cache import build_cache

    info = registry.get(repo)
    if info is None:
        return

    collection_name = info["collection"]

    # Gather all text files with frecency scores
    scored: list[tuple[float, Path]] = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith(".") for part in path.parts):
            continue  # Skip hidden dirs/files
        score = compute_frecency(repo, path)
        scored.append((score, path))

    # Sort descending by frecency
    scored.sort(key=lambda x: x[0], reverse=True)

    # Update ripgrep cache — include path hash to avoid collisions between
    # repos with the same basename (e.g. two different "myproject/" dirs).
    import hashlib as _hl
    _repo_hash = _hl.sha256(str(repo).encode()).hexdigest()[:8]
    cache_path = Path.home() / ".config" / "nexus" / f"{repo.name}-{_repo_hash}.cache"
    build_cache(repo, cache_path, scored)

    # Chunk and index (requires T3 credentials)
    from nexus.config import get_credential
    voyage_key = get_credential("voyage_api_key")
    chroma_key = get_credential("chroma_api_key")
    if not voyage_key or not chroma_key:
        raise CredentialsMissingError(
            f"T3 credentials missing for repo '{repo.name}' "
            f"(voyage_api_key={'set' if voyage_key else 'missing'}, "
            f"chroma_api_key={'set' if chroma_key else 'missing'})"
        )

    from nexus.db.t3 import T3Database

    db = T3Database(
        tenant=get_credential("chroma_tenant"),
        database=get_credential("chroma_database") or "default",
        api_key=chroma_key,
        voyage_api_key=voyage_key,
    )

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
                tags=file.suffix.lstrip("."),
                category="code",
                session_id="",
                source_agent="nexus-indexer",
                store_type="code",
                ttl_days=0,
            )
