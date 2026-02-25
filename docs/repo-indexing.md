# Smart Repository Indexing

## Overview

`nx index repo` walks a git repository and classifies each file by extension, routing code
and prose to separate ChromaDB collections with purpose-built Voyage AI embedding models.
Code files receive AST-aware chunking via tree-sitter; prose files receive semantic markdown
chunking. Git frecency scores are computed in a single subprocess and attached to every chunk.

## File Classification

Every file is classified into one of three categories based on its extension:

- **CODE** (23 extensions): `.py`, `.js`, `.jsx`, `.ts`, `.tsx`, `.java`, `.go`, `.rs`,
  `.cpp`, `.cc`, `.c`, `.h`, `.hpp`, `.rb`, `.cs`, `.sh`, `.bash`, `.kt`, `.swift`,
  `.scala`, `.r`, `.m`, `.php`
- **PDF**: `.pdf` files are extracted and chunked separately, then stored alongside prose.
- **PROSE**: everything else that is not binary or PDF.

Classification uses `git ls-files --cached -z` so only tracked files are indexed, fully
respecting `.gitignore`, `.git/info/exclude`, and the global gitignore. Hidden directories
(names starting with `.`) and configurable ignore patterns (`node_modules`, `vendor`,
`.venv`, `__pycache__`, `dist`, `build`, `.git`) are also skipped.

Classification is overridable via `.nexus.yml` (see [Per-Repo Configuration](#nexusyml-per-repo-configuration)).

## Dual-Collection Architecture

Each indexed repository produces two T3 (ChromaDB Cloud) collections:

| Collection | Embedding Model (Index) | Embedding Model (Query) | Contents |
|---|---|---|---|
| `code__<name>-<hash8>` | `voyage-code-3` | `voyage-4` | Code files |
| `docs__<name>-<hash8>` | `voyage-context-3` (CCE) | `voyage-4` | Prose + PDF files |

`<name>` is the repository basename; `<hash8>` is the first 8 hex characters of the
SHA-256 digest of the main repository path. Collection names are **stable across git
worktrees** -- `git rev-parse --git-common-dir` resolves to the shared `.git` directory,
so a worktree and its parent produce identical collection names.

Additionally, markdown files under RDR paths (default: `docs/rdr/`) are indexed into a
separate `rdr__<name>-<hash8>` collection via the batch markdown indexer.

**Note**: `voyage-context-3` uses Contextualized Chunk Embeddings (CCE), which require 2+ chunks per batch. Single-chunk files fall back to `voyage-4`. Both models produce 1024-dimensional embeddings, so mixed-model collections stay compatible.

## Code Chunking

Code files are chunked via tree-sitter AST parsing using `llama-index` `CodeSplitter` and
`tree-sitter-language-pack`.

**AST-supported languages** (17 extension mappings to 12 parsers):

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
| `.cpp`, `.hpp` | cpp |
| `.rb` | ruby |
| `.cs` | c\_sharp |
| `.sh`, `.bash` | bash |

Six additional extensions (`.kt`, `.swift`, `.scala`, `.r`, `.m`, `.php`) are classified as CODE for metadata tagging but do **not** have AST chunking — they use the line-based fallback.

When AST parsing fails or no parser exists for the extension, the chunker falls back to
line-based splitting: 150-line chunks with 15% overlap.

Each chunk carries metadata: `file_path`, `filename`, `file_extension`,
`programming_language`, `line_start`, `line_end`, `chunk_index`, `chunk_count`,
`ast_chunked` (bool), `content_hash`, `frecency_score`.

## Prose Chunking

Markdown files (`.md`, `.markdown`) are chunked using `SemanticMarkdownChunker`, built on
the `markdown-it-py` AST parser.

- Headers create section boundaries; the header hierarchy is tracked as `header_path`.
- Sections that fit within the token budget (512 tokens, ~1690 chars) are emitted as single
  chunks.
- Oversized sections are split at content-part boundaries with the section header repeated.
- YAML frontmatter is extracted via `parse_frontmatter()` and preserved; character offsets
  in chunk metadata account for the frontmatter length.
- Chunks carry `chunk_start_char` and `chunk_end_char` instead of line numbers.

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
chunks without re-embedding -- useful for a fast score update after a burst of commits.

## `.nexus.yml` Per-Repo Configuration

Place a `.nexus.yml` at the repository root to customize indexing behavior:

```yaml
indexing:
  code_extensions: [".proto", ".thrift"]     # added to the default code set
  prose_extensions: [".txt.j2", ".md.tmpl"]  # forced to prose (wins over code)
  rdr_paths: ["docs/rdr", "decisions"]       # directories indexed into rdr__ collection
  include_untracked: true                    # also index untracked (but not .gitignored) files
```

`prose_extensions` takes precedence: if an extension appears in both lists, it is classified
as prose. `code_extensions` is additive -- it extends the built-in set, it does not replace
it.

Configuration merges over global config at `~/.config/nexus/config.yml` (repo wins).

## Staleness and Incremental Indexing

Every chunk stores a `content_hash` (SHA-256 of the file contents) and `embedding_model`
in its ChromaDB metadata. On re-index, if both match the stored values, the file is skipped
entirely -- no re-chunking, no re-embedding, no API calls.

When a file is deleted from the repository, the pruning pass removes its orphaned chunks
from both collections. When a file's classification changes (e.g., `.nexus.yml` update
moves it from code to prose), the misclassification pruner deletes chunks from the old
collection.

HEAD polling via `nx serve` monitors registered repositories and triggers automatic
re-indexing when `git rev-parse HEAD` changes.

## Searching Indexed Repos

```bash
nx search "query" --corpus code                    # search code collections only
nx search "query" --corpus docs                    # search prose collections only
nx search "query" --corpus code --corpus docs      # both, merged via reranker
nx search "query" --corpus code --hybrid           # semantic + frecency blend
nx search "query" --corpus code --rerank           # Voyage rerank-2.5 reranking
nx search "query" --corpus code --hybrid --rerank  # hybrid scoring then reranking
```

`--corpus` resolves as a prefix: `code` matches all `code__*` collections, `docs` matches
all `docs__*` collections. A fully-qualified name (containing `__`) matches exactly.

All collections are queried with `voyage-4` at query time regardless of the index-time
model.
