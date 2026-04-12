---
title: "RDR-064: nx console — Process Monitor for Agentic Nexus"
status: accepted
type: feature
priority: P1
created: 2026-04-10
accepted_date: 2026-04-11
reviewed-by: self
---

# RDR-064: nx console — Process Monitor for Agentic Nexus

## Problem Statement

Nexus has two mature surfaces: the `nx` CLI and the MCP tool set consumed by Claude Code agents. Both serve their intended users well. Neither is the right answer for a third question: **what is Nexus actually doing right now, by whom, to what, at what rate, and is anything broken?**

This is not hypothetical. Measured over six days (2026-04-05 to 2026-04-10):

- **15,444 `implements-heuristic` links** created by `index_hook` (94% of all link activity)
- **832 `implements` links** by `filepath_extractor`
- **13,906 documents registered** across 7 days, peak **9,026 on 2026-04-09**
- **3 active T1 sessions** running in parallel right now
- Named agent campaigns tracked in catalog `created_by` provenance: `rdr-068-research`, `research-campaign-2026-04-08`, `synthesis-gap-analysis`, `claude-session-2026-04-09`, `deep-analyst`, `deep-research-synthesizer`, `720-review`

None of this is visible to a human operator without running Python scripts against JSONL files. Nexus is a high-throughput agentic system with zero operator observability surface.

The operator does not search Nexus interactively — agents do that through MCP. The operator does not manually browse memory, catalog, or collections — agents write to them. The operator's actual workflow is **supervising what agents are doing with Nexus**: launching campaigns, watching them run, diagnosing when something breaks, confirming that links and indexing reflect intent.

Shell history confirms the absence: across the entire `~/.zsh_history`, there are zero direct `nx search` invocations, one `nx collection list`, one `nx store list`, zero `nx catalog` commands. The dominant direct CLI usage is `nx index repo .` (40% of all `nx` invocations) and its manual tail-the-log pattern — but even that is a minority of operator work because indexing is mostly triggered by git hooks and agent-driven flows.

An earlier draft of this RDR proposed a CLI-replacement shaped around interactive search, collection management, and diagnostics panels. Usage data contradicts all three: the operator does not search interactively (agents do), rarely manages collections (1 CLI use total), and diagnostics is a real but small slice. The panels were optimizing for a workflow the operator does not have.

`nx console` is the observability surface for the workflow the operator actually has: **supervising agentic Nexus activity.**

## Mental Model

The right precedents are process monitors, not user applications:

- **`top` / `htop`**: live view of what's running, sorted by activity, filterable by process/user
- **Temporal UI / Apache Airflow**: workflow monitoring — see each run, its status, its lineage, drill into failures
- **Prometheus (built-in expression browser)**: one page, one input, data-dense output
- **pgHero**: landing page *is* the diagnosis; no search, no navigation, nothing to configure
- **lnav**: multi-source log merge with temporal correlation as the organizing principle

The wrong precedents (explicitly rejected):

- **pgAdmin**: assumes the user manually queries a database. Agents do that through MCP.
- **Obsidian / Logseq / Roam**: assume the user manually browses a knowledge graph. Agents do that through MCP.
- **Sourcegraph**: assumes the user manually searches code. Agents do that through MCP.
- **Notion / Mem / Reflect**: assume the user edits prose. Agents write notes.

The console is a **window into agentic work**, not a participant in it. Every v0.1 panel must answer a "what happened / what is happening" question. Features that invite interaction with data (edit, re-run, author, annotate) are deferred to v0.2+ and require a separate scope decision.

## Non-Goals

- **Not a CLI replacement.** The `nx` CLI remains the right interface for ad-hoc commands, scripts, and automation. Nothing in the console obsoletes the CLI.
- **Not an interactive search or browse tool.** No search box, no results pane, no knowledge graph browser. Agents do search through MCP; the console observes what they did with it.
- **Not a content authoring surface in v0.1.** No memory entry editor, no plan editor, no catalog annotation tool. ProseMirror / Tiptap is the presumptive starting point when editing becomes in-scope, deferred to v0.2+.
- **Not visualization for its own sake.** The novelty concepts from the earlier research round (transpointing reader, citation map, semantic workbench) are deferred. None of them address the measured highest-volume activity, which is agentic link and document creation.
- **Not destructive actions in v0.1.** Verify / reindex / delete for collections were in the earlier draft and are deferred. v0.1 is read-only observation.
- **Not pretty, not responsive, not mobile.** Desktop workman's tool. Aesthetic target is `htop` + Prometheus + pgHero: dense, data-first, data-present.
- **Not multi-user.** Localhost bind, no auth, no permissions. Operators on shared hosts use SSH tunneling.
- **Not the full eight-panel vision.** v0.1 is three panels. The remaining concepts (Catalog Browser / Indexing Dashboard / Memory Browser / Config Editor / etc.) are deferred to future RDRs after v0.1 validation.

## Proposed Approach

### Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│  nx console --port 8765                                   │
│  ┌────────────────────────────────────────────────────┐  │
│  │  FastAPI app (uvicorn, single process, localhost)  │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │  │
│  │  │  Routes  │→ │  Jinja2  │→ │ HTMX HTML frags  │ │  │
│  │  │ (Python) │  │ templates│  │ + SSE streams    │ │  │
│  │  └────┬─────┘  └──────────┘  └──────────────────┘ │  │
│  │       │                                            │  │
│  │       ↓ direct imports + file watchers             │  │
│  │  ┌────────────────────────────────────────────┐   │  │
│  │  │ catalog/catalog.py | db/t3 | db/t2         │   │  │
│  │  │ Tail watchers:                              │   │  │
│  │  │   links.jsonl | documents.jsonl             │   │  │
│  │  │   sessions/*.session                        │   │  │
│  │  └────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
     reads:                                 writes:
~/.config/nexus/catalog/links.jsonl    ~/.config/nexus/console.<project>.pid
~/.config/nexus/catalog/documents.jsonl  ~/.config/nexus/logs/console.log
~/.config/nexus/sessions/*.session
~/.config/nexus/index.log
~/.config/nexus/memory.db
~/.config/nexus/dolt-server.log
```

### Stack

- **Server**: FastAPI + uvicorn (added to core deps — see RF-064-5 correction and Decision #21)
- **Templating**: Jinja2 (FastAPI native integration)
- **Interactivity**: HTMX for HTML-over-the-wire partial swaps; Alpine.js for local state (modals, inline toggles)
- **Streaming**: Server-Sent Events via `sse-starlette`, consumed by HTMX's `hx-sse` extension
- **Styling**: Pico.css (classless baseline, ~7 KB), custom CSS only where needed
- **Static assets**: vendored inside the Python package — HTMX, Alpine, Pico all ship with `pip install`. No CDN dependency.
- **Build step**: none. No `node_modules`, no bundler, no TypeScript.

### Package Layout

```
src/nexus/console/
  __init__.py
  app.py              # FastAPI app factory
  config.py           # Console config (port, bind, watch paths, scope)
  watchers.py         # JSONL tail + session file scanners + SSE broadcasters
  routes/
    __init__.py
    activity.py       # Activity Stream endpoints
    health.py         # Sessions & Health endpoints
    campaigns.py      # Campaigns & Provenance endpoints
    partials.py       # Shared HTMX partial endpoints
  templates/
    base.html         # Shell: scope selector, nav, layout
    activity/         # Activity Stream templates + partials
    health/
    campaigns/
  static/
    htmx.min.js       # Vendored
    alpine.min.js     # Vendored
    pico.min.css      # Vendored
    console.css       # Custom styles
src/nexus/commands/
  console.py          # `nx console` Click command
```

### Scope Model

Global scope selector in the top nav, persisted via **URL query parameter** (`?scope=project` or `?scope=all`), defaulting to `project` when absent. The scope affects every panel uniformly:

- **Project scope** (default): filter all streams, lists, and aggregates to the current project's owner — derived from the cwd when `nx console` launched
- **All scope**: show cross-project activity (matches the actual data model — one catalog, one Cloud DB, multiple owners)

URL-param persistence is shareable, stateless, and works naturally with HTMX swap model (the parameter carries through `hx-get` attributes). No cookies, no localStorage, no server-side session state for scope.

### Lifecycle (v0.1: Foreground, Per-Project)

- `nx console [--port 8765] [--host 127.0.0.1]` starts uvicorn in the foreground
- Writes pid file with project-scoped name: `~/.config/nexus/console.<project>.pid` where `<project>` is the repo basename (`console.nexus.pid`, `console.arcaneum.pid`, `console.mgrep.pid`)
- Multiple projects can run consoles on different ports simultaneously; each has its own pid file
- File contains JSON `{pid, port, project, cwd, started_at}` (same pattern as `mineru.pid`)
- Cleans up pid file on exit; Ctrl-C shuts down gracefully
- Port conflict with MinerU avoided by fixing the default to 8765 (MinerU uses dynamic ephemeral ports per `commands/mineru.py` — verified)
- Daemon mode deferred to a follow-up if demand materializes

### Session Binding

The console cannot rely on PPID ancestry — it is launched from an independent shell, not inside an agent's process tree. Strategy:

- Scan `~/.config/nexus/sessions/*.session` for active T1 session files
- Each file contains JSON `{session_id, server_host, server_port, server_pid, created_at, tmpdir}`
- Probe each session for liveness (TCP connect to `server_host:server_port`, optional HTTP HEAD to the ChromaDB server's heartbeat endpoint)
- List all active sessions in the Sessions & Health panel; optionally enrich with scratch entry counts by connecting to each session's ChromaDB server directly

## Phase 0 — Logging Prerequisite (Reduced Scope)

**Correction from prior draft**: the earlier RDR claimed logs were stderr-only with no persistence. This is partially wrong. `~/.config/nexus/index.log` exists and is 60 MB, continuously appended during indexing runs — verified 2026-04-10. The indexer writes to this file via a path separate from `cli.py`'s structlog stderr config.

What remains true: stderr is the only sink for most modules (catalog, auto-linker, MCP server, hooks, console), and there is no unified logging configuration.

Reduced Phase 0 scope:

1. **Inventory existing persistent logs**: `index.log`, `dolt-server.log`, any others discovered. Document in `docs/logging.md` what's written where by whom.
2. **Extend routing** to the channels that currently hit stderr only: MCP server output, hook outputs, catalog operations (auto-linker, link generator), `nx console` itself. Use `logging.handlers.RotatingFileHandler` (stdlib, no new deps).
3. **New `src/nexus/logging_setup.py`** with `configure_logging(mode: Literal["cli", "console", "mcp", "hook"])`. CLI mode preserves stderr for TTY contexts (zero behavior change for existing CLI users). Non-interactive modes add rotating file handlers under `~/.config/nexus/logs/`.
4. **Extract `doctor_health_check() -> list[HealthResult]`** from `doctor_cmd` as a library function, separate from the mutation operations (`--fix`, `--clean-checkpoints`, `--clean-pipelines`, `--fix-paths`). This is the one remaining backend refactor required for Sessions & Health — the rest of Phase 1 reads existing JSONL files directly.

   **Extraction sizing (verified 2026-04-10)**: `doctor.py` is 724 lines with 9 helper functions already factored. The mutation-flag branches (lines 279-398) are out of scope — they stay in `doctor_cmd`. The health-check body (~400-720) contains ~40-50 `_check_line()` call sites using the pattern `lines.append(_check_line(label, ok, detail))` + optional `_fix(lines, ...)`. Mechanical conversion:

   ```python
   @dataclass
   class HealthResult:
       label: str
       ok: bool
       detail: str = ""
       fix_suggestions: list[str] = field(default_factory=list)

   def run_health_checks() -> list[HealthResult]: ...
   def format_health_for_cli(results: list[HealthResult]) -> str: ...
   ```

   Each `_check_line()` call becomes a `HealthResult` append; 9 helper functions get signature updates (return `HealthResult` instead of mutating a list); `doctor_cmd` loops over results and formats via `format_health_for_cli()` to preserve existing CLI output verbatim. Estimated **1-2 days focused work**. Risk **low** — checks unchanged, only return shape.

Phase 0 is bundled into RDR-064 rather than split into its own RDR because the reduced scope is small enough and tightly coupled to Phase 1's Panel 2.

## Phase 1 — v0.1 Panels

### Panel 1: Activity Stream (Landing Page)

**Intent**: Live feed answering *"what is Nexus doing right now?"* This is the landing page at `/` — the thing the operator sees the moment they open the console.

**Routes**:
- `GET /?scope={project|all}` — landing page, activity stream shell
- `GET /activity/events?scope=...&since=...&created_by=...&content_type=...&link_type=...&owner=...` — paginated event list (HTMX swap)
- `GET /activity/stream?scope=...&...filters` — SSE endpoint for live updates with the same filter parameters
- `GET /activity/event/{id}` — event detail right-panel

**Data source**:
- `~/.config/nexus/catalog/links.jsonl` and `documents.jsonl` — tail polled at 1 s cadence
- Per-file `mtime` watched via `os.stat`; on change, seek to last known offset, read new bytes, parse JSONL
- New events are server-side filtered against each connected SSE client's subscription filters, then pushed

**UI elements**:
- Scope selector in top nav (persisted via `?scope=` URL param)
- Filter bar: time window, `created_by` selector (autocomplete from distinct values in the tail), `content_type` multiselect, `link_type` multiselect, `owner` selector
- Event list: one row per event — timestamp | actor | action | target | summary
- Row click → right-panel detail with full JSON record and rendered metadata
- Live-update indicator: current rate (events/minute, rolling average over last 5 minutes)
- Pause toggle to freeze the stream for inspection without losing the current buffer

**Backend complexity**: low. File tail polling is a small library (stat mtime, seek to last offset, read new bytes, parse JSONL lines). No schema changes. The only new dependency is `sse-starlette`.

**Filtering location**: server-side (decision #18). Events are filtered in the FastAPI route before being pushed over SSE, minimizing bandwidth at peak event rates.

### Panel 2: Sessions & Health

**Intent**: Answer *"is Nexus healthy and what agents are active right now?"*

**Routes**:
- `GET /health` — dashboard landing (synchronous full render)
- `GET /health/refresh` — manual refresh (HTMX swap)
- `GET /health/stream` — SSE incremental updates at 30 s cadence

**Data source**:
- `doctor_health_check()` library function (extracted in Phase 0)
- `~/.config/nexus/sessions/*.session` scanner with liveness probe
- `~/.config/nexus/dolt-server.log` mtime/size for Dolt server activity
- `db.t3.list_collections()` for T3 Cloud probe (cached 60 s to protect quota)
- Catalog SQLite `.catalog.db` mtime + WAL state
- Recent event rate derived from Activity Stream watchers (events/minute over the last 5 minutes)

**UI elements**:
- All health cards load **synchronously on first paint** (pgHero pattern — no "loading…" state, no SSE wait for first view)
- Status cards: T1 Sessions | T2 SQLite | T3 Cloud | MinerU | Catalog | Activity Rate | Dolt Server | Audit Status
- **Audit Status card** (RF-064-20): scheduled? (launchd/cron probe) | last run date + verdict | next fire | outstanding findings. Data: T2 `rdr_process/audit-*` + `launchctl list | grep rdr-audit` / `crontab -l | grep rdr-audit`. Green = scheduled + last run CLEAN; yellow = scheduled + PARTIAL findings; red = not scheduled or overdue.
- Each card: green / yellow / red status, one-line summary, click to expand for details
- Active sessions table: session_id | host:port | PID | uptime | liveness indicator (TCP probe, green/yellow/red per RF-064-18). Scratch entry count deferred to detail view (click row) — TCP liveness probe only on synchronous first paint.
- 30 s SSE refresh is additive — cards update in place without reloading

**Destructive actions**: none. Read-only. Verify / reindex / delete are deferred to later panels after v0.1 validation.

### Panel 3: Campaigns & Provenance

**Intent**: Answer *"what did that agent campaign actually produce?"*

**Routes**:
- `GET /campaigns?scope=...` — campaigns landing with system vs named split
- `GET /campaigns/{created_by}` — campaign detail
- `GET /campaigns/{created_by}/links?scope=...&...filters` — all links created by a campaign
- `GET /campaigns/{created_by}/docs?scope=...` — all documents registered by a campaign (if applicable)

**Data source**:
- `~/.config/nexus/catalog/links.jsonl` grouped by `created_by` (one-pass in-memory aggregate; milliseconds at 16 K links)
- Catalog SQLite for efficient repeat queries if the JSONL scan becomes slow
- Document registrations joined on owner when applicable

**UI elements**:
- Two lists on the landing page:
  - **System creators** (collapsed by default): `index_hook`, `filepath_extractor`, `auto-linker`
  - **Named campaigns** (expanded by default): agent session names, research campaigns, manual agent runs
- Each named campaign row: name | link count | doc count | time range | link types | content types affected
- Click a campaign → detail view with timeline, link list, doc list, link-type breakdown
- Filters: time window, `link_type`, `content_type`
- Scope toggle respected at all levels

**Backend complexity**: low. One-pass groupby over `links.jsonl` with in-memory aggregation. 16 K links is milliseconds. v0.1: re-scan per request; add LRU cache keyed on `links.jsonl` mtime when page load exceeds 500 ms (expected at ~100 K+ entries).

## Decision Points (Resolved in This RDR)

| # | Decision | Choice | Rejected |
|---|----------|--------|----------|
| 1 | Server framework | FastAPI | Starlette (no ecosystem advantage); Flask (older patterns) |
| 2 | Frontend stack | Jinja2 + HTMX + Alpine | React/Vue SPA (build toolchain, violates workman ethos) |
| 3 | Lifecycle | Foreground v0.1 | Daemon with pid file (deferred) |
| 4 | Port | Default 8765, `--port` override, project-scoped pid file | Dynamic-only (not bookmarkable); global pid file (multi-project race) |
| 5 | Logging destination | Per-source rotating files under `~/.config/nexus/logs/`, extending existing `index.log` | Single ring buffer; stdout-only (blocks Sessions & Health insight) |
| 6 | Logging RDR split | Bundled as reduced-scope Phase 0 | Separate RDR (unnecessary for coupled scope) |
| 7 | Auth | None, localhost bind | Basic auth (premature); SSH tunnel is sufficient |
| 8 | Session binding | File scan of `~/.config/nexus/sessions/*.session` | PPID chain (console not in agent process tree) |
| 9 | CSS framework | Pico.css classless baseline | Tailwind (build step); Bootstrap (heavy); bespoke (slower to ship) |
| 10 | Data vs HTML endpoints | HTML fragments only for v0.1 | JSON API (add when a second client appears) |
| 11 | Static asset delivery | Vendored inside Python package | CDN (airgap/firewall failure modes) |
| 12 | Command name | `nx console` | `nx ui` / `nx web` / `nx admin` (less specific intent) |
| 13 | Keyboard scheme | Vim-aligned (`j`/`k`/`/`) for list navigation | Standard web only — contradicted by operator profile (expert CLI user) |
| 14 | Streaming transport | SSE via `sse-starlette` + HTMX `hx-sse` | HTMX polling (`hx-trigger="every 2s"`) — fallback only if SSE proves brittle |
| 15 | Dogfood gate | One-week minimum single-operator use, honest post-mortem | Ship without usage evidence |
| 16 | Mental model | Process monitor (top/htop/Temporal/Prometheus/pgHero/lnav) | CLI replacement (pgAdmin/Obsidian/Sourcegraph — contradicted by operator's actual usage) |
| 17 | Scope persistence | URL query parameter `?scope={project\|all}`, default `project` | Cookie/localStorage (not shareable); never persisted (loses state) |
| 18 | Activity Stream filtering | Server-side before SSE push | Client-side (unacceptable bandwidth at peak event rates of 9 K docs/day) |
| 19 | Multi-project support | Per-project pid file (`console.<project>.pid`), scope selector toggles cross-project views | Single global console instance (breaks multi-project workflow) |
| 20 | Activity Stream data source | JSONL tail polling at 1 s | Catalog SQLite polling; inotify/FSEvents watcher; catalog-write-path integration (all add backend coupling for v0.1) |
| 21 | Console dependency strategy | Add `fastapi>=0.115` + `uvicorn[standard]` to core deps | Optional `[console]` extra (broken `nx console` for standard installs — unacceptable for P1) |

## Research Findings

_RF-064-1 was drafted and retracted during review — the cited source (`bfdb-thesis-gap-analysis-2026-03-19`) is about BFDB, not Nexus._
_RF-064-7 and RF-064-10 from the earlier draft were superseded by the Phase 1 panel reframe — Ward clustering and ProseMirror are no longer v0.1 concerns. Numbering gaps preserved intentionally._

### RF-064-2: Xanadu-inspired systems share a consistent failure mode

**Source**: T3 document `critique-pattern-xanadu-inspired-systems` in `knowledge__knowledge`; cross-referenced with Maggie Appleton's 2021 "Transclusion and Transcopyright Dreams" analysis.

The recurring failure pattern of Xanadu-inspired systems is building the addressing and linking layer to completion and then stopping before any user-facing surface exists. Nexus has avoided this on the *infrastructure* side (RDR-049, RDR-052, RDR-053 delivered the catalog, tumblers, and chash spans) but has walked directly into it on the *observability* side. RDR-064 is corrective action against a well-documented strategic trap.

### RF-064-3: Logging persistence is partial, not absent

**Source**: Direct inspection of `src/nexus/cli.py:24-31` and `~/.config/nexus/index.log` (2026-04-10).

Earlier draft claimed "logs are stderr-only with no file persistence." Corrected: the indexer *does* persist logs at `~/.config/nexus/index.log` (60 MB, continuously appended during indexing runs). This is separate from `cli.py`'s structlog stderr configuration.

What remains true: stderr is the only sink for most modules (catalog, auto-linker, MCP server, hooks, console), and there is no unified logging configuration. The `cli.py:24-31` block is the only structlog config site, writing to stderr with no file handlers. Phase 0 scope reduces from "build logging from scratch" to "extend existing persistence patterns to MCP, hooks, catalog, console channels."

### RF-064-4: No existing HTTP UI server — `nx serve` explicitly removed

**Source**: Glob of `docs/rdr/*.md` + `src/nexus/commands/` inventory.

Two internal HTTP servers exist in the tree: the T1 session ChromaDB HTTP server (started by the `SessionStart` hook, PPID-keyed, agent IPC) and the MinerU FastAPI extractor (`src/nexus/commands/mineru.py`). Both are internal plumbing, not user-facing. RDR-018 (2026-03) removed the original `nx serve` static file server. No REST endpoints, no WebSocket code, no templates, no frontend assets exist anywhere. All UI work is net-new. Notably, shell history shows 4 attempts at `nx serve` after removal — a signal that the operator has been reaching for a web surface.

### RF-064-5: FastAPI is a MinerU optional-extra dependency, not core (CORRECTED 2026-04-11)

**Source**: `pyproject.toml` lines 56-61, `uv.lock` marker analysis, gate Layer 3 critique.

**Correction**: The prior version of this RF stated FastAPI and uvicorn were "already in tree" at zero dependency cost. This is wrong. FastAPI and uvicorn are locked in `uv.lock` but gated behind `[project.optional-dependencies] mineru`. `uv run python -c "import fastapi"` fails with `ModuleNotFoundError` in the standard install; only `uv run --extra mineru` succeeds. `sse-starlette` IS in core (via `mcp>=1.0`), and `jinja2` IS in core (via docling/chromadb). The server framework is NOT.

**Decision (see Decision #21)**: add `fastapi>=0.115` and `uvicorn[standard]` to core `[dependencies]` in `pyproject.toml`. For a P1 feature, gating behind an optional extra creates a broken `nx console` subcommand for standard installs. Phase 0 gains a task: "add console dependencies to pyproject.toml and update uv.lock."

The MinerU pid-file pattern (`~/.config/nexus/mineru.pid` with JSON `{pid, port, started_at}`) still establishes the convention for `~/.config/nexus/console.<project>.pid` — direct architectural reuse with precedent. MinerU uses dynamic ephemeral ports, so console's fixed default (8765) does not collide.

### RF-064-6: Complete data-access layer exists as importable Python modules

**Source**: Explore agent inventory of `src/nexus/commands/`, `src/nexus/mcp/core.py`, `src/nexus/mcp/catalog.py`, `src/nexus/search_engine.py`, `src/nexus/db/t3.py`, `src/nexus/db/t2/`, `src/nexus/catalog/catalog.py`.

For the revised v0.1 panels, Activity Stream and Campaigns & Provenance read JSONL files directly (lower coupling than going through the data-access layer). Sessions & Health uses `db.t3.list_collections()` plus the `doctor_health_check()` library function (extracted in Phase 0). The only backend work required is the `doctor_cmd` extraction — the rest is file reading and standard FastAPI route construction.

### RF-064-8: HTMX + server-rendered templates matches the workman ethos

**Source**: HTMX documentation (htmx.org), FastAPI Jinja2 integration guide, comparison with React/Vue SPA ceremony.

The "HTML over the wire" pattern eliminates the JavaScript build toolchain entirely. No `node_modules`, no bundler, no TypeScript configuration, no SPA state-management, no hydration. For the three v0.1 panels (all read-only observation), every interaction maps to either an SSE push or a link click. Alpine.js handles the remaining local-state needs (pause toggle, filter expand, scope selector) as a single vendored file. Comparable process monitors (Prometheus built-in UI, pgHero, Kibana's bare mode) all ship server-rendered density-first UIs.

### RF-064-9: SSE stability on localhost is not a risk

**Source**: `sse-starlette` documentation, HTMX `hx-sse` extension reference, W3C EventSource spec.

Common SSE pitfalls (nginx/haproxy proxy buffering, corporate firewall stripping of `text/event-stream`, background-tab throttling, reconnection edge cases) either do not apply to localhost binding or are handled automatically by the browser's `EventSource` implementation. `sse-starlette` is battle-tested with FastAPI and provides proper async generator lifecycle management. For the Activity Stream's 1 s poll cadence and peak event rate (~100/s briefly, <20/s typically), SSE throughput is well within the transport's comfortable operating range.

### RF-064-11: Catalog is a firehose — 16,412 links in 6 days, zero viewer

**Source**: Direct analysis of `~/.config/nexus/catalog/links.jsonl` (2026-04-10).

Catalog link creation over the last 6 days:

| Count | Creator | Link Type |
|---:|---|---|
| 15,444 | `index_hook` | implements-heuristic |
| 832 | `filepath_extractor` | implements |
| 51 | `auto-linker` | cites / relates |
| 35 | `rdr-068-research` | cites / relates / supersedes |
| 17 | `research-campaign-2026-04-08` | relates / implements |
| 8 | `synthesis-gap-analysis` | cites |
| 8 | `claude-session-2026-04-09` | cites |
| 4 | `deep-analyst` | relates |
| 2 | `deep-research-synthesizer` | cites |

Peak: **5,493 links created on 2026-04-08**. Document registrations: **13,906 across 7 days**, peak **9,026 on 2026-04-09**. Content types: 75% code, 13% prose, 8% rdr, 3% knowledge, 1% paper.

None of this is visible to a human operator without running Python scripts against JSONL files. This is the single largest piece of evidence driving the panel reframe from "interactive search/browse" to "process monitor for agentic activity." The Activity Stream panel is a direct response to this data.

### RF-064-12: Named agent campaigns are first-class data in the catalog

**Source**: Direct analysis of `links.jsonl` `created_by` field distribution (2026-04-10).

Distinct named campaigns visible in recent activity:

- `rdr-068-research` — 35 links (named RDR research pass)
- `research-campaign-2026-04-08` — 17 links (dated research campaign)
- `synthesis-gap-analysis` — 8 links (named synthesis work)
- `claude-session-2026-04-09` — 8 links (dated Claude session)
- `deep-analyst` — 4 links (agent type)
- `720-review` — 3 links (review campaign)
- `deep-research-synthesizer` — 2 links (agent type)
- `user` — 1 link (direct human)

These are real agent session names and research campaigns, already tracked via `created_by` provenance in every link. The data is first-class, queryable, and has zero existing surface for retrospective review. The Campaigns & Provenance panel surfaces this directly — answering the question "what did that research campaign actually produce?" in one click.

### RF-064-13: Multiple T1 sessions are typical and invisible to the operator

**Source**: `~/.config/nexus/sessions/` directory inspection (2026-04-10).

Three active session files right now: `14686.session`, `16967.session`, `91673.session` — three parallel Claude Code sessions, each with an independent T1 ChromaDB HTTP server on a distinct port (58667, 57274, 57778). Each session has its own tmpdir, scratch state, and agent activity. The operator cannot see any of this without walking the session files manually.

Supervising parallel agent work is an unsolved observability problem. The Sessions & Health panel directly addresses it with a live sessions table showing session ID, host:port, PID, uptime, and scratch entry count per session.

### RF-064-14: Five process-monitor adopt/reject pairs with specific behaviors

**Source**: htmx.org; lnav.org/features; github.com/tstack/lnav; temporal.io/blog (workflow visualization); github.com/ankane/pghero; prometheus.io/docs/visualization/browser; man7.org/linux/man-pages/man1/htop.1.html.

- **pgHero — adopt synchronous first paint; reject per-panel page navigation.** pgHero renders all health cards server-side before the response is sent. No skeleton screens, no spinners, no deferred AJAX — the page *is* the diagnosis the moment it loads. This is already locked in Decision #2 for Sessions & Health and is the correct call. What pgHero gets wrong for our purposes: separate full-page routes for queries / indexes / space / connections, each requiring navigation. For a three-panel console with a persistent nav bar, single-page scroll with section anchors beats full-page navigation.
- **Prometheus expression browser — adopt URL-as-state; reject React query explanation panel.** Every query is encoded in the URL — bookmarkable, shareable, reload-safe, zero server session. This maps exactly to the `?scope=` URL parameter in Decision #17. The Prometheus 3.0 redesign added a PromLens-style query tree explanation built with React + Mantine — a move that violated the original browser's "one input, data-dense output, no chrome" contract and introduced a JavaScript build pipeline. That is precisely the trap we avoid by vendoring static assets.
- **htop — adopt incremental in-place filter; reject configurable meter setup.** htop's F4 filter updates the process list as you type, no form submit, no reload, Escape to cancel. This maps directly to HTMX's `hx-trigger="input delay:300ms"` on the Activity Stream filter bar. What to reject: htop's F2 setup screen with configurable meters and column ordering. That configurability is htop's signature feature *but was earned after years of use*. v0.1 should have a fixed layout and defer configurability until demand is established.
- **lnav — adopt source-identity color margin; reject SQL mode as primary interaction.** lnav merges multiple log sources into one chronological stream and uses a colored left-margin bar to identify the source of each line without a filter or column. Directly applicable: a narrow left-border color on each Activity Stream row, one color per creator class (system creators, named campaigns), provides instant visual discrimination without cluttering row content. What to reject: lnav's `:` SQL console — powerful for power users, completely undiscoverable for everyone else. Our interaction surface is filters, not queries.
- **Temporal UI — adopt collapsible event stacks; reject raw event history as default.** Temporal groups related events (ActivityTaskScheduled + Started + Completed) into a single collapsed row with a span indicator, and collapses 20 identical executions into one row with a count badge. The raw full event history is an opt-in tab. For the Activity Stream, 9,026 `index_hook` document registrations in a single day should collapse to a summary row (`index_hook: 9,026 documents — 2026-04-09`) that expands on click. Showing all 9,026 rows by default is the exact design failure Temporal solved.

### RF-064-15: `sse-starlette` v0.1 risk profile for localhost single-user deployment

**Source**: github.com/sysid/sse-starlette (README, issues #8, #67, #77); deepwiki.com/sysid/sse-starlette; github.com/fastapi/fastapi/discussions/7572; htmx.org/extensions/sse/.

**Async generator lifecycle — one real pitfall, one-line fix.** `sse-starlette` detects client disconnects via `await request.is_disconnected()` and delivers `asyncio.CancelledError` to the generator. If the generator does not catch and re-raise this error, cleanup code in `finally` blocks silently skips, leaving file handles and shared state open. The fix is a single line: `except asyncio.CancelledError: raise`. Related: FastAPI's `@app.on_event("shutdown")` fires only after all connections close, which never happens while an SSE connection is open. `sse-starlette` v0.6.0 monkey-patches the uvicorn signal handler to address this. Practical consequence for `nx console`: use uvicorn directly (already true per the architecture diagram) and do not rely on FastAPI shutdown hooks for SSE generator cleanup.

**`Last-Event-ID` reconnect — HTMX client side is automatic, server replay is not.** `sse-starlette` does not automatically read the `Last-Event-ID` header or replay missed events. It accepts an `id:` parameter on `ServerSentEvent` objects for client-side tracking, but replay logic is the application's responsibility. HTMX's SSE extension sends `Last-Event-ID` on reconnect via the browser's native `EventSource` with exponential backoff. For the Activity Stream this gap is acceptable: a live tail resumes from current tail position after reconnect; the operator loses at most a few seconds of events during a browser tab switch. Gap-free replay would require a server-side event buffer indexed by sequence — deferred to v0.2+ if demand materializes.

**Memory and throughput — non-issues at our event rates.** The documented memory risk is creating resource objects (DB connections, file handles) outside the generator and closing them there — these leak when the generator is cancelled. The mitigation is to open resources inside the generator body. For `nx console`'s JSONL tail watcher, this is the natural pattern (open file, seek to offset, read, yield, sleep). The anyio Lock contention issue (#77) reduced throughput from ~263 k/sec to ~95 k/sec; at peak event rates of ~16/sec (see RF-064-17), this overhead is unmeasurably small. The `ping=15` default keeps connections alive without proxy involvement on localhost.

**HTMX `hx-sse` compatibility — no gotchas for this stack.** `sse-starlette` implements the W3C SSE spec; HTMX's SSE extension uses the browser's native `EventSource`. They are compatible by construction. The one proxy-specific concern (nginx's 16 KB buffering) is irrelevant for localhost. Enable HTMX's `pauseOnBackground` extension option to avoid the server accumulating open generators when the operator switches tabs. Honest assessment: for single-user localhost at our event rates, `sse-starlette` is fine. The production-scale concerns in the issue tracker (shutdown under load, thundering herd, proxy buffering) do not apply.

### RF-064-16: Eight LLM observability tools solve a different problem — two UI patterns worth stealing

**Source**: agentops.ai; github.com/Arize-ai/phoenix; langfuse.com; smith.langchain.com; docs.wandb.ai/weave/guides/integrations/mcp; helicone.ai; braintrust.dev; literalai.com.

**The shared assumption that excludes all eight tools.** AgentOps, Arize Phoenix, Langfuse, LangSmith, W&B Weave, Helicone, Braintrust, and Literal AI share one architectural assumption: **observation requires SDK instrumentation inside the observed system**. The agent imports their library and calls `trace()`, `record_action()`, `weave.op()`. W&B Weave comes closest to the `nx console` problem by patching `mcp.server.fastmcp.FastMCP` and `mcp.ClientSession` at import time — but this patches the MCP *server* side, not the Claude Code agent that calls the tools. Claude Code's tool dispatch loop is not instrumented by any of these. `nx console` reads JSONL files that Nexus writes as a side effect of normal operation. No SDK, no code changes, no agent cooperation required. The niche is genuinely unoccupied.

**Self-hosting reality.** Phoenix (Arize) and Langfuse are the two tools with credible local-deployment stories. Phoenix runs as a single Docker container with no external dependencies — the most viable for local-first dev. Langfuse self-hosting requires ClickHouse, Redis, S3, and PostgreSQL — a production stack, not a dev tool. The remaining six are cloud-only SaaS. None read from local file artifacts; all instrument network-level LLM API calls. This infrastructure gap confirms that the operator running multiple parallel Claude Code sessions against a local knowledge system has no off-the-shelf tooling.

**Two UI patterns worth stealing.** From **Phoenix's trace tree**: hierarchical nesting of spans (tool call → sub-calls, expand/collapse per node) maps directly to the Campaigns panel's drill-down (campaign row → link batch → individual link). The visual affordance — disclosure triangle with indented children, colored by outcome — is a tested pattern for "one campaign produced many artifacts." From **LangSmith's "run threads" grouping**: multiple agent sessions working on the same topic are grouped into a thread view with session IDs, time ranges, and combined counts as a single row. This maps to the Campaigns landing's `created_by` grouping — where `rdr-068-research` and `research-campaign-2026-04-08` might both contribute to the same knowledge area and warrant visual co-location. Both patterns are trivially implementable with server-rendered HTML fragments + HTMX swap.

### RF-064-17: JSONL append rate is ~16 events/sec at peak — 1s polling has ample headroom

**Source**: `src/nexus/catalog/catalog.py:311-313` (`_append_jsonl`); `src/nexus/catalog/catalog.py:439` and `:920,937` (calls in `register` and `_link_unlocked`); `src/nexus/indexer.py:279-310` (registration loop); `src/nexus/catalog/catalog.py:299-307` (`_acquire_lock`/`_release_lock`); `src/nexus/catalog/catalog.py:1120-1155` (`compact`).

**Write pattern — immediate per-operation appends with context-manager flush.** Each document registration and link creation calls `_append_jsonl()` immediately within the critical section, not batched. The method opens the JSONL file in append mode (`"a"`), writes one JSON line plus newline, and relies on the context manager to flush and close. The context manager guarantees `f.close()` but does not call `fsync()` — the write is buffered at the OS level. `register()` makes three `_append_jsonl` calls per document (owners, documents, SQLite commit); `_link_unlocked()` makes two per link. No batching window exists.

**Concurrency — serialized by directory-level `fcntl.LOCK_EX`.** The entire register/link flow is protected by an exclusive advisory lock (line 299, released line 307). This prevents concurrent writes to the catalog from multiple processes. Partial JSON lines cannot interleave because writes are atomic at the Python level (single `f.write()` per JSON object) and the lock serializes all catalog-mutating operations. File-system-level barriers are absent — a power loss between Python flush and OS page-cache write could lose recent entries, but this is a general FS property, not polling-specific.

**Truncation — only `compact()` rewrites the file.** `compact()` opens each JSONL in write mode (`"w"`) under the same lock guard, rewrites deduplicated records, and calls `rebuild()` to restore SQLite state. `init()` creates empty files via `.touch()` and never truncates existing ones. Tests use temporary directories. The tail watcher must handle size-shrink by restarting from offset 0, but this is only triggered by explicit `compact()` runs.

**Burst math — ~16 appends/sec at measured peak.** RDR-064 measured 9,026 docs and 5,493 links on peak days. Assuming a typical indexing run takes 10–30 minutes (inferred from the indexer loop structure at `src/nexus/indexer.py:277-331`), 9,026 documents over 15 minutes yields **~10 appends/sec for `documents.jsonl`**. Link generation adds **~6 appends/sec for `links.jsonl`**. Combined peak: **~16 appends/sec** — well below the 1-second poll interval's 1,000-operation headroom. Typical activity (outside peak indexing runs) is <1 append/sec.

**1-second polling headroom assessment — no event loss, bounded latency.** The tail watcher polls `os.stat()` mtime every 1 s. At 16 appends/sec, all events created in a 1-second interval are read in batch on the next poll. **No events are lost**: JSONL is append-only, the tail watcher checkpoints its read offset, no line is skipped even if a poll is delayed. The risk is **latency banding** — events created at time T are visible to clients at T+[0, 1] seconds depending on poll alignment. Acceptable for a process monitor. If 100 ms latency becomes a requirement, drop the poll interval to 250 ms (still cheap — `os.stat()` is ~microseconds).

### RF-064-18: T1 scratch probe — use TCP liveness + deferred count, not synchronous `.count()` on first paint

**Source**: `src/nexus/session.py:220-277` (T1 server startup); `src/nexus/session.py:308-333` (session record format); `src/nexus/db/t1.py:109-193` (T1Database construction); `src/nexus/db/t1.py:329-359` (list_entries with pagination); `src/nexus/commands/doctor.py:57-100` (existing session liveness pattern).

**T1 discovery and client connection.** When a T1 session starts, a ChromaDB HTTP server is launched on a dynamically allocated localhost port. Session record written to `~/.config/nexus/sessions/{ppid}.session` as JSON: `{session_id, server_host, server_port, server_pid, created_at, tmpdir}`. The console discovers active sessions by scanning the directory and parsing each file. Client connection is standard: `chromadb.HttpClient(host=record["server_host"], port=record["server_port"])` — no custom wrapper.

**Count cost — O(n) pagination, not O(1) metadata lookup.** ChromaDB's public API does not expose `.count()` on collections in the way the RDR assumed. `T1Database.list_entries()` (lines 329-359) reveals the actual pattern: `.get(where={"session_id": self._session_id}, limit=300, offset=offset)` in a loop until fewer than 300 results return. Each `.get()` is a full collection scan filtered by metadata — **O(n) in total entries**, not constant-time. A session with 10,000 entries requires ~34 HTTP round trips (~10–25 ms each on localhost), so **250–800 ms per session**. For typical sessions (100–1000 entries), it's **1–4 round trips** or 50–200 ms.

**Safety — concurrent probing is safe.** ChromaDB's HTTP server isolates reads from writes via its internal locking. The session-scoped filter (`where={"session_id": self._session_id}`) prevents cross-session contamination. No risk of corrupted reads or interference with active agent writes. Main concern is **latency variance** — a probe issued while a T1 server is under heavy write load may block behind queued operations, adding 100–500 ms.

**TCP liveness is the cheap alternative.** `start_t1_server()` at `session.py:260-271` already uses a TCP liveness pattern: `socket.create_connection((host, port), timeout=0.5)` loop. **~1–5 ms on localhost, zero ChromaDB API cost.** The `doctor_cmd` uses `os.kill(pid, 0)` for orphan detection, which is local-process-only and doesn't validate the HTTP server is actually responding.

**Recommended v0.1 strategy — two-tier probing, synchronous first paint uses cheap tier only.** For Sessions & Health's "active sessions" row: (1) **TCP liveness check** for every session on synchronous first paint — 5 ms each, zero Chroma cost, shows green/yellow/red indicator. (2) **Optional `.get(limit=1)` probe** in a deferred right-panel detail view to display "N+ entries" when the operator clicks a session row. This keeps the landing page under 100 ms total for three sessions, eliminates the O(n) cost from the critical path, and delivers the stated observability goal without the latency tax. The current RDR Panel 2 spec ("scratch entry count per session" on the landing page) should be revised to defer the count to the detail view.

### RF-064-19: Chroma Cloud has no documented rate/request quota — "2,880 calls/day" was precautionary

**Source**: `src/nexus/db/chroma_quotas.py` (full file inspection); Chroma Cloud docs at https://docs.trychroma.com/cloud/quotas-limits as referenced in the quota file comment.

Nexus's `chroma_quotas.py` (RDR-005) is the single source of truth for Chroma Cloud limits. It documents exclusively **data-size and structural limits**: max embedding dimensions (4096), max document bytes (16 KB), max records per write (300), max records per collection (5 M), max collections per account (1 M), max concurrent reads/writes per collection (10 each). **No request-rate quota. No per-day or per-minute API call ceiling. No bandwidth cap.**

The "2,880 calls/day per running console" figure cited in the earlier Risks #5 was calculated (30-second poll × 86,400 seconds/day) but not grounded in any documented ceiling. Chroma Cloud may bill per-request on paid plans, but free-tier does not publish a rate limit.

**Revised risk posture**: the concern is not quota exhaustion but **API cost efficiency** if the user upgrades to a paid plan. Mitigation applies regardless: Sessions & Health should cache T3 health probes for 60 s minimum (already in Risk #5 mitigation text) and prefer local signals (catalog mtime, index log growth, session TCP liveness) as primary health indicators. The T3 Cloud probe is a secondary, slow-path signal that confirms connectivity but does not need to run on every refresh.

**Implication for the RDR**: delete the "2,880 calls/day" claim from Risk #5, replace with "avoid frequent polling of T3 Cloud because (a) the paid-plan billing model is per-request and (b) local signals are sufficient for most health questions." No documented quota to exhaust, but cost sensitivity still justifies the caching strategy.

### RF-064-20: Cross-RDR remediation (065-069) shipped manual-invoke infrastructure with zero automated observability

**Source**: Post-shipment audit of 065-069 cycle + infrastructure inventory (2026-04-11, T2 id 773).

The 065-069 silent-scope-reduction remediation delivered four interventions — all manual-invoke:

| Intervention | Trigger | Automated? |
|---|---|---|
| Close funnel preamble (065) | `/nx:rdr-close` | Manual only |
| Composition probe (066) | `/nx:composition-probe` | Manual only |
| Audit skill (067) | `/nx:rdr-audit` | Templates exist (`scripts/launchd/`, `scripts/cron/`) but NOT installed |
| Critic dispatch (069) | During manual close | Manual only |

Meanwhile, telemetry data accumulates unseen: `relevance_log` (RDR-061 E2), search traces, 2+ parallel T1 sessions, 16K+ catalog links with zero viewer.

The console is the missing activation layer. Without it, the operator doesn't remember to run `/nx:rdr-audit` periodically, the telemetry data is write-only, and parallel agent sessions are invisible.

**Priority upgrade rationale (P2 → P1)**: the console makes the cross-RDR remediation operational, not just available. The 065-069 infrastructure has no value if the operator never invokes it — and the operator won't without an observability surface that reminds them to.

**Proposed scope addition**: "Audit Status" card in Sessions & Health panel showing: (1) is periodic audit scheduled? (launchd/cron check), (2) last audit run date + verdict, (3) next scheduled fire, (4) outstanding PARTIAL/SCOPE-REDUCED findings. Data source: T2 `rdr_process/audit-<project>-<date>` entries + OS-level `launchctl list` / `crontab -l`.

## Risks

1. **Mental-model drift during implementation.** The v0.1 panels are data-oriented, not feature-oriented. Engineers naturally add interactive features as they go ("what if you could click to re-run the campaign?"). Each such addition shifts the console from process monitor toward CLI replacement. Enforcement: the Mental Model section is the scope gate — any feature that isn't "read-only observation of existing data" is v0.2+ and requires a new decision.
2. **JSONL tail polling edge cases.** Files can truncate (git reset, manual edits), rotate (future), or be written non-atomically (partial lines during flush). Mitigation: defensive parser that skips malformed lines; re-read from offset on mtime changes; handle size shrink by restarting tail from beginning; checkpoint parser state per file.
3. **Event rate spikes.** Peak was 9,026 docs in a single day. Burst rates can exceed 100 events/second briefly during indexing runs. Server-side filtering reduces SSE bandwidth, but the file reader must keep up with appends. Mitigation: batch parse per poll interval (1 s window); drop-oldest backpressure on SSE clients; ceiling at 1000 events per paginated page.
4. **Multi-instance conflicts.** Two `nx console` processes launched from the same project would race on the pid file. Mitigation: project-scoped pid file name (`console.<project>.pid`); file lock before write; exit with clear error on contention.
5. **ChromaDB Cloud API cost efficiency.** Chroma Cloud does not publish a request/rate quota — the documented limits in `chroma_quotas.py` are data-size and structural only (see RF-064-19). The concern is not quota exhaustion but per-request billing on paid plans. Mitigation: 60 s minimum cache for T3 health probes; prefer local-only signals (catalog mtime, index log growth, session TCP liveness per RF-064-18) as primary health indicators, with T3 Cloud probe as a secondary slow-path signal that only confirms connectivity.
6. **Phase 0 logging refactor touches working code.** Even with reduced scope, modifying how hooks / catalog / MCP log could introduce regressions. Mitigation: preserve existing stderr behavior additively; add file handlers *alongside* stderr, never replacing. Zero behavior change for interactive CLI use — verified by retaining the TTY-mode configuration path.
7. **Dogfood gate failure reinterpretation.** If the operator uses the console for what it does well (agent observability) and falls back to CLI for what the console intentionally doesn't do (ad-hoc commands), that is a *success*, not a failure. The reframed gate is: "is the console consulted daily for its intended purpose of supervising agentic activity?" not "does it replace the CLI?"
8. **Port 8765 collision with unknown services.** Console binds a fixed default. Mitigation: startup port check; fail gracefully with a clear message recommending `--port` override; document the default in `--help` output.

## Alternatives Considered

1. **Thin REST wrapper over MCP tools.** Rejected. MCP was designed for agent consumption: text responses, tool-use semantics, stdio or websocket transport. Wrapping would add an IPC hop and force JSON transcoding of text responses.
2. **Subprocess CLI wrapping.** Rejected. Slowest option, fragile output parsing, no access to intermediate state, would make log tailing impossible.
3. **SPA frontend (React/Vue).** Rejected. Node toolchain, `node_modules`, TypeScript setup, bundler. Violates the workman ethos. HTMX delivers the needed interactivity at a small fraction of the ceremony.
4. **Reuse MinerU's FastAPI server.** Rejected. MinerU is a specialized PDF-extraction process with its own lifecycle (`nx mineru start/stop`). The console is an always-on observability surface. Conflating lifecycles would couple two unrelated concerns.
5. **Split Phase 0 logging into a separate RDR.** Rejected. Reduced Phase 0 scope is small enough that bundling avoids unnecessary gate cycles.
6. **CLI replacement (pgAdmin / Obsidian / Sourcegraph model).** Rejected by the data. Shell history shows near-zero direct `nx search` or `nx catalog` invocations; the operator does not search interactively. Building a search UI would address a pain point the operator does not have.
7. **Rich TUI (textual / rich) in-terminal.** Considered. Rejected because the operator runs multiple parallel Claude Code sessions and needs a separate window for observability, not another pane sharing terminal real estate with agent work.
8. **Integration with existing workflow monitoring (Temporal UI, Grafana, Prometheus).** Rejected. All three require Nexus to emit standardized metrics, which it does not today. Building that instrumentation would exceed the cost of building a focused console that reads catalog JSONL directly.
9. **Electron / Tauri desktop app.** Rejected. Adds bundler, renderer, and cross-platform packaging. A localhost web server opened in the operator's existing browser delivers the same experience with none of the overhead.
10. **Static HTML report generator (`nx report --open`).** Considered. Rejected because the highest-value workflow is *live observation* of ongoing agent activity, which a static report cannot serve. A report generator might be a useful v0.2 addition for sharing snapshots but is not a replacement.

## Success Criteria for v0.1

- `nx console` starts from any project directory; landing page loads in <500 ms on a warm catalog
- Activity Stream renders recent events immediately from the JSONL tail on launch (no empty state)
- New events appear in the stream within 2 seconds of being appended to `links.jsonl` or `documents.jsonl`
- Server-side filtering correctly reduces SSE payload when filters are active
- Scope selector (`?scope=all` vs default project scope) correctly filters all three panels consistently
- Sessions & Health renders all cards synchronously on first paint (no "loading…" state for steady-state data)
- Campaigns & Provenance distinguishes system creators from named campaigns; clicking a named campaign shows its link activity
- Multi-project test: two consoles launched from different projects on different ports, no pid-file contention, each scoped correctly by default
- Phase 0: new log files appear under `~/.config/nexus/logs/` for the extended channels; CLI stderr behavior unchanged for TTY invocations; `doctor_health_check()` extracted and callable
- Operator dogfood: one-week usage; each panel consulted at least daily for its intended purpose; honest post-mortem — "consulted daily for agent supervision" is success, CLI continuing to be used for ad-hoc work is *expected*, not failure

## Resolved in Review (2026-04-10)

All drafting open questions resolved before gate via iterative review:

- Command name = `nx console`
- Static assets vendored (HTMX, Alpine, Pico)
- SSE-first streaming transport
- Vim-aligned keyboard for list navigation (operator profile override)
- One-week dogfood minimum with reframed success criterion
- **Mental model pivoted** from "CLI replacement for interactive search" to "process monitor for agentic nx" based on agentic usage data (RF-064-11, -12, -13)
- **v0.1 panels reframed** from Search Console / Collection Manager / Diagnostics to Activity Stream / Sessions & Health / Campaigns & Provenance
- **Phase 0 scope reduced** after discovering `index.log` already persists
- **Scope selector** via URL query parameter (`?scope={project|all}`)
- **Activity Stream filtering** server-side
- **Multi-project support** via project-scoped pid file

ProseMirror / Tiptap deferred for v0.2+ editing panels. Catalog Browser / Transpointing Reader / Citation Map / Indexing Dashboard / Memory Browser deferred pending v0.1 validation — each requires a separate scope decision in a future RDR.

## Next Steps

1. Accept this RDR (or revise per further feedback)
2. Register RDR-064 in `docs/rdr/README.md` index
3. Create a bead epic for Phase 0 (reduced-scope logging + `doctor_health_check()` extraction) and Phase 1 (three panels)
4. Phase 0 first — small, reversible, unblocks Sessions & Health
5. Phase 1 panels in sequence: Activity Stream → Sessions & Health → Campaigns & Provenance
6. One-week dogfood; post-mortem with honest usage assessment against the reframed criterion
