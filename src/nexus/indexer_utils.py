# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared utilities for the indexing pipeline.

Extracted from indexer.py to eliminate duplication across code_indexer,
prose_indexer, and _index_pdf_file.

Leaf-ish module: imports from nexus.retry and nexus.errors only.
"""
from __future__ import annotations

import structlog

from nexus.errors import CredentialsMissingError
from nexus.retry import _chroma_with_retry

_log = structlog.get_logger(__name__)


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
