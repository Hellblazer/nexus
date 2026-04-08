# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code repository indexing pipeline — orchestrator.

This module is responsible for:
- Repository file discovery and classification dispatch
- Constructing IndexContext and routing to per-type indexers
- RDR markdown indexing (via doc_indexer)
- Misclassification and deleted-file pruning
- Frecency-only update runs
- Pipeline version stamping
- Per-repo locking

Per-file indexing logic lives in focused sub-modules (RDR-032):
  nexus.code_indexer   — code files (AST chunking, context extraction)
  nexus.prose_indexer  — prose and markdown files (CCE embedding)
  nexus.indexer_utils  — staleness check, credential check, shared helpers
  nexus.index_context  — IndexContext dataclass
"""
import errno
import fcntl
import fnmatch
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus.corpus import index_model_for_collection
from nexus.retry import _chroma_with_retry, _voyage_with_retry  # noqa: F401 — re-exported for any existing imports
from nexus.errors import CredentialsMissingError  # re-exported for backward compatibility
from nexus.indexer_utils import check_credentials, check_local_path_writable, check_staleness

# Re-exports from nexus.code_indexer for backward compatibility with tests that
# import these names directly from nexus.indexer.
from nexus.code_indexer import (  # noqa: F401
    _extract_context,
    _extract_name_from_node,
    _COMMENT_CHARS,
    DEFINITION_TYPES,
)

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from nexus.registry import RepoRegistry

DEFAULT_IGNORE: list[str] = [
    "node_modules", "vendor", ".venv", "__pycache__", "dist", "build", ".git",
    # Dependency lock / checksum files: auto-generated, not semantically useful,
    # and can produce chunks that exceed storage per-document size limits.
    "*.lock", "go.sum",
]


# Pipeline version: bump when indexing changes invalidate existing embeddings.
# History:
#   v1-v3: pre-versioning (no version stamp in collection metadata)
#   v4:    RDR-028 language registry + RDR-014 CCE prefixes
PIPELINE_VERSION: str = "4"


def stamp_collection_version(col: object) -> None:
    """Write PIPELINE_VERSION to collection metadata, preserving existing keys."""
    existing = getattr(col, "metadata", None) or {}
    col.modify(metadata={**existing, "pipeline_version": PIPELINE_VERSION})  # type: ignore[attr-defined]


def get_collection_pipeline_version(col: object) -> str | None:
    """Return the pipeline_version from collection metadata, or None."""
    meta = getattr(col, "metadata", None) or {}
    return meta.get("pipeline_version")


def check_pipeline_staleness(col: object, collection_name: str) -> bool:
    """Check if collection has a stale pipeline version.

    Returns True if the stored version differs from PIPELINE_VERSION.
    Returns False for new collections (stored version is None) or matching versions.
    """
    stored = get_collection_pipeline_version(col)
    if stored is None:
        return False
    if stored != PIPELINE_VERSION:
        _log.warning(
            "collection_pipeline_stale",
            collection=collection_name,
            stored_version=stored,
            current_version=PIPELINE_VERSION,
            hint="Run with --force-stale to re-index stale collections, or --force to re-index all.",
        )
        return True
    return False


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

    In a git repo (.git exists), failure raises RuntimeError — silent fallback
    to rglob would index .gitignored secrets like .env (nexus-3ov6).
    Non-git directories return [] so the caller can use rglob.
    """
    is_git_repo = (repo / ".git").is_dir()
    args = ["git", "ls-files", "--cached", "-z"]
    if include_untracked:
        args.extend(["--others", "--exclude-standard"])
    try:
        result = subprocess.run(
            args, cwd=repo, capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        if is_git_repo:
            raise RuntimeError(
                f"git ls-files failed in git repo {repo}: {exc}"
            ) from exc
        return []
    if result.returncode != 0:
        if is_git_repo:
            raise RuntimeError(
                f"git ls-files failed in git repo {repo}: {result.stderr.strip()}"
            )
        return []  # non-git directory — caller uses rglob
    # -z uses NUL separators (handles filenames with spaces/newlines)
    paths = []
    for rel_str in result.stdout.split("\0"):
        if rel_str:  # filter empty strings from trailing NUL
            paths.append(repo / rel_str)
    return paths


def _current_head(repo: Path) -> str:
    """Return the current HEAD commit hash for *repo*, or '' on error."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("current_head_failed", repo=str(repo), error=str(exc))
        return ""


def _repo_lock_path(repo: Path) -> Path:
    """Return the per-repo lock file path: ~/.config/nexus/locks/<hash8>.lock.

    Uses the same worktree-stable identity as the registry so two worktrees
    of the same repo map to a single lock.
    """
    from nexus.registry import _repo_identity

    _, path_hash = _repo_identity(repo)
    return Path.home() / ".config" / "nexus" / "locks" / f"{path_hash}.lock"


def _clear_stale_lock(lock_path: Path) -> None:
    """Delete *lock_path* if it contains a dead PID.

    Only removes a lock file when we can confirm the recorded PID is dead
    (``ESRCH`` from ``os.kill(pid, 0)``).  Files with no parseable PID are
    left alone — they may belong to an active process that hasn't written
    its PID yet.

    This is advisory only — ``fcntl.flock`` provides the real mutual exclusion.
    """
    if not lock_path.exists():
        return
    try:
        pid = int(lock_path.read_text().strip())
    except (ValueError, OSError):
        # No readable PID — could be a just-opened lock. Leave it;
        # flock will handle contention.
        return
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            _remove_stale(lock_path)


def _remove_stale(lock_path: Path) -> None:
    """Unlink *lock_path*, ignoring FileNotFoundError (concurrent cleanup)."""
    try:
        lock_path.unlink()
        _log.debug("stale_lock_removed", path=str(lock_path))
    except FileNotFoundError:
        pass


def _catalog_hook(
    repo: Path,
    repo_name: str,
    repo_hash: str,
    head_hash: str,
    indexed_files: list[tuple[Path, str, str]],
) -> None:
    """Register/update indexed files in catalog. Silently skipped if catalog absent."""
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            _log.debug("catalog_hook_skipped", reason="catalog not initialized")
            return

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        owner = cat.owner_for_repo(repo_hash)
        if owner is None:
            owner = cat.register_owner(
                name=repo_name,
                owner_type="repo",
                repo_hash=repo_hash,
                description=f"Git repository: {repo_name}",
            )
            _log.info("catalog_owner_created", owner=str(owner), repo=repo_name)

        for abs_path, content_type, collection_name in indexed_files:
            try:
                rel_path = str(abs_path.relative_to(repo))
            except ValueError:
                rel_path = abs_path.name

            existing = cat.by_file_path(owner, rel_path)
            if existing is None:
                cat.register(
                    owner=owner,
                    title=abs_path.name,
                    content_type=content_type,
                    file_path=rel_path,
                    physical_collection=collection_name,
                    head_hash=head_hash,
                )
            else:
                cat.update(
                    existing.tumbler,
                    head_hash=head_hash,
                    physical_collection=collection_name,
                )
        # Auto-generate links after registration
        try:
            from nexus.catalog.link_generator import generate_code_rdr_links, generate_rdr_filepath_links
            link_count = generate_code_rdr_links(cat)
            fp_count = generate_rdr_filepath_links(cat)
            total = link_count + fp_count
            if total:
                _log.info("catalog_links_generated", heuristic=link_count, filepath=fp_count, repo=repo_name)
        except Exception:
            _log.debug("catalog_link_generation_failed", exc_info=True)
    except Exception:
        _log.debug("catalog_hook_failed", exc_info=True)


def index_repository(
    repo: Path,
    registry: "RepoRegistry",
    *,
    frecency_only: bool = False,
    chunk_lines: int | None = None,
    force: bool = False,
    force_stale: bool = False,
    on_locked: str = "wait",
    on_start: Callable[[int], None] | None = None,
    on_file: Callable[[Path, int, float], None] | None = None,
) -> dict[str, int]:
    """Index all files in *repo* into T3 code__ and docs__ collections.

    Files are classified and routed:
    - Code → code__ collection (voyage-code-3, AST chunking)
    - Prose → docs__ collection (voyage-context-3, semantic chunking)
    - PDF → docs__ collection (PDF extraction + voyage-context-3)
    - RDR markdown → rdr__ collection

    Marks status as 'indexing' while running, 'ready' on success,
    'pending_credentials' when T3 credentials are absent.

    *frecency_only* skips re-chunking and re-embedding; only updates the
    ``frecency_score`` metadata field on existing T3 chunks.  Frecency-only
    runs bypass the per-repo lock and do not update ``head_hash``.

    *chunk_lines* overrides the default chunk size (150 lines) for code files.
    When None, the module default is used.

    *on_locked* controls behaviour when another process holds the repo lock:
    ``'wait'`` (default) blocks until the lock is released; ``'skip'`` returns
    ``{}`` immediately without indexing.  Frecency-only runs bypass the lock.

    Returns a stats dict (empty for frecency_only runs) with keys:
    ``rdr_indexed``, ``rdr_current``, ``rdr_failed``.
    """
    lock_fd = None
    lock_path: Path | None = None
    if not frecency_only:
        lock_path = _repo_lock_path(repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _clear_stale_lock(lock_path)
        lock_fd = open(lock_path, "w")  # noqa: SIM115  (must stay open while locked)
        try:
            lock_fd.write(str(os.getpid()))
            lock_fd.flush()
        except OSError:
            pass  # PID write is best-effort; lock still works without it
        lock_flag = fcntl.LOCK_EX if on_locked == "wait" else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(lock_fd, lock_flag)
        except BlockingIOError:
            # on_locked == "skip" and another process holds the lock
            lock_fd.close()
            return {}

    try:
        registry.update(repo, status="indexing")
        try:
            if frecency_only:
                _run_index_frecency_only(repo, registry)
                stats: dict[str, int] = {}
            else:
                stats = _run_index(repo, registry, chunk_lines=chunk_lines, force=force, force_stale=force_stale, on_start=on_start, on_file=on_file)
                registry.update(repo, head_hash=_current_head(repo))
            registry.update(repo, status="ready")
            return stats
        except CredentialsMissingError:
            registry.update(repo, status="pending_credentials")
            raise
        except Exception:
            registry.update(repo, status="error")
            raise
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        if lock_path is not None:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass  # already gone — harmless


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

    from nexus.config import is_local_mode
    if not is_local_mode():
        voyage_key = get_credential("voyage_api_key")
        chroma_key = get_credential("chroma_api_key")
        check_credentials(voyage_key, chroma_key)
    else:
        check_local_path_writable()

    frecency_map = batch_frecency(repo)
    db = make_t3()

    # Update frecency in both collections
    collection_names = [code_collection]
    if docs_collection:
        collection_names.append(docs_collection)

    for collection_name in collection_names:
        col = db.get_or_create_collection(collection_name)
        for file, score in frecency_map.items():
            existing = _paginated_get(
                col,
                include=["metadatas"],
                where={"source_path": str(file)},
            )
            if not existing["ids"]:
                continue  # not yet indexed — needs full nx index repo

            updated_metadatas = [
                {**m, "frecency_score": float(score)}
                for m in existing["metadatas"]
            ]
            db.update_chunks(collection=collection_name, ids=existing["ids"], metadatas=updated_metadatas)


# ── Backward-compatible per-file wrappers ────────────────────────────────────
# These wrappers preserve the old 12-parameter signatures so that:
# 1. Existing tests that import and call _index_code_file/_index_prose_file
#    directly continue to work unchanged.
# 2. Tests that patch nexus.indexer._index_code_file or _index_prose_file
#    still intercept the calls from _run_index.
#
# Implementation delegates to the clean IndexContext-based API in the
# extracted sub-modules (nexus.code_indexer, nexus.prose_indexer).
# The wrappers are intentionally thin — no logic here.


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
    chunk_lines: int | None = None,
    force: bool = False,
    *,
    embed_fn: Callable | None = None,
) -> int:
    """Index a single code file.  Delegates to nexus.code_indexer.index_code_file.

    Backward-compatible wrapper preserving the old 12-parameter signature.
    See nexus.code_indexer.index_code_file for the canonical implementation.
    """
    from nexus.code_indexer import index_code_file
    from nexus.index_context import IndexContext

    ctx = IndexContext(
        col=col,
        db=db,
        voyage_key="",  # code path uses voyage_client directly
        voyage_client=voyage_client,
        repo_path=repo,
        corpus=collection_name,
        embedding_model=target_model,
        git_meta=git_meta,
        now_iso=now_iso,
        score=score,
        chunk_lines=chunk_lines,
        force=force,
        embed_fn=embed_fn,
    )
    return index_code_file(ctx, file)


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
    force: bool = False,
    timeout: float = 120.0,
    *,
    embed_fn: Callable | None = None,
) -> int:
    """Index a single prose file.  Delegates to nexus.prose_indexer.index_prose_file.

    Backward-compatible wrapper preserving the old 12-parameter signature.
    See nexus.prose_indexer.index_prose_file for the canonical implementation.
    """
    from nexus.prose_indexer import index_prose_file
    from nexus.index_context import IndexContext

    ctx = IndexContext(
        col=col,
        db=db,
        voyage_key=voyage_key,
        voyage_client=None,  # prose path uses voyage_key
        repo_path=repo,
        corpus=collection_name,
        embedding_model=target_model,
        git_meta=git_meta,
        now_iso=now_iso,
        score=score,
        force=force,
        timeout=timeout,
        embed_fn=embed_fn,
    )
    return index_prose_file(ctx, file)


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
    force: bool = False,
    timeout: float = 120.0,
    chunk_chars: int | None = None,
    *,
    embed_fn: Callable | None = None,
) -> int:
    """Index a single PDF file into the docs__ collection.

    Uses PDF extraction + chunking from doc_indexer, embeds via _embed_with_fallback.
    Returns the post-filter chunk count (chunks upserted), or 0 if skipped/failed.

    *chunk_chars* overrides the PDF chunk size (default 1500 chars).  Pass
    ``tuning.pdf_chunk_chars`` from TuningConfig to honour per-repo config.
    """
    import hashlib as _hl
    from nexus.doc_indexer import _embed_with_fallback, _pdf_chunks

    content_hash = _hl.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            content_hash.update(block)
    content_hash_hex = content_hash.hexdigest()

    # Staleness check
    if not force and check_staleness(col, file, content_hash_hex, target_model):
        return 0

    prepared = _pdf_chunks(file, content_hash_hex, target_model, now_iso, collection_name, chunk_chars=chunk_chars)
    if not prepared:
        _log.debug("skipped PDF with no chunks", path=str(file))
        return 0

    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas_raw = [p[2] for p in prepared]

    # Build embed_texts with context prefix BEFORE augmentation overwrites 'title'.
    # source_title comes from _pdf_chunks (doc_indexer.py:251, field 'pdf_title').
    # We must read it from metadatas_raw here; after augmentation 'title' is a
    # file-path string like "path/to/file.pdf:page-3".
    embed_texts_pdf: list[str] = []
    for doc, m in zip(documents, metadatas_raw):
        source_title = m.get("source_title", "")
        page_number = m.get("page_number", 0)
        prefix_parts: list[str] = []
        if source_title:
            prefix_parts.append(f"Document: {source_title}")
        prefix_parts.append(f"Page: {page_number}")
        prefix = "## " + "  ".join(prefix_parts)
        embed_texts_pdf.append(f"{prefix}\n\n{doc}")

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

    if embed_fn is not None:
        embeddings = embed_fn(embed_texts_pdf)
        actual_model = target_model
    else:
        embeddings, actual_model = _embed_with_fallback(embed_texts_pdf, target_model, voyage_key, timeout=timeout)
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
    return len(prepared)


def _discover_and_index_rdrs(
    repo: Path,
    rdr_abs_paths: set[Path],
    db: object,
    voyage_key: str,
    now_iso: str,
    *,
    force: bool = False,
) -> tuple[int, int, int]:
    """Find .md files under RDR paths and index them via batch_index_markdowns.

    M2: passes t3=db to avoid creating a redundant T3 client.

    Returns (indexed, skipped, failed) counts.

    Note: ``on_file`` progress callbacks are intentionally NOT wired here.
    RDR files are excluded from the main ``_run_index`` file loop and their
    count is not known up front (discovered inside this function).  For
    standalone RDR progress reporting, call ``batch_index_markdowns`` directly
    with an ``on_file`` callback (Path B in the progress reporting design).
    """
    from nexus.doc_indexer import batch_index_markdowns

    if not rdr_abs_paths:
        _log.debug("RDR indexing skipped — no rdr_paths configured")
        return 0, 0, 0

    md_paths: list[Path] = []
    for rdr_dir in rdr_abs_paths:
        if not rdr_dir.is_dir():
            continue
        for path in sorted(rdr_dir.rglob("*.md")):
            if path.is_file() and not path.is_symlink():
                md_paths.append(path)

    if not md_paths:
        _log.debug("no RDR files found", rdr_paths=[str(p) for p in rdr_abs_paths])
        return 0, 0, 0

    # Collection: rdr__{basename}-{hash8} — uses worktree-stable identity
    from nexus.registry import _repo_identity, _rdr_collection_name
    basename, _ = _repo_identity(repo)
    collection = _rdr_collection_name(repo)

    _log.info("indexing RDR files", count=len(md_paths), collection=collection)
    results = batch_index_markdowns(md_paths, corpus=basename, t3=db, collection_name=collection, force=force)
    indexed = sum(1 for s in results.values() if s == "indexed")
    skipped = sum(1 for s in results.values() if s == "skipped")
    failed = sum(1 for s in results.values() if s == "failed")
    _log.info("RDR indexing complete", indexed=indexed, current=skipped, failed=failed)
    return indexed, skipped, failed


_CHROMA_PAGE_SIZE: int = 300
"""ChromaDB Cloud hard cap per get() call — paginate above this."""


def _paginated_get(col: object, include: list[str], where: dict | None = None) -> dict:
    """Fetch all matching chunks from *col* by paginating in _CHROMA_PAGE_SIZE batches.

    ChromaDB Cloud silently truncates unbounded col.get() calls at 300 records.
    This helper accumulates pages until a short page signals the end.

    Returns a dict with ``"ids"`` and, when ``"metadatas"`` is in *include*,
    a ``"metadatas"`` key — matching the shape returned by col.get().
    """
    offset = 0
    all_ids: list[str] = []
    all_metas: list[dict] = []
    has_metas = "metadatas" in include

    while True:
        kwargs: dict = {"include": include, "limit": _CHROMA_PAGE_SIZE, "offset": offset}
        if where is not None:
            kwargs["where"] = where
        batch = _chroma_with_retry(col.get, **kwargs)
        batch_ids: list[str] = batch["ids"] or []
        all_ids.extend(batch_ids)
        if has_metas:
            all_metas.extend(batch.get("metadatas") or [])
        if len(batch_ids) < _CHROMA_PAGE_SIZE:
            break
        offset += _CHROMA_PAGE_SIZE

    result: dict = {"ids": all_ids}
    if has_metas:
        result["metadatas"] = all_metas
    return result


def _batched_delete(col: object, ids: list[str]) -> int:
    """Delete *ids* from *col* in batches of _CHROMA_PAGE_SIZE (Cloud quota: 300)."""
    deleted = 0
    for i in range(0, len(ids), _CHROMA_PAGE_SIZE):
        batch = ids[i : i + _CHROMA_PAGE_SIZE]
        _chroma_with_retry(col.delete, ids=batch)
        deleted += len(batch)
    return deleted


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
        existing = _paginated_get(code_col, include=[], where={"source_path": source_path})
        if existing["ids"]:
            _batched_delete(code_col, existing["ids"])
            _log.debug("pruned misclassified chunks from code collection",
                       source_path=source_path, count=len(existing["ids"]))

    # Code files should NOT have chunks in the docs__ collection
    code_paths = {str(f) for f in code_files}
    for source_path in code_paths:
        existing = _paginated_get(docs_col, include=[], where={"source_path": source_path})
        if existing["ids"]:
            _batched_delete(docs_col, existing["ids"])
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
        # Get all chunks to find unique source_paths (paginated — Cloud cap is 300)
        all_chunks = _paginated_get(col, include=["metadatas"])
        if not all_chunks["ids"]:
            continue

        # Group chunk IDs by source_path
        stale_ids: list[str] = []
        for chunk_id, meta in zip(all_chunks["ids"], all_chunks["metadatas"]):
            source_path = meta.get("source_path", "")
            if source_path and source_path not in all_current_paths:
                stale_ids.append(chunk_id)

        if stale_ids:
            _batched_delete(col, stale_ids)
            _log.info("pruned deleted-file chunks",
                       collection=collection_name, count=len(stale_ids))


# ── Main indexing pipeline ───────────────────────────────────────────────────


def _run_index(
    repo: Path,
    registry: "RepoRegistry",
    chunk_lines: int | None = None,
    *,
    force: bool = False,
    force_stale: bool = False,
    on_start: Callable[[int], None] | None = None,
    on_file: Callable[[Path, int, float], None] | None = None,
) -> dict[str, int]:
    """Full indexing pipeline: classify → route → embed → upsert → prune.

    Routes files to the appropriate collection based on content classification:
    - Code files → code__ collection (voyage-code-3, AST chunking)
    - Prose files → docs__ collection (voyage-context-3 via CCE)
    - PDF files → docs__ collection (PDF extraction + voyage-context-3)
    - RDR markdown → rdr__ collection (via batch_index_markdowns)

    Returns a stats dict with ``rdr_indexed``, ``rdr_current``, ``rdr_failed``.
    """
    from nexus.classifier import ContentClass, classify_file
    from nexus.config import get_credential, load_config
    from nexus.frecency import batch_frecency
    from nexus.registry import _docs_collection_name
    from nexus.ripgrep_cache import build_cache

    info = registry.get(repo)
    if info is None:
        return {}

    # C2: use deterministic naming function as fallback
    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection") or _docs_collection_name(repo)

    # Load config (picks up per-repo .nexus.yml if present)
    cfg = load_config(repo_root=repo)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    ignore_patterns: list[str] = list(dict.fromkeys(DEFAULT_IGNORE + cfg_patterns))
    indexing_config: dict = cfg.get("indexing", {})
    rdr_paths: list[str] = indexing_config.get("rdr_paths", ["docs/rdr"])
    read_timeout_seconds: float = cfg.get("voyageai", {}).get("read_timeout_seconds", 120.0)

    # Load tuning config and use its chunk_lines if not overridden by caller
    from nexus.config import _tuning_from_dict
    tuning = _tuning_from_dict(cfg.get("tuning", {}))
    effective_chunk_lines: int | None = chunk_lines if chunk_lines is not None else tuning.code_chunk_lines

    # Collect git metadata once for all chunks
    git_meta = _git_metadata(repo)

    # Compute frecency scores in a single git log pass
    frecency_map = batch_frecency(repo, decay_rate=tuning.decay_rate, timeout=tuning.git_log_timeout)

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

        # Skip files under RDR paths — they go to rdr__ separately
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
            case ContentClass.SKIP:
                pass  # known-noise file; silently ignore

    # Sort all lists descending by frecency
    code_files.sort(key=lambda x: x[0], reverse=True)
    prose_files.sort(key=lambda x: x[0], reverse=True)
    pdf_files.sort(key=lambda x: x[0], reverse=True)
    all_text_scored.sort(key=lambda x: x[0], reverse=True)

    # Fire on_start with total non-RDR file count.
    # Note: this fires before the credential check below.  Phase 2 (CLI) must
    # handle CredentialsMissingError by closing the tqdm bar before re-raising.
    if on_start:
        on_start(len(code_files) + len(prose_files) + len(pdf_files))

    # Update ripgrep cache (code + prose text files, not PDFs)
    from nexus.registry import _repo_identity
    _repo_basename, _repo_hash = _repo_identity(repo)
    cache_path = Path.home() / ".config" / "nexus" / f"{_repo_basename}-{_repo_hash}.cache"
    build_cache(repo, cache_path, all_text_scored)

    # Credential check and T3 setup
    from nexus.config import is_local_mode as _is_local
    from datetime import UTC, datetime as _dt
    from nexus.db import make_t3

    _local_mode = _is_local()
    _embed_fn = None

    if _local_mode:
        check_local_path_writable()
        from nexus.db.local_ef import LocalEmbeddingFunction
        _local_ef = LocalEmbeddingFunction()
        _embed_fn = _local_ef  # callable: (texts) -> embeddings
        local_model = _local_ef.model_name
        code_model = local_model
        docs_model = local_model
        voyage_key = ""
        voyage_client = None
        # First-run tier notice
        if _local_ef.model_name == "all-MiniLM-L6-v2":
            _log.info(
                "local_mode_tier0",
                msg="Using basic embeddings (tier 0). For better code search quality: pip install conexus[local]",
            )
    else:
        voyage_key = get_credential("voyage_api_key")
        chroma_key = get_credential("chroma_api_key")
        check_credentials(voyage_key, chroma_key)
        import voyageai
        code_model = index_model_for_collection(code_collection)
        docs_model = index_model_for_collection(docs_collection)
        voyage_client = voyageai.Client(api_key=voyage_key, timeout=read_timeout_seconds, max_retries=3)

    _log.debug("connecting to ChromaDB")
    db = make_t3()
    _log.debug("ChromaDB connected")
    now_iso = _dt.now(UTC).isoformat()

    _log.debug("creating collections", code=code_collection, docs=docs_collection)
    code_col = db.get_or_create_collection(code_collection)
    docs_col = db.get_or_create_collection(docs_collection)
    _log.debug("collections ready")

    # Check pipeline version staleness (informational warning only)
    check_pipeline_staleness(code_col, code_collection)
    check_pipeline_staleness(docs_col, docs_collection)

    # --force-stale: escalate to force if any collection is stale
    if force_stale:
        any_stale = (
            get_collection_pipeline_version(code_col) not in (None, PIPELINE_VERSION)
            or get_collection_pipeline_version(docs_col) not in (None, PIPELINE_VERSION)
        )
        if any_stale:
            _log.info("force_stale_escalating", reason="stale collection detected")
            force = True
        else:
            _log.info("force_stale_skipped", reason="all collections current")

    # Index code files → code__ (voyage-code-3, AST chunking)
    # NOTE: calls _index_code_file (the module-level wrapper) so that tests
    # patching nexus.indexer._index_code_file continue to intercept correctly.
    _log.debug("indexing code files", count=len(code_files))
    for score, file in code_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        chunks = _index_code_file(
            file, repo, code_collection, code_model, code_col, db,
            voyage_client, git_meta, now_iso, score,
            chunk_lines=effective_chunk_lines,
            force=force,
            embed_fn=_embed_fn,
        )
        if on_file:
            on_file(file, chunks, time.monotonic() - t0)

    # Index prose files → docs__ (voyage-context-3 via CCE)
    # NOTE: calls _index_prose_file (the module-level wrapper) — same reason.
    _log.debug("indexing prose files", count=len(prose_files))
    for score, file in prose_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        chunks = _index_prose_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
            force=force,
            timeout=read_timeout_seconds,
            embed_fn=_embed_fn,
        )
        if on_file:
            on_file(file, chunks, time.monotonic() - t0)

    # Index PDF files → docs__ (PDF extraction + voyage-context-3)
    _log.debug("indexing PDF files", count=len(pdf_files))
    for score, file in pdf_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        chunks = _index_pdf_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
            force=force,
            timeout=read_timeout_seconds,
            chunk_chars=tuning.pdf_chunk_chars,
            embed_fn=_embed_fn,
        )
        if on_file:
            on_file(file, chunks, time.monotonic() - t0)

    # Discover and index RDR markdown files → rdr__
    rdr_indexed, rdr_current, rdr_failed = _discover_and_index_rdrs(
        repo, rdr_abs_paths, db, voyage_key, now_iso, force=force
    )

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

    # Stamp pipeline version on force indexing (after all work completes)
    if force:
        stamp_collection_version(code_col)
        stamp_collection_version(docs_col)
        # Stamp RDR collection if it was indexed
        if rdr_indexed > 0:
            from nexus.registry import _rdr_collection_name
            rdr_col_name = _rdr_collection_name(repo)
            try:
                rdr_col = db.get_or_create_collection(rdr_col_name)
                stamp_collection_version(rdr_col)
            except Exception:
                _log.debug("rdr_stamp_skipped", collection=rdr_col_name)

    # Catalog hook: register indexed files (opt-in, graceful absence)
    indexed_for_catalog: list[tuple[Path, str, str]] = []
    for _, f in code_files:
        indexed_for_catalog.append((f, "code", code_collection))
    for _, f in prose_files:
        indexed_for_catalog.append((f, "prose", docs_collection))
    # Include RDR files so code→RDR provenance links can be generated
    # Register regardless of T3 indexing success — catalog tracks existence
    if rdr_abs_paths:
        from nexus.registry import _rdr_collection_name
        rdr_col = _rdr_collection_name(repo)
        for rdr_dir in rdr_abs_paths:
            if rdr_dir.is_dir():
                for md_file in sorted(rdr_dir.rglob("*.md")):
                    if md_file.is_file():
                        indexed_for_catalog.append((md_file, "rdr", rdr_col))
    _catalog_hook(
        repo=repo,
        repo_name=_repo_basename,
        repo_hash=_repo_hash,
        head_hash=_current_head(repo),
        indexed_files=indexed_for_catalog,
    )

    return {"rdr_indexed": rdr_indexed, "rdr_current": rdr_current, "rdr_failed": rdr_failed}


def _is_under(child: Path, parent: Path) -> bool:
    """Return True if *child* is a descendant of *parent*."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
