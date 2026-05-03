# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Code file indexing: AST chunking, context extraction, and Voyage AI embedding.

Extracted from indexer.py (RDR-032).  Public API::

    index_code_file(ctx: IndexContext, file_path: Path) -> int

The module owns _extract_context (AST context extraction) and the associated
language tables (_COMMENT_CHARS, DEFINITION_TYPES).
"""
from __future__ import annotations

import hashlib as _hl
from pathlib import Path

import structlog

from nexus.index_context import IndexContext
from nexus.indexer_utils import build_context_prefix, check_staleness
from nexus.languages import LANGUAGE_REGISTRY
from nexus.retry import _voyage_with_retry

_log = structlog.get_logger(__name__)

# Voyage AI embed() API limit: https://docs.voyageai.com/reference/embeddings-api
_VOYAGE_EMBED_BATCH_SIZE = 128

# Comment character for each language used to build the embed-only context prefix.
_COMMENT_CHARS: dict[str, str] = {
    "python": "#",
    "javascript": "//",
    "typescript": "//",
    "tsx": "//",
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
    "proto": "//",
    "elixir": "#",
    "haskell": "--",
    "clojure": ";",
    "dart": "//",
    "zig": "//",
    "julia": "#",
    "elisp": ";",
    "erlang": "%",
    "ocaml": "(*",
    "ocaml_interface": "(*",
    "perl": "#",
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
    "tsx": {
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
    "dart": {
        "class_definition": "class",
        "method_signature": "method",
        "function_signature": "function",
    },
    "haskell": {
        "function": "function",
        "data_type": "class",
    },
    "julia": {
        "function_definition": "function",
        "struct_definition": "class",
    },
    "ocaml": {
        "value_definition": "function",
        "type_definition": "class",
        "module_definition": "module",
    },
    "perl": {
        "subroutine_declaration_statement": "function",
    },
    "erlang": {
        "function_clause": "function",
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
    for field_name in ("name", "identifier"):
        child = node.child_by_field_name(field_name)
        if child:
            try:
                return child.text.decode("utf-8")
            except (UnicodeDecodeError, AttributeError) as exc:
                _log.debug("extract_name_decode_failed", error=str(exc), exc_info=True)
    for child in node.children:
        if child.type in ("identifier", "name"):
            try:
                return child.text.decode("utf-8")
            except (UnicodeDecodeError, AttributeError) as exc:
                _log.debug("extract_name_child_decode_failed", error=str(exc), exc_info=True)
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
    except Exception as exc:
        _log.warning("get_parser_failed", language=language, error=str(exc), exc_info=True)
        return ("", "")

    try:
        tree = parser.parse(source)
    except Exception as exc:
        _log.debug("tree_parse_failed", language=language, error=str(exc))
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


def index_code_file(ctx: IndexContext, file_path: Path) -> int:
    """Index a single code file into the code__ collection.

    Uses ``ctx`` in place of the old 12-parameter signature.  Reads file
    content, computes SHA-256, performs staleness check, AST-chunks, builds
    embed-only prefix per chunk, embeds via Voyage AI, and upserts to ChromaDB.

    Returns the post-filter chunk count (chunks upserted), or 0 if
    skipped (current) or failed.
    """
    from nexus.chunker import chunk_file

    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file_path), error=type(exc).__name__)
        return 0

    source_bytes = content.encode("utf-8")
    content_hash = _hl.sha256(source_bytes).hexdigest()
    ext = file_path.suffix.lower()
    language = LANGUAGE_REGISTRY.get(ext, "")
    comment_char = _COMMENT_CHARS.get(language, "#")
    rel_path = file_path.relative_to(ctx.repo_path)

    # Staleness check — skip if content + model unchanged.
    # nexus-dcym: prefer doc_id-keyed lookup when the catalog hook
    # supplied a resolver; falls back to source_path for legacy chunks.
    catalog_doc_id_for_staleness = (
        ctx.doc_id_resolver(file_path) if ctx.doc_id_resolver is not None else ""
    )
    if not ctx.force and check_staleness(
        ctx.col, file_path, content_hash, ctx.embedding_model,
        doc_id=catalog_doc_id_for_staleness,
    ):
        return 0

    # nexus-7niu: per-stage timer instrumentation. Silent when
    # ``ctx.stage_timers is None`` — no overhead, no output.
    _stage = (
        ctx.stage_timers.stage if ctx.stage_timers is not None
        else _noop_stage
    )

    with _stage("chunking"):
        chunks = chunk_file(file_path, content, chunk_lines=ctx.chunk_lines)
    if not chunks:
        _log.debug("skipped file with no chunks", path=str(file_path))
        return 0
    total_chunks = len(chunks)

    ids: list[str] = []
    documents: list[str] = []
    embed_texts: list[str] = []  # prefixed texts sent to Voyage AI; raw text stored in ChromaDB
    metadatas: list[dict] = []

    # Pre-compute char offsets for each 1-indexed line so each chunk
    # can carry chunk_start_char / chunk_end_char alongside line_start /
    # line_end. Code chunks previously shipped only line numbers, which
    # forced any reader needing precise character spans (e.g. catalog
    # `chash:<hex>:<start>-<end>` link spans, snippet rendering) to
    # re-read the source file just to compute offsets.
    _line_offsets = [0]
    for _i, _ch in enumerate(content):
        if _ch == "\n":
            _line_offsets.append(_i + 1)

    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415

    # Catalog Document.doc_id (RDR-101 Phase 3 PR δ): resolved once per
    # file. Empty string when no catalog handle exists; ``normalize``
    # Step 4c drops the field on the way to T3.
    catalog_doc_id = (
        ctx.doc_id_resolver(file_path) if ctx.doc_id_resolver is not None else ""
    )

    for i, chunk in enumerate(chunks):
        title = f"{rel_path}:{chunk['line_start']}-{chunk['line_end']}"
        # ``chunk_chroma_id`` is the per-chunk Chroma natural-id
        # (sha256-derived) — disambiguated from catalog
        # ``Document.doc_id`` per RDR-101 Phase 0 nexus-o6aa.3.
        chunk_chroma_id = _hl.sha256(f"{ctx.corpus}:{title}:chunk{i}".encode()).hexdigest()[:32]
        class_ctx, method_ctx = _extract_context(
            source_bytes, language, chunk["line_start"] - 1, chunk["line_end"] - 1
        )
        prefix = build_context_prefix(
            rel_path, comment_char, class_ctx, method_ctx,
            chunk["line_start"], chunk["line_end"],
        )
        _ls = chunk["line_start"]
        _le = chunk["line_end"]
        chunk_start_char = _line_offsets[_ls - 1] if 0 < _ls <= len(_line_offsets) else 0
        chunk_end_char = (
            _line_offsets[_le] if _le < len(_line_offsets) else len(content)
        )
        # Section context for code: innermost class + method (already
        # computed above for the embed prefix). Stored as " > "-joined
        # path matching the markdown / PDF convention so consumers can
        # filter / display uniformly across content types.
        section_chain = [s for s in (class_ctx, method_ctx) if s]
        section_title = " > ".join(section_chain)
        section_type = "method" if method_ctx else ("class" if class_ctx else "")
        metadata = make_chunk_metadata(
            content_type="code",
            chunk_index=chunk.get("chunk_index", i),
            chunk_count=chunk.get("chunk_count", total_chunks),
            chunk_text_hash=_hl.sha256(chunk["text"].encode()).hexdigest(),
            content_hash=content_hash,
            chunk_start_char=chunk_start_char,
            chunk_end_char=chunk_end_char,
            line_start=_ls,
            line_end=_le,
            indexed_at=ctx.now_iso,
            embedding_model=ctx.embedding_model,
            store_type="code",
            corpus=ctx.corpus,
            title=title,
            section_title=section_title,
            section_type=section_type,
            tags=ext.lstrip("."),
            category="code",
            frecency_score=float(ctx.score),
            git_meta=ctx.git_meta,
            doc_id=catalog_doc_id,
        )
        ids.append(chunk_chroma_id)
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
    with _stage("embed"):
        if ctx.embed_fn is not None:
            # Local mode: use injected embedding function
            for batch_start in range(0, total_chunks, _VOYAGE_EMBED_BATCH_SIZE):
                batch = embed_texts[batch_start : batch_start + _VOYAGE_EMBED_BATCH_SIZE]
                embeddings.extend(ctx.embed_fn(batch))
        else:
            for batch_start in range(0, total_chunks, _VOYAGE_EMBED_BATCH_SIZE):
                batch = embed_texts[batch_start : batch_start + _VOYAGE_EMBED_BATCH_SIZE]
                _log.debug(
                    "embedding batch",
                    file=str(file_path),
                    batch=f"{batch_start+1}-{min(batch_start+len(batch), total_chunks)}/{total_chunks}",
                )
                result = _voyage_with_retry(
                    ctx.voyage_client.embed,  # type: ignore[attr-defined]
                    texts=batch,
                    model=ctx.embedding_model,
                    input_type="document",
                )
                embeddings.extend(result.embeddings)

    with _stage("upload"):
        _log.debug("upserting", file=str(file_path), chunks=total_chunks)
        ctx.db.upsert_chunks_with_embeddings(  # type: ignore[attr-defined]
            collection_name=ctx.corpus,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

        # Post-store hook chains (RDR-095). Both single-doc and batch
        # chains fire from every storage event; the per-doc loop covers
        # single-shape consumers on CLI ingest.
        from nexus.mcp_infra import (
            fire_post_document_hooks,
            fire_post_store_batch_hooks,
            fire_post_store_hooks,
        )
        fire_post_store_batch_hooks(
            ids, ctx.corpus, documents, embeddings, metadatas,
        )
        for _did, _doc in zip(ids, documents):
            fire_post_store_hooks(_did, ctx.corpus, _doc)
        # RDR-089 document-grain chain — once per code-file boundary.
        # content="" (chunk-level scope only); hook reads source_path.
        # nexus-tdgc: forward catalog doc_id when available.
        fire_post_document_hooks(
            str(file_path), ctx.corpus, "",
            doc_id=catalog_doc_id,
        )

    return len(ids)


# No-op context manager used when ``ctx.stage_timers is None`` so the
# instrumented code paths stay single-shape regardless of timing mode.
from contextlib import contextmanager as _contextmanager


@_contextmanager
def _noop_stage(_name: str):
    yield
