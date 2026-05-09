# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared utilities for the indexing pipeline.

Extracted from indexer.py to eliminate duplication across code_indexer,
prose_indexer, and _index_pdf_file.

Leaf-ish module: imports from nexus.retry and nexus.errors only.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from nexus.errors import CredentialsMissingError
from nexus.retry import _chroma_with_retry

_log = structlog.get_logger(__name__)

# Patterns always ignored (mirrors indexer.DEFAULT_IGNORE).
_DEFAULT_IGNORE: list[str] = [
    "node_modules", "vendor", ".venv", "__pycache__", "dist", "build", ".git",
    "*.lock", "go.sum",
]


def find_repo_root(path: Path) -> Path | None:
    """Return the git repository root containing *path*, or None.

    Uses ``git rev-parse --show-toplevel`` so it works from any subdirectory.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


#: Tokens that stay all-caps after filename normalisation. Common
#: initialisms / acronyms used in technical filenames where naive
#: title-casing would mis-render them ("api" → "Api" is wrong).
_PRESERVE_UPPER: frozenset[str] = frozenset({
    "ai", "ml", "api", "url", "uri", "pdf", "html", "css", "js",
    "ts", "json", "yaml", "xml", "sql", "cli", "ide", "io",
    "rdr", "mcp", "llm", "gpu", "cpu", "tcp", "udp", "ssl", "tls",
    "ssh", "ftp", "smtp", "http", "https", "rest", "rpc", "uuid",
    "art", "bert", "lstm", "rnn", "cnn", "gan",
    "nlp", "ocr", "tts", "stt",
    "v1", "v2", "v3", "v4", "v5",
})

_INITIALISM_DIGIT_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _normalise_filename_token(token: str) -> str:
    """Title-case a single filename token, preserving known initialisms."""
    if not token:
        return token
    lowered = token.lower()
    if lowered in _PRESERVE_UPPER:
        return token.upper()
    m = _INITIALISM_DIGIT_RE.match(token)
    if m is not None:
        prefix, digits = m.group(1), m.group(2)
        if prefix.lower() in _PRESERVE_UPPER:
            return prefix.upper() + digits
    return token.capitalize()


def derive_title(path: Path, body: str | None) -> str:
    """Resolve a human-readable document title (nexus-8l6).

    Two-step fallback:

      1. **First H1 in *body*** — the first ``# Title`` line wins.
      2. **Normalised filename stem** — split on ``[_\\- ]``, title-case
         each token (preserving common initialisms via ``_PRESERVE_UPPER``).
    """
    if body:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("# "):
                continue
            title = stripped[2:].strip()
            if title:
                return title

    stem = path.stem or path.name
    if not stem:
        return ""
    tokens = re.split(r"[_\- ]+", stem)
    normalised = [_normalise_filename_token(t) for t in tokens if t]
    return " ".join(normalised) or stem


def detect_git_metadata(path: Path) -> dict[str, str]:
    """Return git provenance metadata for the repo containing *path*.

    Walks up via :func:`find_repo_root`, then collects:

      * ``git_project_name`` — basename of the repo root
      * ``git_branch`` — current branch name
      * ``git_commit_hash`` — full SHA of HEAD
      * ``git_remote_url`` — ``origin`` URL (empty when no remote)

    Returns an empty dict when *path* is not inside a git repository
    so callers can ``**``-merge the result without conditional logic.
    Indexer-side code (PDF / markdown / pipeline) needs this so chunks
    carry the same provenance the repo-walk path gets via
    ``indexer._git_metadata`` (nexus-2my fix #3).
    """
    repo = find_repo_root(path)
    if repo is None:
        return {}

    def _run(args: list[str]) -> str:
        try:
            r = subprocess.run(
                args, cwd=repo, capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    return {
        "git_project_name": repo.name,
        "git_branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_commit_hash": _run(["git", "rev-parse", "HEAD"]),
        "git_remote_url": _run(["git", "remote", "get-url", "origin"]),
    }


def _path_glob_match(parts: tuple[str, ...], segs: list[str]) -> bool:
    """Match a tuple of path components against pattern segments.

    Segments are the result of ``pattern.split("/")``. Each non-``**``
    segment is matched against one path component via ``fnmatch`` (so
    ``*`` does NOT cross ``/`` boundaries — the path-aware semantic users
    expect from a slash-separated glob). ``**`` matches zero or more
    components.
    """
    def walk(i: int, j: int) -> bool:
        while j < len(segs):
            seg = segs[j]
            if seg == "**":
                # ** matches zero or more components — try every split.
                for k in range(i, len(parts) + 1):
                    if walk(k, j + 1):
                        return True
                return False
            if i >= len(parts):
                return False
            if not fnmatch.fnmatch(parts[i], seg):
                return False
            i += 1
            j += 1
        return i == len(parts)
    return walk(0, 0)


def should_ignore(rel_path: Path, patterns: list[str]) -> bool:
    """Return True if *rel_path* matches any of *patterns*.

    Pattern semantics (.gitignore-flavored, path-aware):

    - **Path-style** patterns (contain ``/``) match against the full
      relative path component-by-component. ``*`` matches any single
      component (does not cross ``/``); ``**`` matches zero or more
      components. ``docs/papers/**`` matches every file under
      ``docs/papers/`` at any depth; ``src/*.py`` matches Python files
      directly under ``src/`` only.
    - **Part-style** patterns (no ``/``) match against each path
      component independently via ``fnmatch``. ``papers`` matches any
      file under a ``papers/`` directory anywhere in its path;
      ``*.lock`` matches any lock file; ``__pycache__`` matches any
      ``__pycache__`` directory. This preserves the original behaviour
      and the existing ``_DEFAULT_IGNORE`` patterns.

    Pre-fix history: this function used to feed each path component to
    ``fnmatch.fnmatch`` against every pattern. ``fnmatch`` treats ``/``
    as a literal, so a path-style pattern like ``docs/papers/**`` was
    silently ineffective — the matcher only ever saw the parts ``docs``,
    ``papers``, and ``foo.pdf`` independently, none of which can match a
    pattern containing ``/``. Configs that wrote ``docs/papers/**``
    expecting subtree exclusion were silent no-ops; the only patterns
    that worked were single-component or extension globs.
    """
    rel_parts = rel_path.parts
    for pattern in patterns:
        if "/" in pattern:
            segs = pattern.split("/")
            if _path_glob_match(rel_parts, segs):
                return True
        else:
            for part in rel_parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
    return False


def load_ignore_patterns(repo_root: Path | None = None) -> list[str]:
    """Return merged ignore patterns from defaults + ``.nexus.yml``.

    When *repo_root* is provided, picks up the per-repo config.
    """
    from nexus.config import load_config
    cfg = load_config(repo_root=repo_root)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    return list(dict.fromkeys(_DEFAULT_IGNORE + cfg_patterns))


def is_gitignored(path: Path, repo_root: Path) -> bool:
    """Return True if *path* is ignored by git in *repo_root*.

    Uses ``git check-ignore`` for an authoritative answer that respects
    ``.gitignore``, ``.git/info/exclude``, and global gitignore config.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=repo_root, capture_output=True, timeout=10,
        )
        return result.returncode == 0  # 0 = ignored, 1 = not ignored
    except Exception:
        return False


@dataclass
class StalenessCache:
    """Pre-fetched ``{lookup_key: (content_hash, embedding_model)}`` map
    for a single ChromaDB collection.

    Built once by :func:`build_staleness_cache` from a single paginated
    sweep of the collection's chunk metadata; consumed many times by
    :func:`check_staleness` so per-file checks become O(1) dict lookups
    instead of O(N) ChromaDB roundtrips.

    Two indexes:

    - ``by_doc_id`` keys on the catalog tumbler stored in chunk
      metadata. Populated only for chunks whose stored ``doc_id`` field
      is non-empty. The post-RDR-101-Phase-4 write path stamps
      ``doc_id`` on every new chunk; legacy chunks predating the
      backfill are absent from this index, which the cached
      ``check_staleness`` correctly treats as a cache miss → "stale" →
      re-index → ghost-chunk healed.
    - ``by_source_path`` keys on the chunk's ``source_path`` metadata.
      Populated for every chunk that carries a non-empty source_path
      (most of them; RDR-102 D2 dropped source_path from the canonical
      schema, so post-D2 chunks won't appear here — they live only in
      ``by_doc_id``). Used only when the caller has no doc_id (legacy /
      catalog absent code path).

    Why the cache exists: ``nx index repo`` on a healthy repo where
    nothing has changed is dominated by per-file
    ``col.get(where={doc_id})`` roundtrips just to confirm "yes,
    current, skip." On ART (~4,800 files) that's ~4,800 sequential
    ChromaDB Cloud calls at 50-200 ms each — 8-30 minutes of pure
    network latency before any actual indexing work. With the cache,
    the entire staleness phase is a single paginated sweep
    (~ceil(total_chunks / 300) calls) plus per-file dict lookups.
    """

    by_doc_id: dict[str, tuple[str, str]] = field(default_factory=dict)
    by_source_path: dict[str, tuple[str, str]] = field(default_factory=dict)


def build_staleness_cache(col: object) -> StalenessCache:
    """Walk *col* once and index its chunks for fast staleness lookup.

    Pulls every chunk's ``include=["metadatas"]`` via the standard
    paginated helper. Calls per pull are ChromaDB Cloud's 300-record
    cap, so the total round-trip count is ``ceil(N / 300)`` where N is
    the collection's chunk count — independent of the number of files
    being indexed.

    Errors are tolerated: a build failure returns an empty cache and
    callers fall through to the per-file Chroma path. Failing to
    populate the cache must never block indexing; it just costs latency.
    """
    cache = StalenessCache()
    try:
        # Local import to avoid a circular dependency at module-load
        # time. ``_paginated_get`` lives in nexus.indexer (the
        # orchestrator), which itself imports from this module.
        from nexus.indexer import _paginated_get

        all_chunks = _paginated_get(col, include=["metadatas"])
    except Exception:
        return cache

    metadatas = all_chunks.get("metadatas") or []
    for meta in metadatas:
        if not meta:
            continue
        content_hash = meta.get("content_hash", "")
        model = meta.get("embedding_model", "")
        if not (content_hash and model):
            continue
        value = (content_hash, model)
        doc_id = meta.get("doc_id", "")
        if doc_id:
            cache.by_doc_id[doc_id] = value
        source_path = meta.get("source_path", "")
        if source_path:
            cache.by_source_path[source_path] = value
    return cache


def check_staleness(
    col: object,
    source_file: object,
    content_hash: str,
    embedding_model: str,
    *,
    doc_id: str = "",
    cache: StalenessCache | None = None,
) -> bool:
    """Return True if the file is already indexed with an identical hash and model.

    Two execution modes:

    - **Cached (preferred when the orchestrator passes a cache).**
      Looks up ``doc_id`` in :attr:`StalenessCache.by_doc_id` (or
      ``source_file`` in :attr:`StalenessCache.by_source_path` for
      legacy / no-catalog callers). Pure dict lookup, no ChromaDB
      roundtrip. The orchestrator builds the cache once per collection
      via :func:`build_staleness_cache` before the per-file loop, so
      ``nx index repo`` on a healthy repo (most files current) pays
      one paginated sweep instead of one Chroma query per file.
    - **Per-file (back-compat).** When *cache* is ``None``, performs a
      ChromaDB ``get()`` wrapped in ``_chroma_with_retry``. The retry
      logic is part of the staleness check's contract — callers must
      NOT wrap this call. Direct test callers and any caller that has
      not migrated to the cache stay on this path.

    Args:
        col: ChromaDB collection object. Unused when *cache* is supplied.
        source_file: Path (or string) of the source file being checked.
        content_hash: SHA-256 hex digest of the current file content.
        embedding_model: Target embedding model name.
        doc_id: Catalog ``doc_id`` for the file. When non-empty (RDR-101
            Phase 4, nexus-dcym), the chunk lookup keys on ``doc_id`` so
            that the staleness check stays consistent across renames and
            owner-scope changes. Empty falls back to the legacy
            ``source_path``-keyed lookup for chunks predating the
            doc_id backfill.
        cache: Optional :class:`StalenessCache`. When supplied the
            check is a dict lookup; when ``None`` the check is a Chroma
            roundtrip.

    Returns:
        True when the stored chunk has the same content_hash AND embedding_model,
        meaning the file is current and can be skipped.  False otherwise.
    """
    if cache is not None:
        if doc_id:
            stored = cache.by_doc_id.get(doc_id)
            # Cache miss when the caller has a doc_id heals a ghost
            # chunk by treating the file as stale: re-index will write
            # a chunk carrying doc_id metadata and the next sweep
            # populates by_doc_id for it. Mirrors the Chroma-path
            # behaviour at indexer_utils.check_staleness:291.
            if stored is None:
                return False
            return stored == (content_hash, embedding_model)
        # Legacy / no-doc_id caller: fall back to source_path lookup.
        stored = cache.by_source_path.get(str(source_file))
        if stored is None:
            return False
        return stored == (content_hash, embedding_model)

    # RDR-108 Phase 3 (nexus-bdag): chunks no longer carry ``doc_id`` —
    # the catalog ``document_chunks`` manifest is authoritative. Query
    # by ``content_hash`` (a file-level fingerprint that all chunks of
    # the same file share); falling back to ``source_path`` for legacy
    # chunks predating RDR-102 D2.
    where: dict
    if content_hash:
        where = {"content_hash": content_hash}
    else:
        where = {"source_path": str(source_file)}
    existing = _chroma_with_retry(
        col.get,  # type: ignore[attr-defined]
        where=where,
        include=["metadatas"],
        limit=1,
    )
    if not existing["metadatas"]:
        return False
    stored = existing["metadatas"][0]
    if (
        stored.get("content_hash") != content_hash
        or stored.get("embedding_model") != embedding_model
    ):
        return False
    return True


def check_credentials(voyage_key: str, chroma_key: str) -> None:
    """Raise CredentialsMissingError if either API key is absent.

    Args:
        voyage_key: Voyage AI API key string (empty string = missing).
        chroma_key: ChromaDB API key string (empty string = missing).

    Raises:
        CredentialsMissingError: When one or both keys are absent.
    """
    missing: list[str] = []
    if not voyage_key:
        missing.append("voyage_api_key")
    if not chroma_key:
        missing.append("chroma_api_key")
    if missing:
        raise CredentialsMissingError(
            f"{', '.join(missing)} not set — run: nx config init"
        )


def check_local_path_writable() -> None:
    """Validate that the local ChromaDB path is writable.

    Raises:
        CredentialsMissingError: When the local path cannot be written to.
    """
    from nexus.config import _default_local_path
    local_path = _default_local_path()
    try:
        local_path.mkdir(parents=True, exist_ok=True)
        test_file = local_path / ".write_test"
        test_file.touch()
        test_file.unlink()
    except OSError as exc:
        raise CredentialsMissingError(
            f"Local ChromaDB path {local_path} is not writable: {exc}"
        ) from exc


def build_context_prefix(
    filename: object,
    comment_char: str,
    class_name: str,
    method_name: str,
    line_start: int,
    line_end: int,
) -> str:
    """Return the embed-only context prefix for a code chunk.

    The prefix is prepended to chunk text before embedding (not stored in
    ChromaDB) to improve retrieval quality by giving Voyage AI additional
    context about the chunk's location in the codebase.

    Args:
        filename: Relative file path (str or Path).
        comment_char: Language comment character (e.g. "#", "//", "--").
        class_name: Enclosing class name from _extract_context, or "".
        method_name: Enclosing method name from _extract_context, or "".
        line_start: 1-indexed start line of the chunk.
        line_end: 1-indexed end line of the chunk.

    Returns:
        A single-line string like::

            # File: src/foo.py  Class: MyClass  Method: my_method  Lines: 10-25
    """
    return (
        f"{comment_char} File: {filename}"
        f"  Class: {class_name}  Method: {method_name}"
        f"  Lines: {line_start}-{line_end}"
    )
