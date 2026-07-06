# MCP Servers

Nexus ships two MCP servers, bundled in the Claude Code plugin and the Claude Desktop `.mcpb` extension. This page is the **tool catalog** — every tool, on which server, with a one-line purpose.

For **when to use which retrieval interface**, see [Querying Guide](querying-guide.md). For conceptual background, see [Document Catalog](catalog.md) and [Storage Tiers](storage-tiers.md).

## The three servers

| Server | Entry point | Tools | Purpose |
|---|---|---|---|
| `nexus` | `nx-mcp` | 35 | Storage tiers, retrieval, operators, orchestration |
| `nexus-catalog` | `nx-mcp-catalog` | 10 | Document catalog, link graph, tumbler resolution |
| `devonthink` | `nx-mcp-devonthink` | ~17 (DT present) / 1 (DT absent) | DEVONthink agent surface: AI/content/bib/capture tools + the `dt_incorporate` composite (RDR-139 Layer A') |

The `nexus` and `nexus-catalog` servers register automatically when you install the plugin (`/plugin install conexus@nexus-plugins`) or the `.mcpb` extension. No separate install.

**Substrate dependency**: since RDR-155, every persistent tier (T2 + T3 storage/retrieval tools) routes through the native nexus-service (`nx daemon service`, Postgres 17 + pgvector), not a ChromaDB daemon. A single `nx init` provisions and starts it and offers to register the OS autostart unit so it survives reboots (RDR-174 collapsed flow). See [Getting Started](getting-started.md#first-time-setup-the-storage-backend) for the install walkthrough and [Container Integration](container-integration.md) for the multi-process / multi-host model. (The standalone SQLite T2 daemon — `nx daemon t2 install --autostart` — remains an explicit opt-in for `NX_STORAGE_BACKEND=sqlite`.)

## `nexus` — retrieval + storage (35 tools)

Full tool names follow `mcp__plugin_conexus_nexus__<tool>`.

### Retrieval (T3)

| Tool | Purpose |
|---|---|
| `search` | Semantic chunk search over T3 collections. Supports `topic` for topic-scoped search, `cluster_by="semantic"` for topic grouping, automatic same-topic distance boost |
| `query` | Document-level catalog-aware retrieval (scope by `author`, `content_type`, `subtree`, `follow_links`, `depth`). Link-aware + topic-aware ranking |
| `store_put` | Write a document into a T3 collection. Fires post-store hooks: batch chain auto-assigns to nearest topic; document-grain chain enqueues aspect extraction on `knowledge__*` (RDR-089) |
| `store_get` | Retrieve a document by id from a T3 collection |
| `store_get_many` | Batch hydration: given N ids, return N contents (with `missing` for not-found). Handles 300+ ids beyond the per-request 300-record limit |
| `store_list` | Paginate documents in a T3 collection |

### Memory (T2)

| Tool | Purpose |
|---|---|
| `memory_put` | Write a per-project persistent note |
| `memory_get` | Retrieve by `(project, title)` or id. Title resolution is exact-then-prefix; ambiguous prefixes return candidates rather than picking one |
| `memory_search` | FTS5 keyword search over T2 memory |
| `memory_delete` | Delete a single note |
| `memory_consolidate` | Find overlaps, merge, flag stale entries. See [storage tiers § T2](storage-tiers.md#t2----memory-bank) |

### Scratch (T1)

| Tool | Purpose |
|---|---|
| `scratch` | Put / get / search / list / delete session-scoped entries |
| `scratch_manage` | Flag for promotion, unflag, promote to T2, reconnect after T1 restart |

### Collections + plan library

| Tool | Purpose |
|---|---|
| `collection_list` | List all T3 collections visible to the current credentials |
| `plan_save` | Persist a plan template or ad-hoc plan (TTL-bounded) for later reuse |
| `plan_search` | Retrieve cached plans by semantic similarity (FTS5) |
| `traverse` | Walk the catalog link graph from seed tumblers with typed link filters or a named purpose. Depth capped at 3. Returns `{tumblers, ids, collections}` for downstream retrieval |

### Operators (LLM-backed, RDR-079)

Each operator spawns a `claude -p --output-format json --json-schema …` subprocess with a task-specific system prompt. Structured output is unwrapped from the wrapper.

Inside `nx_answer` / `plan_run`, consecutive operator steps collapse into a single subprocess via operator bundling (55–72% latency savings). Direct MCP-tool calls still spawn per-operator subprocesses. See [Querying Guide § Operator bundling](querying-guide.md#operator-bundling).

| Tool | Purpose |
|---|---|
| `operator_extract` | Pull structured fields (`fields="a,b,c"`) from free text |
| `operator_rank` | Order items by a criterion |
| `operator_compare` | Compare items focused on a specific axis |
| `operator_summarize` | Summarize content (citation-aware via `cited=True`) |
| `operator_generate` | Generate text following a template, grounded in `context` |
| `operator_filter` | Narrow items by a natural-language criterion (RDR-088 §D.4). Returns `{items, rationale[{id, reason}]}` |
| `operator_check` | Cross-item consistency probe (RDR-088 §D.2). Returns `{ok, evidence[{item_id, quote, role}]}` |
| `operator_verify` | Single-claim verification against one evidence source (RDR-088 §D.2). Returns `{verified, reason, citations[]}` |
| `operator_groupby` | Partition items by a natural-language key into `[{key_value, items}]` (RDR-093 §D.4). SQL fast-path over `document_aspects` when items carry catalog identity, else `claude -p` |
| `operator_aggregate` | Reduce each `operator_groupby` group to a per-group summary (RDR-093 §D.4). Pairs with `operator_groupby` for the `filter → groupby → aggregate` pipeline |

### Orchestration (RDR-080)

| Tool | Purpose |
|---|---|
| `nx_answer` | Retrieval trunk: `plan_match` → `plan_run` → record. Plan-miss falls through to an inline `claude -p` planner. See [Querying Guide § nx_answer](querying-guide.md#conexusquery-skill--nx_answer-mcp-tool-analytical-queries) |
| `nx_tidy` | Consolidate T3 knowledge entries on a topic |
| `nx_enrich_beads` | Enrich a bead with execution context (file paths, test commands, constraints) |
| `nx_plan_audit` | Audit a plan for correctness and codebase alignment |

All four `nx_*` tools are async with a configurable `timeout` (default 120s).

## `nexus-catalog` — document catalog (10 tools)

Full tool names follow `mcp__plugin_conexus_nexus-catalog__<tool>`. No redundant `catalog_` prefix on the short names.

| Tool | Purpose |
|---|---|
| `search` | Metadata search across the catalog (title, author, corpus, file path) |
| `show` | Full metadata + all inbound/outbound links for a document |
| `list` | Browse catalog entries with filters (type, subtree, owner) |
| `register` | Add a new document to the catalog |
| `update` | Update metadata on an existing catalog entry |
| `link` | Create a typed link between two documents |
| `links` | Return live links for a document (deleted nodes excluded). Optional BFS via `depth` |
| `link_query` | Query the full link table including orphans (admin / audit view) |
| `resolve` | Resolve a file path, title, or tumbler to a catalog entry |
| `stats` | Summary stats — total entries, link counts by type, orphan counts |

## `devonthink` — DEVONthink agent surface (RDR-139 Layer A')

Full tool names follow `mcp__plugin_conexus_devonthink__<tool>`. This server is
the agent-facing face of the DEVONthink integration: a nexus-owned MCP server
that proxies a curated slice of DEVONthink's built-in MCP plus nexus composites.

It **always spawns** and gates internally on `available()`. When DEVONthink is
reachable it advertises the curated surface below; when DEVONthink is absent it
advertises only `devonthink_status` (zero DT tools), still exits 0, and never
errors a DT-less consumer. The `conexus/.mcp.json` entry carries
`alwaysLoad:false` purely as a tool-search startup optimization — the internal
gate is the optionality mechanism, not the `.mcp.json` flag.

The agent's DEVONthink entry point is a record UUID obtained from a **nexus**
search (DT's own selectors stay on the `nx dt` CLI, out of scope here), after
which it uses these tools and the `dt_incorporate` composite.

| Tool | Purpose |
|---|---|
| `devonthink_status` | Whether DT is reachable + open-database count (the always-present stub) |
| `find_similar_records` | DT AI "See Also" neighbours of a record |
| `classify_record` | DT AI suggested groups for a record |
| `extract_record_content` | AI-optimised text of a record |
| `extract_record_highlights` / `extract_record_mentions` | Markdown summary of a record's highlights / mentions |
| `get_record_text` / `get_record_annotation` | A record's body / annotation note |
| `get_record_links` / `get_databases` | A record's item links / the open databases |
| `resolve_doi_metadata` / `search_crossref` / `resolve_google_books_metadata` | Bibliographic resolution (CrossRef / Google Books) |
| `capture_web_page` / `download_pdf_from_doi` / `import_file` | Capture a URL / DOI PDF / loose file into DT |
| `dt_incorporate` | Composite: for an already-indexed record, create DT-derived `relates` edges (Layer B) and stamp the nexus identity back onto the DT record (Layer F), in one agent call |

## CLI-only operations

Some operations are intentionally not exposed as MCP tools — they are destructive, expensive, or maintenance tasks where human-in-the-loop confirmation matters. Available via `nx` CLI only.

| CLI command | Why not MCP |
|---|---|
| `nx store delete` | Destructive T3 document deletion |
| `nx collection info` | Expensive ChromaDB introspection, human-debugging shape |
| `nx collection verify` | Full-collection scan; rarely needed by agents |
| `nx catalog unlink` | Destructive edge removal |
| `nx catalog link-audit` | Full-graph scan, operator-oriented |
| `nx catalog link-bulk-delete` | Bulk link deletion by filter; high blast radius |
| `nx taxonomy *` | Topic curation tasks (discover, review, merge, split, rebuild). Agents benefit from taxonomy via the automatic boost in `search`/`query` |

The Python functions still exist in `src/nexus/mcp/core.py` and `src/nexus/mcp/catalog.py`; they just lack the `@mcp.tool()` decorator.

## Routing rule of thumb

| Task | Server | Tool |
|---|---|---|
| Find code that handles retries | `nexus` | `search` |
| Search within a topic | `nexus` | `search` with `topic=` |
| Summarize papers by an author | `nexus` | `query` with `author=` |
| What RDRs cite this paper? | `nexus-catalog` | `links` with `link_type="cites"` |
| What collection is this paper in? | `nexus-catalog` | `search` or `resolve` |
| Persist a research finding | `nexus` | `store_put` |
| Remember for next session | `nexus` | `memory_put` |
| Share a hypothesis with a sibling agent | `nexus` | `scratch` |
| Cache a query plan for reuse | `nexus` | `plan_save` |

Content (chunks, documents, notes) is on `nexus`; metadata and relationships (entries, typed links, tumblers) are on `nexus-catalog`. `query` crosses the boundary — it uses catalog metadata to scope a content search.

## Pagination

Three tools return paged results and accept `offset`: `search`, `store_list`, `memory_search`. Response footer:

```
--- showing 1-20 of 57. next: offset=20
--- showing 41-57 of 57. (end)
```

Pass `offset=N` back to the same tool to fetch the next page. Default page size: 20 for list-style tools; `n_results` for `search`.

## Permission auto-approval

The plugin installs a `PermissionRequest` hook that auto-approves any tool call matching `mcp__plugin_conexus_.*`. This covers both servers plus the bundled `sequential-thinking` server. Dangerous system operations (force-push, `bd delete`, deploys) are not matched and stay behind the normal confirmation flow.

To enforce stricter permission boundaries on a custom agent, narrow the matcher in `conexus/hooks/hooks.json`.

## Failure modes

The `nx_answer` / `nx_tidy` / `nx_plan_audit` / `nx_enrich_beads` / `operator_*` tools all wrap a `claude -p` subprocess (`src/nexus/operators/dispatch.py::claude_dispatch`). Understanding that substrate explains most of their failure surface.

- **Subprocess timeout (`OperatorTimeoutError`)**: every call to `claude_dispatch` runs under `asyncio.wait_for(proc.communicate(...), timeout=timeout)`. Standalone `operator_*` tools default to 300s; `nx_plan_audit` / `nx_tidy` default to 600s. **Symptom**: the tool call raises with a message like `claude -p timed out after 300.0s; partial output (N B stdout, N B stderr) logged to <path>`. **Cause**: the underlying analytical workload (extraction, ranking, comparison, plan audit) genuinely didn't finish inside the budget — bead nexus-7sbf raised these defaults after real workloads were false-timing-out at 60–120s, so a timeout at the current defaults usually means the input is unusually large, not that the timeout is miscalibrated. **How to check**: the exception message names the log file directly — `~/.config/nexus/logs/operator-timeout-<UTC-timestamp>.log` — which holds whatever partial stdout/stderr the subprocess had produced when it was killed (SIGKILL to the whole process group via `safe_killpg`, so nested `claude -p` children and tool subprocesses are reaped too, not just the leader). Read that file first — it often shows the child was still mid-tool-call, which tells you whether to raise the budget or narrow the input. **Fix**: pass a larger `timeout` argument to the tool call (callers cannot go *below* the 300s floor — `mcp/core.py::_clamp_subagent_timeout` silently clamps a lower request upward and emits a `subagent_timeout_clamped` structlog warning, so lowering it to "fail faster" during debugging won't work; look for that warning in `mcp.log` if a requested timeout appears to have been ignored), or reduce the amount of content passed in (`items`, `context`, `groups`) so the subprocess has less to reason over. **How to verify**: re-run with the raised timeout and confirm the call returns a structured result rather than raising again; for `nx_answer` specifically, `plan_run` emits per-step `nx_answer_step_start` / `nx_answer_step_complete` structlog events to `mcp.log`, so tailing that file during a re-run shows which step is actually slow.
- **`nx_answer` plan-step failure is non-fatal by design**: unlike a standalone `operator_*` call, a single step timing out or erroring inside an `nx_answer` multi-step plan does **not** fail the whole call. `plans/runner.py` catches `OperatorError`/`OperatorTimeoutError` per step (or per bundled segment), logs a `operator_step_failed` structlog warning naming the failing tool and step index, substitutes a sentinel value, and continues the plan. **Symptom**: `nx_answer` returns a plausible-looking answer that's actually missing a step's contribution, or a downstream `$stepN.<field>` reference resolves to an empty/sentinel value instead of raising. **How to check**: grep `mcp.log` for `operator_step_failed` around the call's timestamp — the log line names the tool and step index that degraded. **Fix**: same as above (raise timeout / shrink input for that step), or re-run with `structured=True` to inspect which step produced the sentinel. **How to verify**: `operator_step_failed` no longer appears for that step on re-run, and the field the plan references is populated.
- **`OperatorOutputError` (non-timeout)**: the subprocess exited 0 but stdout was empty or not valid JSON, or exited non-zero. **Symptom**: `claude -p exited N: <stderr snippet>` or `claude -p produced empty stdout` / `claude -p output is not valid JSON`. **Cause**: usually a malformed `--json-schema`, a prompt that induced free-text output despite the schema constraint, or an actual crash in the child (auth failure, missing CLI). **How to check**: the exception carries the first 300 chars of stderr, or the raw stdout snippet — enough to distinguish an auth/CLI problem from a schema-adherence problem. **Fix**: if stderr shows an auth or CLI-not-found error, check that `claude` is on `PATH` for the environment the MCP server process itself runs in (not your interactive shell — see the Desktop-install PATH footgun in `docs/desktop-deployment.md` for the analogous class of bug). If it's a JSON-adherence failure, simplify the schema or the prompt. **How to verify**: re-run and confirm a `dict` is returned instead of an exception.
- **Timeout clamping surprises**: because `_SUBAGENT_TIMEOUT_FLOOR = 300.0` silently raises any caller-supplied timeout below it, a subagent (plan-enricher, plan-auditor) that "already passed a timeout" may not be getting the value it thinks it is. **How to check**: `subagent_timeout_clamped` in `mcp.log`, with `requested` and `floor` fields. This is expected behavior (nexus-7sbf), not a bug — the floor exists specifically to stop agents from re-introducing false-positive timeouts via low overrides.

## See also

- [Querying Guide](querying-guide.md) — when to use which interface, the `nx_answer` trunk, operator bundling, search quality features
- [Document Catalog](catalog.md) — what the catalog is, link types, purposes, topic taxonomy
- [Architecture § Module Map](architecture.md#module-map) — internal module layout
- [CLI Reference — nx catalog](cli-reference.md#nx-catalog) — CLI equivalents for catalog tools
