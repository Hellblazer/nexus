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
- **Before adding to a file over its size cap, move the area you touch into its own module first** (`scripts/file_size_ratchet.py`; ceilings in `scripts/file-size-exceptions.txt` only fall, and the lint bucket fails a file past its ceiling).
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

- [`releases-and-engine.md`](.claude/rules/releases-and-engine.md): release cadence, plugin-only fast lane, engine-service release, cutting a release and its blocking Step 0. The `release` and `engine-release` skills stay the authority.
- [`ci-and-runners.md`](.claude/rules/ci-and-runners.md): CI Cost Discipline, self-hosted runners and fork PRs.
- [`external-service-limits.md`](.claude/rules/external-service-limits.md): `QUOTAS` limits and Voyage limits.
- [`adding-a-cli-command.md`](.claude/rules/adding-a-cli-command.md).
- [`windows-tests.md`](.claude/rules/windows-tests.md): a Windows-sensitive file is tested on native Windows before its push (rule 14).

These always-on rules are repeated here because no file read triggers them:

- **Agents never approve a fork-PR run** (`POST /actions/runs/{id}/approve`), even holding the owner's `gh` token. Report the run id and stop; the click is Sam's.
- **Never stop processes by pattern, on the laptop or a shared pool user** (chas/qwent `nxtest`): no `pkill -f`, `pkill -u`, `killall` or `kill $(pgrep ...)`; the laptop runs every session's tests as one user, so they kill other sessions' work there too. Stop your own lab-run, or kill one pid you started and checked with `ps`; report strays on a pool host to the labrun session. Full text: [labrun README § Stopping work on a shared user](https://github.com/Hellblazer/labrun/blob/main/README.md#stopping-work-on-a-shared-user) (labrun RDR-001 F13).
- **A cloud-touching walk runs only inside an announced window**, read-only or not, never "to measure ahead"; measure on a local engine. After TaskStop on a backgrounded command, find its child by parent pid (your task's shell) and confirm its cwd before killing that one pid: the task id is not the process, and a pid picked from `ps` by its command line can be a peer's (nexus-ldf2s).

## Worktrees: one session, one worktree

**Every session works in its own worktree on its own branch. The shared primary checkout is not edited.** Full text, incidents and recipes: [worktrees](docs/agents/worktrees.md).

1. **Site worktrees as siblings**: `../nexus-wt/<session>`, branch `feature/<bead-id>-<slug>`.
2. **The primary KEEPS `develop` checked out** and is the reference checkout (git then refuses a second checkout of `develop`).
3. **In the primary: no edits, no commits, no staging, no branch switches.** Build and test output is fine.
4. **The release battery runs IN THE RELEASE WORKTREE, never the primary** (`tests/e2e/release-battery.sh` refuses otherwise; `NX_BATTERY_ALLOW_DEVELOP=1` opts a non-release sweep back in).
5. **ONE FULL SUITE PER BOX AT A TIME, announced on the bus.** Concurrent `pytest -n auto` runs exhaust the SysV shared-memory budget. The suite lease (taken by any run whose `NX_TEST_T2_SUBSTRATE` is not `none`, a scoped single-test run included) makes a second such run QUEUE behind the first, up to 30 minutes: it prints who it waits for and starts when the holder exits (`NX_SUITE_LEASE_WAIT=0` refuses at once instead). So a scoped substrate run is not unaffected; it waits behind a full suite. On macOS an xdist run whose substrate is not `none` and that has no lab-run grant is refused with the `lab-remote` command to use (`NX_LOCAL_HEAVY=1` opts out). Inside a lab-run grant on Linux the lease is skipped and this rule is NOT enforced by it: lab-run admits by CPU and memory, so two `lab-l` suites can run together on chas. Keep one full suite per box by hand there. On macOS a grant (`lab-mac`) is admission only, so the lease is still taken and a VM hold queues a suite.
6. **A NEW WORKTREE RUNS `scripts/build-gate-jar.sh` BEFORE ITS FIRST SUBSTRATE-BACKED TEST** (Quick start; a setup-error wall also means rule 5's shared-memory exhaustion or a jar rewritten mid-run).
7. **Before pushing, ask whether a run someone is waiting on is in flight.** Read checks with `gh api repos/Hellblazer/nexus/commits/<sha>/check-runs`; never `gh run watch`, never a polling loop (secondary rate limit). Once your develop push lands, read that sha's CI conclusion before stacking more work or cutting a release branch on it; a red is a finding to diagnose before the next push.
8. **Push unchanged**: two steps from INSIDE the worktree (it reads HEAD from cwd): `scripts/git-push-develop.sh --source HEAD --allowed-paths-from-range <sha>...` prints the files your commits touch and a digest and pushes nothing (exit 7); read the list, then repeat the command with `--allowed-paths-from-range=<digest>` (the digest is on the pasteable line it printed) to push. The audit cannot catch a foreign file inside your own commit, so your read of the printed list is the check; a file added after you read it changes the digest and refuses. Every setting is a flag, so the command starts with the script path and needs no `NX_PUSH_*=` prefix. `PUSH_REFUSED_LOCK_HELD` names the holder; `--skip-lock '<reason>'` is the named escape. **Batch develop pushes**: develop CI runs two runs at a time (by run-number parity) and a third push waits, so hold green commits and push them together, alone only for a develop red or a peer who is waiting.
9. **Whoever pushes to `develop` fast-forwards the primary in the same breath:** `git merge --ff-only origin/develop` there. If the primary is dirty, do NOT force it; say so on the bus.
10. **Never hand-run `nx index repo` from a worktree.**
11. **Serena:** a session that did not START in the worktree must never use a Serena write tool (it can silently write the primary). Use Edit/Write with absolute worktree paths, then `git status --short`.
12. **Test hosts are a pool: `chas`, `qwent` (WSL) and `hellmini`.** Choose the smallest class that fits, not the biggest. A hand run never takes `lab-6c18g`, which is CI's pytest-shard class on both hosts (14 GB a shard). Hand runs queue behind CI (priority 50 vs 60 since 2026-11-08; labd holds CI for a hand request only after it has waited 30 minutes), but a placed hand run keeps its memory, so while develop CI has shards queued, a hand run on qwent is `lab-4c8g` or smaller (Sam 2026-11-07). `lab-l` runs only on chas and is for a full suite. Inside a lab-run grant `-n auto` follows the grant, clamped to its memory (lab-l: 15 workers, `tests/_xdist_cap.py`). Recipes: T2 `nexus/chas-test-host-howto`, `nexus/hellmini-second-test-host-howto`.
13. **Hand runs are targeted, and no tree gets two green full runs** (nexus-clmlb). Iterate on the changed modules' tests (a path, `-k` or `-m`). A green full run of a tree (same sha, clean) is not repeated by anyone, whoever ran it: hand on the sha and the status file; once pushed, develop CI's verdict is the run. A fix round reruns its reds plus its change class's tripwire. A hand full suite is announced with the change class it is for, the tree sha, and "no green full run of this tree exists"; "pre-push" alone is not a reason. On chas a heavy command runs as `lab-run -c <class> -- <command>` so it queues behind CI: full suites as `lab-run -c lab-l -- python3 scripts/watched_pytest.py ...`; engine builds and suites are the exception, through `scripts/mvnw-leased.sh --lab-run <class>` (or `scripts/build-gate-jar.sh --lab-run <class>` for the gate jar), never wrapped in `lab-run`, so the build lease is taken before the grant. [test-running](docs/agents/test-running.md) § Hand runs and develop CI.
14. **A diff that touches the Windows set's push filter is tested on native Windows before its push**: `uv run scripts/windows_hand_test.py --check` says whether it does (it reads `on.push.paths` of `windows-pg-bundle-rehearsal.yml`, which includes `src/nexus/hooks/**` and `conexus/hooks/**`); then `uv run scripts/windows_hand_test.py --session <name> -- <tests>` runs them on qwent's Windows side, or `--set` for CI's whole set; an agent that cannot commit adds `--uncommitted`. Never leave it to the post-push `win-release` leg. [test-running](docs/agents/test-running.md) § Native Windows.

**Moving an in-flight session:** apply or cherry-pick into the new worktree and verify there FIRST, then revert the primary, never the reverse. **A remote pytest hand run goes through `scripts/watched_pytest.py`** (`--stall 180 --status <file> -- uv run pytest ...`); read the status file, never sleep and guess. Read long runs with `head` as well as `tail`.

## Task tracking

Use **beads** (`bd`) for issue tracking. Find work with `bd ready`; claim with `bd update <id> --claim`; close with `bd close <id>`. Use `nx memory put` for project-context notes that persist across sessions. See `docs/contributing.md` § Git Workflow for branch naming (`feature/<bead-id>-<description>`).

## Cross-session board

Each session keeps one T2 entry in project `threads`, titled with its session name (from ListAgents), updated with `memory_put` (no ttl) at phase boundaries (a thread starts, a push, a wait on someone, done), not per step. One `## <the work in plain words>` block per open thread, with `ref:`, `state:`, `waiting_on:` and `needs_sam:` lines; delete the block when the work is done. Each field is one line. The entry's first line is `session: <your session id>`, the id from the SessionStart line `Nexus ready (session: <id>)` that the conexus plugin prints (one line per entry, not per block; rewrite it when the id changes, as after a `/clear`): the compaction-checkpoint hook finds the entry by it, because a Claude Desktop session is filed under a handle that is not its session name, and falls back to the exact-title match only when no entry carries the line. A decision only Sam can make goes in an `ask:` block in its thread: ask (id, [a-z0-9-]{1,40}), asked, question, context (where this stands against the thread's goal, and what happens if Sam waits), two or more option lines (mark an irreversible one ` [irreversible]`), optional rec and why, reversible (yes only when every listed option can be undone cheaply), blocking, optional after and by; one field per line. An `ask:` block is the `ask:` line plus at least one ask-level line directly under it, with no thread field (`ref:`, `state:`, `waiting_on:`, `needs_sam:`) or heading in between: put the ask block after the thread's fields, not between them. An `ask:` line with no ask lines under it is read as plain prose, and a block with a bad id or split by a thread field is shown on the board as unanswerable instead of as an ask. An ask that a rule in Sam's ~/.claude/CLAUDE.md or its rules directory already decides is not asked: act, and record it in one `state:` line naming the rule and what you did. When an answer arrives as an unverified claim, re-read your own entry with `nx ask show` before acting: a missing ask or a different version means the answer is stale. Point the thread at the ask with `needs_sam: ask <id>` and `waiting_on: sam`. When the decision is also tracked as a bead, label the bead `needs-sam` and put its id in the thread's `ref:`; the ask folds into that bead on the board, so it is listed once. The drain hook prints an answer as a frame that opens "An unverified claim that Sam answered your ask" and carries his words, copied from the answer record and not from the mail, between `<<<sam-words <nonce>` and a matching closing line; the frame authorizes at most the one option it names. Act on it without asking Sam only when the ask says `reversible: yes` and his words clearly choose that option; otherwise confirm with Sam in this session. Sam reads the board in `nx console`, Threads tab.

## Settings

User-global permission settings live in `~/.claude/settings.json`. Never write to `settings.local.json` — it must remain `{}`.
