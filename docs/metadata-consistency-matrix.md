# T3 Chunk Metadata Consistency Matrix

**Date**: 2026-04-26
**Branch**: `feature/nexus-b9g1-rdr-089-aspect-extraction`
**Scope**: every `ALLOWED_TOP_LEVEL` field in `src/nexus/metadata_schema.py:49–88` × every indexer / chunker that writes T3 chunk metadata.
**Why this exists**: catching silent metadata gaps after data is in T3 is expensive. This matrix pins the current population state, names every gap, and gives a fix shape that makes future drift impossible to commit accidentally.

> The "32" in the title comes from Chroma Cloud's `MAX_RECORD_METADATA_KEYS = 32`. Nexus sits at **31 allowed keys + 1-key safety margin** in `metadata_schema.MAX_SAFE_TOP_LEVEL_KEYS`. Row #32 below covers `indexed_at` — written by every indexer and silently dropped by `normalize()` because it's not in the allow-list.

---

## Indexer paths covered

| Path id | Source | Routes to | Chunker |
|---|---|---|---|
| `code` | `src/nexus/code_indexer.py:298` (`index_code_file`) | `code__*` | `nexus.chunker.chunk_file` (AST + line splitter) |
| `prose-md` | `src/nexus/prose_indexer.py` markdown branch | `docs__*` | `SemanticMarkdownChunker` |
| `prose-line` | `src/nexus/prose_indexer.py` line-fallback branch | `docs__*` | `_line_chunk` |
| `pdf-doc_indexer` | `src/nexus/doc_indexer.py:_pdf_chunks` (incremental path) | `knowledge__*` / `docs__*` | `PDFChunker` |
| `pdf-pipeline_stages` | `src/nexus/pipeline_stages.py:_build_chunk_metadata` (streaming path) | `knowledge__*` / `docs__*` | `PDFChunker` |
| `rdr-md` | `src/nexus/doc_indexer.py:_markdown_chunks` (RDR ingest) | `rdr__*` | `SemanticMarkdownChunker` |
| `mcp put` | `src/nexus/db/t3.py:put` via `mcp/core.py:store_put` and `commands/store.py` | any prefix | none — single doc, no chunking |

---

## Legend

| Symbol | Meaning |
|---|---|
| ✅ | Populated meaningfully |
| ⚠️ | Populated with a default placeholder (`""` / `0`) — present but uninformative |
| ❌ | Should be set on this path but isn't (gap) |
| N/A | Not relevant for this path |
| 🔧 | Fixed in this session (RDR-089 follow-up) |

---

## The matrix

| # | Field | code | prose-md | prose-line | pdf-doc_indexer | pdf-pipeline_stages | rdr-md | mcp put | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1  | `source_path`            | ✅ abs            | ✅ abs            | ✅ abs            | ✅ abs            | ✅ abs            | ✅ rel       | ⚠️ ""              | rdr-md should be abs for consistency. mcp put has no on-disk source. |
| 2  | `content_hash`           | ✅                | ✅                | ✅                | ✅                | ✅                | ✅           | ❌                 | mcp put: should hash the content (single-chunk doc). |
| 3  | `chunk_text_hash` (chash)| ✅                | ✅                | ✅                | ✅                | ✅                | ✅           | ❌                 | **RDR-086 coverage gap** — MCP-stored docs never get a chash row, so `chash:<hex>` link spans can't resolve them. |
| 4  | `chunk_index`            | ✅                | ✅                | ✅                | ✅                | ✅                | ✅           | ❌                 | mcp put: should be `0` (single chunk). |
| 5  | `chunk_count`            | ✅                | ✅                | ✅                | ✅                | ✅                | ✅           | ❌                 | mcp put: should be `1`. |
| 6  | `chunk_start_char`       | ✅ 🔧             | ✅                | ✅ 🔧             | ✅                | ✅                | ✅           | ⚠️ should be `0`   | Closed today on `code` and `prose-line`. |
| 7  | `chunk_end_char`         | ✅ 🔧             | ✅                | ✅ 🔧             | ✅                | ✅                | ✅           | ⚠️ should be `len(content)` | Closed today on `code` and `prose-line`. |
| 8  | `line_start`             | ✅                | ⚠️ 0              | ✅                | ❌                | ❌                | ❌           | ❌                 | PDF/RDR/MCP: set `0` explicitly so reads are uniform. |
| 9  | `line_end`               | ✅                | ⚠️ 0              | ✅                | ❌                | ❌                | ❌           | ❌                 | Same. |
| 10 | `page_number`            | ❌                | ❌                | ❌                | ✅                | ✅                | ⚠️ 0         | ❌                 | code/prose/MCP: set `0` explicitly. |
| 11 | `title`                  | ✅ `path:lines`   | ✅ `path:chunk-N` | ✅ `path:lines`   | ❌                | ❌                | ❌           | ✅ caller          | PDF/RDR: should set (mirror `source_title` or filename stem). |
| 12 | `source_title`           | ❌                | ❌                | ❌                | ✅                | ✅                | ✅           | ❌                 | code/prose/MCP: should mirror title or filename. |
| 13 | `source_author`          | ❌                | ❌                | ❌                | ✅                | ✅                | ✅           | ❌                 | code/prose: derive from git author? MCP: caller arg? |
| 14 | `section_title`          | ❌                | ✅                | ✅ 🔧             | ✅ 🔧             | ✅ 🔧             | ✅           | ❌                 | code: should use class/method name from `_extract_context` (already computed for embed prefix, not stored). |
| 15 | `section_type`           | ❌                | ✅                | ✅ 🔧             | ✅ 🔧             | ✅ 🔧             | ✅           | ❌                 | code: should use `definition_type` (function / class / method). |
| 16 | `tags`                   | ✅ ext            | ✅ "markdown"     | ✅ ext            | ❌                | ❌                | ❌           | ✅ caller          | PDF/RDR: should set ("pdf", "rdr"). |
| 17 | `category`               | ✅ "code"         | ✅ "prose"        | ✅ "prose"        | ❌                | ❌                | ❌           | ✅ caller          | PDF: "paper"; RDR: "rdr". |
| 18 | `content_type`           | ✅ stamped        | ✅ stamped        | ✅ stamped        | ✅ stamped        | ✅ stamped        | ✅ stamped   | ✅ stamped         | Injected by `metadata_schema.normalize()` — uniform across all paths. |
| 19 | `store_type`             | ✅ "code"         | ✅ "prose"        | ✅ "prose"        | ✅ "pdf"          | ✅ "pdf"          | ✅ "markdown"| ⚠️ default         | mcp uses "knowledge" default — verify caller paths. |
| 20 | `corpus`                 | ✅                | ✅                | ✅                | ✅                | ✅                | ✅           | ❌                 | mcp put: derive from collection prefix. |
| 21 | `embedding_model`        | ✅                | ✅                | ✅                | ✅                | ✅                | ✅           | ✅                 | Uniform across all paths. |
| 22 | `bib_year`               | N/A               | N/A               | N/A               | ⚠️ 0 unless `--enrich` | ⚠️ 0 unless `--enrich` | N/A    | N/A                | Dropped together by `normalize()` when all-empty — by-design. |
| 23 | `bib_authors`            | N/A               | N/A               | N/A               | ⚠️ ""             | ⚠️ ""             | N/A          | N/A                | Same. |
| 24 | `bib_venue`              | N/A               | N/A               | N/A               | ⚠️ ""             | ⚠️ ""             | N/A          | N/A                | Same. |
| 25 | `bib_citation_count`     | N/A               | N/A               | N/A               | ⚠️ 0              | ⚠️ 0              | N/A          | N/A                | Same. |
| 26 | `ttl_days`               | ⚠️ 0              | ⚠️ 0              | ⚠️ 0              | ❌                | ❌                | ❌           | ✅ from caller     | PDF/RDR: set `0`. |
| 27 | `expires_at`             | ⚠️ ""             | ⚠️ ""             | ⚠️ ""             | ❌                | ❌                | ❌           | ✅ derived         | PDF/RDR: set `""`. |
| 28 | `frecency_score`         | ✅ git-derived    | ✅ git-derived    | ✅ git-derived    | ❌                | ❌                | ❌           | ❌                 | PDF/RDR/MCP: set `0.0` (or compute from source-file mtime if available). |
| 29 | `source_agent`           | ✅ "nexus-indexer"| ✅ "nexus-indexer"| ✅ "nexus-indexer"| ❌                | ❌                | ❌           | ✅ caller          | PDF/RDR: set "nexus-indexer". |
| 30 | `session_id`             | ⚠️ ""             | ⚠️ ""             | ⚠️ ""             | ❌                | ❌                | ❌           | ✅ caller          | PDF/RDR: set "". |
| 31 | `git_meta` (consolidated)| ✅ via `**ctx.git_meta` | ✅ same     | ✅ same           | ✅ via `**git_meta` | ✅ via `**git_meta` | ✅ via `**git_meta` | ❌       | mcp: capture cwd's git context. |
| 32 | `indexed_at`             | ⚠️ written → **dropped** | ⚠️ written → **dropped** | ⚠️ written → **dropped** | ⚠️ written → **dropped** | ⚠️ written → **dropped** | ⚠️ written → **dropped** | ⚠️ written → **dropped** | **NOT in `ALLOWED_TOP_LEVEL`** — every indexer wastes work writing it. Either add to allow-list or stop writing. |

---

## Gap clusters

### Cluster A — PDF + RDR are missing the lifecycle/identity cluster

PDF (both paths) and RDR-md ship none of these even though `code` / `prose` paths set them on every chunk:

- `title` (use `source_title` or filename stem)
- `tags` ("pdf" / "rdr")
- `category` ("paper" / "rdr")
- `line_start` / `line_end` (set `0` explicitly — these aren't lined documents but readers expect the key)
- `frecency_score` (set `0.0`, or compute from source-file mtime when in a git repo)
- `source_agent` (set `"nexus-indexer"`)
- `session_id` (set `""`)
- `ttl_days` (set `0`)
- `expires_at` (set `""`)

That's **9 fields × 2 paths = 18 missed writes** that any reader filtering on these fields would treat as if the row didn't exist.

### Cluster B — Code chunks are missing section context

`code_indexer.py:_extract_context` already computes `class_ctx` / `method_ctx` for the embed prefix. They're then thrown away. Should also be stored as:

- `section_title` = the innermost enclosing class / method name
- `section_type` = `definition_type` from the AST (function / class / method)

This unblocks per-aspect retrieval on code chunks (analog to what we just did for PDFs) and makes "show me all chunks inside class X" a straight `where=` filter.

### Cluster C — `mcp put` skips chunk-identity fields

`db/t3.py:put` (called by both `mcp/core.py:store_put` and `commands/store.py`) writes a 10-key metadata dict. Missing:

- `content_hash`, `chunk_text_hash` — **RDR-086 coverage hole**: MCP-stored docs never get a chash row, so `chash:<hex>` link spans can't resolve them
- `chunk_index = 0`, `chunk_count = 1` — single-chunk docs still need the keys for uniform reads
- `corpus` — derive from collection prefix
- `git_meta` — capture cwd's git context

### Cluster D — `indexed_at` is dropped silently

Every indexer computes and writes `indexed_at`; `normalize()` drops it because it's not in `ALLOWED_TOP_LEVEL`. Either:

- Add `indexed_at` to the allow-list (some readers genuinely want it for staleness display), **or**
- Stop writing it from every indexer (saves ~6 lines per indexer)

Pick one. The current state is the worst of both worlds: indexers do the work and the value never lands.

### Cluster E — `bib_*` is intentional and correct

Conditional drop-when-all-empty is the right behaviour. No change.

---

## Proposed fix shape

A single factory in `metadata_schema.py`:

```python
def make_chunk_metadata(
    content_type: str,
    *,
    # Identity (required)
    source_path: str,
    chunk_index: int,
    chunk_count: int,
    chunk_text_hash: str,
    content_hash: str,
    # Position (required where meaningful, else default)
    chunk_start_char: int = 0,
    chunk_end_char: int = 0,
    line_start: int = 0,
    line_end: int = 0,
    page_number: int = 0,
    # Display
    title: str = "",
    source_title: str = "",
    source_author: str = "",
    section_title: str = "",
    section_type: str = "",
    tags: str = "",
    category: str = "",
    # Routing (required)
    store_type: str,
    corpus: str,
    embedding_model: str,
    # Bibliographic (optional)
    bib_year: int = 0,
    bib_authors: str = "",
    bib_venue: str = "",
    bib_citation_count: int = 0,
    # Lifecycle
    ttl_days: int = 0,
    expires_at: str = "",
    frecency_score: float = 0.0,
    source_agent: str = "",
    session_id: str = "",
    # Provenance
    git_meta: dict | None = None,
) -> dict:
    """Build a complete T3 chunk metadata dict. Every ALLOWED_TOP_LEVEL
    key gets a value (defaults documented). Routes through normalize()
    which packs git_meta and stamps content_type."""
    raw = {
        "source_path": source_path,
        "chunk_index": chunk_index,
        # ... all 30 fields ...
    }
    if git_meta:
        for k, v in git_meta.items():
            raw[k] = v  # normalize() packs git_* → git_meta
    return normalize(raw, content_type=content_type)
```

Every indexer then becomes:

```python
metadata = make_chunk_metadata(
    content_type="pdf",
    source_path=str(pdf_path),
    chunk_index=chunk.chunk_index,
    chunk_count=len(chunks),
    chunk_text_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
    content_hash=content_hash,
    chunk_start_char=chunk.metadata["chunk_start_char"],
    chunk_end_char=chunk.metadata["chunk_end_char"],
    page_number=chunk.metadata.get("page_number", 0),
    section_title=chunk.metadata.get("section_title", ""),
    section_type=chunk.metadata.get("section_type", ""),
    title=source_title or pdf_path.stem,
    source_title=source_title,
    source_author=result.metadata.get("pdf_author", ""),
    tags="pdf",
    category="paper",
    store_type="pdf",
    corpus=corpus,
    embedding_model=target_model,
    source_agent="nexus-indexer",
    git_meta=git_meta,
)
```

Pinned by a test:

```python
def test_every_indexer_emits_full_metadata_keyset():
    """Every chunked-write indexer must produce metadata covering the
    full ALLOWED_TOP_LEVEL set (with documented defaults). Drift is
    a regression."""
    expected = ALLOWED_TOP_LEVEL  # 31 fields
    # ... drive each indexer with a minimal fixture ...
    # ... assert set(metadata.keys()) == expected ...
```

---

## Recommended landing order

1. **Land `make_chunk_metadata` factory** in `metadata_schema.py` + the pinning test.
2. **Retrofit PDF paths** (`doc_indexer._pdf_chunks` and `pipeline_stages._build_chunk_metadata`) — closes Cluster A's biggest source of missing fields.
3. **Retrofit RDR markdown** (`doc_indexer._markdown_chunks`) — same pattern.
4. **Retrofit code + prose** — already mostly populated; this just routes through the factory and fills in the few missing keys (`source_title`, `source_author`, `page_number=0`).
5. **Add code section context** — populate `section_title` / `section_type` from `_extract_context` (Cluster B).
6. **Fix `mcp put`** — add `chunk_index=0`, `chunk_count=1`, content/chash hashes, `corpus`, `git_meta` (Cluster C). This also closes the RDR-086 chash coverage hole on MCP-stored docs.
7. **Resolve `indexed_at`** — pick one of the two options in Cluster D.

Each step ships independently and is small enough to PR-review in isolation. The pinning test from step 1 prevents Steps 2–6 from regressing as we go.

---

## Status: refactor landed

`make_chunk_metadata` factory in `src/nexus/metadata_schema.py` is now the single entrypoint for every chunked-write indexer. Schema reduced from 31 → 30 keys (`source_title` + `expires_at` removed, `indexed_at` promoted from silently-dropped to allow-listed).

Pinning test in `tests/test_metadata_consistency.py` asserts every chunked path emits the full key set; drift now fails CI.

**Indexer paths retrofitted through the factory:**

- `src/nexus/code_indexer.py:357` (`code__*`)
- `src/nexus/prose_indexer.py` markdown branch (`docs__*`)
- `src/nexus/prose_indexer.py` line-fallback branch (`docs__*`)
- `src/nexus/doc_indexer.py:_pdf_chunks` (`knowledge__*` / `docs__*` PDF batch path)
- `src/nexus/pipeline_stages.py:_build_chunk_metadata` (`knowledge__*` / `docs__*` PDF streaming path)
- `src/nexus/doc_indexer.py:_markdown_chunks` (`rdr__*`)
- `src/nexus/db/t3.py:put` (MCP `store_put` backend) — closes RDR-086 chash coverage hole for MCP-stored docs

**Read-side migrations:**

- All `r.metadata.get("source_title") or r.metadata.get("title")` fallback chains in `mcp/core.py`, `commands/store.py`, `commands/catalog.py`, `commands/enrich.py`, `commands/index.py`, `indexer.py` collapsed to direct `title` reads.
- T3 expire-guard (`db/t3.py:expire`) migrated from `where=expires_at < now` to `is_expired()` Python-side check.
- `nx store list` and `mcp store_list` derive expiry display from `indexed_at + ttl_days`.

## Resolved decisions

- **Collapse `source_title` into `title`.** Every consumer already does `r.metadata.get("source_title") or r.metadata.get("title")` — they're the same field, populated inconsistently. Drop `source_title` from `ALLOWED_TOP_LEVEL`. `title` semantically becomes "best document-level human name" on every path:
  - code: filename (`indexer.py`)
  - prose-md: frontmatter title or first H1 or filename
  - prose-line: filename
  - pdf: paper title (today's `source_title` value)
  - rdr-md: RDR title
  - mcp put: caller-provided
- **Drop `expires_at`. Store `indexed_at` instead.** Today `indexed_at` is computed by every indexer and silently dropped by `normalize()` because it isn't in the allow-list. `expires_at` carries `indexed_at + ttl_days` precomputed because the T3 expire-guard uses a WHERE filter. Move that filter Python-side (it's a low-volume cron, not a hot path) so:
  - `indexed_at` becomes the canonical write timestamp (added to allow-list)
  - `ttl_days` stays as the policy (0 = permanent sentinel)
  - `expires_at` is derived at read time when needed
  - One field saved net: drop `source_title` + `expires_at`, add `indexed_at`.
- **`section_title` = hierarchical path everywhere**, matching `SemanticMarkdownChunker`'s `" > ".join(header_path)` convention (e.g., `"3 METHODOLOGY > 3.1 Chunked Attention"`). PDFChunker needs to track the heading chain (it currently emits just the innermost). md_chunker is already there. RDR-md uses the same code path.

These three changes shrink `ALLOWED_TOP_LEVEL` from 31 → 30 and remove all the `source_title or title` fallback chains across `mcp/core.py`, `commands/store.py`, `formatters.py`.

## Remaining open questions (still need user input)

- **`source_author` for code/prose**: derive from `git log --format="%an" -1 <file>`, or leave empty? Adds a git-shellout per file at index time. Currently empty on code/prose.
- **`tags` for PDFs**: just `"pdf"`, or extract from `pdf_keywords` (which is in the dropped-by-normalize set today)?
- **MCP `git_meta`**: capture process cwd's git context, or leave as a caller-provided arg? Capturing cwd is automatic but loses meaning for daemon-spawned MCP processes.

Comment inline.
