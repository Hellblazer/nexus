# SPDX-License-Identifier: AGPL-3.0-or-later
"""AST-based code chunking with line-based fallback."""
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()

# Extensions supported by llama-index CodeSplitter / tree-sitter
AST_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".cs": "c_sharp",
    ".sh": "bash",
    ".bash": "bash",
}

_CHUNK_LINES = 150
_OVERLAP = 0.15
# ChromaDB Cloud enforces a 16 384-byte per-document hard limit.
# Keep a small buffer so metadata serialisation overhead doesn't tip us over.
_CHUNK_MAX_BYTES = 16_000


def _make_code_splitter(language: str, content: str, chunk_lines: int = _CHUNK_LINES) -> list:
    """Chunk *content* via CodeSplitter for *language*; returns list of nodes.

    All llama-index imports live here so tests can patch this single function.
    Uses tree-sitter-language-pack to obtain the parser, passing it explicitly
    to CodeSplitter (which otherwise tries the deprecated tree_sitter_languages).
    """
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*validate_default.*")
        from llama_index.core import Document  # type: ignore[import]
        from llama_index.core.node_parser import CodeSplitter  # type: ignore[import]
    from tree_sitter_language_pack import get_parser  # type: ignore[import]

    parser = get_parser(language)
    splitter = CodeSplitter(
        language=language,
        parser=parser,
        chunk_lines=chunk_lines,
        chunk_lines_overlap=int(chunk_lines * _OVERLAP),
    )
    doc = Document(text=content)
    return splitter.get_nodes_from_documents([doc])


def _line_chunk(
    content: str,
    chunk_lines: int = _CHUNK_LINES,
    overlap: float = _OVERLAP,
    max_bytes: int = _CHUNK_MAX_BYTES,
) -> list[tuple[int, int, str]]:
    """Split *content* into overlapping line-based chunks.

    Each chunk contains at most *chunk_lines* lines and at most *max_bytes*
    bytes (UTF-8 encoded).  When a window of *chunk_lines* would exceed
    *max_bytes*, the window is shrunk via binary search until it fits.
    A single line that is itself larger than *max_bytes* is emitted as-is
    (the byte limit cannot be honoured without splitting the line).

    The step between chunk starts is derived from the *actual* chunk size
    written, preserving the overlap ratio regardless of byte-capping.

    Returns list of (line_start, line_end, text) tuples (1-indexed).
    """
    lines = content.splitlines()
    n = len(lines)
    if n == 0:
        return []

    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < n:
        end = min(start + chunk_lines, n)
        chunk_text = "\n".join(lines[start:end])

        # Byte cap: binary-search for the largest window that fits.
        if len(chunk_text.encode()) > max_bytes:
            lo, hi = 1, end - start
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len("\n".join(lines[start : start + mid]).encode()) <= max_bytes:
                    lo = mid
                else:
                    hi = mid - 1
            # Always emit at least 1 line even if it alone exceeds max_bytes.
            end = start + max(1, lo)
            chunk_text = "\n".join(lines[start:end])

        chunks.append((start + 1, end, chunk_text))  # 1-indexed
        if end == n:
            break
        # Step is relative to actual chunk size to preserve overlap ratio.
        actual_lines = end - start
        start += max(1, int(actual_lines * (1 - overlap)))
    return chunks


def _enforce_byte_cap(
    chunks: list[dict[str, Any]],
    max_bytes: int = _CHUNK_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Post-process a chunk list and split any entry that exceeds *max_bytes*.

    Used for AST-produced chunks where CodeSplitter may emit a single node
    (e.g. a 400-line function body) that is larger than the storage limit.
    Renumbers chunk_index and chunk_count across the returned list.
    """
    result: list[dict[str, Any]] = []
    for chunk in chunks:
        text: str = chunk["text"]
        if len(text.encode()) <= max_bytes:
            result.append(chunk)
            continue

        # Oversized node: re-split line-by-line with binary-search byte cap.
        base_ls: int = chunk.get("line_start", 1)
        lines = text.splitlines()
        pos = 0
        while pos < len(lines):
            lo, hi = 1, len(lines) - pos
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len("\n".join(lines[pos : pos + mid]).encode()) <= max_bytes:
                    lo = mid
                else:
                    hi = mid - 1
            take = max(1, lo)
            sub_text = "\n".join(lines[pos : pos + take])
            result.append({
                **chunk,
                "text": sub_text,
                "line_start": base_ls + pos,
                "line_end": base_ls + pos + take - 1,
            })
            pos += take

    # Renumber indices across the (possibly expanded) list.
    total = len(result)
    for i, c in enumerate(result):
        c["chunk_index"] = i
        c["chunk_count"] = total
    return result


def chunk_file(file: Path, content: str, chunk_lines: int | None = None) -> list[dict[str, Any]]:
    """Chunk *file* content; use AST splitter for known extensions, else lines.

    Each returned dict contains:
        file_path, filename, file_extension, ast_chunked,
        chunk_index, chunk_count, line_start, line_end, text

    *chunk_lines* overrides the module default (_CHUNK_LINES = 150) when set.
    """
    effective_chunk_lines = chunk_lines if chunk_lines is not None else _CHUNK_LINES
    ext = file.suffix.lower()
    language = AST_EXTENSIONS.get(ext)

    base_meta = {
        "file_path": str(file),
        "filename": file.name,
        "file_extension": ext,
    }

    if language:
        try:
            nodes = _make_code_splitter(language, content, chunk_lines=effective_chunk_lines)
            if nodes:
                count = len(nodes)
                result = []
                for i, node in enumerate(nodes):
                    meta = {**base_meta, **node.metadata, "ast_chunked": True, "chunk_index": i, "chunk_count": count}
                    # Ensure line_start / line_end exist (CodeSplitter may or may not provide)
                    meta.setdefault("line_start", 1)
                    meta.setdefault("line_end", len(content.splitlines()))
                    meta["text"] = node.text
                    result.append(meta)
                # Post-process: split any AST node that exceeds the byte cap
                # (e.g. a single function body longer than _CHUNK_MAX_BYTES).
                return _enforce_byte_cap(result)
        except Exception:
            _log.debug("AST chunking failed, falling back to line chunks", file=str(file), exc_info=True)

    # Line-based fallback
    raw_chunks = _line_chunk(content, chunk_lines=effective_chunk_lines)
    if not raw_chunks:
        if not content.strip():
            return []
        raw_chunks = [(1, 1, content)]
    count = len(raw_chunks)
    result = []
    for i, (ls, le, text) in enumerate(raw_chunks):
        result.append(
            {
                **base_meta,
                "ast_chunked": False,
                "chunk_index": i,
                "chunk_count": count,
                "line_start": ls,
                "line_end": le,
                "text": text,
            }
        )
    return result
