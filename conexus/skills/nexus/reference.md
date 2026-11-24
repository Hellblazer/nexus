# Nexus — Agent Usage Guide

Nexus provides MCP tools for semantic search, persistent memory, and knowledge management across sessions.

**Three storage tiers:**
- **T1 scratch** — session-scoped, service-backed Postgres (`scratch`,
  `scratch_manage` tools)
- **T2 memory** — service-backed Postgres, survives restarts (`memory_put`,
  `memory_get`, `memory_delete`, `memory_search` tools)
- **T3 knowledge** — pgvector via the nexus service, permanent (`search`,
  `store_put`, `store_get`, `store_list` tools). Local mode uses the bundled
  PG17+pgvector, embedding with bge-768 by default or Voyage when
  `NX_VOYAGE_API_KEY` reaches the local service (nexus-umm29 carve-out — one
  posture per boot; RDR-210 tracks a dual-mode engine); cloud mode uses the
  managed service and always embeds with Voyage.

All three tiers are Postgres behind the engine. SQLite is retired (RDR-158 P4)
and ChromaDB is not a live substrate in any mode (RDR-155 P4b — the dependency
is gone from `uv.lock`); this section described both as current until 7.20.0.

## MCP Tool Reference

Core tools are prefixed `mcp__plugin_conexus_nexus__`; catalog tools are prefixed `mcp__plugin_conexus_nexus-catalog__`.

The core server registers 46 tools, plus two `hook_*` tools the plugin's hooks call. Not tabled below, each described by its own tool description: scoped search (`search_metadata_scoped`, `search_topic_scoped`, `search_graph_hop`, `search_aspect_scoped`), `plan_delete`, `procedure_run`, `nx_answer_report`, `brief`, `tuple_*` and `mailbox_send` (see the mailbox skill), `setup`, `setup_status`, `daemon_uninstall`. The analytical operators are plan steps inside `nx_answer`, not tools (see Operators below).
There are 10 catalog tools (nexus-catalog server): `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats`.

### search

Semantic search across T3 knowledge collections.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query string |
| `corpus` | str | `"knowledge,code,docs"` | Corpus prefix, comma-separated list of prefixes, full collection name, or `"all"` to search every T3 collection |
| `limit` | int | `10` | Page size (results per page) |
| `offset` | int | `0` | Skip this many results. Footer shows `next: offset=N` for next page |
| `where` | str | `""` | Metadata filter: `KEY=VALUE` or `KEY>=VALUE`, comma-separated. Operators: `=`, `>=`, `<=`, `>`, `<`, `!=`. Numeric fields auto-coerced: `bib_year`, `bib_citation_count`, `page_count` |
| `cluster_by` | str | `""` | Set to `"semantic"` to group results by Ward hierarchical clustering. Each result gets `_cluster_label` metadata |
| `topic` | str | `""` | Restrict to one discovered taxonomy topic label (`nx taxonomy discover`) |
| `structured` | bool | `false` | Return the `{ids, tumblers, distances, ...}` dict instead of text |
| `threshold` | float \| None | `None` | Override the per-collection distance cutoff (lower is stricter) |
| `lexical` | bool | `false` | Also add exact-text hits; for a rare identifier a semantic search misses |

```
mcp__plugin_conexus_nexus__search(query="query"                                  # knowledge + code + docs (default)
mcp__plugin_conexus_nexus__search(query="query", corpus="all"                    # all T3 collections
mcp__plugin_conexus_nexus__search(query="query", corpus="code"                   # code collections only
mcp__plugin_conexus_nexus__search(query="query", corpus="knowledge__art-1-1__voyage-context-3__v1", limit=15  # specific collection (full name as `nx collection list` prints it)
mcp__plugin_conexus_nexus__search(query="query", where="bib_year>=2023"          # filter by year
mcp__plugin_conexus_nexus__search(query="query", where="section_type!=references" # exclude reference sections
mcp__plugin_conexus_nexus__search(query="query", cluster_by="semantic"           # group results by topic
```

**Automatic quality features**: results are filtered by per-collection distance thresholds (Voyage-embedded collections only), knowledge/docs/rdr collections over-fetch at 4x, and selective metadata filters route through the catalog.

### query

Document-level semantic search for analytical questions. Unlike `search` which returns individual chunks, `query` groups results by source document and returns the best-matching snippet per document with full metadata.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `question` | str | required | Natural-language research question |
| `corpus` | str | `"knowledge"` | Corpus prefix or full collection name. `"all"` for all corpora |
| `where` | str | `""` | Metadata filter: `KEY=VALUE`, comma-separated. Same syntax as `search` |
| `limit` | int | `10` | Maximum documents to return |
| `author` | str | `""` | Catalog filter: author contains this text (case-insensitive). Any catalog filter routes through the catalog |
| `content_type` | str | `""` | Catalog filter: `code`, `paper`, `rdr`, `knowledge` |
| `follow_links` | str | `""` | Follow catalog links of this type (`cites`, `implements`, ...) from the matches |
| `depth` | int | `1` | BFS depth for `follow_links` |
| `subtree` | str | `""` | Tumbler prefix or registered owner name (a 3+ segment address is refused) |
| `structured` | bool | `false` | Return the structured dict |

```
mcp__plugin_conexus_nexus__query(question="adaptive resonance theory cortical maps"
mcp__plugin_conexus_nexus__query(question="speech processing", corpus="knowledge__art-1-1__voyage-context-3__v1", where="page_count>=50"
mcp__plugin_conexus_nexus__query(question="error handling patterns", corpus="code", limit=5
```

Returns per document: title, relevance score, bibliographic and technical metadata, collection, and the best matching snippet.

Use `search` for chunk-level retrieval. Use `query` when you need to know **which documents** match.

### nx_answer

Plan-match-first analytical retrieval (RDR-080), for answers that must be reduced from many documents (cross-corpus synthesis, ranking, RDR research), not file:line, single-fact or already-in-T2 questions (use `search`/`query`: seconds). It runs a saved plan from the plan library; on a miss an inline `claude -p` planner grows, runs and saves one (there are no `plan_match`/`plan_run` tools). Every run is logged to T2 `nx_answer_runs`. Cost: p50 80s, p95 217s, can time out at 300s.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `question` | str | required | Natural-language question |
| `scope` | str | `""` | Catalog subtree (e.g. `"1.2"`) or corpus filter (e.g. `"knowledge"`). A subtree filters every `query` and `search_metadata_scoped` step whatever its corpus; the other search steps and any step under a document-level address such as `"1.2.4686"` run unscoped, which the answer names (`[scope not applied: ...]`) |
| `context` | str | `""` | Supplementary caller-supplied context for the plan matcher |
| `max_steps` | int | `6` | Cap on plan DAG size (applied to inline planner on miss) |
| `budget_usd` | float \| None | `None` | Soft per-call cost guidance in USD: an over-estimate warns, it does not refuse. `None` = the derived default; non-positive is refused |
| `budget_seconds` | float \| None | server default | Hard wall-clock cap on plan execution; on a trip, results so far come back |
| `trace` | bool | `True` | When False, redacts question + final_text in the run log |
| `dimensions` | dict/null | `null` | Dimensional filter for the matcher. Verb skills pin `{"verb": "research"}` etc. |
| `structured` | bool | `false` | Return an envelope dict (`final_text`, `chunks`, `plan_id`, ...) |
| `min_confidence` | float \| None | `None` | Per-call plan-match confidence floor |
| `force_dynamic` | bool | `false` | Skip plan matching; use the inline planner |
| `bindings` | dict/null | `null` | Plan bindings (e.g. `{"content_type": "rdr"}`) that make type-scoped builtin plans reachable; `source`, `owner`, `project` are reserved |
| `continuation` | bool \| None | `None` | `True` stops before the final reduction; finish it with `nx_answer_report` |

```
mcp__plugin_conexus_nexus__nx_answer(question="how does plan matching work"
mcp__plugin_conexus_nexus__nx_answer(question="...", dimensions={"verb":"research"}
mcp__plugin_conexus_nexus__nx_answer(question="...", scope="1.2"
mcp__plugin_conexus_nexus__nx_answer(question="...", trace=False            # redact in run log
```

### traverse

Walk the catalog link graph from seed tumblers (RDR-078 P3). Accepts `link_types` **XOR** `purpose` — never both. Returns the standard retrieval step-output contract so downstream plan steps can reference `$stepN.tumblers` / `$stepN.collections` / `$stepN.ids`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `seeds` | list/str | required | Tumbler(s) to start from (e.g. `["1.1.635"]` or `"1.1.635"`) |
| `link_types` | list/null | `null` | Catalog link types to follow (`implements`, `cites`, ...). Mutually exclusive with `purpose` |
| `purpose` | str | `""` | Named alias for a link-type set (e.g. `"find-implementations"`). Mutually exclusive with `link_types` |
| `depth` | int | `1` | BFS depth. Capped at 3 (SC-4) |
| `direction` | str | `"both"` | `"out"`, `"in"`, or `"both"` |

```
mcp__plugin_conexus_nexus__traverse(seeds=["1.1.635"], link_types=["implements","cites"], depth=2
mcp__plugin_conexus_nexus__traverse(seeds="1.1.635", purpose="find-implementations"
```

Returns `{"tumblers": [...], "ids": [...], "collections": [...]}`.

### store_get_many

Batch-hydrate document content by ID past the 300-record read cap (MAX_QUERY_RESULTS; RDR-079 hydration primitive). Use after `search(structured=True)` or `traverse` to fetch the actual text.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ids` | list/str | required | Document IDs. Accepts comma-separated string or list |
| `collections` | list/str | `"knowledge"` | Target collection(s). Single name, comma-separated, or list aligned 1:1 with `ids` |
| `max_chars_per_doc` | int | `4000` | Per-document truncation cap |
| `structured` | bool | `false` | Return `{contents, missing}` dict when True; human-readable string when False |

```
mcp__plugin_conexus_nexus__store_get_many(ids=["id1","id2"], collections="<collection name>"
mcp__plugin_conexus_nexus__store_get_many(ids="id1,id2", collections="<collection name>", structured=True
```

### Operators (plan steps, not MCP tools)

The ten analytical operators are not callable from a session. They run as steps
of a plan inside `nx_answer` (or `plan_run`), each a `claude -p` subprocess
(default timeout 300s); consecutive steps are bundled into one subprocess. A
plan step names the bare verb, and the plan runner resolves it in-process to
`operator_<verb>`. To use one, ask `nx_answer` a question its plans cover, or
save a plan with `plan_save` whose steps name these verbs.

| Plan verb | Input | Returns |
|---|---|---|
| `summarize` | `content`, optional `cited=True` | summary text, with citations when cited |
| `extract` | `inputs` (list or JSON array), `fields` (comma-separated) | `{"extractions": [...]}` |
| `rank` | `items`, `criterion` | `{"ranked": [...]}` |
| `compare` | `items` (or `items_a`/`items_b`), optional `focus` | `{"comparison": "<markdown>"}` |
| `generate` | `template`, `context`, optional `cited=True` | `{"output": "<generated>"}` |
| `filter` | `items` (JSON array), `criterion` | `{"items": [...], "rationale": [{"id", "reason"}]}` |
| `check` | `items`, `check_instruction` | `{"schema_version": 1, "ok": bool, "evidence": [{"item_id", "quote", "role", "state", "span", "tumbler", "chash", "collection"}], "rejected": [...], "uncovered": [...], "requires_review": bool}`; the host binds `state`/`span`/source fields, only `state: "bound"` is a valid witness |
| `verify` | `claim`, `evidence` | `{"schema_version": 1, "verified": bool, "reason": str, "evidence": [<same items as check>], "rejected": [...], "requires_review": bool, "citations": [...]}`; `citations` is deprecated (host-derived quotes) |
| `groupby` | `items`, `key` | `[{"key_value", "items"}]` |
| `aggregate` | `groups` (from `groupby`), `reducer` | one summary per group |

### nx_tidy / nx_enrich_beads / nx_plan_audit

Each spawns a `claude -p` subprocess (300-600s timeout) and requires real arguments — none of the three runs with no args. For T2 consolidation (overlaps, stale entries), use `memory_consolidate` instead — it operates on T2 directly with no subprocess.

- **`nx_tidy(topic, collection="knowledge")`** — read-only. Searches ONE T3 collection for entries matching `topic` and reports duplicates, contradictions and outdated entries with suggested actions; it writes nothing.
- **`nx_enrich_beads(bead_description, context="")`** — enriches the ONE bead description passed in with execution context (file paths, test commands, constraints) and returns markdown; it reads and writes no bead, so the caller writes the result back.
- **`nx_plan_audit(plan_json, context="")`** — audits ONE plan for file-path, dependency and assumption correctness against the codebase; not a scan of the plan library.

```
mcp__plugin_conexus_nexus__nx_tidy(topic="embedding quotas", collection="<subject>")
mcp__plugin_conexus_nexus__nx_enrich_beads(bead_description="<bead title + description text>")
mcp__plugin_conexus_nexus__nx_plan_audit(plan_json='{"steps": [...]}')
```

### store_put

Store content in the T3 permanent knowledge store.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | str | required | Text content to store |
| `collection` | str | required | The bare subject the note belongs to (`distributed-systems`); the placeholders `default`/`knowledge`/`notes`/`tmp`/`test` are refused (nexus-0fw11; Rule 1 of https://github.com/Hellblazer/nexus/blob/main/docs/collections.md) |
| `title` | str | `""` | Document title (recommended for dedup) |
| `tags` | str | `""` | Comma-separated tags |
| `ttl` | str | `"permanent"` | TTL: `Nd`, `Nw`, or `"permanent"` |
| `agent` | str | `""` | Subagent/role attribution (mirrors `memory_put`, nexus-4ftd7). Falls back to `NX_AGENT` env, then a distinct `"mcp"` marker — never left empty, never the T3 indexer's own default. Always pass it explicitly; an unmarked write collapses onto `"mcp"` and defeats `_flag_contradictions`'s agent-diversity precondition. |
| `session` | str | `""` | Deprecated: accepted and ignored |

```
mcp__plugin_conexus_nexus__store_put(content="finding text", collection="<subject>", title="research-topic", tags="arch", agent="<your-role>"
mcp__plugin_conexus_nexus__store_put(content="notes", collection="<subject>", title="sprint-notes", ttl="30d", agent="<your-role>"
```

**TTL formats**: `30d` (30 days), `4w` (4 weeks), `permanent` or `never` (no expiry).

### store_get

Retrieve the full content and metadata of a T3 entry by document ID.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `doc_id` | str | required | The 64-character content-hash id (from `store_list`, `store_put` or a `search` hit), or an exact document title |
| `collection` | str | `"knowledge"` | Collection name or prefix |

```
mcp__plugin_conexus_nexus__store_get(doc_id="research-adaptive-resonance", collection="knowledge"   # by exact title
```

### store_list

List entries in a T3 knowledge collection.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `collection` | str | `"knowledge"` | Collection name or prefix |
| `limit` | int | `20` | Page size |
| `offset` | int | `0` | Skip this many entries. Footer shows `next: offset=N` for next page |
| `docs` | bool | `false` | Show unique documents instead of individual chunks. Deduplicates by content_hash, shows title, chunk count, page count, extraction method |

```
mcp__plugin_conexus_nexus__store_list(collection="<subject>"                       # the bare subject; the catalog resolves it to the collection's label
mcp__plugin_conexus_nexus__store_list(collection="knowledge__art-1-1__voyage-context-3__v1", docs=true  # document-level view; a full collection name as `nx collection list` prints it
mcp__plugin_conexus_nexus__store_list(collection="knowledge__notes-1-1__voyage-context-3__v1", limit=50, offset=100
```

### memory_put

Store a memory entry in T2 (service-backed Postgres). Upserts by (project, title).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | str | required | Text content to store |
| `project` | str | required | Project namespace |
| `title` | str | required | Entry title (unique within project) |
| `tags` | str | `""` | Comma-separated tags |
| `ttl` | int \| None | `None` | Time-to-live in days. Omit for permanent (the default, nexus-473mx); a positive integer expires the row in that many days, extended on read. `ttl<=0` (0 included) is refused with a 400 — there is no "0 means permanent" coercion. |

```
mcp__plugin_conexus_nexus__memory_put(content="content", project="{repo}", title="findings.md"
mcp__plugin_conexus_nexus__memory_put(content="content", project="{repo}", title="findings.md", ttl=14
```

**Project naming**: Use purpose-specific suffixes:
- bare `{repo}` — general project memory and notes
- `{repo}_rdr` — RDR documents and gate results

### memory_get

Retrieve a memory entry by project and title.

When title is empty, lists all entries for the project (titles only — a second call with the specific title is required to get content).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | str | required | Project namespace |
| `title` | str | `""` | Entry title. Leave empty to LIST entries (titles only) |

```
mcp__plugin_conexus_nexus__memory_get(project="{repo}", title="findings.md"       # get content
mcp__plugin_conexus_nexus__memory_get(project="{repo}", title=""                   # list titles only
```

### memory_delete

Delete a T2 memory entry by project and title.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | str | required | Project namespace |
| `title` | str | required | Entry title to delete |

```
mcp__plugin_conexus_nexus__memory_delete(project="{repo}", title="stale-finding"
```

### memory_search

Full-text search across T2 memory entries: PostgreSQL full-text search over the title, content, and tags fields.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Plain-text search query, matched against title, content, and tags (no query grammar or operators) |
| `project` | str | `""` | Optional project filter |
| `limit` | int | `20` | Page size |
| `offset` | int | `0` | Skip this many results. Footer shows `next: offset=N` for next page |

```
mcp__plugin_conexus_nexus__memory_search(query="query"
mcp__plugin_conexus_nexus__memory_search(query="query", project="{repo}"
```

### memory_consolidate

T2 memory hygiene (RDR-061 E6): find overlapping entries, merge duplicates, flag stale entries. Merge is destructive and gated by `dry_run` and `confirm_destructive`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | str | required | `"find-overlaps"`, `"merge"`, `"flag-stale"` |
| `project` | str | required | T2 project namespace |
| `min_similarity` | float | `0.7` | Jaccard threshold for `find-overlaps` |
| `idle_days` | int | `30` | Staleness threshold for `flag-stale` |
| `keep_id` | int | `0` | Entry ID to keep when merging (must be > 0) |
| `delete_ids` | str | `""` | Comma-separated IDs to delete during merge |
| `merged_content` | str | `""` | Replacement content for kept entry |
| `dry_run` | bool | `False` | Preview merge without modifying T2 |
| `confirm_destructive` | bool | `False` | Required when merging > 1 entry |

```
mcp__plugin_conexus_nexus__memory_consolidate(action="find-overlaps", project="{repo}"
mcp__plugin_conexus_nexus__memory_consolidate(action="flag-stale", project="{repo}", idle_days=30
mcp__plugin_conexus_nexus__memory_consolidate(action="merge", project="{repo}",
    keep_id=42, delete_ids="43", merged_content="...", dry_run=True
mcp__plugin_conexus_nexus__memory_consolidate(action="merge", project="{repo}",
    keep_id=42, delete_ids="43,44,45", merged_content="...", confirm_destructive=True
```

Merge safety: aborts with `KeyError` if `keep_id` does not exist (prevents data loss when `expire()` races with merge). `delete_ids` are preserved in that case.

### scratch

T1 session scratch pad: ephemeral working storage (idle sessions are swept after 24 hours; flagged entries are copied to T2).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | str | required | `"put"`, `"search"`, `"list"`, `"get"`, `"delete"` |
| `content` | str | `""` | Content to store (for `"put"`) |
| `query` | str | `""` | Search query (for `"search"`) |
| `tags` | str | `""` | Comma-separated tags (for `"put"`) |
| `entry_id` | str | `""` | Entry ID (for `"get"`, `"delete"`) |
| `limit` | int | `10` | Max results for search |
| `agent` | str | `""` | Role attribution for `"put"` |
| `persist` | bool | `false` | For `"put"`: copy the entry to T2 at session end |
| `scope` | str | `"session"` | `"identity"` survives `/clear` and restart |

```
mcp__plugin_conexus_nexus__scratch(action="put", content="working hypothesis: the cache is stale"
mcp__plugin_conexus_nexus__scratch(action="search", query="cache"
mcp__plugin_conexus_nexus__scratch(action="list"
mcp__plugin_conexus_nexus__scratch(action="get", entry_id="<id>"
mcp__plugin_conexus_nexus__scratch(action="delete", entry_id="<id>"
```

### scratch_manage

Manage scratch entries: flag for persistence or promote to T2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | str | required | `"flag"`, `"promote"` |
| `entry_id` | str | required | Scratch entry ID |
| `project` | str | `""` | Target project (required for promote; flag's destination, default `scratch_sessions`) |
| `title` | str | `""` | Target title (required for promote; flag's title) |
| `scope` | str | `"session"` | `"session"` or `"identity"` |

```
mcp__plugin_conexus_nexus__scratch_manage(action="flag", entry_id="<id>"
mcp__plugin_conexus_nexus__scratch_manage(action="promote", entry_id="<id>", project="{repo}", title="findings.md"
```

**Promote return value** (RDR-057 Phase 1c): The promote action returns a `PromotionReport` rendered in the response string:
- `(action=new)` — clean write, no overlap detected
- `(action=overlap_detected)` — the full-text check found a similar existing T2 entry. The new entry is still written as a separate row; the agent must decide whether to also call `memory_consolidate(action="merge")` to dedupe.

**Usage pattern**: T1 scratch for in-flight notes; flag important items to copy them to T2 at session end; validated findings go to T3 via store_put.

### collection_list

List all T3 collections with document counts.

```
mcp__plugin_conexus_nexus__collection_list()
```

Returns collection names, document counts, and embedding models for every collection in the T3 database.

### plan_save

Save a query execution plan to the T2 plan library.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Original natural-language question |
| `plan_json` | str | required | JSON string: `{"steps": [...], "tools_used": [...], "outcome_notes": "..."}` |
| `verb` | str | required | Retrieval verb (research, analyze, query, review, debug, document) |
| `answer_kind` | str | required | `lookup`, `synthesis`, `discovery/listing`, `comparison`, `audit`, `debug` or `unknown`. A saved plan serves only a request of this kind, only from `project`; `unknown` serves nothing. A plan ending in retrieval (not an operator) cannot claim lookup, synthesis, comparison, audit or debug |
| `project` | str | `""` | Project namespace; a saved plan serves only requests from this project |
| `outcome` | str | `"success"` | Plan outcome: "success" or "partial" |
| `tags` | str | `""` | Comma-separated tags |

```
Use plan_save tool: query="What does RDR-232 say about plan eligibility?", plan_json='{"steps": [{"tool": "search", "args": {"query": "$intent"}}, {"tool": "summarize", "args": {"inputs": "$step1.ids"}}]}', verb="query", answer_kind="synthesis", project="nexus"
```

### plan_search

Search the T2 plan library for similar query plans.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query |
| `project` | str | `""` | Optional project filter |
| `limit` | int | `5` | Maximum results |

```
Use plan_search tool: query="enumerate documents", project="nexus"
```

## Indexing (CLI only)

Indexing operations have no MCP equivalent — use `nx` CLI via Bash:

```bash
nx index repo <path>                       # register and index a repo
nx index repo <path> --frecency-only       # refresh git frecency scores only (fast)
nx index pdf <path> --corpus my-papers
nx index pdf --dir <path> --collection art-papers  # the bare subject; the catalog names the collection
nx index md  <path> --corpus notes
```

## Health and Server (CLI only)

```bash
nx doctor                                  # verify all credentials and tools
nx mineru start                            # start MinerU PDF extraction server
nx mineru status                           # check server health
nx mineru stop                             # stop server
```

## Workflow — when and why to use each tier

**Session lifecycle:**
1. Search T3 for prior art before starting work: mcp__plugin_conexus_nexus__search(`query="topic", corpus="knowledge"`
2. Index the codebase once per repo: `nx index repo <path>` (CLI)
3. Use T1 scratch for working notes during the session
4. Flag important scratch items for auto-promote to T2: mcp__plugin_conexus_nexus__scratch_manage(`action="flag", entry_id="<id>"`
5. Persist validated findings to T3 at session end: Use store_put tool

**Tier selection:**
- **T1 scratch**: hypotheses, interim findings, checkpoints — anything ephemeral to this session
- **T2 memory**: cross-session state, agent relay notes, active project context
- **T3 knowledge**: validated findings, architectural decisions, reusable patterns — anything worth keeping permanently

**Collection naming**: type the bare subject (`collection="distributed-systems"`); never a model token, version or `__` segment ([rules](https://github.com/Hellblazer/nexus/blob/main/docs/collections.md)). `nx index repo` mints `docs`, `code` and `rdr` collections, rendered `<content_type>__<owner>__<embedding_model>__v<n>`; a NEW `knowledge` subject is labelled `c<id>` by the engine. Existing collections keep their names; a full name is accepted wherever one is read.

**Title conventions** (use hyphens, not colons):
- `research-{topic}` — research findings
- `decision-{component}-{name}` — architectural decisions
- `pattern-{name}` — reusable patterns
- `debug-{component}-{issue}` — debugging insights

## T2 Search

memory_search uses PostgreSQL full-text search: it matches words, not semantic meaning, and the query is plain text with no operators. Rules:

- Searches all three fields: **title**, **content**, and **tags**
- Use the terms the stored document uses, not conceptual paraphrases; English words match across stemming (`indexing` finds `indexed`), and accents are folded
- `"retain slash commands"` works if those words appear in the document
- A multi-word query matches a document when every word matches it the same way: all as English stems (title and content), or all as exact words (tags, and the title split at `/ . - _`); a query made only of English stopwords can match only the exact-word way
- When results are empty: drop one term at a time to identify which token has no match. Use memory_get with empty title to browse titles directly

## Code Search: When to Use search vs Grep

Use **Grep** (the Grep tool) for:
- Finding a class or function by name: `class EmbeddingClient`
- Locating all usages of a symbol: `EmbeddingClient`
- Exact text matches: error messages, config keys, import paths

Use **search tool** with `corpus="code"` for:
- Conceptual queries when you don't know the file name or function name
- Finding "what handles PDF processing" across an unfamiliar codebase
- Cross-file concept queries: "retry logic with exponential backoff"
