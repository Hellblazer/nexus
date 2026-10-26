# Nexus Configuration Reference

## Config Hierarchy

Four levels, highest priority wins:

1. **Environment variables** (`NX_*`) — highest priority
2. **Per-repo**: `.nexus.yml` in repo root (gitignored by default)
3. **Global**: `~/.config/nexus/config.yml`
4. **Built-in defaults** — lowest priority

Each level is deep-merged, with higher-priority values winning.

## Local Mode

Nexus auto-detects local mode when cloud credentials are absent. The recommended setup is `uv tool install conexus --python 3.12 && nx init`: `nx init` provisions the local service stack and sets its embedder to bge-768 by default, fetching the ONNX the Java service reads (see [nx init](cli-reference.md#nx-init)). The service can be switched to embed with Voyage instead — see [CLI reference § Local mode with Voyage](cli-reference.md#nx-init). It does **not** add the `[local]` extra — the RDR-144 embedder picker that did was removed at RDR-174 P1.3. Request the extra at install time instead, with `uv tool install "conexus[local]" --python 3.12`; `[local]` adds the Python-side bge-768 embedder used by the non-service local paths. In the default service mode the service embeds server-side (bge-768 by default) and the client names collections from the service's token, so a plain `conexus` install searches with bge-768 too. Upgrade with [`nx self install`](cli-reference.md#nx-self-install), which carries the extras recorded in the install receipt into the new generation; `uv tool upgrade conexus` and `uv tool install --reinstall conexus` do not touch a generation install.

| Env var | Default | Description |
|---|---|---|
| `NX_LOCAL` | (auto) | `1` = force local, `0` = force cloud, unset = auto-detect |
| `NX_LOCAL_CHROMA_PATH` | `~/.local/share/nexus/chroma` | Path to a legacy ChromaDB store. **Inert as of 7.0.0** (the migration reader was deleted with the `chromadb` dependency, RDR-155 P4b). The directory is a relic nothing reads, with no path back to that era (Sam, 2026-08-29); T3 serves from the Postgres+pgvector service. |
| `NEXUS_CATALOG_PATH` | `~/.config/nexus/catalog` | Where the retired local catalog directory (a repository plus JSONL files) used to live. The live catalog is in the service's Postgres; the path is still read by the subagent-start hook, which looks for a leftover directory there. |
| `NEXUS_CATALOG_ALLOW_CROSS_PROJECT` | unset | Set to `1` on the **client** to bypass the register-time cross-project source_uri guard, enforced engine-side (`CatalogRepository.deriveSourceUri`, nexus-e7cys). `HttpCatalogClient.register`/`.register_many` read this and forward it on the wire as `allow_cross_project` — the engine has no access to the client's environment. Emergency-only escape hatch for known-good recovery scripts that legitimately need to register rows across project boundaries; never the right answer for normal indexing. The engine logs `event=cross_project_source_uri_override_used` whenever the override is actually exercised |

**`config.yml` keys** (set by `nx init`, under the `local:` block in `~/.config/nexus/config.yml`):

| Key | Default | Description |
|---|---|---|
| `local.embed_model` | (auto-select) | The embedder `nx init` recorded (`BAAI/bge-base-en-v1.5`; `nx init` keeps an existing `voyage-*` value). Absent = legacy auto-select (bge if the `[local]` extra is importable, else MiniLM). A `voyage-code-3` / `voyage-context-3` value is also legal (nexus-umm29 opt-in) — it also needs `voyage_api_key` configured and the service restarted before the engine serves Voyage (and, with the bge files present on an engine at or above `engine-service-v0.1.161`, it then serves bge beside it); see [CLI reference § Local mode with Voyage](cli-reference.md#nx-init). |
| `local.fastembed_cache_path` | `~/.local/share/nexus/fastembed_cache` (XDG-aware) | Stable cache dir for the bge-768 model so it is not re-downloaded to a volatile `$TMPDIR` on every reboot. |

**Mode selection**: As of 6.0, managed-cloud mode activates when `NX_SERVICE_URL` (+ `NX_SERVICE_TOKEN`) is set — the client routes T3 through the hosted service (see [Managed-Cloud Credentials](#managed-cloud-credentials) below). Otherwise local mode is used. Set `NX_LOCAL=1` to force local mode even with service credentials present. (The legacy `CHROMA_API_KEY`/`VOYAGE_API_KEY` auto-detect predates the service substrate and applies only to pre-6.0 installs that have not migrated.)

**Embedding tiers**: these apply to the client's own embedder, which only the non-service local paths use; the service embeds with bge-768 whatever the client has (the `NX_EMBED_PROFILE` row below). Tier 0 (bundled MiniLM-L6-v2, 384d) is always available in the client. Ask for tier 1 (bge-base-en-v1.5, 768d, better quality; downloads the model on first embed) when you install the CLI: `uv tool install "conexus[local]"`. The extra is recorded in the generation's install receipt and [`nx self install`](cli-reference.md#nx-self-install) threads it into every later generation, so an upgrade never drops it; to ADD it to an existing generation install, run `nx self install --extras local` (nexus-pffc4 — merges with the extras the install already has). (On a box still on the legacy uv-tool layout, `uv tool install --reinstall "conexus[local]"` still adds it; on a generation install that same command rebuilds the legacy uv tree over the nexus-owned shims instead.)

**Legacy ChromaDB store path**: Defaults to `$XDG_DATA_HOME/nexus/chroma` or `~/.local/share/nexus/chroma`, overridable with `NX_LOCAL_CHROMA_PATH`. **No longer read as of 7.0.0** — the migration reader was deleted with the `chromadb` dependency. The directory is a relic nothing reads, with no path back to that era (Sam, 2026-08-29) and no cleanup verb; live T3 serves from the Postgres+pgvector service.

**Switching embedders or modes**: Changing the embedding model (switching local↔cloud, *or* switching local tiers 384-dim MiniLM ↔ 768-dim bge) makes the existing vectors incompatible (different dimensions/space). On the next `nx index repo .` the staleness check detects the model change and re-embeds into **new** collections under the new model token. **It does NOT automatically delete or migrate the old collections**: they remain behind under the previous token and silently return no results (their dimension no longer matches the active embedder).

`nx init` offers no migration (that flow was removed at RDR-174 P1.3) and never re-embeds or deletes a collection. Clean up by hand: `nx doctor` flags the dimension mismatch, `nx collection reindex <name>` rebuilds one from source, and `nx collection delete <name>` removes an orphan.

## Managed-Cloud Credentials

As of 6.0, managed-cloud mode points `nx` at a hosted nexus service that owns its cloud Postgres + pgvector and embeds with Voyage server-side. The client credentials are the service URL and a bearer token. Both resolve **env first, then `config.yml`**: set them interactively with `nx config init` (or `nx config set service_url/service_token`), or export the env vars below — the environment always takes precedence:

| Env var | Required | Notes |
|---|---|---|
| `NX_SERVICE_URL` | Cloud mode | Managed nexus service base URL (or `service_url` in `config.yml`). Setting it is what switches `nx` into managed mode; the URL is never defaulted for mode resolution. `https://api.conexus-nexus.com` is only the fallback the managed endpoint probe uses once a mode is chosen. A URL carrying `user:password@` is refused (here and by `nx config set service_url`); the credential is `NX_SERVICE_TOKEN` or the mint pair below. |
| `NX_SERVICE_TOKEN` | Cloud mode, static token | Bearer token for a self-hosted or static-token service (`service_token`). |
| `NX_MINT_TOKEN` | Cloud mode, managed | Mint credential (`mint_token`): a token bound to one tenant that mints short-lived data tokens. Managed users hold this kind. |
| `NX_MINT_TENANT` | With `NX_MINT_TOKEN` | The tenant the mint credential is bound to (`mint_tenant`). The two travel as a pair. |

Hold one kind of credential. The CLI prefers a static `service_token` when both are saved, so `nx config init` asks which kind you hold, saves only that kind's keys and removes the other's (`nx config unset KEY` removes one saved key; an environment variable of the same name still applies).

Export both in your shell profile or process manager. You do not supply a Voyage key in managed-cloud mode (the service owns it).

### Migration-source credentials — RETIRED as of 7.0.0

`CHROMA_API_KEY`, `CHROMA_DATABASE` and `CHROMA_TENANT` are **inert**. Setting them does nothing.

Through 6.x they were read by the ladder's substrate rung so `nx upgrade` could read an existing ChromaDB Cloud store as a migration source. RDR-155 P4b deleted the Chroma read client, the migration ETL and finally the `chromadb` dependency itself, so there is no code left that could consume them. They are documented here only so an operator who finds them in an old `config.yml` or shell profile knows they are dead rather than broken — they can be deleted.

**If you are still on a pre-migration install**, do not set these and expect an upgrade to work. Upgrading straight from a Chroma-era install into 7.0.0 is detected and refused with a loud two-hop redirect (`nexus.stranded_install`): migrate on a 6.x release first, which still ships the migration tool, then upgrade to 7.0.0. Run that 6.x migration against a local engine (stop any running local service, clear `NX_SERVICE_URL` and the `service_url` key in `config.yml`, and `export NX_LOCAL=1`, which is needed in addition because it does not by itself override `service_url`), never against a managed endpoint; reaching the managed cloud is a later hop with the current client ([Migration Runbook § Getting that data into the managed cloud](migration-runbook.md#getting-that-data-into-the-managed-cloud)). Frozen Chroma directories on disk are relics nothing reads, with no path back to that era (Sam, 2026-08-29) and no cleanup verb.

## Bibliographic enrichment (`nx enrich bib`)

| Env var | Required | Notes |
|---|---|---|
| `S2_API_KEY` | No | Semantic Scholar API key (100 req/s, vs 100/5min unauthenticated). Get one at https://www.semanticscholar.org/product/api#api-key |
| `OPENALEX_MAILTO` | No | Your email, for OpenAlex's polite pool (higher rate limit, no key needed). |

`nx enrich bib` fetches bibliographic metadata (year, venue, authors, citation count) for the catalog documents of a collection, and records it on each document's catalog row and on the chunks only that document holds. A collection enriched before the catalog carried `bib_*` columns (nexus-9l2lg) keeps its ids on chunk rows only: run `nx enrich bib <collection> --backfill-catalog` first (no API calls), or each such document costs one backend lookup again. `--source auto` (the default) picks the backend: Semantic Scholar when `S2_API_KEY` is set, OpenAlex otherwise (nexus-57mk added the OpenAlex backend so a missing key no longer means unauthenticated rate-limited Semantic Scholar). Pass `--source s2` or `--source openalex` to pin one explicitly, or `--source dt` to gap-fill from DEVONthink's CrossRef resolver after the `auto` pass. See `nx enrich bib --help` for the full option list.

**Historical note (7.0.0: no longer operative).** `chroma_database` named the ChromaDB Cloud database the migration read from; all collection prefixes (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexisted in it, and `chroma_tenant` was inferred from the API key except in multi-workspace setups. Retained as provenance for the collection-naming scheme, which outlived the backend.

**Single-database architecture (history).** RDR-037 (2026-03-14) consolidated the legacy four-database layout (`{base}_code` / `{base}_docs` / `{base}_rdr` / `{base}_knowledge`) into a single database with collection prefixes. The transitional auto-detect probe was retired in 4.14.2 once the migration window closed.

## LLM backend

Search and storage need no language model. The steps that do (the `nx_answer` planner and operators, `nx_tidy`, aspect extraction, topic labels, memory roll-up) go through one dispatch with three backends (nexus-2k66t.3):

| Backend | What it is | When `auto` picks it |
|---|---|---|
| `cli` | The `claude -p` subprocess: your Claude Code login and its billing. | `claude` is on PATH. |
| `anthropic` | The Messages API over HTTPS (`api.anthropic.com`), with your API key. | No `claude` on PATH and an `anthropic_api_key` resolves. |
| `openai_compat` | Chat Completions (`{base_url}/v1/chat/completions`) on Ollama, vLLM, OpenRouter or any server speaking it. | Never: set `llm.backend: openai_compat`. |

With neither a CLI nor a key, `auto` has no backend: LLM steps fail with a message naming both remedies, and search and storage keep working. `nx doctor` has an "LLM backend" row that names the backend in use and why (it never prints a key) and a warning when the key lives in shell env only, which a GUI-launched `nx-mcp` cannot see.

```bash
printf %s "$ANTHROPIC_KEY" | nx config set anthropic_api_key --stdin       # persist it where a GUI host can read it
nx config set llm.backend=anthropic                                         # optional: use the API even when claude is installed
nx config set llm.openai_compat.base_url=http://localhost:11434             # for llm.backend=openai_compat
nx config set llm.openai_compat.model=qwen2.5:14b
printf %s "$KEY" | nx config set openai_compat_api_key --stdin              # optional: local servers usually take none
```

`anthropic_api_key` resolves from `ANTHROPIC_API_KEY`, then `config.yml` (`nx config get` masks it). `openai_compat_api_key` resolves from `NX_LLM_OPENAI_API_KEY` then `config.yml`; `OPENAI_API_KEY` is never used, because it is commonly exported for something else and must not be sent to a server a config file names. A key goes only into the request header of the backend that owns it: never into `os.environ`, a log line, an error message or a `claude` child process. A key set only in `config.yml` is never injected into a `claude` child's environment, so a subscription user whose key lives there is not moved to API billing. An `ANTHROPIC_API_KEY` that is exported in the shell is different: `claude -p` honours it, as it did before these backends existed, and the children nexus starts inherit it, so on a box with both a Claude login and an exported key the CLI may bill the key. `NX_LLM_OPENAI_API_KEY` is removed from every `claude` child's environment. A malformed `config.yml` is reported by line number only (`global config.yml is unreadable at line N`); the parser's own message quotes the offending line, which can be a key, and is never logged.

**Direct-API behaviour** (`anthropic`, `openai_compat`):

- The operator's JSON schema is offered as one tool, `StructuredOutput`, and the tool input is validated locally with `jsonschema`. A schema failure gets exactly one repair turn; a second raises `OperatorOutputError`. A response cut off at `max_tokens` is not repaired.
- `tool_choice` forces the tool where the model allows it. Claude Opus 5.5, Sonnet 5.5 and Fable 5.1 reject a forced tool choice, so for them the tool is offered with `tool_choice: auto` and an instruction, and the local validation and the repair turn hold the contract. A 400 that blames `tool_choice` on any other model is retried once with `auto`.
- The CLI aliases map to API ids in `operators/model_tiers.py` (`haiku` to `claude-haiku-5-5`, `sonnet` to `claude-sonnet-5-5`, `opus` to `claude-opus-5-5`, `fable` to `claude-fable-5-1`); a full id passes through. No model means the strong default, as on the CLI path.
- `cost_usd` comes from the price table in `operators/llm_models.py`. A model that is not in it reports `cost_usd: None` and logs `llm_price_unknown`; it is never `0.0`. `max_budget_usd` (and `NX_DISPATCH_MAX_BUDGET_USD`) is a pre-flight ceiling: a call whose estimated input alone exceeds it is refused before any request, otherwise the output cap is clamped to what the remainder buys. An unpriced model with a ceiling is refused, on both backends. A model served through `openai_compat` has no table price: set `llm.openai_compat.price_in_per_mtok` and `llm.openai_compat.price_out_per_mtok` (USD per million tokens, both or neither) and it is priced and enforced like any other, with `cost_usd` computed from them; or set `llm.openai_compat.unpriced_ok: true` to accept a ceiling that cannot be enforced (a free local server), in which case a warning says so on every call. `llm_price_unknown` carries `prices_as_of`, the date the table was last read against the published list.
- A request waits for a free `NX_LLM_MAX_CONCURRENCY` slot only until the caller's deadline; a call that reaches it still queued raises a timeout error and sends nothing. A 200 whose content type is not `text/event-stream` (a server that ignores `stream: true`, a proxy's HTML page) and a 3xx (redirects are never followed) each raise their own error naming the status and are not retried.
- The API path sends no effort or thinking setting, so each model runs at its default effort and adaptive thinking. Its quality and cost can differ from the CLI path's, and the RDR-196 tier measurements are not claimed for it.
- `openai_compat` needs a server and a model that support tool calling (the schema rides one function tool); a model without it fails the call or answers in text, which is accepted only when it is JSON that satisfies the schema. Servers may truncate a prompt to the model's context window without saying so (Ollama's `num_ctx` default is the usual case); size it for the longest prompt the operators send. `max_tokens` is sent, and a server that answers it with a 400 naming `max_completion_tokens` is retried once with that spelling and remembered for the process. A key sent over `http://` to a host that is not this machine logs `llm_openai_compat_key_over_cleartext` once per host; use `https`.
- Timeouts keep the CLI's two phases (to the first stream event, then to the end), each inside the caller's deadline. A timeout keeps the partial output in the usual `operator-timeout-*.log`. 429, 529, 5xx and lost connections are retried twice with `retry-after` honoured (and not waited out past the deadline) and jittered backoff; 400, 401 and 403 are not retried.

## Settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | RETIRED (RDR-188): reranking runs server-side; the engine picks the model via `NX_RERANK_MODEL` in its environment. A set value emits a deprecation notice and is otherwise ignored |
| `llm.backend` | `NX_LLM_BACKEND` | `auto` | Which backend runs operator, extraction and labelling LLM calls (nexus-2k66t.3): `auto`, `cli`, `anthropic`, `openai_compat`. `auto` uses the `claude` CLI when it is on PATH, else the Anthropic API when an `anthropic_api_key` resolves, else no backend. `anthropic` and `openai_compat` force that backend for every tool-free call. A call that grants tools (`nx_plan_audit`, `nx_enrich_beads`) always uses `cli`. The env var wins over config. **Read from the env and the GLOBAL `~/.config/nexus/config.yml` only, never from a per-repo `.nexus.yml`**: a cloned repository cannot choose the backend, redirect prompts or change the billed account. A missing or unreadable global config means `auto`. See [LLM backend](#llm-backend). |
| `llm.openai_compat.base_url` | `NX_LLM_OPENAI_BASE_URL` | unset | Server root for the `openai_compat` backend (`http://localhost:11434`, or an OpenAI-style root ending in `/v1` such as `https://openrouter.ai/api/v1`). `http`/`https` only, no `user:password@`. Global config and env only. |
| `llm.openai_compat.price_in_per_mtok`, `llm.openai_compat.price_out_per_mtok` | — | unset | USD per million input / output tokens for the `openai_compat` model. Both or neither. Needed to enforce `max_budget_usd` and to report `cost_usd`; without them a ceiling is refused unless `unpriced_ok` is set. Global config only. |
| `llm.openai_compat.unpriced_ok` | — | `false` | `true` accepts a `max_budget_usd` ceiling on an unpriced `openai_compat` model, unenforced, with a warning per call: for a free local server. Global config only. |
| `llm.openai_compat.model` | `NX_LLM_OPENAI_MODEL` | unset | The model every `openai_compat` call uses. The Claude aliases dispatch passes (`haiku`, `opus`) mean nothing to such a server and are ignored. Global config and env only. |
| — | `NX_ANSWER_STRUCTURED_EXTRACT` | off | `1`, `true`, `yes` or `on` lets `nx_answer`'s request-coverage step (RDR-232, nexus-jypmj.2.2) make one direct, schema-closed model call when its deterministic first pass cannot settle the answer kind or the clause structure. Never recursive `nx_answer`, never retried. Its latency and cost are reported in the envelope's `request_coverage.extraction`, apart from plan-match latency. Off means no model call is made for request coverage. |
| — | `NX_ANSWER_STRUCTURED_EXTRACT_TIMEOUT` | `20` | Seconds that call may take (floor 1, ceiling 60), inside `nx_answer`'s own `budget_seconds`. A timeout leaves the first-pass result and says so in `request_coverage.extraction.outcome`. |
| — | `NX_LLM_MAX_TOKENS` | `16000` | Output-token cap of one direct-API call (`anthropic`, `openai_compat`). A response cut off at the cap fails with an error naming it. A non-positive or non-integer value fails loud. |
| — | `NX_LLM_MAX_CONCURRENCY` | `4` | How many direct-API requests one process has in flight at once (plan fan-out and taxonomy batches queue behind it). A non-positive or non-integer value fails loud. |
| — (engine env) | `NX_HNSW_EF_SEARCH` | `600` (`200` before the engine cut after v0.1.150) | Engine-side serving floor for pgvector `hnsw.ef_search` on vector-ranked paths (engine ≥ 0.1.93, nexus-4ktfm; raised for RDR-225's per-leaf graphs, nexus-3wh8d.31). Effective ef is `clamp(max(floor, n_results), 1, 1000)`. Raises recall under shared-index cross-tenant crowding; higher values cost plain-search latency. A single-collection plain-search statement with no per-collection index runs at 1000 regardless of this floor (RDR-227); the floor governs every other statement. Validated at engine boot — an out-of-range value refuses to start |
| — (engine env) | `NX_HNSW_MAX_SCAN_TUPLES` | `200000` | Engine-side `hnsw.max_scan_tuples` set (SET LOCAL) on every vector-ranked path (nexus-wbfpw.47; pgvector's own default is 20000). An iterative HNSW scan stops at this many tuples; at 98% correlated-dead chunks on a shared index the default lost recall (0.85-0.89 at recall@10), and this cap together with the memory budget below restored 1.000. Range 1000..100000000. It also raises the cost of a filtered search that cannot fill its LIMIT. Validated at engine boot; an out-of-range value refuses to start |
| — (engine env) | `NX_ORT_NATIVE_DIR` | `~/.cache/nexus/ort_native` | Engine-side (local mode) root directory where the engine extracts ONNX Runtime's native libraries once, under `<root>/<ORT version>-<os-arch>`, instead of 16 MB into a fresh temp directory on every start (nexus-8ool4); blank is unset, and the supervisor passes it through. On POSIX the root and that version directory must be owned by the user running the engine and not writable by group or others. Ownership is judged by uid (the directory's `unix:uid` against the uid the engine runs as) where the platform has one, so a container with no passwd entry for its user works; with no uid it falls back to comparing owner names. Recovering a broken version directory takes a lock file in the root for at most 10 seconds (opening and locking it both count); past that the engine warns `ort_native_dir_lock_timeout` and recovers without the lock. Only the root is tightened: a root the engine's user owns that is group- or world-writable (for example a `mkdir` under umask `0002`, the Ubuntu default, gives `rwxrwxr-x`) gets `chmod g-w,o-w` (nothing else changes), is re-checked, and is used; the engine logs a WARN `ort_native_dir_tightened` with the old and new modes. To keep a shared directory instead, point `NX_ORT_NATIVE_DIR` elsewhere or make the directory owner-writable only. A root with a setuid, setgid or sticky bit is never chmodded and is not used (`reason=special_bits`). A group- or world-writable version directory is never tightened and reused, because another user could have planted a library in it: the engine replaces it with a fresh extraction. A root someone else owns, a symbolic link, or a chmod that fails or does not take effect is not used either; each logs `ort_native_dir_untrusted` with a `reason` (`foreign_owner`, `symlink`, `unknown_user`, `special_bits`, `tighten_failed`, `tighten_ineffective`) and ONNX Runtime unpacks into the temp directory on that start, as before. A symbolic link is refused whatever it points at, for the root and for the version directory alike, even when the target is owned by the engine's user and writable by nobody else (nexus-z9jpg); a link higher up the path (macOS `/var`, a linked `~/.cache`) is not checked. Windows is unchanged: no mode check and no permission changes. Setting the JVM property `onnxruntime.native.path` yourself bypasses all of this (nexus-bk7nu) |
| — (engine env) | `NX_HNSW_SCAN_MEM_BUDGET_MB` | `16` | Fixed per-search memory budget for the same scan (nexus-wbfpw.47). The engine reads its role's effective `work_mem` at boot and sets `hnsw.scan_mem_multiplier = max(1, budget / work_mem)` (4 at the stock 4 MB `work_mem`; 1 at a managed cloud's 384 MB), and logs `work_mem`, the multiplier and the effective budget (`event=hnsw_scan_budget`). Range 1..4096. Validated at engine boot |
| — (engine env) | `NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS` | `30` | Engine-side (nexus-u9zkn). Where the engine sets a `statement_timeout` from Java, it also gives that connection a socket read timeout of the statement bound plus this many seconds, so a statement on a Postgres server that has gone silent (no RST, as in the 2026-10-05 failover) fails instead of waiting on TCP. The server cancels at the statement bound and answers; this only fires when it does not. The bound covers the bounded statements of a request, not the whole request: reads with no statement bound stay unbounded, among them the token store's reads on an auth-cache miss and session resolution, `CollectionRegistry.lookup` on a cache miss, and every path listed below as unbounded. At the defaults a search statement ends at 30 + 30 = 60 s (taxonomy assign 60 s, reaper statements 55 s, a bounded reaper census 90 s, the sweep gate 35 s, tuple subspace list 40 s, the per-collection index catalog read 60 s), which is about what that failover took anyway: the gain is a deterministic end, not a faster one; lower the margin (5 gives 35 s for a search) to notice sooner, keeping it above the time a healthy server needs to deliver its cancel. Paths with no statement bound get NO read bound, on purpose: collection re-home, quarantine, purge-trash, delete and rename collection, the taxonomy link joins, `VACUUM`, writes in general and the Liquibase migration run for minutes with the server silent, and a socket timeout closes the socket without cancelling the backend, so the write rolls back and the retry stacks a second backend behind the first. The tenant stamp that opens every borrow runs before any path sets its bound, so it gets its own read bound of the margin with a 5 s floor (a 1 s margin still gives the stamp 5 s, so a saturated but healthy server is not killed on a trivial statement), reset to none before the path runs. Server-side bounds are separate: the `gc_*` and `reaper_*` plpgsql functions set `statement_timeout` themselves (5 s or 25 s); the paths that call a 25 s function set a Java bound of 25 s, so their read bound is 25 s plus the margin and always outlasts it. `0` turns the per-path bound off. A non-integer, negative or over-`3600` value fails the service AT BOOT. `tcpKeepAlive` is also on for both pools, but its OS default of about 2 h means it is not the fix. |
| — (engine env) | `NX_SEARCH_STATEMENT_TIMEOUT_MS` | `30000` | Engine-side `statement_timeout` on every vector-ranked path (engine ≥ 0.1.96, nexus-g17tf). A scan past the bound cancels with SQLSTATE 57014 instead of surviving its container and pinning xmin. Range 1..600000; `0` (Postgres for "disabled") is refused at engine boot. Sized to the edge's 30s budget: a longer bound leaves a backend burning CPU for a client that already gave up. |
| — (engine env) | `NX_SEARCH_EXACT_MAX_ROWS` | `20000` | Engine-side cardinality router (nexus-tu8wp.6). Before each plain-search statement the engine counts, under the tenant's row-level security and bounded at this value plus one, the physical rows the selected collections hold. At or below the value the statement runs exact (index scans off, one fetch) instead of walking the shared HNSW index; above it, or at `0`, it is the HNSW path with the empty-result exact re-run. Above the threshold, or at `0`, a statement over one collection with no per-collection index walks at `hnsw.ef_search` 1000 (RDR-227); a statement over several collections keeps the request-sized value. A batched search sums its collections, a per-collection arm uses its own. `0` disables the router. Range 0..1000000; a non-integer or out-of-range value refuses to start (`event=pg_session_env_invalid`). The default (RDR-227, 2026-10-10; it was 30000) equals `NX_SEARCH_PCI_BUILD_MIN_ROWS`, so a collection leaves the exact scan at the row count where it gets its own partial HNSW index. Measured live at 20000, the 27,893- and 29,355-row code collections moved from exact arms of 0.6-0.9 s under fan-out to index arms of 25-50 ms. There is no margin between the two thresholds: a collection above this value with no valid per-collection index (just past the build threshold and before the next DDL pass, or on an engine whose builder cannot run because it has no admin credentials or privilege) walks the shared index at `hnsw.ef_search` 1000, which measured recall 0.975 at worst on a 27,893-row collection; set `30000` to keep such collections exact. A higher value keeps larger collections exact and complete, and each exact arm reads every row of its collection: at 60000 (engine-service-v0.1.155) the 45k- and 58k-row collections ran exact, five arms at a time, at 1.1-2.0 s each. Do not set it below the build threshold. Logged at boot as `event=search_exact_router`; a statement over 1 s logs `event=vector_search_statement_slow` with its route |
| — (engine env) | `NX_SEARCH_PCI` | `1` | Switch for the DDL half of per-collection partial HNSW indexes (RDR-227, nexus-43ulx.10): the engine builds and drops one index per large collection. `1` (default) builds and drops; `0` builds and drops nothing. The read half runs either way: every engine reads the catalog of existing `pci_` indexes at boot and then every `NX_SEARCH_PCI_SWEEP_SECONDS`, and a single-collection search over a collection with a valid index walks it at the serving `hnsw.ef_search`, whatever this is set to (nexus-43ulx.12). The build and drop half is the reconciler (nexus-43ulx.19): on the engine that holds the builder advisory lock it counts rows per collection, drops what no longer qualifies and builds one index at a time on a connection of its own using the `NX_DB_ADMIN_*` values. Only `1` and `0` are accepted; any other value (`true`, `yes`) refuses to start (`event=pg_session_env_invalid`). Logged at boot as `event=pci_settings` |
| — (engine env) | `NX_SEARCH_PCI_BUILD_MIN_ROWS` | `20000` | B, the row count at or above which a collection is a candidate for its own partial HNSW index (RDR-227). Integer, 1..1000000; a non-integer or out-of-range value refuses to start (`event=pg_session_env_invalid`). When the router threshold `NX_SEARCH_EXACT_MAX_ROWS` (T) is above 0 and B is above T, boot logs `event=pci_build_threshold_above_router` at WARN: a collection with more than T and fewer than B rows walks at `hnsw.ef_search` 1000 and never gets an index. The warning is skipped when nothing builds (`NX_SEARCH_PCI=0` or `NX_SEARCH_PCI_MAX_PER_LEAF=0`). Read only by the build half (the reconciler); the read half does not use it |
| — (engine env) | `NX_SEARCH_PCI_SWEEP_SECONDS` | `600` | Seconds between the engine's catalog sweeps (RDR-227). Integer, 60..3600; a non-integer or out-of-range value refuses to start (`event=pg_session_env_invalid`). The maximum was lowered from 86400 to 3600 in the same change that added the read half, because this period is also how long a peer can route on a stale answer (below). Every engine runs the read half regardless of `NX_SEARCH_PCI`: one read at boot, then one this many seconds after each read ends, on a thread of its own, into an in-memory set the search path consults without touching the database. The build and drop half (the reconciler) runs on the same period, one pass after each pass ends, and its first pass starts after a random delay of up to 60 seconds or a tenth of the period, whichever is shorter, so engines restarted together do not all count at once. **Retry and engines.** Since nexus-5tykh a second engine on one database is refused at boot, so what follows about several engines describes only a deployment that sets `NX_ENGINE_SINGLETON_LOCK=off` on them (see that row) or runs engines older than that change, which neither take nor honour the lock. A failed build is retried no sooner than 10 minutes later on the engine that ran it, doubling to 24 hours, and counts as `failing` after three in a row; that state is per engine, in memory and lost on restart. The builder lock serialises passes but is held per pass, so with more than one engine the holder alternates, and a peer with no failure entry drops the invalid index a failed build left (it must go before the name can be built again) and rebuilds it on its next pass: across N engines a failing build is retried at most once per period by whichever engine holds the lock, which is bounded but not backed off to 24 hours. A build ended because a schema migration or shutdown terminated the builder session, or the connection dropped, is not a failure and is not charged. **Staleness.** After an engine builds an index it re-reads at once, but a peer engine only sees it at its next read, so until then the peer walks that collection at `hnsw.ef_search` 1000 (latency only). After an index is dropped, a peer still lists it for up to this period plus the read bound (30 s), so it routes the collection to the serving `ef_search` with no per-collection graph behind it; that is harmful only when the drop is not a count drop (an index superseded or dropped by an operator) or when the router threshold T is below half the build threshold B, because a count drop leaves a collection under B/2 and a router that is on sends such a collection to exact. A set that has had no successful read for three periods plus the read bound expires to empty (`event=pci_sweep_set_expired` once, every statement then walks at 1000, `event=pci_sweep_set_recovered` at the next good read); a read that fails keeps the previous set until then (`event=pci_sweep_read_failed`). The catalog read is bounded: it ends at 30 s (the sweep statement bound) or, on a silent server, at 30 s plus `NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS`. `event=pci_sweep valid= invalid= unparsed= reindex_leftover=` logs each good read, at WARN when `unparsed` is above 0 and `valid` is 0 (every `pci_` index failed to parse; a changed index deparse after a PostgreSQL upgrade looks like this). **Status** (nexus-43ulx.23): `GET /v1/status` carries a `per_collection_indexes` object, like the `reaper` object (unauthenticated, additive, served from cached values with no catalog query): the last read's `valid`, `invalid`, `unparsed`, `reindex_leftover` (invalid `pci_<hash>_ccnew`/`_ccold` indexes a failed `REINDEX CONCURRENTLY` left behind, nexus-rn1yd; the reconciler drops them with reason `reindex_leftover`, after a re-read of their validity just before the drop, and they are not also in `unparsed`), `last_read_at`, `expired` (true when the set has expired to empty) and `sweep_seconds` (this setting), and under `this_engine` the DDL half of the answering engine only: `builder_state` (`ok`, `auth_failed`, `no_privilege`, `off`, `standby`; a privilege or authentication failure shows here while it lasts), `building` (0 or 1), `failing` (this engine's own count), `last_ddl_pass_at` (this engine's last completed pass that processed at least one leaf; kept when the engine is standby now, null only before its first), `pass_started_at` (when the pass in flight started, null when none) and `pass_in_progress` (a boolean, so a long first pass is visible while `last_ddl_pass_at` is still null). `building`, `failing` and `pass_started_at` are null, and `pass_in_progress` false, on an engine that does not hold the builder lock: a `standby` (a peer holds it), an `off` or `auth_failed` engine, and an engine that has not yet started its first pass, which reads `builder_state` `ok` with those fields null until the pass starts (the delay above; `nx doctor` prints "no DDL pass yet"). From the moment the first pass starts the holder fields are filled (`pass_in_progress` true, `pass_started_at` set, `building` and `failing` counts), while `last_ddl_pass_at` stays null until a pass completes having processed at least one leaf. `ok` with null holder-only fields is therefore not a standby; `builder_state` is what says which. The key is absent on an engine that predates it (or before the engine has finished starting). RDR-227 § Day 2 Operations is the full description. The same period paces a second read on its own thread, the bloat view of `nexus.chunks` behind the status object's `chunks_leaf_bloat` key (nexus-dxsjc; see `docs/architecture.md`, "Bloat per leaf"), which carries aggregates only because the status route is unauthenticated (Sam's ruling, 2026-10-19; the engine log names the leaves, `event=chunks_leaf_bloat_leaf`); it has its own cache, so a failing bloat read never expires the router's set. The period does not set how long a leaf may stay overdue before the doctor warns: that is a fixed 30 minutes, which the status object states as `overdue_warn_after_seconds` |
| — (engine env) | `NX_SEARCH_PCI_MAX_PER_LEAF` | `16` | Cap on partial HNSW indexes per leaf table (RDR-227). Integer, 0..1000; `0` builds none. A non-integer or out-of-range value refuses to start (`event=pg_session_env_invalid`). Read only by the build half (the reconciler); the read half does not use it |
| — (engine env) | `NX_SEARCH_FANOUT_CONCURRENCY` | `max(1, NX_POOL_SIZE / 2)` | Engine-side (nexus-tu8wp.1). How many collections ONE `POST /v1/vectors/search-per-collection` request searches at once; each is its own statement and holds a pooled connection while it runs. An override is clamped to `NX_POOL_SIZE`. This is a per-request figure: several requests at once are bounded by `NX_SEARCH_FANOUT_ARM_PERMITS` below, not by this. A blank value takes the default; a malformed or non-positive one takes the default and logs `search_fanout_setting_invalid`. |
| — (engine env) | `NX_SEARCH_FANOUT_ARM_PERMITS` | `max(1, NX_POOL_SIZE / 2)` | Engine-side (nexus-tu8wp.1). How many fan-out statements may be in flight across ALL `search-per-collection` requests on one pool. An arm takes a permit before it asks for an admission permit or a connection, so an arm that is waiting holds neither, and at least half the pool stays free for `/health`, writes and plain search however many fan-outs run at once. Clamped to `NX_POOL_SIZE`; a malformed or non-positive value takes the default. A request can still queue behind other requests' arms for a permit, but only for as long as the fan-out budget below allows. |
| — (engine env) | `NX_SEARCH_FANOUT_BUDGET_MS` | `20000` | Engine-side (nexus-tu8wp.1). The wall budget of one `search-per-collection` fan-out, counted from the request's arrival at the repository (so the query embed counts), kept under the public edge's 30 s upstream bound with the same 10 s margin as the nexus-99r7y budget. Collections whose search has not started when it is spent are returned in `per_collection[]` with `error_kind: fanout_budget_exhausted` instead of being run; a statement still running is bounded by what is left and, if it is cancelled by that bound, is reported the same way. Range 1..600000; a malformed or out-of-range value takes the default and logs `search_fanout_setting_invalid`. |
| — | `NX_SEARCH_PER_COLLECTION` | unset (route on) | Client-side kill switch for the per-collection search route (nexus-tu8wp.2). `0`, `false`, `off` or `no` (any case) makes every search take the batched `POST /v1/vectors/search` path instead of `POST /v1/vectors/search-per-collection`; any other value, or unset, leaves the route on. Read on every search, so a flip takes effect on the next one. Without it a defect in the route would need a client release once the pinned engine carries it. An engine or edge that does not serve the route already falls back by itself (remembered 10 minutes per process; a 500 for 60 s), so this switch is for the case where the route answers but answers wrongly. |
| — | `NX_SEARCH_ROUTE_LEXICAL` | unset (off) | Sends `lexical: true` on the per-collection route for a `--lexical` search, so the engine runs each collection's lexical arm in the same request (nexus-xd2rc). Off by default (nexus-n51v1): on engine-service-v0.1.163 those arms made a `--lexical` search over 118 collections take 11.99 s against 6.90 s for the batched `/hybrid-search` leg the client runs without the field. `1`, `true`, `on` or `yes` (any case) turns it on; read on every search. |
| — | `NX_MCP_SEARCH_WARMUP` | unset (warm-up on) | Client-side (nexus-vpa9q). At start the MCP server warms a first search's cold steps on a daemon thread: the cloud version probe, two routing listings, an empty link-pairs read and an absent-id catalog lookup. It never fetches the full stats listing. `0` turns it off; the unit suite sets that by default. |
| — (engine env) | `NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS` | `30000` | Engine-side `statement_timeout` on the taxonomy assign transaction (`POST /v1/taxonomy/assignments/assign_from_chashes`, nexus-r0vkh). Sized to the client's own 30s per-request timeout; a call past it cancels with SQLSTATE 57014, the client records that batch on its taxonomy tripwire, and the index write it followed is unaffected. Range 1..600000; `0` refused at engine boot. |
| — (engine env) | `NX_OWNERLESS_WRITE_MODE` | unset = `log-only` | Engine-side (RDR-223 Phase 3 Step 2, nexus-z0o2p.24). What `POST /v1/vectors/upsert-chunks` does with a chash that has no live manifest row in the collection. Accepted values: `enforce` (refuse the whole request with 422, `reason: ownerless_chunk_write`, naming `/v1/catalog/manifest/write_many` and `/append`) and `log-only` (write as before, log `ownerless_chunk_write_would_refuse` once per route, tenant and collection per minute with the request's `User-Agent` and `X-Nexus-Client-Version`, and count it). **Unset or blank means `log-only`; only an explicit `enforce` enforces.** Any other value refuses to start the engine. The local engine launch (`nx daemon service start`) sets `enforce` itself unless the variable already carries a non-blank value: **at the local launcher a blank value counts as unset (enforce), while the raw engine parses blank as log-only**, so an empty `NX_OWNERLESS_WRITE_MODE=` still enforces on a local install and does not on an engine you start by hand. The log line is a SAMPLE of writers, never a complete list: one line per route, tenant and collection per minute, naming the first unowned chunk only; the key table holds 10,000 live keys, past that new keys share ONE overflow bucket (one line a minute, its `suppressed_since_last` pooled across keys, so a quiet writer can go unlogged for a window), and under key churn a minute can carry up to 10,001 lines. The counters are the complete signal: decide the flip to `enforce` from the delta of `ownerless_writes_would_refuse_total` over a window as long as the longest writer cadence (nexus-z0o2p.40), not from the log. In `log-only`, a request whose chashes the pre-embed check already reported counts once, and the in-transaction recheck is skipped for it: a chash in the same request that loses its owner during the embed is not counted again. The log line carries tenant content: the first unowned chunk's `source_path`, `title` and `source_agent` (a URL value loses its userinfo, query and fragment), the collection name, `User-Agent` and `X-Nexus-Client-Version`, each cleaned of control characters and quoted or delimited so it cannot forge a field; never `source_uri` or chunk text. Its retention is the deployment's log retention. `GET /v1/status` reports `ownerless_write_mode`, `ownerless_writes_refused_total` and `ownerless_writes_would_refuse_total`. |
| — (engine env) | `NX_PG_DATA_DIR` | unset = no disk check | Engine-side (RDR-225 P1.4, nexus-3wh8d.9). The Postgres data directory, when Postgres shares the engine's filesystem. While the `vectors-030-1` migration is pending, the engine refuses to start unless that filesystem has at least 2.2 times the size of `nexus.chunks` plus `nexus.taxonomy_centroids` (indexes and TOAST included) free, and the message names the required bytes, the free bytes and the shortfall. The 2.2 factor is an upper bound: one populated walk (100k 768-d chunks, WAL at its `max_wal_size` steady state) grew the filesystem 1.08 times the tables at peak (nexus-s7cyy). The local launch (`nx daemon service start`) sets it from the bundled cluster's `PG_DATA`. Unset, or naming a path the engine cannot see (managed or remote Postgres), the check is skipped and logged as `disk_preflight_skipped`. Once the migration is applied the check never runs. A second check, on every deployment, refuses to start while that migration is pending when its estimated relation locks (100 + 80 per model + 19 per leaf partition, leaves = models x tenants x 2) exceed the server's lock table (`max_locks_per_transaction` x (MaxBackends + `max_prepared_transactions`), MaxBackends being `max_connections` plus the autovacuum, background-worker and WAL-sender slots, the table Postgres guarantees (nexus-xzit5)); the message names the `max_locks_per_transaction` to set and, with this variable set, the cluster's `postgresql.conf` (nexus-vxmcl). At default settings and four models that is about 48 tenants; the local cluster runs with `max_locks_per_transaction = 128` (about 99 tenants), written into its managed `postgresql.conf` block unless the file already sets it (nexus-o26hs). |
| — (engine env) | `NX_ENGINE_LOCAL` | unset = off | Engine-side (RDR-210 Phase 6 Step 2, nexus-f98bg). The value `1` (surrounding whitespace is ignored, as for `NX_TEST_TENANT_RESET_ROUTE`) starts the cross-model re-embed job runner, a single thread (`reembed-runner`) that picks up planned, running and cutover jobs five seconds after boot and every 30 seconds after that, and gives each ready job a turn of at most ten batches in rotation. It also gates the job control routes on `/v1/vectors/reembed` (`POST /jobs`, `POST /jobs/{id}/pause`, `/resume`, `/abort`), which are registered on every engine and answer 503 naming local mode when the variable is off (or the engine has no embedder). `nx daemon service start` sets it on the engine it spawns, forcing it over any inherited value, and is the only launcher in this repository that does: an engine started any other way has no runner unless its environment carries the value. Any other value, or unset, leaves the runner off. |
| — (engine env) | `NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS` | `5000` | Engine-side `lock_timeout` on the same transaction (nexus-r0vkh). Concurrent assign calls queue on `nexus.topics` row locks behind the first; a queued call now fails with SQLSTATE 55P03 at this bound and returns its pool connection instead of holding it for the head's lifetime (2026-09-16: nine of ten pool connections sat 11 minutes behind one 782s call). Range 1..600000; `0` refused at engine boot. |
| — (engine env) | `NX_EMBED_PROFILE` | unset = keyed means `voyage`, keyless means `bge` | Engine-side (RDR-210 Phase 2, nexus-qhclj). The embedding family new collections get: `voyage` seeds `voyage-code-3` for `code` and `voyage-context-3` for the other content types, `bge` seeds `bge-base-en-v15-768` for every content type. The seeded tokens follow the profile, not which embedders were built. Exactly `voyage` or `bge` after trimming (case is not folded); an unset or blank value keeps the default rule, and any other value stops the boot (`event=embed_profile_invalid`, naming the allowed values). A profile the boot cannot serve still boots, degraded: a `voyage` profile with no key, or a `bge` profile with no bge files, logs `embed_profile_degraded` naming the missing piece, the family it can serve keeps serving, and the other family's collections answer 422 naming the key or `nx init`. The profile also picks the reranker: `voyage` with a key reranks with Voyage, every other combination with the local cross-encoder, so a `bge` profile sends no rerank text to Voyage even on a keyed box. The boot logs `event=embed_profile_resolved profile= source=`. The local supervisor always sets it, from the function the client's write model reads (`voyage` when `local.embed_model` names a Voyage model, or records none and `config.yml` persists a Voyage key; `bge` otherwise), and overrides a value in your own environment (nexus-wypwq, RDR-210 Phase 3 Step 1); it is yours to set by hand only for an engine the supervisor does not start. `GET /version` and `GET /v1/status` report `embedding_profile`, `reranker` and `embedding_degraded` |
| — (engine env) | `NX_REQUEST_LEDGER_RETENTION_DAYS` | `7` | Engine-side (RDR-222 Phase 1, nexus-yye8l.3). Days the request ledger (`nexus.request_ledger`, which backs `Idempotency-Key` on the bounded sweep routes and `GET /v1/requests/<id>`) keeps a finished row. A retention arm runs every six hours, per tenant, and deletes committed and failed rows that finished before now minus this many days, and `in_flight` rows whose lease expired before then; a live lease is never deleted, and one tenant's failure does not stop the next. A client that has not seen the answer to a keyed sweep can ask `GET /v1/requests/<key>` only while the row exists, so this is also how long a lost response stays recoverable. Integer, 1..3650; unset, blank, non-integer or out-of-range falls back to `7` with one `request_ledger_retention_invalid` warning (it does not refuse to start). Logged per tenant as `event=request_ledger_retention deleted=` |
| — (engine env) | `NX_ENGINE_SINGLETON_LOCK` | `on` | Engine-side (nexus-5tykh). One engine JVM per database, enforced: at boot, before the schema migration, the engine takes a session-level PostgreSQL advisory lock (key 6552453789481138892, the first eight bytes of SHA-256 of `nexus.engine.singleton-lock.v1`) on a connection of its own, held for the JVM's life and named `<pool application_name>/singleton-lock` in `pg_stat_activity`. A second engine on the same database waits `NX_ENGINE_SINGLETON_LOCK_WAIT_SECONDS`, then logs `event=engine_singleton_lock_refused` at ERROR, naming the holder (`holder_pid`, `holder_client_addr`, `holder_application_name`, `holder_backend_start`; never a credential), and exits 1. The line ends with the remedy: stop the other engine (on a local install `nx daemon service stop`), or for a half-open connection left by a stopped engine whose host is gone, `pg_terminate_backend(<holder_pid>)`. Right after taking the lock the engine checks that it shows as held by its own connection's backend and that the backend does not change between two statements; a database URL that goes through a transaction-mode pooler fails that, and the boot logs `event=engine_singleton_lock_pooler_suspected` at ERROR and exits 1, naming `NX_PGBOUNCER_ADMIN_URL` and `NX_ENGINE_SINGLETON_LOCK=off` as the ways out. `CollectionRegistry` caches collection rows with no TTL and is correct only with one engine per database, which is why this exists. `off` skips the lock and logs `event=engine_singleton_lock_disabled` at WARN: for an operator who knowingly runs behind a transaction-mode pooler (a session lock does not hold through one). When `NX_PGBOUNCER_ADMIN_URL` is set the lock is skipped the same way (`event=engine_singleton_lock_skipped`, WARN). Any other value refuses to start (`event=engine_singleton_lock_env_invalid`). The managed cloud connects straight to Postgres, so it needs neither. Advisory locks are per database: engines on different databases of one cluster do not meet. An engine older than this one does not take the lock, so it is not protected against and does not protect. **Loss is detected, not prevented:** an engine that lost the lock to a Postgres restart or a network drop learns of it at its next check, so two engines can serve for up to `NX_ENGINE_SINGLETON_LOCK_CHECK_SECONDS` plus the connection timeouts (10 s to connect, 30 s per statement). The lock connection is named `<pool application_name>/singleton-lock`; the release part of the pool name is cut to 25 bytes so the whole name fits Postgres' 63. |
| — (engine env) | `NX_ENGINE_SINGLETON_LOCK_WAIT_SECONDS` | `30` | Engine-side (nexus-5tykh). How long boot waits for the singleton lock before refusing, so a stop-then-start whose old process is still shutting down does not fail the deploy. Integer, 0..600; `0` refuses at once. A non-integer or out-of-range value refuses to start (`event=engine_singleton_lock_env_invalid`). While it waits the engine logs `event=engine_singleton_lock_waiting` at once and every 10 s after, each line naming the holder. **Under the local supervisor:** its readiness deadline for the pre-migration phase is 60 s since the last progress, and any new engine log line counts as progress (`src/nexus/daemon/readiness.py`), so those lines keep the supervisor from killing a waiting engine however long the wait is. `nx daemon service start` and `nx init` stop waiting for the supervisor's lease at 60 s (`ensure_storage_supervisor`), so a wait above about 50 s makes that command report a timeout while the supervisor keeps going. The range stays 0..600 for the cloud, where deploy tooling sets its own patience; keep the default 30 on a local install. |
| — (engine env) | `NX_ENGINE_SINGLETON_LOCK_CHECK_SECONDS` | `10` | Engine-side (nexus-5tykh). How often the engine checks that its lock connection still holds the lock, on a daemon thread of its own. Integer, 1..60 (the maximum bounds the window in which two engines can serve). A lost lock (a Postgres restart, a network drop) is taken back once per check: success logs `event=engine_singleton_lock_reacquired` and clears the `CollectionRegistry` cache, because another engine may have run in the gap; a stale backend of this same boot still holding it is terminated first; an unreachable database defers to the next check; another engine holding it, positively identified by pid and a name that is not this boot's own and still holding it on a second try, logs `event=engine_singleton_lock_lost_to_another_engine` at ERROR and exits 1 rather than serve on; a holder that cannot be identified is no verdict and the next check tries again. Each check reads `pg_locks`, a snapshot of the whole lock table: measured 0.6 ms with the table idle, 2 ms with 6,000 locks held (the default table is full at 6,400), 33 ms with 100,000 (the RDR-225 walk holds one lock per leaf until it commits). |
| — (engine env) | `NX_TUPLE_TIMEOUT_CAP_SECONDS` | `25` | Engine-side (tuple space). The longest park the engine accepts on `rd`, `in` and `wait` (`tuple_rd`, `tuple_in`, `nx tuple rd`/`in` and the channel waiter go through them). A `timeout_s` above it is refused with `TimeoutTooLong` (HTTP 400), not clamped, so a wait longer than the cap is a loop of parks; a negative one is a `SchemaViolation`. The client sets its own HTTP timeout five seconds above each call's `timeout_s`, so this cap fires first. An integer; blank counts as unset. See [Tuple Space](tuple-space.md) |
| `install.mode` | — | (stamped by `nx init`) | Explicit mode record: `local` or `managed`. Written at init/onboarding; `is_local_mode()` reads it ahead of artifact inference (a configured `service_url` still wins over a stale `local` record, loudly) |
| `client.host` | `NX_CLIENT_HOST` | `localhost` | Legacy ChromaDB host override; no-op as of 6.0 (the managed/local service URL is `NX_SERVICE_URL`). |
| — | `NX_UPGRADE_AUTO_DEADLINE_SECONDS` | `900` | Time limit, in seconds of elapsed process-awake time, after which the detached `nx upgrade --auto` child (started by the SessionStart hook) stops starting new work (nexus-dcbof). It stops at safe points only: between upgrade-ladder rungs, and inside the RDR-192 rung between census calls and between backfill collections (a collection's backfill is finished or not started). The stopped rung is reported deferred, `upgrade_auto_deadline_reached` or `rdr192_backfill_deadline_reached` goes to `<config>/logs/upgrade-auto-child.log`, the command exits 0, and the next session's `--auto` run resumes (backfill progress persists in the database, and the census resumes from the collections already read; `nx doctor` shows `STARVED` after 3 cut sessions in a row that read nothing new and backfilled nothing, and names a collection whose census call keeps failing). **Not bounded:** (a) a census call already in flight (up to its own HTTP timeout) and (b) one collection's entire backfill once it has started (many calls in a per-document loop; on a large collection it can run well past the limit), the finish pass the root command group runs before the upgrade starts, and the service stop and start after the ladder (about 60 s each); the clock starts when `upgrade()` starts. Minimum `60`: a number below that, zero, negative, `nan` or text falls back to `900` with an `upgrade_auto_deadline_env_invalid` warning; blank is unset. An interactive `nx upgrade` ignores it |
| `pdf.extractor` | — | `auto` | PDF extraction backend: `auto`, `docling`, or `mineru`. Set globally with `nx config set pdf.extractor=mineru` |
| `pdf.mineru_server_url` | — | `http://127.0.0.1:8010` | MinerU API server URL. Auto-updated when `nx mineru start` binds a port |
| `pdf.mineru_table_enable` | — | `true` | Table extraction in MinerU. Off, every table becomes an image reference and its values are not indexed; the chunk then carries a `[Table N not extracted as text; values not indexed]` marker. Set `false` only for corpora with no tables where the table models' memory matters |
| `pdf.mineru_page_batch` | — | `1` | Pages per MinerU request. Increase for faster throughput at the cost of memory |
| `voyageai.read_timeout_seconds` | `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` | `120` | Request timeout (seconds) for Voyage AI API calls. Increase for large PDF indexing |
| `search.hybrid_default` | — | `false` | Default hybrid-scoring mode for `nx search`: blends git frecency into the score for code corpora (0.7*vector + 0.3*frecency). Set `true` to always blend |
| `search.hnsw_ef` | — | `256` | Legacy client-side HNSW tuning key, inert in production. Every current install (local or cloud) serves T3 through the nexus-service over pgvector, which tunes HNSW server-side — that knob is `NX_HNSW_EF_SEARCH` above, not this key. This value only still applies to a Chroma-backed test double; there is no live SPANN or ChromaDB Cloud path left to be "ignored in" |
| `search.rerank_max_candidates` | — | unset | How many candidates the engine's rerank scores, per request. Unset: `max(3 * n, 60)` for a search that returns `n` results, in vector order, in LOCAL mode, and every candidate in cloud mode; the rest stay in the result, unscored, behind the scored rows. A positive integer fixes the count in either mode; `0` scores every candidate (the behaviour before the cap). A negative, non-integer or boolean value is logged and the mode's default applies. The `--lexical` leg is never capped, nor is a search whose caller filters after retrieval (`--path`, `--max-file-chunks`). The cap is per request: a search the client splits or batches into several requests scores up to the cap in each. Scoring cost is per candidate, so this is the knob for local-mode search latency (the local cross-encoder spent 3.5 to 4.9 s scoring 135 candidates). Needs an engine that carries the field; an older engine ignores it |
| `search.distance_threshold.code` | — | `0.45` | Maximum distance for code corpus results. Results above this are filtered as noise |
| `search.distance_threshold.knowledge` | — | `0.65` | Maximum distance for knowledge corpus results |
| `search.distance_threshold.docs` | — | `0.65` | Maximum distance for docs corpus results |
| `search.distance_threshold.rdr` | — | `0.65` | Maximum distance for RDR corpus results |
| `search.distance_threshold.default` | — | `0.55` | Maximum distance for unknown corpus types |
| `search.cluster_by` | — | `null` | Set to `semantic` to group search results by Ward hierarchical clustering. Disabled by default |
| `search.contradiction_check` | — | `false` | JIT contradiction detection (RDR-057), opt-in. Flags result pairs with high similarity but different `source_agent` provenance. Adds `[CONTRADICTS ANOTHER RESULT]` to search output. Set to `true` to enable. The check runs in the MCP `search` tool over the page it renders, not in `nx search` or `query`. It fetches embeddings only for rows on that page that carry a `source_agent` while another row of the same collection on the page carries a different one: one round trip per such collection, none when no collection on the page has a mixed-agent pair |

Embedding models are selected automatically (see [Storage Tiers](storage-tiers.md)). In the Voyage posture (cloud mode, or a local service given a Voyage key) that is `voyage-code-3` for code and `voyage-context-3` (CCE) for docs/rdr/knowledge. In the local default every content type uses bge-768 (`NX_EMBED_PROFILE` below). All collections use the same model for both index and query.

**Distance thresholds** filter noise from search results automatically. Thresholds are calibrated for Voyage AI embeddings and only apply in cloud mode. Override per corpus in `.nexus.yml`:

```yaml
search:
  distance_threshold:
    knowledge: 0.60   # tighter threshold for knowledge collections
```

## Per-Repo Overrides (.nexus.yml)

Place `.nexus.yml` at repo root. It is gitignored by default.

```yaml
indexing:
  code_extensions: [".proto", ".thrift"]    # added to the built-in code set (default: [])
  prose_extensions: [".txt.j2", ".md.tmpl"] # forced to prose, wins over code (default: [])
  rdr_paths: ["docs/rdr", "decisions"]      # directories indexed into rdr__ collection (default: ["docs/rdr"])
  include_untracked: true                   # also index untracked (but not .gitignored) files (default: false)
```

```yaml
pdf:
  extractor: mineru             # auto | docling | mineru (default: auto)
  mineru_server_url: http://127.0.0.1:8010  # MinerU API endpoint (default)
  mineru_table_enable: true     # table extraction (default: true; off = tables indexed as markers only)
  mineru_page_batch: 1          # pages per MinerU request (default: 1)
```

Or set via CLI: `nx config set pdf.extractor=mineru` (writes to global config). See [PDF Extraction Backends](cli-reference.md#pdf-extraction-backends) for details.

Merge behavior: nested dict keys are **additive** (both global and per-repo keys are retained). Scalar values and lists are **replacement** (the per-repo value wins entirely between config levels). However, `code_extensions` is additive to the **built-in** extension set — it extends the defaults, it does not replace them. `prose_extensions` wins over everything: if an extension appears in both lists, it is classified as prose. See [Repo Indexing](repo-indexing.md) for the full extension list and override semantics.

## Taxonomy

Topic taxonomy settings. Topics are auto-discovered after `nx index repo`.

```yaml
taxonomy:
  auto_label: true                       # Generate labels through the LLM backend (default); needs the claude CLI or an anthropic_api_key
  local_exclude_collections: ["code__*"] # Skip code collections in local mode (general-purpose local embeddings are poor for code)
  collection_prefixes: [docs, code, knowledge, rdr]  # Prefixes recognized by nx taxonomy validate-refs (RDR-081)
```

| Key | Default | Description |
|-----|---------|-------------|
| `auto_label` | `true` | Auto-label topics after discover through the LLM backend (the `claude` CLI, an `anthropic_api_key`, or `openai_compat`). Set `false` to keep c-TF-IDF labels. |
| `local_exclude_collections` | `["code__*"]` | Glob patterns for collections to skip in local mode. Cloud mode (Voyage embeddings) ignores this — set to `[]` to enable all collections locally. |
| `collection_prefixes` | `["docs", "code", "knowledge", "rdr"]` | Prefix whitelist for `nx taxonomy validate-refs`. Extend this when your project adds a new user-facing collection prefix (e.g. `"custom"`). Internal-prefix collections (`taxonomy__*`, `plans__*`) are implementation-fixed and intentionally excluded. |

## Aspects

Which `docs__` collections get aspect extraction (nexus-kk4ut). `knowledge__` and `rdr__` collections are always extracted; `docs__` collections are extracted only when opted in, because every document costs an LLM call each time it changes.

```yaml
aspects:
  docs_collections: ["docs__1-29__*"]    # glob patterns; default [] (none)
```

| Key | Default | Description |
|-----|---------|-------------|
| `docs_collections` | `[]` | Glob patterns naming the `docs__` collections to extract. A matching collection's prose files (`.md`, `.markdown`, `.mdx`, `.rst`, `.adoc`, `.asciidoc`, `.org`, `.txt`) get `general-prose-v1` (summary, key decisions, entities, open questions); its other files (fixtures, word lists, graphs) are skipped. Also accepts a comma-separated string, so `nx config set aspects.docs_collections "docs__1-29__*,docs__1-41__*"` works. Documents already indexed are not queued retroactively: run `nx enrich aspects <collection>` after opting in. Opting a collection back OUT stops new extraction but does not delete aspect rows already written; `nx enrich delete <collection> <source_path>` removes one row, and `nx enrich delete <collection> --all` (nexus-3foc9) removes every row in the collection (dry-run by default; pass `--no-dry-run --yes` to actually delete). |

**Two homes for the same decision (nexus-l46pu, follow-up to nexus-kk4ut).** `docs_collections` above is per MACHINE — this config file — while `document_aspects` rows and the aspect queue are tenant-wide in the engine, so two machines indexing the same shared `docs__` collection with different local config used to give partial, machine-dependent coverage. `nx collection aspects <name> --enable`/`--disable` (see [`docs/cli-reference.md`](cli-reference.md#nx-collection)) sets the SAME opt-in on the engine's `catalog_collections.aspects_enabled` row instead — tenant-wide, read by every machine indexing the collection.

**The engine is AUTHORITATIVE** (round-2 critic decision, T2 `critique-nexus-l46pu-tenant-wide-aspects-enabled` item 1): the moment a collection's engine attribute carries an opinion — set explicitly `true` or `false` by `--enable`/`--disable`/`--from-config` — every machine reading it agrees, and the local `docs_collections` list is NOT consulted for that collection any more, even if it still names a matching pattern. Keeping "local wins" would have kept the exact cross-machine drift this bead exists to close: a stale local entry on one machine could silently re-override a value another machine had already synced to the engine. The local list is consulted ONLY as a fallback for a collection the engine has no opinion on at all — the column (`aspects_enabled BOOLEAN NULL`, catalog-040) is `NULL` on every row until an operator explicitly runs one of the three write verbs above, so "no opinion" covers a fresh install, an engine older than catalog-040 that never sends the key at all, AND an ordinary, otherwise-untouched row on a perfectly current engine that nobody has ever synced — the same fallback in every case. This is a deliberate round-2 fix (T2 `critique-nexus-l46pu-round2-2026-09-27` Finding A): an earlier cut of this column defaulted new AND existing rows to a plain `false`, which is indistinguishable from an explicit `--disable` and would have silently overridden every machine's local opt-in the moment a tenant's engine crossed the migration — `NULL` keeps "nobody has set this" a distinct, third value on the wire.

`nx collection aspects --from-config [--dry-run]` is the migration path off the local-only list: it enables the engine attribute for every registered `docs__` collection this machine's `docs_collections` already matches, prints what it changed, and never disables anything (the local list is opt-in only, so there is nothing in it that means "turn off"). `nx doctor`'s default sweep includes a `docs-aspects-config` row that warns when a local match has not been synced this way, naming the remedy; it is not-applicable on a machine with no `docs_collections` entries. Prefer the engine attribute for anything shared across machines — run `--from-config` once you have one — and keep the local list only for a genuinely local, one-off experiment on a collection nobody else indexes.

Every write path — `--enable`/`--disable`, and `--from-config` when it would actually change something — is a TENANT-WIDE setting: every machine indexing the collection sees it, and enabling it means an LLM call per changed prose document from then on. Each prompts for confirmation before writing (`--yes`/`-y` skips the prompt, for scripts); `--dry-run` never prompts, since it never writes. A non-interactive run without `--yes` refuses rather than hanging on a prompt nothing will answer.

## Daemon environment variables

T2 and T3 both route through the single native `nexus-service` (`nx daemon service`, RDR-152/RDR-155), discovered via `~/.config/nexus/storage_service_addr.<uid>` and overridden with `NX_SERVICE_URL` (see [Managed-Cloud Credentials](#managed-cloud-credentials)). This unified the earlier RDR-120 (conexus 4.34.0) split, where the CLI and MCP server routed T2 through a separate T2 daemon publishing `~/.config/nexus/t2_addr.<uid>`; that daemon and discovery file are retired (RDR-158 — see the `NX_T2_ADDR`/`NX_T2_SOCK` note below). Clients honour these env-var overrides:

| Variable | Effect | Default |
|----------|--------|---------|
| `NX_SERVICE_URL` | Full base URL of the nexus-service (T3 vectors over `/v1/vectors`). Used by dev containers and managed-cloud clients. | `storage_service_addr.<uid>` lease, then `https://api.conexus-nexus.com` |
| `NX_SERVICE_TOKEN` | Bearer token for the nexus-service. | local: from `pg_credentials`; managed: user-supplied |
| `NX_STORAGE_BACKEND` | Storage-backend env guard (RDR-152/158). `service` (the default and only backend) routes T2 stores + T3 vectors through the Java/Postgres nexus-service. `sqlite` is RETIRED (RDR-158 P3): setting it is a hard error carrying the stranded-install redirect — the SQLite stores were deleted; to migrate old local data, install the last migration-capable 6.x release, run `nx upgrade` there against a LOCAL engine (stop any running local service, clear `NX_SERVICE_URL` and the `service_url` key in `config.yml`, `export NX_LOCAL=1` (needed in addition; it does not by itself override `service_url`), keep `NX_VOYAGE_API_KEY` set for Voyage-embedded data; see [Migration Runbook § Installs that predate Postgres](migration-runbook.md#installs-that-predate-postgres)), then upgrade back. | `service` |
| `NX_STORAGE_BACKEND_<STORE>` | Per-store override of `NX_STORAGE_BACKEND`, taking precedence over the global value. Known `<STORE>` suffixes: `T1`, `CATALOG`, `VECTORS`, `TAXONOMY`, `ASPECT_QUEUE` (e.g. `NX_STORAGE_BACKEND_VECTORS=service`). `service` is the only accepted value; `=sqlite` hard-errors (RDR-158 P3). | inherits `NX_STORAGE_BACKEND` |
| `NX_LOCAL` | Force local mode (local nexus-service, bge-768 by default, or Voyage when `NX_VOYAGE_API_KEY` reaches the service — nexus-umm29) even when cloud credentials exist. | unset (cloud mode if credentials present) |

> Note: the retired `nx daemon t3` ChromaDB path and its `NX_T3_ADDR` override no longer route T3 serving; T3 traffic goes to the nexus-service via `NX_SERVICE_URL`.

> Note: `NX_T2_ADDR` and `NX_T2_SOCK` are **gone**, not deprecated — nothing
> reads them. They addressed the SQLite T2 daemon, which is retired along with
> its discovery file (`t2_addr.<uid>`) and the whole `nx daemon t2` verb group.
> T2 is served by the nexus-service, so a dev container reaches it through
> `NX_SERVICE_URL` like every other tier. See
> [Container Integration](container-integration.md) for the transport matrix.

## Storage Service (Postgres) Prerequisites

> **Local installs: skip this section.** `nx init` provisions the
> bundled relocatable Postgres + pgvector cluster, creates the roles, and enables
> the extensions for you — no DBA, no manual `CREATE EXTENSION`. The prerequisites
> below apply only when you bring your **own** Postgres (an operator pointing the
> service at a managed/self-hosted cluster via `NX_DB_*`).

The Java storage service (RDR-152/RDR-155) applies its Liquibase changelog at
startup using the `NX_DB_ADMIN_*` credentials (falling back to `NX_DB_*` in
single-role development setups). When you bring your own Postgres, two
prerequisites must be satisfied by a DBA (superuser) before the first migration
run:

1. **Extensions.** `CREATE EXTENSION IF NOT EXISTS vector;` and
   `CREATE EXTENSION IF NOT EXISTS pg_trgm;` — neither is a trusted extension,
   and the migration role (`nexus_admin`) is NOSUPERUSER, so changeset
   `vectors-001-1` fails without this pre-step. Once the extensions exist the
   changeset is an idempotent no-op.
2. **Roles.** Create `nexus_admin` (schema owner, NOSUPERUSER) and `nexus_svc`
   (NOSUPERUSER NOBYPASSRLS NOINHERIT LOGIN data role). `NOINHERIT` is
   REQUIRED, not optional decoration — a bring-your-own-Postgres cluster has
   no `bootstrap_superuser` to self-heal it later (unlike the local bundle's
   `_backfill_svc_noinherit`), so a DBA who leaves it off here bakes in the
   INHERIT-default divergence nexus-v80f2 fixed everywhere else; see the
   NOINHERIT discussion below for why it matters. The changelog's grant
   changesets give `nexus_svc` its DML rights automatically during the first
   run.
   Optionally create `nexus_diag` (NOSUPERUSER NOCREATEDB NOCREATEROLE
   BYPASSRLS LOGIN) — the client-side diagnostic role for the pre-upgrade
   chash-poison probe (`nx doctor`, the `install-binary` gate); without it
   that check degrades to a loud WARN, never a false clean. Note the client
   probe is **local-only by design** (nexus-y3wuu): it reaches only a local
   Postgres via the local `pg_credentials` file, so on a remote/managed
   store the role serves server-side diagnostics run with your own
   credentials.
   Also run `GRANT pg_monitor TO nexus_admin WITH ADMIN OPTION;` (nexus-hzhgl,
   RDR-191 Phase 3/4 pre-flight) — PostgreSQL only lets a role grant
   membership in another role it already holds WITH ADMIN OPTION (or as
   superuser), and `nexus_admin` is neither by default, so without this
   one-time superuser step the changelog's `grants-004-monitor-wal-visibility`
   changeset (which grants `pg_monitor` onward to `nexus_svc` for WAL-
   retention visibility — `pg_ls_waldir()` / `pg_stat_*`, **not** filesystem
   free space) fails loud on every migration run.

   **The grant alone does not necessarily make the privilege usable**
   (nexus-bb5c8) — it depends on `nexus_svc`'s INHERIT attribute.
   `NOINHERIT` is the posture in **every** mode (nexus-v80f2, 2026-08-15):
   the managed conexus cloud deployment's `nexus_svc` is `NOINHERIT`
   (measured live) — a deliberate posture, not an oversight, so that its
   OTHER role memberships never become ambient on every connection — and
   local `nx init` provisioning now creates `nexus_svc` `NOINHERIT` too
   (`src/nexus/db/pg_provision.py`'s `_create_roles`, converged on an
   already-provisioned install via `_backfill_svc_noinherit`), matching
   `role-001-nexus-svc.xml`'s fallback bootstrap, which always has. A
   bring-your-own-Postgres deployment that provisions `nexus_svc`
   `NOINHERIT` per this section gets the same behavior: a plain session
   gets `permission denied` from `pg_ls_waldir()` even after this grant,
   until it issues `SET ROLE pg_monitor` first (and, optionally,
   `RESET ROLE` after) — the same PostgreSQL behavior any NOINHERIT
   membership has everywhere, not a defect in this changeset. Product code
   never needs a bring-your-own DBA to do anything about this either way:
   `src/nexus/db/svc_monitor.py` is the one place a `nexus_svc` session
   performs that escalation — unconditionally, so it is correct whether
   the role is NOINHERIT or INHERIT — and `nx doctor
   --check-wal-retention` samples retained WAL through it, reporting an
   explicit `UNMEASURED` (never a false clean) if the grant above was
   never applied.
3. **Diagnostic counts view (RDR-182 Amendment A6).** After the first
   migration run has created the chunk tables, create the counts view and
   grant it to `nexus_diag` (the local bundle's provisioning does this
   automatically; bring-your-own-Postgres DBAs run it once — or let the
   engine's own `taxonomy-011-8` Liquibase changeset create it for you on
   its next boot, since 2026-08-17). `WITH (security_invoker = true)`
   evaluates row-level security against the QUERYING role rather than the
   view's owner (PG15+), so `nexus_diag` (`LOGIN ... BYPASSRLS`) sees every
   tenant's rows through this view regardless of who created it — the view
   no longer NEEDS a superuser owner the way it did before this option
   existed, though creating it as the superuser (as below) remains a safe,
   supported path too:

   ```sql
   CREATE OR REPLACE VIEW nexus.diag_chash_conformance WITH (security_invoker = true) AS
   SELECT 'nexus.chunks' AS table_name, count(*) AS non_conformant FROM nexus.chunks WHERE octet_length(chash) <> 32
   UNION ALL
   SELECT 'nexus.catalog_document_chunks' AS table_name, count(*) AS non_conformant FROM nexus.catalog_document_chunks WHERE octet_length(chash) <> 32
   UNION ALL
   SELECT 'nexus.topic_assignments' AS table_name, count(*) AS non_conformant FROM nexus.topic_assignments t WHERE NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.chash = t.doc_id)
   UNION ALL
   SELECT 'nexus.frecency' AS table_name, count(*) AS non_conformant FROM nexus.frecency t WHERE t.chunk_id ~ '^[0-9a-f]+$' AND length(t.chunk_id) % 2 = 0 AND NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.chash = decode(t.chunk_id, 'hex'))
   UNION ALL
   SELECT 'nexus.relevance_log' AS table_name, count(*) AS non_conformant FROM nexus.relevance_log t WHERE t.chunk_id ~ '^[0-9a-f]+$' AND length(t.chunk_id) % 2 = 0 AND NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.chash = decode(t.chunk_id, 'hex'));
   GRANT SELECT ON nexus.diag_chash_conformance TO nexus_diag;
   ```

   Once the view exists, the engine's `grants-nexus-diag-2` changeset revokes
   `nexus_diag`'s direct table SELECT on its next boot — the diagnostic role
   then reads counts by construction, never row content. (This SQL is
   generated from `nexus.db.chash_tables.CHASH_BEARING_TABLES`; a drift test
   pins this rendered copy to the generator.) DBAs who created an earlier
   view generation: re-run the `CREATE OR REPLACE` above once — nexus-z5j0t
   added the three legacy-debt legs (`topic_assignments.doc_id`,
   `frecency.chunk_id`, `relevance_log.chunk_id`; observed-only, they do not
   gate upgrades), and RDR-187 (nexus-piwya.5) retired the
   `nexus.chash_index` leg (the router table is being dropped; the gate
   filters by table_name, so an older view with the extra leg still
   satisfies it). Until re-created, debt counts report as unknown, never as
   clean.

## Tuning Parameters

The `[tuning]` section in `~/.config/nexus/config.yml` controls search scoring, chunking, and timeout behavior. All values have sensible defaults — only override what you need.

```yaml
tuning:
  scoring:
    vector_weight: 0.7            # weight for vector similarity in hybrid scoring
    frecency_weight: 0.3          # weight for git frecency in hybrid scoring
    file_size_threshold: 30       # accepted, ignored as of nexus-0bmhd — see table below
  frecency:
    decay_rate: 0.01              # frecency decay rate (higher = faster decay)
  chunking:
    code_chunk_lines: 150         # target lines per code chunk (fallback splitter)
    pdf_chunk_chars: 1500         # target chars per PDF chunk
  timeouts:
    git_log: 30                   # seconds — timeout for git log subprocess
```

| YAML path | Default | Description |
|-----|---------|-------------|
| `tuning.scoring.vector_weight` | `0.7` | Vector similarity weight in hybrid scoring formula |
| `tuning.scoring.frecency_weight` | `0.3` | Git frecency weight in hybrid scoring formula |
| `tuning.scoring.file_size_threshold` | `30` | Accepted, ignored as of nexus-0bmhd — RDR-006's chunk-count scoring penalty was superseded by a render-layer file-diversity cap (`search_engine.apply_file_diversity_cap`); the key is retained for config-file back-compat but no longer read |
| `tuning.frecency.decay_rate` | `0.01` | Exponential decay rate for frecency scoring |
| `tuning.chunking.code_chunk_lines` | `150` | Target lines per code chunk (line-based fallback) |
| `tuning.chunking.pdf_chunk_chars` | `1500` | Target characters per PDF chunk |
| `tuning.timeouts.git_log` | `30` | Timeout (seconds) for `git log` subprocess |

These values are exposed as a `TuningConfig` dataclass in `nexus.config`. The search command, indexer, and scoring modules all read from this config — changes take effect on the next invocation without restarting anything.

## Heat-Weighted T2 Expiry

T2 memory entries use a heat-weighted effective TTL (RDR-057 Phase 2a):

```
effective_ttl = base_ttl * (1 + log(access_count + 1))
```

Highly-accessed entries survive longer than their nominal TTL. Unaccessed entries (`access_count=0`) expire at the base rate (`log(1) = 0`, so multiplier = 1). Every `memory_get` or `memory_search` hit increments `access_count` and updates `last_accessed`.

| access_count | Multiplier | Effective TTL (base 30 days) |
|--------------|------------|------------------------------|
| 0 | 1.00 | 30 days |
| 1 | 1.69 | ~51 days |
| 5 | 2.79 | ~84 days |
| 10 | 3.40 | ~102 days |
| 50 | 4.93 | ~148 days |

**Note**: This differs from the paper (Memory in the LLM Era) which uses division for relevance-decay. Nexus uses multiplication for heat-based survival — entries agents keep touching stick around longer. If you need strict time-bounded expiry regardless of access, use `ttl=None` (permanent) and explicit `memory_delete` instead.

Expiry quarantines; it does not delete (RDR-207). `T2Database.expire(relevance_log_days=90)`, which the session-end hook and `nx memory expire` run, hides every entry past its effective TTL from get, search and list, and keeps the row. `nx memory reap` deletes a quarantined entry only once a rollup summary covers it, and `nx memory restore ID` brings one back as permanent; see [nx memory](cli-reference.md#nx-memory). The same call also purges the `relevance_log` telemetry table (RDR-061 E2) of entries older than 90 days, and those rows are deleted.

## File Locations

| File | Purpose |
|---|---|
| `~/.config/nexus/config.yml` | Global config and credentials |
| `~/.local/share/nexus/chroma/` | Legacy ChromaDB store. A relic: nothing has read it since 7.0.0; not live T3 data |
| `~/.config/nexus/postgres/` | The nx-provisioned Postgres cluster the service serves T3 from (local `nx init`) |
| `~/.config/nexus/memory.db` | **Deleted (RDR-158 P4).** Historical: the pre-migration T2 SQLite database — a frozen migration source only, for installs that have not yet moved off it. Live T2 is a Postgres table set served by `nexus-service`; there is no client-local T2 file. See [Storage Tiers § T2](storage-tiers.md#t2----memory-bank). |
| `~/.config/nexus/catalog/.catalog.db` | **Deleted (RDR-158 P4).** Historical: the local SQLite catalog (replaced `repos.json` as the source of truth in 5.4.0, RDR-137). Live catalog data is Postgres-backed, served by `nexus-service` via `HttpCatalogClient` — see [Document Catalog](catalog.md). |
| `~/.config/nexus/sessions/` | JSON session records (T1 server address, session ID, `created_at`, `tmpdir`) + `session.lock` |
| `~/.config/nexus/index.log` | Background indexing log (written by git hooks) |
| `~/.config/nexus/cli_lockstep_marker` | Last CLI version confirmed in lockstep with the plugin (RDR-143). Written by the version-lockstep SessionStart hook only after a confirmed upgrade; a missing or stale value makes the hook fall back to the CLI's own `nx --version`: a CLI at least as new as the plugin is in lockstep, an older or unreadable one triggers a re-nudge next session. |
| `~/.config/nexus/lockstep.log` | Durable, always-on (never gated behind `NX_HOOK_DEBUG`) append-only log of every RDR-143 lockstep venv-swap attempt — one `lockstep_upgrade_started` line and one `lockstep_upgrade_result` line (outcome: `success` / `uv_upgrade_failed` / `nx_upgrade_failed` / `version_still_mismatched`) per attempt (nexus-otnvr item 4: the detached action's own stdout/stderr are DEVNULL'd by the dispatching hook, so this file is the only durable record that a background swap happened at all). Capped at ~1MB with a single-generation manual rotate to `lockstep.log.1` (a plain `Path.replace()`, not `logging.handlers.RotatingFileHandler` — kept minimal-footprint like the rest of this bare-interpreter hook, which also cannot import the `nexus` package itself). |
| `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/` | The MiniLM embedding model, downloaded once and verified by sha256; the engine's `OnnxEmbedder` reads the same artifact. `NX_MINILM_CACHE_DIR` replaces the `~/.cache/chroma/onnx_models` root. The unit suite sets it to the real cache, so a test that moves HOME does not download the model again. |
| `.nexus.yml` | Per-repo config overrides |

## Logging

Central configuration: `src/nexus/logging_setup.py` — `configure_logging(mode, verbose)`.

### Entry Points

| Entry point | Mode | File handler | Notes |
|---|---|---|---|
| `nx` CLI | `cli` | None (stderr only) | WARNING default, DEBUG with `-v` |
| `nx-mcp` (core MCP) | `mcp` | `~/.config/nexus/logs/mcp.log` | RotatingFileHandler 10 MB × 5 |
| `nx-mcp-catalog` | `mcp` | `~/.config/nexus/logs/mcp.log` | Shares log with core MCP |
| `nx console` | `console` | `~/.config/nexus/logs/console.log` | RotatingFileHandler 10 MB × 5 |

### Log Files

| File | Writer | Format |
|---|---|---|
| `~/.config/nexus/index.log` | Git post-commit hook (`nx index repo`) | Unstructured, ~60 MB observed |
| `~/.config/nexus/dolt-server.log` | Dolt server process | Dolt native format |
| `~/.config/nexus/logs/mcp.log` | MCP servers (via `logging_setup`) | `%(asctime)s %(name)s %(levelname)s %(message)s` |
| `~/.config/nexus/logs/console.log` | Console server (via `logging_setup`) | Same as above |

### Suppressed Loggers

`httpx`, `httpcore`, `chromadb.telemetry`, `opentelemetry` — forced to WARNING in all modes.

### Special Cases

- `search_cmd.py` overrides structlog to ERROR level when producing machine-parseable output (`--json`, `--vimgrep`, `--files`, `--compact`).
- The indexer hook script redirects stdout/stderr to `index.log` directly in the shell — not managed by `logging_setup`.
