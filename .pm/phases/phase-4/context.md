# Phase 4 — Persistent Server + Hybrid Search + Code Indexing Pipeline

**Duration**: 2–3 weeks
**Goal**: Build persistent `nx serve` process for multi-repo management, HEAD polling for auto-reindex, and hybrid ripgrep+semantic search.

## Scope

Phase 4 delivers:

1. **Persistent Server** (`nx serve`)
   - Flask/Waitress process (background daemon)
   - Repo registry: `~/.config/nexus/repos.json` (list of indexed repos + state)
   - HEAD polling: per-repo every 10 seconds (configurable)
   - PID management: `~/.config/nexus/server.pid`
   - Lifecycle: `nx serve start`, `nx serve stop`, `nx serve status`, `nx serve logs`

2. **Code Indexing Pipeline** (Full implementation from Phase 3 foundation)
   - Per-repo frecency cache update (incremental on HEAD change)
   - Ripgrep line cache: flat mmap file (~500MB soft cap, per-repo)
   - Ripgrep line format: `path:line:content\n`
   - Re-index trigger: HEAD polling detects change → re-chunk + re-embed → upsert T3

3. **Hybrid Search** (`nx search --hybrid`)
   - Semantic results from T3 (via voyage-code-3 embeddings)
   - Full-text results from ripgrep line cache (local, no network)
   - Merge strategy: per-chunk score = 0.7 * vector_norm + 0.3 * frecency_norm
   - min_max_normalize over combined result window

4. **Multi-Repo Support**
   - `nx index code <path>` registers repo with nx serve
   - Each repo: own T3 collection `code__{repo-name}`, own ripgrep cache
   - `nx search --corpus code` queries all indexed code repos (merged results)
   - `nx serve status` shows all repos + accuracy % + indexing state

5. **CLI Commands**
   - `nx serve start [--port N]` (background daemon)
   - `nx serve stop` (graceful shutdown)
   - `nx serve status` (repos, accuracy, uptime)
   - `nx serve logs` (tail ~/.config/nexus/serve.log)
   - `nx search <query> --hybrid --corpus code`

## Success Criteria

### Functional
- [ ] `nx serve start` daemonizes successfully (PID file verified)
- [ ] HEAD polling detects repo changes within 10 seconds (configurable)
- [ ] Ripgrep cache built + memory-mapped successfully
- [ ] Hybrid search merges semantic + ripgrep results correctly
- [ ] Scoring formula (0.7 * vector + 0.3 * frecency) applied
- [ ] `nx serve status` shows accurate repo list + accuracy % (sigmoid decay pattern)
- [ ] Multi-repo search works: `nx search --corpus code` queries all code collections

### Quality
- [ ] Test coverage >85% (nexus/indexing/code, nexus/search/hybrid)
- [ ] All public functions type-hinted
- [ ] Custom exceptions: IndexError, CacheError
- [ ] Logging via Python logging (no print statements)

### Integration
- [ ] End-to-end: register repo → index → search → results
- [ ] Concurrent readers + server indexing (no blocking)

## Key Files to Create

| File | Purpose | Status |
|------|---------|--------|
| `nexus/server/daemon.py` | Flask/Waitress server lifecycle | To create |
| `nexus/server/repo_registry.py` | Registry management (repos.json) | To create |
| `nexus/server/indexer.py` | HEAD polling + re-index orchestration | To create |
| `nexus/indexing/code/ripgrep_cache.py` | Ripgrep line cache builder + mmap reader | To create |
| `nexus/search/hybrid.py` | Semantic + ripgrep merge + scoring | To create |
| `nexus/cli/serve_commands.py` | nx serve lifecycle CLI | To create |
| `tests/integration/test_serve_lifecycle.py` | Daemon start/stop/status | To create |
| `tests/integration/test_head_polling.py` | Mock HEAD changes, verify re-index | To create |
| `tests/integration/test_hybrid_search.py` | Semantic + ripgrep merge scoring | To create |

## Design Patterns

### 1. Repo Registry (repos.json)

```json
{
  "repos": [
    {
      "name": "nexus",
      "path": "/Users/hal.hildebrand/git/nexus",
      "collection_name": "code__nexus",
      "ripgrep_cache_path": "~/.config/nexus/caches/nexus.rgcache",
      "last_indexed_at": "2026-02-21T10:30:00Z",
      "head_hash": "abc123def456...",
      "accuracy_percent": 95,
      "file_count": 42,
      "indexed_at": "2026-02-21T10:30:00Z"
    },
    {
      "name": "arcaneum",
      "path": "/Users/hal.hildebrand/git/arcaneum",
      "collection_name": "code__arcaneum",
      "ripgrep_cache_path": "~/.config/nexus/caches/arcaneum.rgcache",
      "last_indexed_at": "2026-02-21T09:15:00Z",
      "head_hash": "xyz789abc123...",
      "accuracy_percent": 78,
      "file_count": 156,
      "indexed_at": "2026-02-21T09:15:00Z"
    }
  ]
}
```

**Accuracy calculation** (SeaGOAT pattern):
```python
# Accuracy = sigmoid of days since last index
import math
def compute_accuracy(indexed_at: datetime) -> float:
    days_ago = (datetime.now(tz.utc) - indexed_at).days
    sigmoid = 1 / (1 + math.exp(-(-0.1 * days_ago + 4.5)))
    return int(sigmoid * 100)
```

### 2. Ripgrep Line Cache

**File format** (mmap-friendly):
```
/Users/hal.hildebrand/git/nexus/nexus/storage/t2/memory.py:42:    def put(self, project: str, title: str) -> int:
/Users/hal.hildebrand/git/nexus/nexus/storage/t2/memory.py:43:        """Insert or upsert memory entry."""
/Users/hal.hildebrand/git/nexus/nexus/storage/t2/memory.py:44:        pass
...
```

**Building the cache** (ripgrep):
```python
import subprocess

def build_ripgrep_cache(repo_path: str, cache_path: str) -> int:
    """Build ripgrep line cache for a repo."""
    # Use ripgrep to generate line:content pairs
    result = subprocess.run(
        ["rg", "--no-heading", "--with-filename", "-n", "."],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    # Outputs: path:line:content (ripgrep native format)
    # Write to cache_path
    with open(cache_path, "w") as f:
        f.write(result.stdout)
    # Check size
    size_mb = Path(cache_path).stat().st_size / (1024 * 1024)
    if size_mb > 500:
        logger.warning(f"Ripgrep cache exceeds 500MB ({size_mb:.1f}MB); omitting low-frecency files")
    return len(result.stdout.splitlines())
```

**Reading the cache** (mmap):
```python
import mmap

def search_ripgrep_cache(cache_path: str, query: str) -> List[LineMatch]:
    """Search ripgrep cache via mmap (memory-mapped file)."""
    results = []
    with open(cache_path, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
            # Search through mmapped file
            # Each line: path:line:content
            for line in iter(mmapped.readline, b""):
                if query.encode() in line:
                    # Parse path:line:content
                    results.append(parse_line_match(line))
    return results
```

### 3. HEAD Polling Loop

```python
import time
from datetime import datetime

def head_polling_loop(server_state: ServerState, interval: int = 10):
    """Continuously poll HEAD for each repo; re-index on change."""
    while True:
        for repo in server_state.repos:
            current_head = get_git_head(repo.path)
            if current_head != repo.head_hash:
                logger.info(f"HEAD changed for {repo.name}; re-indexing")
                index_repo(repo)
                repo.head_hash = current_head
                repo.last_indexed_at = datetime.now(tz.utc).isoformat()
                # Write updated registry
                write_repo_registry(server_state.repos)
        time.sleep(interval)
```

### 4. Hybrid Search Scoring

```python
def compute_hybrid_score(
    vector_similarity: float,
    frecency_score: float,
    combined_results: List[SearchResult]
) -> float:
    """Compute hybrid score: 0.7 * vector + 0.3 * frecency."""
    # Normalize over combined result window
    vector_scores = [r.vector_similarity for r in combined_results]
    frecency_scores = [r.frecency_score for r in combined_results]

    vector_min, vector_max = min(vector_scores), max(vector_scores)
    frecency_min, frecency_max = min(frecency_scores), max(frecency_scores)

    # min_max_normalize(x, min, max) = (x - min) / (max - min + ε)
    eps = 1e-8
    vector_norm = (vector_similarity - vector_min) / (vector_max - vector_min + eps)
    frecency_norm = (frecency_score - frecency_min) / (frecency_max - frecency_min + eps)

    return 0.7 * vector_norm + 0.3 * frecency_norm
```

## Testing Strategy

### Unit Tests

**`tests/unit/server/test_repo_registry.py`**:
- Registry serialization (JSON)
- Accuracy computation (sigmoid)
- Repo state updates

**`tests/unit/indexing/test_ripgrep_cache.py`**:
- Cache building from sample repo
- Cache file format verification
- mmap reading

**`tests/unit/search/test_hybrid_scoring.py`**:
- min_max_normalize correctness
- Scoring formula (0.7 + 0.3 = 1.0)
- Edge cases (all scores same, zero ranges, etc.)

### Integration Tests

**`tests/integration/test_serve_lifecycle.py`**:
- `nx serve start` creates PID file, verifies process running
- `nx serve status` shows correct repo list + accuracy
- `nx serve stop` terminates gracefully
- Multiple start calls are idempotent (no duplicate processes)

**`tests/integration/test_head_polling.py`**:
- Mock server with mock repo
- Detect HEAD change (simulate git commit)
- Verify re-index triggered (check logging + registry update)
- Accurate timing (within ±2s of poll interval)

**`tests/integration/test_hybrid_search.py`**:
- Index sample repo (both T3 + ripgrep cache)
- Search query: get semantic results + ripgrep results
- Merge + score: verify final ranking order
- Ripgrep exact-match gets vector_norm = 1.0 (high rank)

### Fixtures

```python
@pytest.fixture
def mock_repo_with_index(tmp_path):
    """Create sample git repo with T3 + ripgrep cache indexed."""
    repo_path = setup_sample_repo(tmp_path)
    # Index: build cache, mock T3 upsert
    cache_path = build_ripgrep_cache(repo_path)
    # Mock T3 collection with semantic search results
    return repo_path, cache_path

@pytest.fixture
def mock_server(mock_repo_with_index):
    """Mock nx serve process."""
    repo_path, cache_path = mock_repo_with_index
    registry = RepositoryRegistry([
        RepositoryEntry(name="test", path=str(repo_path), cache_path=str(cache_path))
    ])
    return registry
```

## Configuration (Phase 4)

**New config fields** (`~/.config/nexus/config.yml`):
```yaml
server:
  port: 7890
  headPollInterval: 10  # seconds
  ignorePatterns:
    - node_modules
    - __pycache__
    - .venv
```

**Env var override**:
```bash
NX_SERVER_HEAD_POLL_INTERVAL=20
```

## Dependencies (Phase 4)

**New dependencies**:
```toml
[project]
dependencies = [
    # ... existing ...
    "flask==3.0.3",
    "waitress==3.0.1",
]
```

## Bead Structure for Phase 4

```bash
bd create "Phase 4: Persistent Server + Hybrid Search + Code Indexing" -t epic -p 1

# Server infrastructure
bd create "Flask/Waitress daemon (daemon.py)" -t task -p 1
bd create "Repo registry management (repos.json)" -t task -p 1
bd create "PID file management + lifecycle" -t task -p 1

# HEAD polling
bd create "HEAD polling loop + re-index orchestration" -t task -p 1
bd create "Accuracy computation (sigmoid pattern)" -t task -p 1

# Ripgrep integration
bd create "Ripgrep line cache builder (ripgrep_cache.py)" -t task -p 1
bd create "Ripgrep cache mmap reader" -t task -p 1

# Hybrid search
bd create "Semantic + ripgrep result merge" -t task -p 1
bd create "Hybrid scoring formula (0.7 vector + 0.3 frecency)" -t task -p 1

# CLI
bd create "nx serve start/stop/status/logs commands" -t task -p 1
bd create "nx search --hybrid flag implementation" -t task -p 1

# Tests
bd create "Server lifecycle tests: start/stop/status" -t task -p 1
bd create "HEAD polling tests: detect change + re-index" -t task -p 1
bd create "Hybrid search scoring tests" -t task -p 1
bd create "Integration test: multi-repo index + search" -t task -p 1

# Verify
bd create "Phase 4 validation: nx serve start + search --hybrid working" -t task -p 1
```

## Open Questions (Phase 4)

1. **Ripgrep output format**: Does ripgrep output `path:line:content` or `path:line:col:content`? Test with `rg --with-filename -n "pattern"`. Adjust parsing as needed.

2. **Cache file location**: Should per-repo ripgrep cache files be in `~/.config/nexus/caches/` with subdirs, or flat in a single directory? Decision: flat in `~/.config/nexus/caches/`, named by repo (nexus.rgcache, arcaneum.rgcache). Keep it simple.

3. **Concurrent indexing**: If multiple repos trigger re-index simultaneously, will T3 upserts queue correctly? Expected: yes (ChromaDB handles concurrent writes). Verify in integration tests.

4. **Cache staleness**: If a repo's ripgrep cache is built at T1, then a file changes at T2, search results between T1-T2 will show stale line numbers. When is cache refreshed? Only when server re-indexes (HEAD polling detects change). Known limitation documented in spec (frecency staleness).

## Known Limitations (Phase 4)

1. **Soft ripgrep cache cap** — 500MB cap is advisory, not enforced. Low-frecency files omitted with warning.
2. **HEAD polling granularity** — 10-second default may miss rapid commits. Fine for typical workflows.
3. **No post-commit hook yet** — HEAD polling only; hook is optional installer (Phase 4+).
4. **Accuracy % staleness** — Accuracy is computed at query time from last_indexed_at; may be out of sync if server crashes.

## Deferred to Phase 5+

- **PDF/markdown indexing** — Phase 5
- **Agentic search refinement** — Phase 6
- **Mixedbread fan-out** — Phase 6
- **PM infrastructure** — Phase 7
- **Claude Code plugin** — Phase 8

## Next Actions (Phase 4 Complete)

1. Update CONTINUATION.md: Phase 4 complete
2. Validate: multiple repos indexed, hybrid search working
3. Create Phase 5 context
4. Relay to Phase 5

## Validation Checklist (Phase 4 Complete)

- [ ] `nx serve start` daemonizes successfully
- [ ] PID file created + verified
- [ ] HEAD polling detects repo changes within 10 seconds
- [ ] Re-index triggered on HEAD change (logs verify)
- [ ] Ripgrep cache built successfully (file size < 500MB soft cap)
- [ ] mmap cache reading works efficiently
- [ ] Hybrid search merges semantic + ripgrep results
- [ ] Scoring formula applied correctly (0.7 + 0.3)
- [ ] `nx serve status` shows accurate repo list + accuracy %
- [ ] Multi-repo search works (all code__ collections queried)
- [ ] `nx search --hybrid` returns results in correct merged order
- [ ] Test coverage >85% (server, indexing, search)
- [ ] All public functions type-hinted
- [ ] No circular imports
- [ ] Server logs at ~/.config/nexus/serve.log
- [ ] Beads closed/done for Phase 4
- [ ] CONTINUATION.md updated with Phase 4 complete status
