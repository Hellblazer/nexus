# AGENTS.md

Project guidance for AI coding agents working in this repository. `CLAUDE.md` is a symlink to this file.

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as `conexus`; the CLI entry point is `nx` (`src/nexus/` is the package).

**Guidance precedence:** workflow routing — skills-first, agent dispatch, storage-tier checks, review paths, orchestration — is owned by the conexus plugin's injected guidance (the SessionStart guidance, the subagent-start preflight, the orchestration skill). This file and any personal CLAUDE.md yield to the plugin on workflow; they carry repo facts, hot rules, and durable authorizations. A restriction here that contradicts the plugin's workflow layer is a defect to surface, not a tiebreak to silently win.

**This file keeps the rules to obey on every turn.** History, incident archives, measurements and procedures live in [`docs/agents/`](docs/agents/README.md), moved there on 2026-10-14 (nexus-2ziaz) and edited since; each section below links its full text.

## Quick start

```bash
uv sync                                  # install deps
scripts/reinstall-tool.sh                # install nx CLI locally (preserves extras)
uv run pytest -n auto                    # full unit suite (no API keys needed; auto-capped); hand full suites: rule 13
uv run pytest                            # serial fallback (debugging only)
uv run pytest -m lint                    # O(repo) meta-tests (out of hot loop, PR-gated in CI)
uv run pytest -m integration             # E2E (requires .env from .env.example)
uv sync && scripts/reinstall-tool.sh && nx --version    # after edits
```

- `-n auto` caps itself to the box's SysV shared-memory budget (each xdist worker holds a segment) and, inside a lab-run grant, to the grant's memory (about 3.2 GiB a worker; lab-l runs 15). A wall of substrate setup errors means look at `ipcs -m` before the diff; `NX_XDIST_NO_CAP=1` opts out.
- After any pull/rebase or edit touching `service/` (a Java comment or changelog XML counts; the gate is mtime-keyed), run `scripts/build-gate-jar.sh`. Session start refuses with exit 75 on a stale jar. Never backdate mtimes.
- Unit tests use `InMemoryVectorClient` + bundled ONNX MiniLM (no keys, no network); engine-substrate tests self-provision a local service or skip. Test-authoring directives live in [`tests/AGENTS.md`](tests/AGENTS.md). Full text: [test-running](docs/agents/test-running.md).

## Architecture at a glance

Three storage tiers, by lifetime. **ChromaDB is not a live substrate in any mode** (RDR-155 P4b: dependency dropped, absent from `uv.lock`):

- **T1** — service-backed session scratch (`HttpScratchStore`; `nx scratch`), PG-only. `NX_T1_ISOLATED` is retired (nexus-4lkmz) — setting it hard-fails with `T1IsolatedLegRetiredError`; there is no in-process opt-out.
- **T2** — nine domain stores behind a `T2Database` facade, all HTTP clients over the engine's PG tables. Persistent notes, plans, taxonomy, telemetry, chash, aspects, aspect queue, DEVONthink highlights, tuples.
- **T3** — `HttpVectorClient` over the nexus-service `/v1/vectors` (pgvector) in both modes: local = bundled PG17+pgvector embedding bge-768 by default, or Voyage when `NX_VOYAGE_API_KEY` reaches the service (nexus-umm29 carve-out; one posture per boot, see RDR-210); cloud = managed service + Voyage always. Permanent knowledge (`nx store`, `nx search`).

T1 rules ([full contract](docs/agents/t1-sub-agent-contract.md); rule of record `docs/rdr/joint/JDR-001-t1-three-scopes.md`): Agent-tool sub-agents share the parent's T1; `claude -p` sub-processes default to `owned` and opt in with `share_t1=True`. Cross-process findings and durable write-back go to T2 (`memory_put`).

Collection prefixes coexist in one T3 database. Always `__` (double underscore) as separator (the RDR-103 name grammar). Conformant collection-name shape is `<content_type>__<owner_id>__<embedding_model>__v<n>`, e.g. `code__1-1__voyage-code-3__v1` (the owner segment is the owner tumbler's first two segments joined by a hyphen):

| Prefix | Embedder | Document identity (catalog) | Chunk natural ID (T3) |
|---|---|---|---|
| `code__*` | `voyage-code-3` | `source_uri` (file path) | `chunk_text_hash` (full 64-hex; 32 bytes stored — RDR-180) |
| `docs__*`, `rdr__*` | `voyage-context-3` (CCE) | `source_uri` (file path) | `chunk_text_hash` (full 64-hex) |
| `knowledge__*` | `voyage-context-3` | `source_uri` then `title` (fallback for MCP-stored notes) | `chunk_text_hash` (full 64-hex) |

**Catalog/T3 split (RDR-108, widths per RDR-180)**: Catalog Documents are graph nodes addressed by tumblers; T3 chunks are content-addressed by the FULL `sha256(chunk_text)` (64 hex on the wire, 32 bytes stored). Document structure lives in the catalog `document_chunks` manifest, never in chunk metadata; identical chunk text in one collection collapses to one T3 row by design. [Full text](docs/agents/catalog-t3-split.md).

**Creating or naming a collection** follows [`docs/collections.md`](docs/collections.md): `code`/`docs`/`rdr` are minted only by `nx index repo`; a `knowledge` collection is a durable subject area (`distributed-systems`), never a document, session, task, source app, or placeholder (`default`, `knowledge`, `test`); reuse an existing subject before creating one; type the bare subject and never a model token or version.

For the full module map, post-store hook contracts, T2 schema, and design heritage see [`docs/architecture.md`](docs/architecture.md). Module-local guidance (read the one for the area you edit): the `AGENTS.md` files in `src/nexus/catalog/`, `src/nexus/db/`, `src/nexus/mcp/`, `src/nexus/daemon/`, `docs/rdr/` and `tests/`.

## Critical conventions

- **Python 3.12+** — use `match/case`, `tomllib`, `typing.Protocol`, walrus freely.
- **Type hints on every public API.** Module-level constants too.
- **No ORM.** Raw SQL. (SQLite is a migration source and maintenance path only: see the NO-SQLITE hot rule.)
- **Composition over inheritance.** Protocols, not deep hierarchies. Constructor injection — no global singletons, no service locators.
- **TDD.** Test file before implementation. Deterministic: seeded randomness, fixed clocks, `port=0` for dynamic allocation.
- **Gates fail loud on absent dependencies.** A gate that skip-passes when
  its dependency is absent must carry a max-skip / non-vacuity assert; a
  sweep that found nothing to check is a failure, not a pass (the
  nexus-moht0 vacuous-gate doctrine).
- **Integration over mocks.** Hit real substrates — mocks hide boundary bugs. New persistence targets PG via the engine (NO-SQLITE hot rule), so its tests hit PG, never a new SQLite fixture; a surviving SQLite maintenance path is tested against a real tmp-path SQLite.
- **Structured logging only.** `structlog.get_logger(__name__)`. Never `print()` in library code; CLI commands use `click.echo()`.
- **`uv` as package manager.** `pyproject.toml` for deps. Don't bump `llama-index-core` or `tree-sitter-language-pack` without exercising the chunking pipeline — they have known breaking incompatibilities.

## Hot rules (don'ts paired with dos)

Full text with history: [hot-rules](docs/agents/hot-rules.md).

- **⛔ NO new SQLite — nexus is MIGRATING from SQLite TO PG, in EVERY mode** (Sam's directive; record: hot-rules). SQLite is a migration SOURCE only, never a destination. Never add a SQLite table, database file, or `CREATE TABLE` bootstrap in Python; new persistent state goes to PG through Liquibase via the engine. In review, a diff adding SQLite DDL or a new `sqlite3.connect` substrate is a **Critical**. Exemptions are Sam's explicit decisions, never code comments. There is no path back to the Chroma/SQLite era: no downgrade, no rollback, no probe of leftover `.db` files or Chroma directories; `SQLITE_CONNECT_ALLOWLIST` is empty and stays empty.
- **`develop` is releasable; Chroma is gone.** Pre-PG installs: `docs/migration-runbook.md` § Installs that predate Postgres (the redirect path is in hot-rules).
- **Integration branch is `develop`.** Open PRs against `develop`, not `main`. Releases promote `develop` to `main` via a PR-gated release branch (nexus-mkj6u); there are NO direct-to-`main` commits at all (`docs/contributing.md` § Release Process).
- **Never `git add -A` or `git add .`.** Stage by explicit path so untracked drafts don't sneak in.
- **Push `develop` only through `scripts/git-push-develop.sh <sha>...`**: it refuses unless `origin/develop..develop` equals the commits you name (worktree rule 8 below). Never `git commit --amend` in the primary checkout. See `docs/contributing.md` § Git Workflow.
- **Never include AI attribution in commits.** No "Generated with Claude", no `Co-Authored-By: Claude`. Bead references and `Closes #N` only.
- **Never delete RDR files.** Closing an RDR is a frontmatter `status: closed` flip — the file stays. See [`docs/rdr/AGENTS.md`](docs/rdr/AGENTS.md). Closed vocabularies (RDR status, and future ones) are CHECKED TABLES in `src/nexus/tables/`, not prose.
- **A review gate has a stated end and a triage.** Write "Ship when <X>" in the relay before dispatching a reviewer, run both `code-review-expert` and `substantive-critic`, triage each finding as a ship-blocker or a bead (never both), and paste a new check's first live run (counts examined and skipped) into its bead before close. Mechanics: `orchestration` skill § Review Gate, § Review Rounds, § Implementer and Debugger Dispatch.
- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime.
- **Worktree-dispatched agents run `scripts/agent-worktree-preflight.sh` first and stop on `PREFLIGHT_FAIL`.** The conexus SubagentStart hook injects this into every dispatch; reason and recovery: hot-rules.
- **Daemon-lifecycle fixes land in the shared primitive (`src/nexus/daemon/service_registry.py`), never one tier's copy.** See [`src/nexus/daemon/AGENTS.md`](src/nexus/daemon/AGENTS.md).

## Situational rules (`.claude/rules/`)

These load only when you read a file their `paths:` matches; each is the short-rules form of a former section of this file, and the full text is in [`docs/agents/`](docs/agents/README.md):

- [`releases-and-engine.md`](.claude/rules/releases-and-engine.md): release cadence, plugin channel, engine-service release, cutting a release and its blocking Step 0. The `release` and `engine-release` skills stay the authority.
- [`ci-and-runners.md`](.claude/rules/ci-and-runners.md): CI Cost Discipline, self-hosted runners and fork PRs.
- [`external-service-limits.md`](.claude/rules/external-service-limits.md): `QUOTAS` limits and Voyage limits.
- [`adding-a-cli-command.md`](.claude/rules/adding-a-cli-command.md).

These always-on rules are repeated here because no file read triggers them:

- **Agents never approve a fork-PR run** (`POST /actions/runs/{id}/approve`), even holding the owner's `gh` token. Report the run id and stop; the click is Sam's.
- **Never stop processes by pattern on a shared pool user** (chas/qwent `nxtest`): no `pkill -f`, `pkill -u`, `killall` or `kill $(pgrep ...)`; they kill other sessions' work. Stop your own lab-run, or kill one pid you checked with `ps`; report strays to the labrun session. Full text: [labrun README § Stopping work on a shared user](https://github.com/Hellblazer/labrun/blob/main/README.md#stopping-work-on-a-shared-user) (labrun RDR-001 F13).

## Worktrees: one session, one worktree

**Every session works in its own worktree on its own branch. The shared primary checkout is not edited.** Full text, incidents and recipes: [worktrees](docs/agents/worktrees.md).

1. **Site worktrees as siblings**: `../nexus-wt/<session>`, branch `feature/<bead-id>-<slug>`.
2. **The primary KEEPS `develop` checked out** and is the reference checkout (git then refuses a second checkout of `develop`).
3. **In the primary: no edits, no commits, no staging, no branch switches.** Build and test output is fine.
4. **The release battery runs IN THE RELEASE WORKTREE, never the primary** (`tests/e2e/release-battery.sh` refuses otherwise; `NX_BATTERY_ALLOW_DEVELOP=1` opts a non-release sweep back in).
5. **ONE FULL SUITE PER BOX AT A TIME, announced on the bus.** Concurrent `pytest -n auto` runs exhaust the SysV shared-memory budget. The suite lease (taken by any run whose `NX_TEST_T2_SUBSTRATE` is not `none`, a scoped single-test run included) makes a second such run QUEUE behind the first, up to 30 minutes: it prints who it waits for and starts when the holder exits (`NX_SUITE_LEASE_WAIT=0` refuses at once instead). So a scoped substrate run is not unaffected; it waits behind a full suite. On macOS an xdist run whose substrate is not `none` and that has no lab-run grant is refused with the `lab-remote` command to use (`NX_LOCAL_HEAVY=1` opts out). Inside a lab-run grant on Linux the lease is skipped and this rule is NOT enforced by it: lab-run admits by CPU and memory, so two `lab-l` suites can run together on chas. Keep one full suite per box by hand there. On macOS a grant (`lab-mac`) is admission only, so the lease is still taken and a VM hold queues a suite.
6. **A NEW WORKTREE RUNS `scripts/build-gate-jar.sh` BEFORE ITS FIRST SUBSTRATE-BACKED TEST** (Quick start; a setup-error wall also means rule 5's shared-memory exhaustion or a jar rewritten mid-run).
7. **Before pushing, ask whether a run someone is waiting on is in flight.** Read checks with `gh api repos/Hellblazer/nexus/commits/<sha>/check-runs`; never `gh run watch`, never a polling loop (secondary rate limit).
8. **Push unchanged**: `NX_PUSH_SOURCE=HEAD scripts/git-push-develop.sh <sha>...` from INSIDE the worktree (it reads HEAD from cwd). `PUSH_REFUSED_LOCK_HELD` names the holder; `NX_PUSH_SKIP_LOCK='<reason>'` is the named escape. **Batch develop pushes**: develop CI runs one run at a time, so hold green commits and push them together, alone only for a develop red or a peer who is waiting.
9. **Whoever pushes to `develop` fast-forwards the primary in the same breath:** `git merge --ff-only origin/develop` there. If the primary is dirty, do NOT force it; say so on the bus.
10. **Never hand-run `nx index repo` from a worktree.**
11. **Serena:** a session that did not START in the worktree must never use a Serena write tool (it can silently write the primary). Use Edit/Write with absolute worktree paths, then `git status --short`.
12. **Default test host is `chas`** (`ssh chas-test`); `hellmini` is the second. Inside a lab-run grant `-n auto` follows the grant, clamped to its memory (lab-l: 15 workers, `tests/_xdist_cap.py`). Recipes: T2 `nexus/chas-test-host-howto`, `nexus/hellmini-second-test-host-howto`.
13. **Hand runs are targeted, and no tree gets two green full runs** (nexus-clmlb). Iterate on the changed modules' tests (a path, `-k` or `-m`). A green full run of a tree (same sha, clean) is not repeated by anyone, whoever ran it: hand on the sha and the status file; once pushed, develop CI's verdict is the run. A fix round reruns its reds plus its change class's tripwire. A hand full suite is announced with the change class it is for, the tree sha, and "no green full run of this tree exists"; "pre-push" alone is not a reason. On chas a heavy command runs as `lab-run -c <class> -- <command>` so it queues behind CI: full suites as `lab-run -c lab-l -- python3 scripts/watched_pytest.py ...`; engine builds and suites are the exception, through `scripts/mvnw-leased.sh --lab-run <class>` (or `scripts/build-gate-jar.sh --lab-run <class>` for the gate jar), never wrapped in `lab-run`, so the build lease is taken before the grant. [test-running](docs/agents/test-running.md) § Hand runs and develop CI.

**Moving an in-flight session:** apply or cherry-pick into the new worktree and verify there FIRST, then revert the primary, never the reverse. **A remote pytest hand run goes through `scripts/watched_pytest.py`** (`--stall 180 --status <file> -- uv run pytest ...`); read the status file, never sleep and guess. Read long runs with `head` as well as `tail`.

## Task tracking

Use **beads** (`bd`) for issue tracking. Find work with `bd ready`; claim with `bd update <id> --claim`; close with `bd close <id>`. Use `nx memory put` for project-context notes that persist across sessions. See `docs/contributing.md` § Git Workflow for branch naming (`feature/<bead-id>-<description>`).

## Cross-session board

Each session keeps one T2 entry in project `threads`, titled with its session name (from ListAgents), updated with `memory_put` (no ttl) at phase boundaries (a thread starts, a push, a wait on someone, done), not per step. One `## <the work in plain words>` block per open thread, with `ref:`, `state:`, `waiting_on:` and `needs_sam:` lines; delete the block when the work is done. Each field is one line. A decision only Sam can make goes in `needs_sam:` and is also a bead labelled `needs-sam`, with that bead's id in the thread's `ref:` so the board shows it once. Sam reads the board in `nx console`, Threads tab.

## Settings

User-global permission settings live in `~/.claude/settings.json`. Never write to `settings.local.json` — it must remain `{}`.
