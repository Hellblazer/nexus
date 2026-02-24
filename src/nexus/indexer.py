# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code repository indexing pipeline."""
import fnmatch
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus.corpus import index_model_for_collection
from nexus.errors import CredentialsMissingError  # re-exported for backward compatibility

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from nexus.registry import RepoRegistry

DEFAULT_IGNORE: list[str] = [
    "node_modules", "vendor", ".venv", "__pycache__", "dist", "build", ".git",
]

# Voyage AI embed() API limit: https://docs.voyageai.com/reference/embeddings-api
_VOYAGE_EMBED_BATCH_SIZE = 128

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


def _git_ls_files(repo: Path, *, include_untracked: bool = False) -> list[Path]:
    """Return repository files using git ls-files, respecting .gitignore.

    By default returns only tracked (committed/staged) files.
    With *include_untracked*, also includes untracked files that are not
    ignored by .gitignore / .git/info/exclude / global gitignore.
    """
    args = ["git", "ls-files", "--cached", "-z"]
    if include_untracked:
        args.extend(["--others", "--exclude-standard"])
    result = subprocess.run(
        args, cwd=repo, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        _log.warning("git ls-files failed, falling back to rglob", error=result.stderr.strip())
        return []  # caller should fall back
    # -z uses NUL separators (handles filenames with spaces/newlines)
    paths = []
    for rel_str in result.stdout.split("\0"):
        if rel_str:  # filter empty strings from trailing NUL
            paths.append(repo / rel_str)
    return paths


def index_repository(repo: Path, registry: "RepoRegistry", *, frecency_only: bool = False) -> None:
    """Index all files in *repo* into T3 code__ and docs__ collections.

    Files are classified and routed:
    - Code → code__ collection (voyage-code-3, AST chunking)
    - Prose → docs__ collection (voyage-context-3, semantic chunking)
    - PDF → docs__ collection (PDF extraction + voyage-context-3)
    - RDR markdown → docs__rdr__ collection

    Marks status as 'indexing' while running, 'ready' on success,
    'pending_credentials' when T3 credentials are absent.

    *frecency_only* skips re-chunking and re-embedding; only updates the
    ``frecency_score`` metadata field on existing T3 chunks.
    """
    registry.update(repo, status="indexing")
    try:
        if frecency_only:
            _run_index_frecency_only(repo, registry)
        else:
            _run_index(repo, registry)
        registry.update(repo, status="ready")
    except CredentialsMissingError:
        registry.update(repo, status="pending_credentials")
        # Re-raise so the polling loop skips recording head_hash for this repo.
        # A clean return would incorrectly signal success (see polling.py).
        raise
    except Exception:
        registry.update(repo, status="error")
        raise


def _run_index_frecency_only(repo: Path, registry: "RepoRegistry") -> None:
    """Update frecency_score metadata on all indexed chunks without re-embedding.

    Handles both code__ and docs__ collections.
    """
    from nexus.config import get_credential
    from nexus.frecency import batch_frecency
    from nexus.db import make_t3
    from nexus.registry import _docs_collection_name

    info = registry.get(repo)
    if info is None:
        return

    # C2: use deterministic naming function as fallback
    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection") or _docs_collection_name(repo)

    voyage_key = get_credential("voyage_api_key")
    chroma_key = get_credential("chroma_api_key")
    if not voyage_key or not chroma_key:
        raise CredentialsMissingError(
            f"T3 credentials missing for frecency reindex of '{repo.name}'"
        )

    frecency_map = batch_frecency(repo)
    db = make_t3()

    # Update frecency in both collections
    collection_names = [code_collection]
    if docs_collection:
        collection_names.append(docs_collection)

    for collection_name in collection_names:
        col = db.get_or_create_collection(collection_name)
        for file, score in frecency_map.items():
            existing = col.get(
                where={"source_path": str(file)},
                include=["metadatas"],
            )
            if not existing["ids"]:
                continue  # not yet indexed — needs full nx index repo

            updated_metadatas = [
                {**m, "frecency_score": float(score)}
                for m in existing["metadatas"]
            ]
            db.update_chunks(collection=collection_name, ids=existing["ids"], metadatas=updated_metadatas)


# ── Per-file indexing helpers ────────────────────────────────────────────────


def _index_code_file(
    file: Path,
    repo: Path,
    collection_name: str,
    target_model: str,
    col: object,
    db: object,
    voyage_client: object,
    git_meta: dict,
    now_iso: str,
    score: float,
) -> bool:
    """Index a single code file into the code__ collection.

    Returns True if the file was indexed (upserted), False if skipped.
    """
    import hashlib as _hl
    from nexus.chunker import chunk_file

    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file), error=type(exc).__name__)
        return False

    content_hash = _hl.sha256(content.encode()).hexdigest()

    # Staleness check
    existing = col.get(
        where={"source_path": str(file)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
            return False

    chunks = chunk_file(file, content)
    if not chunks:
        _log.debug("skipped file with no chunks", path=str(file))
        return False
    total_chunks = len(chunks)

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for i, chunk in enumerate(chunks):
        title = f"{file.relative_to(repo)}:{chunk['line_start']}-{chunk['line_end']}"
        doc_id = _hl.sha256(f"{collection_name}:{title}:chunk{i}".encode()).hexdigest()[:32]
        ext = file.suffix.lower()
        metadata: dict = {
            "title": title,
            "tags": ext.lstrip("."),
            "category": "code",
            "session_id": "",
            "source_agent": "nexus-indexer",
            "store_type": "code",
            "indexed_at": now_iso,
            "expires_at": "",
            "ttl_days": 0,
            "source_path": str(file),
            "line_start": chunk["line_start"],
            "line_end": chunk["line_end"],
            "frecency_score": float(score),
            "chunk_index": chunk.get("chunk_index", i),
            "chunk_count": chunk.get("chunk_count", total_chunks),
            "ast_chunked": chunk.get("ast_chunked", False),
            "filename": chunk.get("filename", str(file.name)),
            "file_extension": chunk.get("file_extension", ext),
            "programming_language": _EXT_TO_LANGUAGE.get(ext, ""),
            "corpus": collection_name,
            "embedding_model": target_model,
            "content_hash": content_hash,
            **git_meta,
        }
        ids.append(doc_id)
        documents.append(chunk["text"])
        metadatas.append(metadata)

    # Embed with voyage-code-3 direct call; batch per API limit
    embeddings: list[list[float]] = []
    for batch_start in range(0, len(documents), _VOYAGE_EMBED_BATCH_SIZE):
        batch = documents[batch_start : batch_start + _VOYAGE_EMBED_BATCH_SIZE]
        result = voyage_client.embed(texts=batch, model=target_model, input_type="document")
        embeddings.extend(result.embeddings)

    db.upsert_chunks_with_embeddings(
        collection_name=collection_name,
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    return True


def _index_prose_file(
    file: Path,
    repo: Path,
    collection_name: str,
    target_model: str,
    col: object,
    db: object,
    voyage_key: str,
    git_meta: dict,
    now_iso: str,
    score: float,
) -> bool:
    """Index a single prose file into the docs__ collection.

    Uses SemanticMarkdownChunker for .md files, _line_chunk for all others.
    Embeds via _embed_with_fallback (CCE for voyage-context-3).

    Returns True if the file was indexed (upserted), False if skipped.
    """
    import hashlib as _hl
    from nexus.chunker import _line_chunk
    from nexus.doc_indexer import _embed_with_fallback
    from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter

    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file), error=type(exc).__name__)
        return False

    content_hash = _hl.sha256(content.encode()).hexdigest()

    # Staleness check
    existing = col.get(
        where={"source_path": str(file)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
            return False

    ext = file.suffix.lower()
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    if ext in (".md", ".markdown"):
        # Markdown: use SemanticMarkdownChunker (M1: uses char offsets, not line numbers)
        frontmatter, body = parse_frontmatter(content)
        frontmatter_len = len(content) - len(body)
        base_meta: dict = {"source_path": str(file), "corpus": collection_name}
        chunks = SemanticMarkdownChunker().chunk(body, base_meta)
        if not chunks:
            _log.debug("skipped file with no chunks", path=str(file))
            return False

        for chunk in chunks:
            title = f"{file.relative_to(repo)}:chunk-{chunk.chunk_index}"
            doc_id = _hl.sha256(f"{collection_name}:{title}".encode()).hexdigest()[:32]
            metadata: dict = {
                "title": title,
                "tags": "markdown",
                "category": "prose",
                "session_id": "",
                "source_agent": "nexus-indexer",
                "store_type": "prose",
                "indexed_at": now_iso,
                "expires_at": "",
                "ttl_days": 0,
                "source_path": str(file),
                # M1: SemanticMarkdownChunker uses char offsets, not line numbers
                "line_start": 0,
                "line_end": 0,
                "chunk_start_char": chunk.metadata.get("chunk_start_char", 0) + frontmatter_len,
                "chunk_end_char": chunk.metadata.get("chunk_end_char", 0) + frontmatter_len,
                "section_title": chunk.metadata.get("header_path", ""),
                "frecency_score": float(score),
                "chunk_index": chunk.chunk_index,
                "chunk_count": len(chunks),
                "corpus": collection_name,
                "embedding_model": target_model,
                "content_hash": content_hash,
                **git_meta,
            }
            ids.append(doc_id)
            documents.append(chunk.text)
            metadatas.append(metadata)
    else:
        # Non-markdown prose: use line-based chunking
        raw_chunks = _line_chunk(content)
        if not raw_chunks:
            if not content.strip():
                return False
            raw_chunks = [(1, 1, content)]
        total_chunks = len(raw_chunks)

        for i, (ls, le, text) in enumerate(raw_chunks):
            title = f"{file.relative_to(repo)}:{ls}-{le}"
            doc_id = _hl.sha256(f"{collection_name}:{title}".encode()).hexdigest()[:32]
            metadata = {
                "title": title,
                "tags": ext.lstrip("."),
                "category": "prose",
                "session_id": "",
                "source_agent": "nexus-indexer",
                "store_type": "prose",
                "indexed_at": now_iso,
                "expires_at": "",
                "ttl_days": 0,
                "source_path": str(file),
                "line_start": ls,
                "line_end": le,
                "frecency_score": float(score),
                "chunk_index": i,
                "chunk_count": total_chunks,
                "corpus": collection_name,
                "embedding_model": target_model,
                "content_hash": content_hash,
                **git_meta,
            }
            ids.append(doc_id)
            documents.append(text)
            metadatas.append(metadata)

    if not documents:
        return False

    # Embed via _embed_with_fallback (CCE for voyage-context-3)
    embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key)
    if actual_model != target_model:
        for m in metadatas:
            m["embedding_model"] = actual_model

    db.upsert_chunks_with_embeddings(
        collection_name=collection_name,
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    return True


def _index_pdf_file(
    file: Path,
    repo: Path,
    collection_name: str,
    target_model: str,
    col: object,
    db: object,
    voyage_key: str,
    git_meta: dict,
    now_iso: str,
    score: float,
) -> bool:
    """Index a single PDF file into the docs__ collection.

    Uses PDF extraction + chunking from doc_indexer, embeds via _embed_with_fallback.
    Returns True if the file was indexed, False if skipped.
    """
    import hashlib as _hl
    from nexus.doc_indexer import _embed_with_fallback, _pdf_chunks

    content_hash = _hl.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            content_hash.update(block)
    content_hash_hex = content_hash.hexdigest()

    # Staleness check
    existing = col.get(
        where={"source_path": str(file)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash_hex and stored.get("embedding_model") == target_model:
            return False

    prepared = _pdf_chunks(file, content_hash_hex, target_model, now_iso, collection_name)
    if not prepared:
        _log.debug("skipped PDF with no chunks", path=str(file))
        return False

    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas_raw = [p[2] for p in prepared]

    # Augment metadata with repo-indexer fields
    metadatas: list[dict] = []
    for m in metadatas_raw:
        augmented = {
            **m,
            "title": f"{file.relative_to(repo)}:page-{m.get('page_number', 0)}",
            "tags": "pdf",
            "category": "prose",
            "session_id": "",
            "source_agent": "nexus-indexer",
            "expires_at": "",
            "ttl_days": 0,
            "frecency_score": float(score),
            **git_meta,
        }
        metadatas.append(augmented)

    embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key)
    if actual_model != target_model:
        for m in metadatas:
            m["embedding_model"] = actual_model

    db.upsert_chunks_with_embeddings(
        collection_name=collection_name,
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    return True


def _discover_and_index_rdrs(
    repo: Path,
    rdr_abs_paths: set[Path],
    db: object,
    voyage_key: str,
    now_iso: str,
) -> None:
    """Find .md files under RDR paths and index them via batch_index_markdowns.

    M2: passes t3=db to avoid creating a redundant T3 client.
    """
    import hashlib as _hl
    from nexus.doc_indexer import batch_index_markdowns

    if not rdr_abs_paths:
        return

    md_paths: list[Path] = []
    for rdr_dir in rdr_abs_paths:
        if not rdr_dir.is_dir():
            continue
        for path in sorted(rdr_dir.rglob("*.md")):
            if path.is_file() and not path.is_symlink():
                md_paths.append(path)

    if not md_paths:
        return

    # Corpus: rdr__{basename}-{hash8} — uses worktree-stable identity
    from nexus.registry import _repo_identity
    basename, path_hash = _repo_identity(repo)
    corpus = f"rdr__{basename}-{path_hash}"

    batch_index_markdowns(md_paths, corpus, t3=db)


def _prune_misclassified(
    repo: Path,
    code_collection: str,
    docs_collection: str,
    code_files: list[Path],
    prose_files: list[Path],
    pdf_files: list[Path],
    db: object,
) -> None:
    """Remove chunks from the wrong collection after reclassification.

    If a file was previously classified as code but is now prose (or vice versa),
    its chunks in the old collection must be removed.
    """
    code_col = db.get_or_create_collection(code_collection)
    docs_col = db.get_or_create_collection(docs_collection)

    # Prose + PDF files should NOT have chunks in the code__ collection
    docs_paths = {str(f) for f in prose_files} | {str(f) for f in pdf_files}
    for source_path in docs_paths:
        existing = code_col.get(where={"source_path": source_path}, include=[])
        if existing["ids"]:
            code_col.delete(ids=existing["ids"])
            _log.debug("pruned misclassified chunks from code collection",
                       source_path=source_path, count=len(existing["ids"]))

    # Code files should NOT have chunks in the docs__ collection
    code_paths = {str(f) for f in code_files}
    for source_path in code_paths:
        existing = docs_col.get(where={"source_path": source_path}, include=[])
        if existing["ids"]:
            docs_col.delete(ids=existing["ids"])
            _log.debug("pruned misclassified chunks from docs collection",
                       source_path=source_path, count=len(existing["ids"]))


def _prune_deleted_files(
    code_collection: str,
    docs_collection: str,
    all_current_paths: set[str],
    db: object,
) -> None:
    """Remove chunks for files that no longer exist in the repo (C3 fix).

    Queries each collection for all distinct source_paths and deletes chunks
    for any path not in *all_current_paths*.
    """
    for collection_name in (code_collection, docs_collection):
        col = db.get_or_create_collection(collection_name)
        # Get all chunks to find unique source_paths
        all_chunks = col.get(include=["metadatas"])
        if not all_chunks["ids"]:
            continue

        # Group chunk IDs by source_path
        stale_ids: list[str] = []
        for chunk_id, meta in zip(all_chunks["ids"], all_chunks["metadatas"]):
            source_path = meta.get("source_path", "")
            if source_path and source_path not in all_current_paths:
                stale_ids.append(chunk_id)

        if stale_ids:
            col.delete(ids=stale_ids)
            _log.info("pruned deleted-file chunks",
                       collection=collection_name, count=len(stale_ids))


# ── Main indexing pipeline ───────────────────────────────────────────────────


def _run_index(repo: Path, registry: "RepoRegistry") -> None:
    """Full indexing pipeline: classify → route → embed → upsert → prune.

    Routes files to the appropriate collection based on content classification:
    - Code files → code__ collection (voyage-code-3, AST chunking)
    - Prose files → docs__ collection (voyage-context-3 via CCE)
    - PDF files → docs__ collection (PDF extraction + voyage-context-3)
    - RDR markdown → docs__rdr__ collection (via batch_index_markdowns)
    """
    from nexus.classifier import ContentClass, classify_file
    from nexus.config import load_config
    from nexus.frecency import batch_frecency
    from nexus.registry import _docs_collection_name
    from nexus.ripgrep_cache import build_cache

    info = registry.get(repo)
    if info is None:
        return

    # C2: use deterministic naming function as fallback
    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection") or _docs_collection_name(repo)

    # Load config (picks up per-repo .nexus.yml if present)
    cfg = load_config(repo_root=repo)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    ignore_patterns: list[str] = list(dict.fromkeys(DEFAULT_IGNORE + cfg_patterns))
    indexing_config: dict = cfg.get("indexing", {})
    rdr_paths: list[str] = indexing_config.get("rdr_paths", ["docs/rdr"])

    # Collect git metadata once for all chunks
    git_meta = _git_metadata(repo)

    # Compute frecency scores in a single git log pass
    frecency_map = batch_frecency(repo)

    # Build absolute RDR path set for exclusion
    rdr_abs_paths: set[Path] = set()
    for rdr_rel in rdr_paths:
        rdr_abs = (repo / rdr_rel).resolve()
        rdr_abs_paths.add(rdr_abs)

    # Walk repo and classify files into code, prose, and PDF lists
    code_files: list[tuple[float, Path]] = []
    prose_files: list[tuple[float, Path]] = []
    pdf_files: list[tuple[float, Path]] = []
    all_text_scored: list[tuple[float, Path]] = []  # code + prose for ripgrep cache

    # Use git ls-files to respect .gitignore (security + efficiency)
    include_untracked = indexing_config.get("include_untracked", False)
    git_files = _git_ls_files(repo, include_untracked=include_untracked)

    if git_files:
        candidate_files = git_files
    else:
        # Fallback to rglob if git ls-files fails (not a git repo, etc.)
        _log.warning("falling back to rglob file walk", repo=str(repo))
        candidate_files = sorted(p for p in repo.rglob("*") if p.is_file() and not p.is_symlink())

    for path in candidate_files:
        if not path.is_file():
            continue  # git ls-files may list deleted files not yet committed
        rel = path.relative_to(repo)
        # Defense-in-depth: still filter hidden dirs and ignore patterns
        if any(part.startswith(".") for part in rel.parts):
            continue  # Skip hidden dirs/files
        if _should_ignore(rel, ignore_patterns):
            continue  # Skip ignored patterns

        # Skip files under RDR paths — they go to docs__rdr__ separately
        resolved = path.resolve()
        if any(resolved == rdr or _is_under(resolved, rdr) for rdr in rdr_abs_paths):
            continue

        score = frecency_map.get(path, 0.0)
        classification = classify_file(path, indexing_config=indexing_config)

        match classification:
            case ContentClass.CODE:
                code_files.append((score, path))
                all_text_scored.append((score, path))
            case ContentClass.PROSE:
                prose_files.append((score, path))
                all_text_scored.append((score, path))
            case ContentClass.PDF:
                pdf_files.append((score, path))
                # PDF files not included in ripgrep text cache

    # Sort all lists descending by frecency
    code_files.sort(key=lambda x: x[0], reverse=True)
    prose_files.sort(key=lambda x: x[0], reverse=True)
    pdf_files.sort(key=lambda x: x[0], reverse=True)
    all_text_scored.sort(key=lambda x: x[0], reverse=True)

    # Update ripgrep cache (code + prose text files, not PDFs)
    from nexus.registry import _repo_identity
    _repo_basename, _repo_hash = _repo_identity(repo)
    cache_path = Path.home() / ".config" / "nexus" / f"{_repo_basename}-{_repo_hash}.cache"
    build_cache(repo, cache_path, all_text_scored)

    # Credential check (required for T3 operations)
    from nexus.config import get_credential
    voyage_key = get_credential("voyage_api_key")
    chroma_key = get_credential("chroma_api_key")
    if not voyage_key or not chroma_key:
        raise CredentialsMissingError(
            f"T3 credentials missing for repo '{repo.name}' "
            f"(voyage_api_key={'set' if voyage_key else 'missing'}, "
            f"chroma_api_key={'set' if chroma_key else 'missing'})"
        )

    import voyageai
    from datetime import UTC, datetime as _dt
    from nexus.db import make_t3

    db = make_t3()
    now_iso = _dt.now(UTC).isoformat()

    # Initialize collections and models
    code_model = index_model_for_collection(code_collection)
    docs_model = index_model_for_collection(docs_collection)
    voyage_client = voyageai.Client(api_key=voyage_key)
    code_col = db.get_or_create_collection(code_collection)
    docs_col = db.get_or_create_collection(docs_collection)

    # Index code files → code__ (voyage-code-3, AST chunking)
    for score, file in code_files:
        _index_code_file(
            file, repo, code_collection, code_model, code_col, db,
            voyage_client, git_meta, now_iso, score,
        )

    # Index prose files → docs__ (voyage-context-3 via CCE)
    for score, file in prose_files:
        _index_prose_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
        )

    # Index PDF files → docs__ (PDF extraction + voyage-context-3)
    for score, file in pdf_files:
        _index_pdf_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
        )

    # Discover and index RDR markdown files → docs__rdr__
    _discover_and_index_rdrs(repo, rdr_abs_paths, db, voyage_key, now_iso)

    # Prune misclassified chunks (reclassification cleanup)
    _prune_misclassified(
        repo, code_collection, docs_collection,
        [f for _, f in code_files],
        [f for _, f in prose_files],
        [f for _, f in pdf_files],
        db,
    )

    # C3: Prune deleted files — remove chunks for files no longer in the repo
    all_current_paths: set[str] = set()
    for _, f in code_files:
        all_current_paths.add(str(f))
    for _, f in prose_files:
        all_current_paths.add(str(f))
    for _, f in pdf_files:
        all_current_paths.add(str(f))
    _prune_deleted_files(code_collection, docs_collection, all_current_paths, db)


def _is_under(child: Path, parent: Path) -> bool:
    """Return True if *child* is a descendant of *parent*."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
