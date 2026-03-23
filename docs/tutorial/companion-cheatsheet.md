# Nexus Cheat Sheet

Quick reference for the [video tutorial](https://github.com/Hellblazer/nexus/tree/main/docs/tutorial).

## Install

```bash
uv tool install conexus                # install nx CLI (requires Python 3.12–3.13)
uv tool update conexus                 # update to latest
nx --version                           # verify
nx doctor                              # health check
```

## Claude Code Plugin

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins       # same command to install or update
/nx:nx-preflight                       # verify plugin dependencies
```

## Memory (persistent project notes)

```bash
nx memory put "text" -p PROJECT -t TITLE               # expires in 30 days
nx memory put "text" -p PROJECT -t TITLE --ttl permanent  # never expires
nx memory get -p PROJECT -t TITLE
nx memory search "query" -p PROJECT
nx memory list -p PROJECT
```

## Scratch (session-only working notes)

```bash
nx scratch put "text"
nx scratch list
nx scratch search "query"
```

## Index and Search

```bash
nx index repo .                        # index current repo (local, no keys needed)
nx index repo . --monitor              # with per-file progress
nx index rdr .                         # index RDR documents only
nx search "query"                      # semantic search
nx search "query" --corpus code        # code only
nx search "query" --corpus docs        # docs only
nx search "query" --corpus rdr         # RDR decisions only
nx search "query" -c                   # show matching text
nx search "query" --hybrid             # semantic + keyword blend
```

## Git Hooks (auto-index on commit)

```bash
nx hooks install                       # set up auto-indexing
nx hooks status                        # check hook status
```

## Key Skills (inside Claude Code)

| Skill | When to use |
|-------|------------|
| `/nx:brainstorming-gate` | Before building anything new |
| `/nx:debug` | Test failure or unexpected behavior |
| `/nx:review-code` | Before committing changes |
| `/nx:create-plan` | Break work into steps |
| `/nx:implement` | Execute from a plan |
| `/nx:analyze-code` | Understand unfamiliar code |
| `/nx:research` | Investigate a topic |
| `/nx:architecture` | Design a system |
| `/nx:deep-analysis` | Complex problem investigation |
| `/nx:substantive-critique` | Critique a plan or design |
| `/nx:pdf-process` | Index a PDF into search |

## RDR (Decision Tracking)

| Skill | Purpose |
|-------|---------|
| `/nx:rdr-create TITLE` | Start a new decision document |
| `/nx:rdr-research add ID` | Add a research finding |
| `/nx:rdr-gate ID` | Validate before accepting |
| `/nx:rdr-accept ID` | Lock the decision |
| `/nx:rdr-close ID` | Archive when implemented |
| `/nx:rdr-list` | List all decisions |
| `/nx:rdr-show ID` | View one in detail |

## Cloud Mode (optional)

```bash
nx config init                         # interactive credential setup (needs API keys)
nx doctor                              # verify cloud connectivity
nx index repo .                        # re-index with cloud models
```
