# Smart Repository Indexing

## Overview

`nx index repo` walks a git repository and classifies each file by extension, routing code
and prose to separate ChromaDB collections with purpose-built Voyage AI embedding models.
Code files receive AST-aware chunking via tree-sitter with an embed-only context prefix;
prose files receive semantic markdown chunking. Git frecency scores are computed in a single
subprocess and attached to every chunk.

## File Classification

Every file is classified into one of four categories:

- **CODE** (52 extensions): 44 AST-supported extensions (see table below) plus 8 GPU
  shader extensions (`.cl`, `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`, `.hlsl`)
  that receive line-based chunking
- **PDF**: `.pdf` files are extracted and chunked separately, then stored in `docs__`.
- **PROSE**: `.md`, `.markdown`, and any extension not in CODE, PDF, or SKIP.
- **SKIP** (18 extensions — not indexed): `.xml`, `.json`, `.yml`, `.yaml`, `.toml`,
  `.properties`, `.ini`, `.cfg`, `.conf`, `.gradle`, `.html`, `.htm`, `.css`, `.svg`,
  `.cmd`, `.bat`, `.ps1`, `.lock`

**Extensionless files** (e.g., `Makefile`, `LICENSE`): if the first two bytes are `#!`
(shebang), the file is classified as CODE; otherwise SKIP.

Classification uses `git ls-files --cached -z` so only tracked files are indexed, fully
respecting `.gitignore`, `.git/info/exclude`, and the global gitignore. Hidden directories
(names starting with `.`) and configurable ignore patterns (`node_modules`, `vendor`,
`.venv`, `__pycache__`, `dist`, `build`, `.git`) are also skipped.

Classification is overridable via `.nexus.yml` (see [Per-Repo Configuration](#nexusyml-per-repo-configuration)).
`prose_extensions` config takes priority over all built-in classifications including SKIP.

## Dual-Collection Architecture

Each indexed repository produces two T3 (ChromaDB Cloud) collections:

| Collection | Embedding Model (Index) | Embedding Model (Query) | Contents |
|---|---|---|---|
| `code__<name>-<hash8>` | `voyage-code-3` | `voyage-4` | Code files |
| `docs__<name>-<hash8>` | `voyage-context-3` (CCE) | `voyage-4` | Prose + PDF files |

`<name>` is the repository basename; `<hash8>` is the first 8 hex characters of the
SHA-256 digest of the main repository path. Long basenames are truncated to stay within
ChromaDB's 63-character collection name limit. Collection names are **stable across git
worktrees** — `git rev-parse --git-common-dir` resolves to the shared `.git` directory,
so a worktree and its parent produce identical collection names.

Additionally, markdown files under RDR paths (default: `docs/rdr/`) are indexed into a
separate `rdr__<name>-<hash8>` collection via the batch markdown indexer.

**Note**: `voyage-context-3` uses Contextualized Chunk Embeddings (CCE), which require 2+ chunks per batch. Single-chunk files fall back to `voyage-4`. Both models produce 1024-dimensional embeddings, so mixed-model collections stay compatible.

## Code Chunking

Code files are chunked via tree-sitter AST parsing using `llama-index` `CodeSplitter` and
`tree-sitter-language-pack`.

**AST-supported languages** (44 extension mappings, 31 parsers):

| Extensions | Parser |
|---|---|
| `.py` | python |
| `.js`, `.jsx` | javascript |
| `.ts` | typescript |
| `.tsx` | tsx |
| `.java` | java |
| `.go` | go |
| `.rs` | rust |
| `.c`, `.h` | c |
| `.cpp`, `.cc`, `.cxx`, `.hpp` | cpp |
| `.rb` | ruby |
| `.cs` | c\_sharp |
| `.sh`, `.bash` | bash |
| `.kt`, `.kts` | kotlin |
| `.scala`, `.sc` | scala |
| `.swift` | swift |
| `.m` | objc |
| `.php` | php |
| `.r` | r |
| `.lua` | lua |
| `.proto` | proto |
| `.ex`, `.exs` | elixir |
| `.erl`, `.hrl` | erlang |
| `.hs` | haskell |
| `.clj`, `.cljs`, `.cljc` | clojure |
| `.ml` | ocaml |
| `.mli` | ocaml\_interface |
| `.el` | elisp |
| `.dart` | dart |
| `.zig` | zig |
| `.jl` | julia |
| `.pl`, `.pm` | perl |

Additionally, 8 GPU shader extensions (`.cl`, `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`, `.hlsl`) are classified as CODE with line-based chunking (no AST parser).

When AST parsing fails or no parser exists for the extension, the chunker falls back to
line-based splitting: 150-line chunks with 15% overlap.

### Minified Code Handling

The AST chunker detects minified files (average line length > 500 characters) and falls
back to byte-based splitting instead of producing single oversized chunks. This prevents
minified JavaScript, CSS, and similar files from consuming disproportionate storage and
degrading search quality.

### Context Prefix Injection (Embed-Only)

Each code chunk's **embedded text** is prefixed with a one-line context header identifying
the file, class, method, and line range. The raw chunk text is stored in ChromaDB unchanged
— the prefix affects only the Voyage AI embedding call, not stored documents or search previews.

```
// File: art-modules/art-core/src/FuzzyART.java  Class: FuzzyART  Method: computeMatch  Lines: 200–350
<original chunk text>
```

Class and method names are extracted by tree-sitter using `DEFINITION_TYPES` covering 23
languages (Python, JavaScript, TypeScript, TSX, Java, Go, Rust, C, C++, C#, Ruby, PHP,
Swift, Kotlin, Scala, R, Lua, Dart, Haskell, Julia, OCaml, Perl, Erlang).
For a chunk spanning multiple methods, the method field is empty. For unsupported languages
or when parsing fails, the prefix falls back to `File + Lines` only.

The comment character (`//` vs `#`) is selected per language. This prefix anchors each
chunk's semantic meaning for retrieval, improving recall for algorithm-level queries in
domain-specific codebases where many files share vocabulary.

Each chunk carries metadata: `filename`, `source_path`, `file_extension`,
`programming_language`, `line_start`, `line_end`, `chunk_index`, `chunk_count`,
`ast_chunked` (bool), `class_name`, `method_name`, `content_hash`, `frecency_score`,
`embedding_model`, `git_commit_hash`, `git_project_name`, `git_branch`, `git_remote_url`.

## Prose Chunking

Markdown files (`.md`, `.markdown`) are chunked using `SemanticMarkdownChunker`, built on
the `markdown-it-py` AST parser.

- Headers create section boundaries; the header hierarchy is tracked as `section_title`.
- Sections that fit within the token budget (512 tokens, ~1690 chars) are emitted as single
  chunks.
- Oversized sections are split at content-part boundaries with the section header repeated.
- YAML frontmatter is extracted via `parse_frontmatter()` and preserved; character offsets
  in chunk metadata account for the frontmatter length.
- Chunks carry `chunk_start_char` and `chunk_end_char` instead of line numbers.
- Fenced code blocks are preserved intact (`preserve_code_blocks=True` by default) — a
  code block is never split mid-content even if it exceeds the section size limit.
- Structural markdown-it-py tokens (`paragraph_open`, `list_item_open`, `tr_open`, etc.)
  are filtered via `_STRUCTURAL_TOKEN_TYPES` blocklist so content appears exactly once
  per chunk (no duplication from open/close token pairs).

Non-markdown prose files use the same line-based fallback as unsupported code languages:
150-line chunks with 15% overlap.

When `markdown-it-py` is unavailable, `SemanticMarkdownChunker` degrades to naive
paragraph-boundary splitting.

## Git Frecency Scoring

Frecency measures how recently and frequently a file has been touched in git history.

**Formula**: `score = sum(exp(-0.01 * days_since_commit))` for every commit that touched
the file. Recent commits contribute close to 1.0; a commit 100 days old contributes ~0.37.

`batch_frecency()` computes scores for the entire repository in a single
`git log --format="COMMIT %ct" --name-only` subprocess, avoiding one-process-per-file
overhead.

Frecency is used by hybrid search:

```
hybrid_score = 0.7 * vector_similarity_norm + 0.3 * frecency_norm
```

Normalization uses min-max over the combined result window. Hybrid scoring applies only to
`code__` collections; `docs__` and `knowledge__` collections use pure vector similarity.

The `--frecency-only` flag on `nx index repo` refreshes frecency metadata on all existing
chunks without re-embedding — useful for a fast score update after a burst of commits.

## `.nexus.yml` Per-Repo Configuration

Place a `.nexus.yml` at the repository root to customize indexing behavior:

```yaml
indexing:
  code_extensions: [".thrift"]              # added to the default code set
  prose_extensions: [".txt.j2", ".md.tmpl"] # forced to prose (wins over code and SKIP)
  rdr_paths: ["docs/rdr", "decisions"]      # directories indexed into rdr__ collection
  include_untracked: true                   # also index untracked (but not .gitignored) files
```

`prose_extensions` takes precedence over everything — if an extension appears in both lists,
or is normally SKIP, it is classified as PROSE. `code_extensions` is additive.

Configuration merges over global config at `~/.config/nexus/config.yml` (repo wins).

## Staleness and Incremental Indexing

Every chunk stores a `content_hash` (SHA-256 of the file contents) and `embedding_model`
in its ChromaDB metadata. On re-index, if both match the stored values, the file is skipped
entirely — no re-chunking, no re-embedding, no API calls.

When a file is deleted from the repository, the pruning pass removes its orphaned chunks
from both collections. When a file's classification changes (e.g., `.nexus.yml` update
moves it from code to prose, or a previously-PROSE extension is now SKIP), the
misclassification pruner deletes chunks from the old collection. Previously-indexed
SKIP files are cleaned automatically on the next `nx index repo` run.

Git hooks (`post-commit`, `post-merge`, `post-rewrite`) trigger automatic re-indexing
in the background after each qualifying git operation. Install them with `nx hooks install`.

## Pipeline Versioning

Every indexed collection stores a `PIPELINE_VERSION` stamp in its ChromaDB metadata. When
the indexing pipeline changes (new chunking logic, updated context prefixes, etc.), the
version is bumped. This enables targeted re-indexing:

- **`--force`** — re-index all files unconditionally (ignores staleness and pipeline version)
- **`--force-stale`** — re-index only collections whose stored pipeline version is older
  than the current version. Files within those collections still use hash-based staleness
  checks, so only changed files are re-embedded. This is the recommended flag after upgrading
  Nexus to a version with pipeline changes.

`nx doctor` reports the pipeline version status of each collection:

```
✓ pipeline (code__nexus-571b8edd): v4
✓ pipeline (code__myrepo-abc12345): no version stamp (index with --force to stamp)
```

Collections without a version stamp were indexed before pipeline versioning was introduced.
Run `nx index repo --force` once to stamp them.

## Transient Error Resilience

All ChromaDB Cloud network calls (staleness checks, upserts, deletes, queries) are wrapped
with `_chroma_with_retry` — an exponential backoff helper in `retry.py`. If a call raises a
transient error (HTTP 502/503/504/429, or a transport-level error such as `ConnectError` or
`ReadTimeout`), it is retried up to 5 times with delays of 2 → 4 → 8 → 16 → 30 s (capped).
Non-retryable errors (400, 401, 403, 404) raise immediately without retry.

Each retry attempt is logged at WARNING level with the event key
`chroma_transient_error_retry`. After 5 failed attempts the original exception propagates
and the indexing job fails fast.

This means a single transient 504 from the ChromaDB Cloud gateway no longer aborts a
multi-thousand-file indexing run. See [RDR-019](rdr/rdr-019-chromadb-transient-retry.md)
for the full decision record.

## Searching Indexed Repos

```bash
nx search "query" --corpus code                    # search code collections only
nx search "query" --corpus docs                    # search prose collections only
nx search "query" --corpus code --corpus docs      # both, merged via reranker
nx search "query" --corpus code --hybrid           # semantic + frecency blend
nx search "query" --corpus code --hybrid --no-rerank  # hybrid without cross-corpus reranking
```

`--corpus` resolves as a prefix: `code` matches all `code__*` collections, `docs` matches
all `docs__*` collections. A fully-qualified name (containing `__`) matches exactly.

All collections are queried with `voyage-4` at query time regardless of the index-time
model.
