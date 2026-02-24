---
name: nexus
description: Use when running nx commands for search, memory, knowledge storage, or project management — or when unsure which nx subcommand to use
---

# Nexus — Agent Usage Guide

Nexus gives you a single CLI to index code, PDFs, and notes; search across all of them semantically; and manage persistent memory across sessions.

**Three storage tiers:**
- **T1 scratch** — in-memory, session-scoped (`nx scratch`)
- **T2 memory** — local SQLite, survives restarts (`nx memory`)
- **T3 knowledge** — ChromaDB cloud + Voyage AI, permanent (`nx search`, `nx store`, `nx index`)

## Search

```bash
nx search "query"                          # semantic search across all T3 knowledge
nx search "query" --corpus code            # code collections only
nx search "query" --corpus docs            # docs collections only
nx search "query" --corpus knowledge       # knowledge collections only
nx search "query" --corpus code --corpus docs  # multi-corpus with reranker merge
nx search "query" --hybrid                 # semantic + ripgrep + git frecency (code only)
nx search "query" --answer                 # retrieval + Haiku answer synthesis
nx search "query" --agentic               # Haiku-driven multi-step query refinement
nx search "query" --mxbai                  # fan out to Mixedbread-indexed collections
nx search "query" --vimgrep               # path:line:col:content output
nx search "query" --json                   # JSON array output
nx search "query" --files                  # unique file paths only
nx search "query" --content               # show matched text inline
nx search "query" -C 3                    # 3 context lines around each result
nx search "query" --where store_type=pm-archive  # metadata filter
```

## Memory (T2 — persistent across sessions)

```bash
nx memory put "content" --project {repo} --title title.md
nx memory put - --project {repo} --title title.md   # from stdin
nx memory get --project {repo} --title title.md
nx memory search "query"
nx memory search "query" --project {repo}
nx memory list --project {repo}
nx memory expire                           # remove TTL-expired entries
nx memory promote <id> --collection knowledge  # push to T3
```

**Project naming**: use bare `{repo}` for all project memory (e.g., `nexus`). No `_active` or `_pm` suffixes.

## Knowledge store (T3 — permanent, cloud)

```bash
nx store put analysis.md --collection knowledge --tags "arch"
echo "# Finding..." | nx store put - --collection knowledge --title "My Finding" --tags "research"
nx store put notes.md --collection knowledge --tags "notes" --ttl 30d
nx store list
nx store list --collection knowledge__notes
nx store expire
```

**TTL formats**: `30d` (30 days), `4w` (4 weeks), `permanent` or `never` (no expiry). Use `Nd`/`Nw` format — NOT bare integers. Omit `--ttl` entirely for permanent entries.

## Scratch (T1 — session-scoped, cleared at session end)

```bash
nx scratch put "working hypothesis: the cache is stale"
nx scratch search "cache"
nx scratch list
nx scratch get <id>
nx scratch flag <id>                       # mark for auto-flush to T2 at session end
nx scratch unflag <id>
nx scratch promote <id> --project {repo} --title findings.md
nx scratch clear
```

**Usage pattern**: Use T1 scratch for in-flight working notes (hypotheses, interim findings, checkpoints). Flag important items so they auto-promote to T2 at session end. Permanently validated findings go to T3 via `nx store put`.

## Indexing

```bash
nx index code <path>                       # register and index a code repo
nx index code <path> --frecency-only       # refresh git frecency scores only (fast)
nx index pdf <path> --corpus my-papers
nx index md  <path> --corpus notes
```

## Project management (PM)

```bash
nx pm init                                 # initialise for current git repo
nx pm resume                               # inject continuation context (auto-called by hooks)
nx pm status                               # phase, agent, blockers
nx pm block "waiting on API approval"
nx pm unblock 1
nx pm phase next
nx pm search "what did we decide about caching"
nx pm promote phases/phase-2/context.md --collection knowledge --tags "decision"
nx pm archive                              # synthesise → T3, start 90-day T2 decay
nx pm close                                # archive + mark completed (alias for archive --status completed)
nx pm restore <project>
nx pm reference "how did we handle rate limiting"
nx pm expire
```

## Health and server

```bash
nx doctor                                  # verify all credentials and tools
nx serve start                             # start background HEAD-polling daemon
nx serve stop
nx serve status
nx serve logs
```

## Workflow — when and why to use each tier

**Session lifecycle:**
1. Search T3 for prior art before starting work: `nx search "topic" --corpus knowledge`
2. Index the codebase once per repo: `nx index code <path>`
3. Use T1 scratch for working notes during the session
4. Flag important scratch items for auto-promote to T2: `nx scratch flag <id>`
5. Persist validated findings to T3 at session end: `nx store put`

**Tier selection:**
- **T1 scratch**: hypotheses, interim findings, checkpoints — anything ephemeral to this session
- **T2 memory**: cross-session state, agent relay notes, active project context
- **T3 knowledge**: validated findings, architectural decisions, reusable patterns — anything worth keeping permanently

**Collection naming**: always `__` as separator — `code__myrepo`, `docs__corpus`, `knowledge__topic`. Colons are invalid in ChromaDB collection names.

**Title conventions** (use hyphens, not colons):
- `research-{topic}` — research findings
- `decision-{component}-{name}` — architectural decisions
- `pattern-{name}` — reusable patterns
- `debug-{component}-{issue}` — debugging insights
