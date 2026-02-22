# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code repository indexing pipeline."""
import fnmatch
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING


class CredentialsMissingError(RuntimeError):
    """Raised when T3 credentials are absent; prevents marking repo as ready."""

if TYPE_CHECKING:
    from nexus.registry import RepoRegistry

DEFAULT_IGNORE: list[str] = [
    "node_modules", "vendor", ".venv", "__pycache__", "dist", "build", ".git",
]

_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".cs": "csharp",
    ".sh": "bash",
    ".bash": "bash",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".r": "r",
    ".m": "objc",
    ".php": "php",
}


def _git_metadata(repo: Path) -> dict:
    """Collect git metadata for *repo*. Returns empty strings for missing values."""
    def run(args: list[str]) -> str:
        r = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""

    return {
        "git_project_name": repo.name,
        "git_branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_commit_hash": run(["git", "rev-parse", "HEAD"]),
        "git_remote_url": run(["git", "remote", "get-url", "origin"]),
    }


def _should_ignore(rel_path: Path, patterns: list[str]) -> bool:
    """Return True if any component of *rel_path* matches any of *patterns*."""
    for part in rel_path.parts:
        for pattern in patterns:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


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
    import hashlib as _hl

    from nexus.chunker import chunk_file
    from nexus.config import load_config
    from nexus.frecency import batch_frecency
    from nexus.ripgrep_cache import build_cache

    info = registry.get(repo)
    if info is None:
        return

    collection_name = info["collection"]

    # Load config (picks up per-repo .nexus.yml if present)
    cfg = load_config(repo_root=repo)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    ignore_patterns: list[str] = list(dict.fromkeys(DEFAULT_IGNORE + cfg_patterns))

    # Collect git metadata once for all chunks
    git_meta = _git_metadata(repo)

    # Compute frecency scores in a single git log pass
    frecency_map = batch_frecency(repo)

    # Gather all text files with frecency scores
    scored: list[tuple[float, Path]] = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(repo)
        if any(part.startswith(".") for part in rel.parts):
            continue  # Skip hidden dirs/files
        if _should_ignore(rel, ignore_patterns):
            continue  # Skip ignored patterns (node_modules, __pycache__, etc.)
        score = frecency_map.get(path, 0.0)
        scored.append((score, path))

    # Sort descending by frecency
    scored.sort(key=lambda x: x[0], reverse=True)

    # Update ripgrep cache — include path hash to avoid collisions between
    # repos with the same basename (e.g. two different "myproject/" dirs).
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

    from datetime import UTC, datetime as _dt
    from nexus.db import make_t3

    db = make_t3()
    now_iso = _dt.now(UTC).isoformat()

    for score, file in scored:
        try:
            content = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        # Compute content hash once per file (reused across all chunks of the file)
        content_hash = _hl.sha256(content.encode()).hexdigest()

        chunks = chunk_file(file, content)
        total_chunks = len(chunks)
        for i, chunk in enumerate(chunks):
            title = f"{file.relative_to(repo)}:{chunk['line_start']}-{chunk['line_end']}"
            doc_id = _hl.sha256(f"{collection_name}:{title}".encode()).hexdigest()[:16]
            ext = file.suffix.lower()
            metadata: dict = {
                # Core fields
                "title": title,
                "tags": ext.lstrip("."),
                "category": "code",
                "session_id": "",
                "source_agent": "nexus-indexer",
                "store_type": "code",
                "indexed_at": now_iso,
                "expires_at": "",
                "ttl_days": 0,
                "source_path": str(file.relative_to(repo)),
                # Line range — spec names
                "line_start": chunk["line_start"],
                "line_end": chunk["line_end"],
                "frecency_score": float(score),
                # Chunk fields from chunker
                "chunk_index": chunk.get("chunk_index", i),
                "chunk_count": chunk.get("chunk_count", total_chunks),
                "ast_chunked": chunk.get("ast_chunked", False),
                "filename": chunk.get("filename", str(file.name)),
                "file_extension": chunk.get("file_extension", ext),
                # Computed fields
                "programming_language": _EXT_TO_LANGUAGE.get(ext, ""),
                "corpus": collection_name,
                "embedding_model": "voyage-code-3",
                "content_hash": content_hash,
                # Git fields (computed once per index run)
                **git_meta,
            }
            db.upsert_chunks(
                collection=collection_name,
                ids=[doc_id],
                documents=[chunk["text"]],
                metadatas=[metadata],
            )
