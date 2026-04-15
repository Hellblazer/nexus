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
    """Title-case a single filename token, preserving known initialisms.

    Handles the common ``<initialism><digits>`` pattern (e.g. ``art1``,
    ``v2``) by preserving the alphabetic prefix in upper-case when it
    matches :data:`_PRESERVE_UPPER`. ``art1`` becomes ``ART1``;
    ``carpenter`` becomes ``Carpenter``.
    """
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

      1. **First H1 in *body*** — the first ``# Title`` line wins. H2
         and lower are not titles. Empty H1s are skipped.
      2. **Normalised filename stem** — split on ``[_\\- ]``, strip
         the extension, title-case each token (preserving common
         all-caps initialisms via :data:`_PRESERVE_UPPER`).

    Returns the bare stem when normalisation collapses to nothing.
    Used by :mod:`nexus.doc_indexer` to populate ``source_title`` on
    every markdown / PDF chunk so ``nx store list`` never displays
    ``untitled``.
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


def should_ignore(rel_path: Path, patterns: list[str]) -> bool:
    """Return True if any component of *rel_path* matches any of *patterns*."""
    for part in rel_path.parts:
        for pattern in patterns:
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


def check_staleness(
    col: object,
    source_file: object,
    content_hash: str,
    embedding_model: str,
) -> bool:
    """Return True if the file is already indexed with an identical hash and model.

    Performs a ChromaDB get() wrapped in _chroma_with_retry.  The retry logic
    is part of the staleness check's contract — callers must NOT wrap this call.

    Args:
        col: ChromaDB collection object.
        source_file: Path (or string) of the source file being checked.
        content_hash: SHA-256 hex digest of the current file content.
        embedding_model: Target embedding model name.

    Returns:
        True when the stored chunk has the same content_hash AND embedding_model,
        meaning the file is current and can be skipped.  False otherwise.
    """
    existing = _chroma_with_retry(
        col.get,  # type: ignore[attr-defined]
        where={"source_path": str(source_file)},
        include=["metadatas"],
        limit=1,
    )
    if not existing["metadatas"]:
        return False
    stored = existing["metadatas"][0]
    return (
        stored.get("content_hash") == content_hash
        and stored.get("embedding_model") == embedding_model
    )


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
