# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code repository indexing pipeline."""
import fnmatch
import subprocess
import time
from collections.abc import Callable
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
    # Dependency lock / checksum files: auto-generated, not semantically useful,
    # and can produce chunks that exceed storage per-document size limits.
    "*.lock", "go.sum",
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
    ".cs": "c_sharp",
    ".sh": "bash",
    ".bash": "bash",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".r": "r",
    ".m": "objc",
    ".php": "php",
    ".lua": "lua",
    ".cxx": "cpp",
    ".kts": "kotlin",
    ".sc": "scala",
}

# Comment character for each language used to build the embed-only context prefix.
_COMMENT_CHARS: dict[str, str] = {
    "python": "#",
    "javascript": "//",
    "typescript": "//",
    "java": "//",
    "go": "//",
    "rust": "//",
    "cpp": "//",
    "c": "//",
    "c_sharp": "//",
    "ruby": "#",
    "php": "//",
    "swift": "//",
    "kotlin": "//",
    "scala": "//",
    "bash": "#",
    "r": "#",
    "objc": "//",
    "lua": "--",
}

# Tree-sitter node types → semantic code_type, per language.
# Ported from arcaneum/src/arcaneum/indexing/fulltext/ast_extractor.py:51-136.
# Used by _extract_context to identify class/method boundaries in code chunks.
DEFINITION_TYPES: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
        "decorated_definition": "decorated",
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "arrow_function": "function",
        "generator_function_declaration": "function",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "arrow_function": "function",
    },
    "java": {
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "class",
        "constructor_declaration": "method",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "class",
    },
    "rust": {
        "function_item": "function",
        "impl_item": "class",
        "struct_item": "class",
        "trait_item": "interface",
        "enum_item": "class",
    },
    "c": {
        "function_definition": "function",
        "struct_specifier": "class",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "class",
    },
    "c_sharp": {
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "class",
    },
    "ruby": {
        "method": "method",
        "class": "class",
        "module": "module",
    },
    "php": {
        "function_definition": "function",
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "class",
    },
    "swift": {
        "function_declaration": "function",
        "class_declaration": "class",
        "struct_declaration": "class",
        "protocol_declaration": "interface",
    },
    "kotlin": {
        "function_declaration": "function",
        "class_declaration": "class",
        "object_declaration": "class",
        "interface_declaration": "interface",
    },
    "scala": {
        "function_definition": "function",
        "class_definition": "class",
        "object_definition": "class",
        "trait_definition": "interface",
    },
    "r": {
        "function_definition": "function",
    },
    "lua": {
        "function_declaration": "function",
        "local_function": "function",
    },
}

_CLASS_SEMANTICS: frozenset[str] = frozenset({"class", "interface", "module"})
_METHOD_SEMANTICS: frozenset[str] = frozenset({"function", "method", "decorated"})


def _extract_name_from_node(node) -> str:  # type: ignore[no-untyped-def]
    """Extract the identifier name from a tree-sitter definition node.

    Adapted from arcaneum ast_extractor.py:366-386.  Uses the field-name API
    first (most grammars expose 'name' or 'identifier' as named fields), then
    falls back to scanning child nodes by type.

    For ``decorated_definition`` (Python), the identifier lives inside the
    wrapped ``function_definition`` / ``class_definition`` child — recurse
    into that child once to retrieve the name.

    Returns empty string (not 'anonymous') so callers can skip empty names.
    """
    # Python decorated_definition: the name is carried by the wrapped inner
    # definition node, not by the decorated_definition node itself.
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("function_definition", "class_definition", "async_function_definition"):
                return _extract_name_from_node(child)
        return ""
    for field in ("name", "identifier"):
        child = node.child_by_field_name(field)
        if child:
            try:
                return child.text.decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                pass
    for child in node.children:
        if child.type in ("identifier", "name"):
            try:
                return child.text.decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                pass
    return ""


def _extract_context(
    source: bytes,
    language: str,
    chunk_start_0idx: int,
    chunk_end_0idx: int,
) -> tuple[str, str]:
    """Return (class_name, method_name) for the chunk at the given 0-indexed line range.

    Walks the AST with depth-first pre-order traversal, pruning subtrees that
    lie entirely outside the chunk range.  Class and method are the innermost
    definitions whose ranges fully enclose the chunk — pre-order traversal
    ensures outer definitions are visited before inner ones, so overwriting
    gives the innermost result.

    Returns ('', '') when the language is unsupported, tree-sitter is
    unavailable, or no enclosing definition is found.
    """
    lang_types = DEFINITION_TYPES.get(language)
    if not lang_types:
        return ("", "")

    try:
        from tree_sitter_language_pack import get_parser  # lazy import
        parser = get_parser(language)
    except Exception:
        return ("", "")

    try:
        tree = parser.parse(source)
    except Exception:
        return ("", "")

    class_name = ""
    method_name = ""

    def _walk(node) -> None:  # type: ignore[no-untyped-def]
        nonlocal class_name, method_name
        node_start = node.start_point[0]
        node_end = node.end_point[0]

        # Prune: subtree lies entirely outside chunk range
        if node_end < chunk_start_0idx or node_start > chunk_end_0idx:
            return

        code_type = lang_types.get(node.type)
        if code_type:
            # Only record when this definition fully encloses the chunk so that
            # a chunk spanning two sibling methods returns method_name=''.
            if node_start <= chunk_start_0idx and node_end >= chunk_end_0idx:
                name = _extract_name_from_node(node)
                if name:
                    if code_type in _CLASS_SEMANTICS:
                        class_name = name  # innermost enclosing class wins
                    elif code_type in _METHOD_SEMANTICS:
                        method_name = name  # innermost enclosing method wins

        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return (class_name, method_name)


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


def index_repository(
    repo: Path,
    registry: "RepoRegistry",
    *,
    frecency_only: bool = False,
    chunk_lines: int | None = None,
    force: bool = False,
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
    ``frecency_score`` metadata field on existing T3 chunks.

    *chunk_lines* overrides the default chunk size (150 lines) for code files.
    When None, the module default is used.

    Returns a stats dict (empty for frecency_only runs) with keys:
    ``rdr_indexed``, ``rdr_current``, ``rdr_failed``.
    """
    registry.update(repo, status="indexing")
    try:
        if frecency_only:
            _run_index_frecency_only(repo, registry)
            stats: dict[str, int] = {}
        else:
            stats = _run_index(repo, registry, chunk_lines=chunk_lines, force=force, on_start=on_start, on_file=on_file)
        registry.update(repo, status="ready")
        return stats
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
        missing = []
        if not voyage_key:
            missing.append("voyage_api_key")
        if not chroma_key:
            missing.append("chroma_api_key")
        raise CredentialsMissingError(
            f"{', '.join(missing)} not set — run: nx config init"
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
    chunk_lines: int | None = None,
    force: bool = False,
) -> int:
    """Index a single code file into the code__ collection.

    Returns the post-filter chunk count (chunks upserted), or 0 if skipped/failed.
    """
    import hashlib as _hl
    from nexus.chunker import chunk_file

    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file), error=type(exc).__name__)
        return 0

    content_hash = _hl.sha256(content.encode()).hexdigest()
    source_bytes = content.encode("utf-8")
    ext = file.suffix.lower()
    language = _EXT_TO_LANGUAGE.get(ext, "")
    comment_char = _COMMENT_CHARS.get(language, "#")
    rel_path = file.relative_to(repo)

    # Staleness check
    existing = col.get(
        where={"source_path": str(file)},
        include=["metadatas"],
        limit=1,
    )
    if not force and existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
            return 0

    chunks = chunk_file(file, content, chunk_lines=chunk_lines)
    if not chunks:
        _log.debug("skipped file with no chunks", path=str(file))
        return 0
    total_chunks = len(chunks)

    ids: list[str] = []
    documents: list[str] = []
    embed_texts: list[str] = []  # prefixed texts sent to Voyage AI; raw text stored in ChromaDB
    metadatas: list[dict] = []

    for i, chunk in enumerate(chunks):
        title = f"{rel_path}:{chunk['line_start']}-{chunk['line_end']}"
        doc_id = _hl.sha256(f"{collection_name}:{title}:chunk{i}".encode()).hexdigest()[:32]
        class_ctx, method_ctx = _extract_context(
            source_bytes, language, chunk["line_start"] - 1, chunk["line_end"] - 1
        )
        prefix = (
            f"{comment_char} File: {rel_path}"
            f"  Class: {class_ctx}  Method: {method_ctx}"
            f"  Lines: {chunk['line_start']}-{chunk['line_end']}"
        )
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
            "programming_language": language,
            "corpus": collection_name,
            "embedding_model": target_model,
            "content_hash": content_hash,
            **git_meta,
        }
        ids.append(doc_id)
        documents.append(chunk["text"])
        embed_texts.append(f"{prefix}\n{chunk['text']}")
        metadatas.append(metadata)

    # Filter out empty documents before embedding (Voyage AI rejects empty strings);
    # keep embed_texts in sync with documents.
    valid = [
        (idx, d, m, et)
        for idx, d, m, et in zip(ids, documents, metadatas, embed_texts)
        if d and d.strip()
    ]
    if not valid:
        return 0
    ids, documents, metadatas, embed_texts = map(list, zip(*valid))

    # Embed using prefixed texts for improved retrieval quality; raw documents are stored.
    embeddings: list[list[float]] = []
    total_chunks = len(documents)
    for batch_start in range(0, total_chunks, _VOYAGE_EMBED_BATCH_SIZE):
        batch = embed_texts[batch_start : batch_start + _VOYAGE_EMBED_BATCH_SIZE]
        _log.debug("embedding batch", file=str(file), batch=f"{batch_start+1}-{min(batch_start+len(batch), total_chunks)}/{total_chunks}")
        result = voyage_client.embed(texts=batch, model=target_model, input_type="document")
        embeddings.extend(result.embeddings)

    _log.debug("upserting", file=str(file), chunks=total_chunks)
    db.upsert_chunks_with_embeddings(
        collection_name=collection_name,
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    return len(ids)


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
) -> int:
    """Index a single prose file into the docs__ collection.

    Uses SemanticMarkdownChunker for .md files, _line_chunk for all others.
    Embeds via _embed_with_fallback (CCE for voyage-context-3).

    Returns the post-filter chunk count (chunks upserted), or 0 if skipped/failed.
    """
    import hashlib as _hl
    from nexus.chunker import _line_chunk
    from nexus.doc_indexer import _embed_with_fallback
    from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter

    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file), error=type(exc).__name__)
        return 0

    content_hash = _hl.sha256(content.encode()).hexdigest()

    # Staleness check
    existing = col.get(
        where={"source_path": str(file)},
        include=["metadatas"],
        limit=1,
    )
    if not force and existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
            return 0

    ext = file.suffix.lower()
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    embed_texts: list[str] = []

    if ext in (".md", ".markdown"):
        # Markdown: use SemanticMarkdownChunker (M1: uses char offsets, not line numbers)
        frontmatter, body = parse_frontmatter(content)
        frontmatter_len = len(content) - len(body)
        base_meta: dict = {"source_path": str(file), "corpus": collection_name}
        chunks = SemanticMarkdownChunker().chunk(body, base_meta)
        if not chunks:
            _log.debug("skipped file with no chunks", path=str(file))
            return 0

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
            # Embed-only prefix: helps Voyage AI locate the right context without
            # polluting stored text.  Use header_path from chunk metadata (the raw
            # field, not the stored section_title which is the same string).
            header_path = chunk.metadata.get("header_path", "")
            if header_path:
                embed_texts.append(f"## Section: {header_path}\n\n{chunk.text}")
            else:
                embed_texts.append(chunk.text)
    else:
        # Non-markdown prose: use line-based chunking
        raw_chunks = _line_chunk(content)
        if not raw_chunks:
            if not content.strip():
                return 0
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
        return 0

    # For non-markdown prose, embed_texts is empty; normalise to documents so
    # the filter below can work uniformly across both paths.
    if not embed_texts:
        embed_texts = list(documents)

    # Filter empty documents before embedding (Voyage AI rejects empty strings).
    valid = [
        (i, d, m, et)
        for i, d, m, et in zip(ids, documents, metadatas, embed_texts)
        if d and d.strip()
    ]
    if not valid:
        return 0
    ids, documents, metadatas, embed_texts = map(list, zip(*valid))

    texts_to_embed = embed_texts

    # Embed via _embed_with_fallback (CCE for voyage-context-3)
    embeddings, actual_model = _embed_with_fallback(texts_to_embed, target_model, voyage_key)
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
    return len(ids)


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
) -> int:
    """Index a single PDF file into the docs__ collection.

    Uses PDF extraction + chunking from doc_indexer, embeds via _embed_with_fallback.
    Returns the post-filter chunk count (chunks upserted), or 0 if skipped/failed.
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
    if not force and existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash_hex and stored.get("embedding_model") == target_model:
            return 0

    prepared = _pdf_chunks(file, content_hash_hex, target_model, now_iso, collection_name)
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

    embeddings, actual_model = _embed_with_fallback(embed_texts_pdf, target_model, voyage_key)
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


def _run_index(
    repo: Path,
    registry: "RepoRegistry",
    chunk_lines: int | None = None,
    *,
    force: bool = False,
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
    from nexus.config import load_config
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

    # Credential check (required for T3 operations)
    from nexus.config import get_credential
    voyage_key = get_credential("voyage_api_key")
    chroma_key = get_credential("chroma_api_key")
    if not voyage_key or not chroma_key:
        missing = []
        if not voyage_key:
            missing.append("voyage_api_key")
        if not chroma_key:
            missing.append("chroma_api_key")
        raise CredentialsMissingError(
            f"{', '.join(missing)} not set — run: nx config init"
        )

    import voyageai
    from datetime import UTC, datetime as _dt
    from nexus.db import make_t3

    _log.debug("connecting to ChromaDB Cloud")
    db = make_t3()
    _log.debug("ChromaDB connected")
    now_iso = _dt.now(UTC).isoformat()

    # Initialize collections and models
    code_model = index_model_for_collection(code_collection)
    docs_model = index_model_for_collection(docs_collection)
    voyage_client = voyageai.Client(api_key=voyage_key)
    _log.debug("creating collections", code=code_collection, docs=docs_collection)
    code_col = db.get_or_create_collection(code_collection)
    docs_col = db.get_or_create_collection(docs_collection)
    _log.debug("collections ready")

    # Index code files → code__ (voyage-code-3, AST chunking)
    _log.debug("indexing code files", count=len(code_files))
    for score, file in code_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        chunks = _index_code_file(
            file, repo, code_collection, code_model, code_col, db,
            voyage_client, git_meta, now_iso, score,
            chunk_lines=chunk_lines,
            force=force,
        )
        if on_file:
            on_file(file, chunks, time.monotonic() - t0)

    # Index prose files → docs__ (voyage-context-3 via CCE)
    _log.debug("indexing prose files", count=len(prose_files))
    for score, file in prose_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        chunks = _index_prose_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
            force=force,
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
    return {"rdr_indexed": rdr_indexed, "rdr_current": rdr_current, "rdr_failed": rdr_failed}


def _is_under(child: Path, parent: Path) -> bool:
    """Return True if *child* is a descendant of *parent*."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
