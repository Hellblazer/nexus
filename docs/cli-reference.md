# Nexus CLI Reference

All commands use the `nx` binary. Global flags: `--help`, `--version`.

---

## nx search

Semantic search across T3 cloud collections.

```
nx search "authentication middleware" --corpus code --hybrid --n 20
```

| Flag | Description |
|------|-------------|
| `QUERY` (positional) | Search query text |
| `PATH` (positional, optional) | Scope search to a directory |
| `--corpus NAME` | Collection to search (repeatable; default: `knowledge`, `code`, `docs`) |
| `-a` / `--answer` | Synthesize cited answer (requires Anthropic key) |
| `--agentic` | Multi-step refinement via Haiku |
| `--hybrid` | Semantic + ripgrep frecency (code corpora only) |
| `--no-rerank` | Disable Voyage reranking |
| `--mxbai` | Fan out to Mixedbread-indexed collections (read-only) |
| `--where KEY=VALUE` | Metadata filter (repeatable) |
| `--n` / `-m` / `--max-results NUM` | Max results (default 10) |
| `-A NUM` | Lines after each match |
| `-C NUM` | Lines after each match (alias for `-A`) |
| `-c` / `--content` | Show matched text inline |
| `-r` / `--reverse` | Reverse result order |
| `--vimgrep` | Output as `path:line:col:content` |
| `--json` | JSON array output |
| `--files` | Unique file paths only |
| `--no-color` | Disable colored output |

---

## nx index

Index content into T3 cloud collections.

```
nx index repo ./my-project
```

| Subcommand | Description |
|------------|-------------|
| `repo PATH` | Index code repository (smart classification: code to `code__`, prose to `docs__`) |
| `rdr [PATH]` | Index RDR documents in `docs/rdr/` into `rdr__` collection (default: current dir) |
| `pdf PATH --corpus NAME` | Index a PDF file |
| `md PATH --corpus NAME` | Index a markdown file |

**`repo` flags:**

| Flag | Description |
|------|-------------|
| `--frecency-only` | Refresh git scores without re-embedding |

---

## nx store

Manage T3 cloud knowledge entries.

```
echo "# Cache Strategy" | nx store put - --collection knowledge --title "decision-cache" --tags "decision,arch"
```

| Subcommand | Description |
|------------|-------------|
| `put FILE_OR_DASH --collection NAME` | Store document (use `-` for stdin) |
| `list` | List stored entries |
| `expire` | Remove expired entries |

**`put` flags:**

| Flag | Description |
|------|-------------|
| `--title TITLE` | Entry title (required for stdin) |
| `--tags TAG,TAG` | Comma-separated tags |
| `--ttl TTL` | Time to live (`30d`, `4w`, `permanent`) |

**`list` flags:**

| Flag | Description |
|------|-------------|
| `--collection NAME` | Filter by collection |

---

## nx memory

T2 persistent memory (SQLite + FTS5). See [Storage Tiers](storage-tiers.md) for what T2 holds and how it bridges sessions.

```
nx memory put "auth uses JWT" --project nexus_active --title findings.md --ttl 30d
```

| Subcommand | Description |
|------------|-------------|
| `put CONTENT --project NAME --title NAME` | Write a memory entry |
| `get ID` | Read entry by numeric ID |
| `get --project NAME --title NAME` | Read entry by project + title |
| `search QUERY` | FTS5 keyword search |
| `list --project NAME` | List entries in project |
| `expire` | Remove expired entries |
| `promote ID --collection NAME` | Promote entry to T3 by ID |

**`put` flags:** `--tags`, `--ttl`

**`promote` flags:** `--collection` (required), `--tags`, `--remove`

**`search` flags:** `--project NAME`

---

## nx scratch

T1 ephemeral session notes (in-memory ChromaDB).

```
nx scratch put "hypothesis: cache invalidation is stale"
```

| Subcommand | Description |
|------------|-------------|
| `put CONTENT` | Store ephemeral note |
| `get ID` | Retrieve by ID |
| `search QUERY` | Search scratch notes |
| `list` | List all notes |
| `flag ID` | Mark for auto-flush to T2 at session end |
| `unflag ID` | Remove flush mark |
| `promote ID --project NAME --title NAME` | Promote to T2 |
| `clear` | Delete all scratch notes |

**`put` flags:** `--tags` (comma-separated), `--persist` (auto-flush to T2), `--project` / `--title` (explicit T2 destination)

**`flag` flags:** `--project` / `--title` (explicit T2 destination)

---

## nx pm

Project management (T2 + T3). See [Project Management](project-management.md) for the lifecycle and integration details.

```
nx pm resume
```

| Subcommand | Description |
|------------|-------------|
| `init` | Initialize PM for current git repo |
| `resume` | Inject continuation context into session |
| `status` | Show project status, blockers, active work |
| `phase next` | Snapshot current context and start new phase |
| `block "REASON"` | Record a blocker |
| `unblock ID` | Remove a blocker |
| `search QUERY` | FTS5 search across PM docs |
| `promote TITLE` | Promote PM doc to T3 |
| `archive` | Synthesize to T3, start 90-day T2 decay |
| `close` | Archive + mark project completed |
| `restore PROJECT` | Restore within 90-day decay window |
| `reference QUERY` | Search archived syntheses |
| `expire` | Remove decayed PM entries |

---

## nx collection

Manage T3 cloud collections.

```
nx collection list
```

| Subcommand | Description |
|------------|-------------|
| `list` | All cloud collections with document counts |
| `info NAME` | Details for one collection |
| `verify NAME` | Existence check + document count |
| `delete NAME --yes` | Delete collection (irreversible); aliases: `-y`, `--confirm` |

**`verify` flags:**

| Flag | Description |
|------|-------------|
| `--deep` | Probe embeddings in addition to count |

---

## nx serve

Background daemon for indexing and search.

```
nx serve start
```

| Subcommand | Description |
|------------|-------------|
| `start` | Start background daemon |
| `stop` | Stop daemon |
| `status` | Uptime and per-repo indexing state |
| `logs` | Last 20 lines of daemon log |

**`logs` flags:**

| Flag | Description |
|------|-------------|
| `-n NUM` | Number of log lines to show |

---

## nx config

Configuration management.

```
nx config init
```

| Subcommand | Description |
|------------|-------------|
| `init` | Interactive credential wizard |
| `list` | Show all config values |
| `get KEY` | Get single value |
| `set KEY VALUE` | Set single value |

---

## nx doctor

Health check for all dependencies.

```
nx doctor
```

Checks: ChromaDB connectivity, Voyage AI key, Anthropic key, ripgrep binary, git binary.

---

## nx install / nx uninstall

Lightweight Claude Code integration for repos that don't contain the `nx/` plugin directory.

```
nx install claude-code
nx uninstall claude-code
```

Adds or removes `SessionStart`/`SessionEnd` hooks and a Nexus skill reference in `~/.claude/`. Not needed when the full `nx/` plugin is loaded (it provides these hooks and more).
