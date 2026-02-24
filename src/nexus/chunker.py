# SPDX-License-Identifier: AGPL-3.0-or-later
"""AST-based code chunking with line-based fallback."""
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()

# Extensions supported by llama-index CodeSplitter / tree-sitter
_AST_EXTENSIONS: dict[str, str] = {
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


def _make_code_splitter(language: str, content: str) -> list:
    """Chunk *content* via CodeSplitter for *language*; returns list of nodes.

    All llama-index imports live here so tests can patch this single function.
    Uses tree-sitter-language-pack to obtain the parser, passing it explicitly
    to CodeSplitter (which otherwise tries the deprecated tree_sitter_languages).
    """
    from llama_index.core import Document  # type: ignore[import]
    from llama_index.core.node_parser import CodeSplitter  # type: ignore[import]
    from tree_sitter_language_pack import get_parser  # type: ignore[import]

    parser = get_parser(language)
    splitter = CodeSplitter(
        language=language,
        parser=parser,
        chunk_lines=_CHUNK_LINES,
        chunk_lines_overlap=int(_CHUNK_LINES * _OVERLAP),
    )
    doc = Document(text=content)
    return splitter.get_nodes_from_documents([doc])


def _line_chunk(
    content: str,
    chunk_lines: int = _CHUNK_LINES,
    overlap: float = _OVERLAP,
) -> list[tuple[int, int, str]]:
    """Split *content* into overlapping line-based chunks.

    Returns list of (line_start, line_end, text) tuples (1-indexed).
    """
    lines = content.splitlines()
    n = len(lines)
    if n == 0:
        return []

    step = max(1, int(chunk_lines * (1 - overlap)))
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < n:
        end = min(start + chunk_lines, n)
        chunk_text = "\n".join(lines[start:end])
        chunks.append((start + 1, end, chunk_text))  # 1-indexed
        if end == n:
            break
        start += step
    return chunks


def chunk_file(file: Path, content: str) -> list[dict[str, Any]]:
    """Chunk *file* content; use AST splitter for known extensions, else lines.

    Each returned dict contains:
        file_path, filename, file_extension, ast_chunked,
        chunk_index, chunk_count, line_start, line_end, text
    """
    ext = file.suffix.lower()
    language = _AST_EXTENSIONS.get(ext)

    base_meta = {
        "file_path": str(file),
        "filename": file.name,
        "file_extension": ext,
    }

    if language:
        try:
            nodes = _make_code_splitter(language, content)
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
                return result
        except Exception:
            _log.debug("AST chunking failed, falling back to line chunks", file=str(file), exc_info=True)

    # Line-based fallback
    raw_chunks = _line_chunk(content)
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
