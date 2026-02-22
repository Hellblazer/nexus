# Phase 3 — T3 Cloud Storage + Code Indexing Foundation

**Duration**: 2–3 weeks
**Goal**: Build cloud-backed permanent storage (T3) via ChromaDB CloudClient + Voyage AI, plus foundational code indexing pipeline.

## Scope

Phase 3 delivers:

1. **T3 Cloud Storage** (`nexus/storage/t3/`)
   - ChromaDB CloudClient initialization (tenant, database, api_key)
   - VoyageAIEmbeddingFunction for both `voyage-code-3` and `voyage-4` models
   - Connection pooling + auth error handling
   - CRUD operations: upsert, query, get, delete (mapped from ChromaDB API)
   - Metadata schema validation (flat types only)
   - Collection management: list, create, delete

2. **Search Integration** (`nexus/search/`)
   - Single-corpus semantic search via T3 collections
   - Basic query routing (which corpus to search)
   - Result formatting + citation support
   - Error handling for auth failures

3. **Code Indexing Pipeline** (Phase 3 foundation; full impl in Phase 4)
   - Code chunking: llama-index CodeSplitter (AST-based for 30+ languages)
   - Frecency scoring: git log analysis (exponential decay: exp(-0.01 * days_passed))
   - Metadata extraction: file path, line numbers, language, git context
   - Basic upsert to T3 collection `code__{repo-name}`
   - Command: `nx index code <path>` (registers repo, triggers indexing)

4. **CLI Commands** (`nexus/cli/`)
   - `nx search <query> --corpus code [--max-results N]` (semantic search only, no hybrid yet)
   - `nx index code <path>` (index a git repo)
   - `nx collection list` (show T3 collections + doc counts)
   - `nx collection info <name>` (details about a collection)
   - `nx doctor` (verify CHROMA_API_KEY, VOYAGE_API_KEY, connectivity)

5. **Environment & Config**
   - Required env vars: `CHROMA_API_KEY`, `VOYAGE_API_KEY`
   - ChromaDB config: tenant, database, client.host
   - Voyage model names verified (code-3, voyage-4)
   - nx doctor checks all prerequisites

## Success Criteria

### Functional
- [ ] T3 CloudClient connects successfully (auth verified)
- [ ] VoyageAIEmbeddingFunction embeds code chunks (voyage-code-3)
- [ ] Upsert to T3 collection succeeds (verified in ChromaDB UI)
- [ ] Semantic search queries return results ranked by similarity
- [ ] Code chunking: AST-based splits on function/class boundaries (or line-based fallback)
- [ ] Frecency scores computed from git log
- [ ] `nx index code` indexes sample repo; collection appears in `nx collection list`
- [ ] `nx doctor` verifies all env vars + connectivity

### Quality
- [ ] Test coverage >85% (nexus/storage/t3, nexus/indexing/code)
- [ ] All public functions type-hinted
- [ ] Custom exceptions: ChromaAuthError, VoyageAuthError
- [ ] Metadata schema validated (no nested objects)
- [ ] Error handling for API failures

### Integration
- [ ] CLI end-to-end: `nx index code <path> && nx search <query> --corpus code`
- [ ] T3 search can handle multiple collection types (code__, docs__, knowledge__)
- [ ] Collection naming matches spec: `code__nexus`, `docs__papers`, etc.

## Key Files to Create

| File | Purpose | Status |
|------|---------|--------|
| `nexus/storage/t3/cloud.py` | CloudClient wrapper + CRUD | To create |
| `nexus/storage/t3/models.py` | SearchResult, ChunkMetadata dataclasses | To create |
| `nexus/indexing/code/chunker.py` | llama-index CodeSplitter wrapper | To create |
| `nexus/indexing/code/frecency.py` | Git log → frecency scores | To create |
| `nexus/indexing/code/metadata.py` | code__ metadata builder | To create |
| `nexus/indexing/code/indexer.py` | Orchestrates chunking + embedding + upsert | To create |
| `nexus/search/semantic.py` | Single-corpus semantic search | To create |
| `nexus/cli/search_commands.py` | nx search subcommands | To create |
| `nexus/cli/index_commands.py` | nx index subcommands | To create |
| `nexus/cli/collection_commands.py` | nx collection management | To create |
| `nexus/cli/doctor.py` | nx doctor health check | To create |
| `tests/unit/storage/test_t3_cloud.py` | T3 CloudClient CRUD (mocked) | To create |
| `tests/unit/indexing/test_code_chunking.py` | Code chunking correctness | To create |
| `tests/unit/indexing/test_frecency.py` | Frecency score calculation | To create |
| `tests/integration/test_code_indexing_e2e.py` | Index sample repo, search it | To create |

## Design Patterns

### 1. T3 Collection Abstraction

```python
class ChromaCollection:
    """Wrapper around ChromaDB collection."""

    def __init__(self, collection_name: str, embedding_function: EmbeddingFunction):
        self.collection = client.get_or_create_collection(
            name=collection_name,
            embedding_function=embedding_function,
            metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, ids: List[str], documents: List[str], metadatas: List[dict]) -> None:
        """Upsert chunks with metadata."""
        self.collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )

    def query(self, query_texts: List[str], n_results: int = 10) -> dict:
        """Query via embedding. Returns results dict with ids, documents, metadatas, distances."""
        return self.collection.query(
            query_texts=query_texts,
            n_results=n_results,
        )
```

### 2. Code Chunking Strategy

```python
from llama_index.core.node_parser import CodeSplitter

chunker = CodeSplitter(
    language="python",
    chunk_size=1536,  # tokens; spec says ~150 lines
    chunk_overlap=256,  # 15% for line-based fallback
)

chunks = chunker.get_nodes_from_documents([Document(text=code_text)])
# Returns: [Chunk(text=..., metadata={language:, lines:, ast_node:}), ...]
```

### 3. Frecency Computation

```python
import subprocess
from datetime import datetime

def compute_frecency(repo_path: str, file_path: str) -> float:
    """Compute frecency = sum(exp(-0.01 * days_since_commit))."""
    # Get all commits touching this file
    result = subprocess.run(
        ["git", "log", "--follow", "--format=%aI", file_path],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    timestamps = [datetime.fromisoformat(line) for line in result.stdout.strip().split('\n')]

    now = datetime.now(tz.utc)
    frecency = sum(
        exp(-0.01 * (now - ts).days)
        for ts in timestamps
    )
    return frecency
```

### 4. Metadata Schema (code__ collection)

```python
@dataclass
class CodeMetadata:
    """Flat metadata for code chunks (ChromaDB-compatible)."""
    file_path: str
    filename: str
    file_extension: str
    programming_language: str
    corpus: str  # repo name
    store_type: str  # "code"

    git_project_name: str
    git_branch: str
    git_commit_hash: str
    git_remote_url: str

    line_start: int
    line_end: int
    chunk_index: int
    chunk_count: int
    ast_chunked: bool

    frecency_score: float
    embedding_model: str  # "voyage-code-3"
    indexed_at: str  # ISO 8601
    content_hash: str  # git object ID

    def to_dict(self) -> dict:
        """Convert to flat dict for ChromaDB metadata."""
        return {
            "file_path": self.file_path,
            "filename": self.filename,
            # ... etc, all as str/int/float/bool
        }
```

## Testing Strategy

### Unit Tests

**`tests/unit/storage/test_t3_cloud.py`**:
- CloudClient initialization (mock ChromaDB)
- Upsert with flat metadata (validation)
- Query returns results with correct schema
- Collection listing

**`tests/unit/indexing/test_code_chunking.py`**:
- CodeSplitter on Python sample: splits at function boundaries
- Line-based fallback for unsupported language
- Chunk metadata (line_start, line_end, ast_chunked flag)
- Overlap preservation

**`tests/unit/indexing/test_frecency.py`**:
- Frecency for single commit: exp(-0.01 * 0) = 1.0
- Frecency for commits 100 days apart: sum([1.0, exp(-1.0)]) ≈ 1.368
- Multiple commits in same day: counted separately

### Integration Tests

**`tests/integration/test_code_indexing_e2e.py`**:
- Create sample git repo with few Python files
- Run `nx index code <repo_path>` (stores to mocked T3)
- Verify collection created: `code__test-repo`
- Query collection: verify results ranked by similarity
- End-to-end workflow

### Fixtures

```python
@pytest.fixture
def sample_python_repo(tmp_path):
    """Create a small git repo with Python files."""
    repo_path = tmp_path / "sample-repo"
    repo_path.mkdir()
    # Initialize git, commit some files
    subprocess.run(["git", "init"], cwd=repo_path)
    # Write sample.py with functions
    (repo_path / "sample.py").write_text("def foo():\n    pass\n")
    subprocess.run(["git", "add", "."], cwd=repo_path)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path)
    return repo_path

@pytest.fixture
def mock_chroma_client():
    """Mock ChromaDB CloudClient for unit tests."""
    with patch("chromadb.CloudClient") as mock:
        mock.return_value.get_or_create_collection.return_value = MagicMock()
        yield mock
```

## Configuration (Phase 3)

**Required environment variables**:
```bash
export CHROMA_API_KEY="<cloud api key>"
export VOYAGE_API_KEY="<voyage ai api key>"
```

**Optional config** (`~/.config/nexus/config.yml`):
```yaml
chromadb:
  tenant: "default"
  database: "default"
  client:
    host: "api.trychroma.com"  # or self-hosted Chroma URL

embeddings:
  codeModel: voyage-code-3
  docsModel: voyage-4
```

## Dependencies (Phase 3)

**New dependencies**:
```toml
[project]
dependencies = [
    # ... existing from Phase 1-2 ...
    "chromadb==0.5.8",
    "voyageai==1.2.3",
    "llama-index-core==0.12.18",
    "tree-sitter-language-pack==0.25.1",
]
```

**Version pinning note**: llama-index-core and tree-sitter-language-pack have known compatibility issues (issues #13521, #17567). Exact versions must be verified before release.

## Bead Structure for Phase 3

```bash
bd create "Phase 3: T3 Cloud + Code Indexing Foundation" -t epic -p 1

# T3 storage
bd create "T3 CloudClient wrapper (cloud.py)" -t task -p 1
bd create "T3 upsert + query operations" -t task -p 1
bd create "Metadata schema validation (flat types)" -t task -p 1

# Code indexing
bd create "Code chunking: llama-index CodeSplitter wrapper" -t task -p 1
bd create "Frecency scoring from git log" -t task -p 1
bd create "code__ metadata builder + upsert" -t task -p 1

# Search
bd create "Semantic search: T3 query + ranking" -t task -p 1

# CLI
bd create "nx search command (semantic only)" -t task -p 1
bd create "nx index code command" -t task -p 1
bd create "nx collection management (list, info, delete)" -t task -p 1
bd create "nx doctor: verify env vars + connectivity" -t task -p 1

# Tests
bd create "T3 unit tests: CloudClient CRUD (mocked)" -t task -p 1
bd create "Code indexing unit tests: chunking, frecency" -t task -p 1
bd create "Code indexing integration test: index + search sample repo" -t task -p 1

# Verify
bd create "Phase 3 validation: index sample repo, search working" -t task -p 1
```

## Open Questions (Phase 3)

1. **Voyage model verification**: voyage-code-3 and voyage-4 are not enumerated by the SDK. First invalid API call fails. Should nx doctor validate model names? Yes, add a test call to voyageai.Client() with the configured model names on first connect.

2. **Collection naming**: Confirm `code__` prefix (double underscore) avoids FTS5 delimiter conflicts. Already decided in spec; proceed with double underscore.

3. **Frecency staleness**: Spec notes that frecency-only reindex (updating scores without re-embedding) is not implemented in v1. Accepted as known limitation. When a file hasn't changed but other files have recent commits, its relative frecency becomes stale. Mitigated by re-indexing on HEAD change (Phase 4 with nx serve).

4. **Metadata cap per chunk**: ChromaDB metadata is flat (str/int/float/bool). No nested objects. The code__ schema has ~20 fields. Is there a per-metadata size limit? Test with sample chunk.

## Known Limitations (Phase 3)

1. **No hybrid search yet** — semantic only; ripgrep integration in Phase 4
2. **No incremental re-indexing** — full re-index on changes; Phase 4 adds HEAD polling
3. **Single repo per command** — `nx index code <path>` indexes one repo; multi-repo registry in Phase 4
4. **No PDF/markdown indexing yet** — code only; Phase 5

## Deferred to Phase 4+

- **Ripgrep hybrid search** — line cache + semantic merge scoring
- **Persistent server + HEAD polling** — nx serve daemon
- **Multi-repo registry** — ~/.config/nexus/repos.json
- **Agentic search refinement** — --agentic flag
- **Mixedbread fan-out** — --mxbai flag

## Next Actions (Phase 3 Complete)

1. Update CONTINUATION.md: Phase 3 complete
2. Create Phase 4 context
3. Relay to Phase 4

## Validation Checklist (Phase 3 Complete)

- [ ] T3 CloudClient connects successfully (CHROMA_API_KEY verified)
- [ ] Upsert to T3 collection succeeds
- [ ] Semantic search returns results ranked by similarity
- [ ] Code chunking: AST-based + line-based fallback working
- [ ] Frecency scores computed from git log
- [ ] code__ metadata schema valid (all flat types)
- [ ] `nx index code <path>` works end-to-end
- [ ] `nx search <query> --corpus code` returns results
- [ ] `nx collection list` shows indexed collection
- [ ] `nx doctor` verifies env vars + connectivity
- [ ] Test coverage >85% (storage/t3, indexing/code)
- [ ] All public functions type-hinted
- [ ] No circular imports
- [ ] Beads closed/done for Phase 3
- [ ] CONTINUATION.md updated with Phase 3 complete status
