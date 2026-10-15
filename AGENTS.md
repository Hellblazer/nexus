# AGENTS.md

Project guidance for AI coding agents working in this repository. `CLAUDE.md` is a symlink to this file.

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as `conexus`; the CLI entry point is `nx` (`src/nexus/` is the package).

**Guidance precedence:** workflow routing — skills-first, agent dispatch, storage-tier checks, review paths, orchestration — is owned by the conexus plugin's injected guidance (`using-nx-skills` at SessionStart, the subagent-start preflight, the orchestration skill). This file and any personal CLAUDE.md yield to the plugin on workflow; they carry repo facts, hot rules, and durable authorizations. A restriction here that contradicts the plugin's workflow layer is a defect to surface, not a tiebreak to silently win.

**This file keeps the rules to obey on every turn.** History, incident archives, measurements and procedures live in [`docs/agents/`](docs/agents/README.md), moved verbatim on 2026-10-14 (nexus-2ziaz); each section below links its full text.

## Quick start

```bash
uv sync                                  # install deps
scripts/reinstall-tool.sh                # install nx CLI locally (preserves extras)
uv run pytest -n auto                    # full unit suite, ~13min parallel (no API keys needed; auto-capped)
uv run pytest                            # serial fallback (~14min; debugging only)
uv run pytest -m lint                    # O(repo) meta-tests (out of hot loop, PR-gated in CI)
uv run pytest -m integration             # E2E (requires .env from .env.example)
uv sync && scripts/reinstall-tool.sh && nx --version    # after edits
```

- `-n auto` caps itself to the box's SysV shared-memory budget (each xdist worker holds a segment). A wall of substrate setup errors means look at `ipcs -m` before the diff; `NX_XDIST_NO_CAP=1` opts out.
- After any pull/rebase or edit touching `service/` (a Java comment or changelog XML counts; the gate is mtime-keyed), run `scripts/build-gate-jar.sh`. Session start refuses with exit 75 on a stale jar. Never backdate mtimes.
- Unit tests use `InMemoryVectorClient` + bundled ONNX MiniLM (no keys, no network); engine-substrate tests self-provision a local service or skip. Test-authoring directives live in [`tests/AGENTS.md`](tests/AGENTS.md). Full text: [test-running](docs/agents/test-running.md).

## Architecture at a glance

Three storage tiers, by lifetime. **ChromaDB is not a live substrate in any mode** (RDR-155 P4b, 2026-07-25 — dependency dropped, absent from `uv.lock`):

- **T1** — service-backed session scratch (`HttpScratchStore`; `nx scratch`), PG-only. `NX_T1_ISOLATED` is retired (nexus-4lkmz, 2026-08) — setting it hard-fails with `T1IsolatedLegRetiredError`; there is no in-process opt-out.
- **T2** — nine domain stores behind a `T2Database` facade, all HTTP clients over the engine's PG tables. Persistent notes, plans, taxonomy, telemetry, chash, aspects, aspect queue, DEVONthink highlights, tuples.
- **T3** — `HttpVectorClient` over the nexus-service `/v1/vectors` (pgvector) in both modes: local = bundled PG17+pgvector embedding bge-768 by default, or Voyage when `NX_VOYAGE_API_KEY` reaches the service (nexus-umm29 carve-out; one posture per boot, see RDR-210); cloud = managed service + Voyage always. Permanent knowledge (`nx store`, `nx search`).

T1 rules ([full contract](docs/agents/t1-sub-agent-contract.md); rule of record `docs/rdr/joint/JDR-001-t1-three-scopes.md`): Agent-tool sub-agents share the parent's T1; `claude -p` sub-processes default to `owned` and opt in with `share_t1=True`. Cross-process findings and durable write-back go to T2 (`memory_put`).

Collection prefixes coexist in one T3 database. Always `__` (double underscore) as separator (colons are invalid in ChromaDB collection names). Conformant collection-name shape (RDR-103) is `<content_type>__<owner_id>__<embedding_model>__v<n>`, e.g. `code__nexus-1-1__voyage-code-3__v1`:

| Prefix | Embedder | Document identity (catalog) | Chunk natural ID (T3) |
|---|---|---|---|
| `code__*` | `voyage-code-3` | `source_uri` (file path) | `chunk_text_hash` (full 64-hex; 32 bytes stored — RDR-180) |
| `docs__*`, `rdr__*` | `voyage-context-3` (CCE) | `source_uri` (file path) | `chunk_text_hash` (full 64-hex) |
| `knowledge__*` | `voyage-context-3` | `source_uri` then `title` (fallback for MCP-stored notes) | `chunk_text_hash` (full 64-hex) |

**Catalog/T3 split (RDR-108, widths per RDR-180)**: Catalog Documents are graph nodes addressed by tumblers; T3 chunks are content-addressed by the FULL `sha256(chunk_text)` (64 hex on the wire, 32 bytes stored). Document structure lives in the catalog `document_chunks` manifest, never in chunk metadata; identical chunk text in one collection collapses to one T3 row by design. [Full text](docs/agents/catalog-t3-split.md).

**Creating or naming a collection** follows [`docs/collections.md`](docs/collections.md): `code`/`docs`/`rdr` are minted only by `nx index repo`; a `knowledge` collection is a durable subject area (`distributed-systems`), never a document, session, task, source app, or placeholder (`default`, `knowledge`, `test`); reuse an existing subject before creating one; type the bare subject and never a model token or version.

For the full module map, post-store hook contracts, T2 schema, and design heritage see [`docs/architecture.md`](docs/architecture.md). Module-local guidance: the `AGENTS.md` files in `src/nexus/catalog/`, `src/nexus/db/`, `src/nexus/mcp/`.

## Critical conventions

- **Python 3.12+** — use `match/case`, `tomllib`, `typing.Protocol`, walrus freely.
- **Type hints on every public API.** Module-level constants too.
- **No ORM.** Raw SQL. (Existing T2 SQLite code: raw `sqlite3`, WAL on open — maintenance only, see the NO-SQLITE hot rule.)
- **Composition over inheritance.** Protocols, not deep hierarchies. Constructor injection — no global singletons, no service locators.
- **TDD.** Test file before implementation. Deterministic: seeded randomness, fixed clocks, `port=0` for dynamic allocation.
- **Gates fail loud on absent dependencies.** A gate that skip-passes when
  its dependency is absent must carry a max-skip / non-vacuity assert; a
  sweep that found nothing to check is a failure, not a pass (the
  nexus-moht0 vacuous-gate doctrine).
- **Integration over mocks.** Hit real substrates — mocks hide boundary bugs. For existing SQLite-backed stores that means a real tmp-path SQLite (maintenance only); NEW persistence targets PG via the engine (see the NO-SQLITE hot rule), so its tests hit PG, not a new SQLite fixture.
- **Structured logging only.** `structlog.get_logger(__name__)`. Never `print()` in library code; CLI commands use `click.echo()`.
- **`uv` as package manager.** `pyproject.toml` for deps. Don't bump `llama-index-core` or `tree-sitter-language-pack` without exercising the chunking pipeline — they have known breaking incompatibilities.

## External service limits — check before every call

The single source of truth is `src/nexus/db/limits.py` (`QUOTAS: ServiceLimits`). Only `SAFE_CHUNK_BYTES` and `MAX_QUERY_RESULTS` have module-level aliases; the rest are `QUOTAS.<FIELD>`.

| Operation | Limit | Constant |
|---|---|---|
| paging (`limit=N`) | N ≤ 300 | `MAX_QUERY_RESULTS` |
| query (`n_results=N`) | N ≤ 300 | `MAX_QUERY_RESULTS` |
| batch write (`ids=[...]`) | ≤ 300 records | `QUOTAS.MAX_RECORDS_PER_WRITE` |
| Concurrent reads / writes per collection | ≤ 10 each | `QUOTAS.MAX_CONCURRENT_READS/WRITES` |
| Document size | ≤ 16384 bytes | `QUOTAS.MAX_DOCUMENT_BYTES` (use `SAFE_CHUNK_BYTES = 12288`) |
| Query string | ≤ 256 chars | `QUOTAS.MAX_QUERY_STRING_CHARS` |
| `where` predicates | ≤ 8 top-level | `QUOTAS.MAX_WHERE_PREDICATES` |
| Embedding dims | ≤ 4096 | `QUOTAS.MAX_EMBEDDING_DIMENSIONS` |

Voyage AI: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens, 1,000 inputs per request (`VoyageEmbedder.MAX_BATCH_TEXTS`). Use `nexus.retry._voyage_with_retry` for transient failures. Pagination over a large collection: `limit ≤ 300` per call, `offset += 300` in a loop. Provenance and the retired-client notes: [external-service-limits](docs/agents/external-service-limits.md).

## Hot rules (don'ts paired with dos)

Full text with history: [hot-rules](docs/agents/hot-rules.md).

- **⛔ NO new SQLite — nexus is MIGRATING from SQLite TO PG, in EVERY mode** (Hal directive 2026-07-18; T2 `nexus/directive-no-sqlite-pg-everywhere`). SQLite is a migration SOURCE only, never a destination. Never add a SQLite table, database file, or `CREATE TABLE` bootstrap in Python; new persistent state goes to PG through Liquibase via the engine. In review, a diff adding SQLite DDL or a new `sqlite3.connect` substrate is a **Critical**. Exemptions are Hal's explicit decisions, never code comments. There is no path back to the Chroma/SQLite era: no downgrade, no rollback, no probe of leftover `.db` files or Chroma directories; `SQLITE_CONNECT_ALLOWLIST` is empty and stays empty.
- **Never `print()` in library code.** Use `structlog.get_logger(__name__).info(event=..., **fields)`.
- **`develop` is releasable; Chroma is gone.** Pre-PG installs redirect through `conexus==6.18.1` (`nx guided-upgrade`, against a LOCAL engine): `docs/migration-runbook.md` § Installs that predate Postgres.
- **Integration branch is `develop`.** Open PRs against `develop`, not `main`. Releases promote `develop` to `main` via a PR-gated release branch (nexus-mkj6u); there are NO direct-to-`main` commits at all (`docs/contributing.md` § Release Process).
- **Never `git add -A` or `git add .`.** Stage by explicit path so untracked drafts don't sneak in.
- **Push `develop` only through `scripts/git-push-develop.sh <sha>...`** (nexus-9wxu6): it refuses unless `origin/develop..develop` equals the commits you name. `NX_PUSH_SOURCE=HEAD` from a worktree pushes your commits without touching the local branch. Never `git commit --amend` in the primary checkout. See `docs/contributing.md` § Git Workflow.
- **Never include AI attribution in commits.** No "Generated with Claude", no `Co-Authored-By: Claude`. Bead references and `Closes #N` only.
- **Never delete RDR files.** Closing an RDR is a frontmatter `status: closed` flip — the file stays. See [`docs/rdr/AGENTS.md`](docs/rdr/AGENTS.md). Closed vocabularies (RDR status, and future ones) are CHECKED TABLES in `src/nexus/tables/`, not prose.
- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime.
- **Worktree-dispatched agents run `scripts/agent-worktree-preflight.sh [required-sha]` as their FIRST action and stop on any `PREFLIGHT_FAIL` line** (nexus-5kwkf: the harness cuts worktrees from the default branch's tip, so one can be stale by construction).
- **Daemon-lifecycle fixes land in the shared primitive (`src/nexus/daemon/service_registry.py`), never one tier's copy.** See [`src/nexus/daemon/AGENTS.md`](src/nexus/daemon/AGENTS.md).

## CI Cost Discipline

Project policy, cited by name from the workflows. Full text and origin: [ci-cost-discipline](docs/agents/ci-cost-discipline.md).

- **Never test the same tree twice.** A tree that passed a PR's required checks does not re-run on the merge-push or at tag time; tag/release workflows publish, they do not re-test.
- **Expensive jobs run only when their inputs changed** (per-job path filters). Required checks stay satisfied via job-level skip or always-run-report-skip WITH a non-vacuity assert.
- **Never rebuild deterministic artifacts.** Version-pinned compiles are cached or prebuilt, keyed on exact inputs.
- **BILLED macOS/premium runners only where the artifact requires the platform** (release/tag builds), never in routine push/PR CI. Self-hosted runners on owned hardware are the exception.
- **Every workflow has a concurrency group; superseded runs cancel.**
- **Full matrix breadth only at merge boundaries.** PRs run the full matrix; interior pushes run the minimal representative.
- **Tag cadence is a cost decision.** Batch related work into one cut.

## Self-hosted runners and fork PRs

Full text (runner inventory, lab pool, hellmini, win-release): [self-hosted-runners](docs/agents/self-hosted-runners.md).

- CI runs on GitHub-hosted runners except two lab-pool jobs (Service CI Java on `lab-16c16g`, pytest shards on `lab-6c18g`) and the release legs on `hellmini` / `win-release`. **A pull request from a fork never runs on those:** a new pool job must carry the same `runs-on` expression that sends fork PRs to `ubuntu-latest`. Repo variable `NX_CI_POOL=off` sends every run back to `ubuntu-latest`.
- Pool runners share a persistent `$HOME`: a step that writes a fixed path under it must be safe against a sibling job. No sudo; hard memory cap (exit 137 = over it).
- **Before approving a fork-PR run, read its diff for:** anything under `.github/`; any `runs-on` naming a self-hosted label (`self-hosted`, `hellmini`, `win-release`, a `lab-*` label, `hellmini-ci`, `qwen-linux`, `gtr-windows`); any change to the `runs-on` expression that routes forks to `ubuntu-latest`.
- **Agents never approve a fork-PR run** (`POST /actions/runs/{id}/approve`), even holding the owner's `gh` token. Report the run id and stop; the click is Sam's.
- **Read a contributor change as release-runner code, not only a fork-PR run:** merged code on `win-release` runs without a further click. Read in full what it executes if it touches `windows-pg-bundle-rehearsal.yml`'s `paths:` filter (including `service/pom.xml`), `scripts/*windows*`, `scripts/windows_*.py`, `.github/actions/windows-*`, `.github/actions/resolve-windows-python/**`, or the Windows jobs of `engine-service-release.yml` and `pg-bundle-cache-seed.yml`.
- Registering `win-release` or flipping `NX_WINDOWS_RELEASE_LEGS` needs Sam's explicit go.
- On chas run a heavy suite as `lab-run -c <class> -- <command>` so it queues behind CI.

## Workflows

### Adding a CLI command

1. Create `src/nexus/commands/your_cmd.py` with a Click group/command.
2. Register it in `src/nexus/cli.py` via `cli.add_command()`.
3. Add tests in `tests/test_your_cmd.py`.
4. Document the new flags/subcommands in `docs/cli-reference.md`.

### Release cadence policy (nexus-mkj6u)

Full text: [release-cadence](docs/agents/release-cadence.md).

1. **Releases are hand-cut.** CI does not publish on merge; a tag push publishes.
2. **`source.ref` only ever points at immutable release tags.** Never a branch, never main HEAD.
3. **One channel until proven otherwise.** No `-dev` / `-rc` / `-canary` variants.
4. **Bump cadence matches user-visible impact, not commit volume.**
5. **Releaser is human. AI prepares; human cuts** (merge, tag, push tag).
6. **Parity tests stay strict.** `pyproject.toml` version must match `mcpb/pyproject.toml`, `mcpb/manifest.json`, marketplace.json `plugins[].version` and `source.ref`, every plugin's `plugin.json`, and `uv.lock`. No `# noqa` escape hatches.

### Independent plugin release channel (RDR-197)

Full text: [plugin-release-channel](docs/agents/plugin-release-channel.md). Plugin-surface content (`conexus/`, `sn/`, `.claude-plugin/marketplace.json`) ships without a client release via an anchored tag `plugin-v{X.Y.Z}-{n}`. The cut is `scripts/cut_plugin_release.py`; the post-cut back-merge is `scripts/plugin_cut_back_merge.sh`, never a bare merge. Never cut while a release PR is open; batch related plugin work. **Rehearse before every real cut:** `tests/e2e/plugin-cut-rehearsal/run.sh` must end `PLUGIN-CUT REHEARSAL PASSED`.

### Engine-service release (a SECOND lifecycle — decoupled from the PyPI release)

**Authority and the full incident archive: [engine-service-release](docs/agents/engine-service-release.md)** (moved verbatim; it is not a summary). Invoke the `engine-release` skill as the executable checklist.

- The Java engine-service binary is cut by an `engine-service-vX.Y.Z` tag, separate from the PyPI release and not gated by it. **Prep is AI, the tag push is human (Sam).** Version is tag-stamped; there is no manifest to bump.
- Build or test the engine only through `scripts/mvnw-leased.sh`, never a bare `./mvnw`/`mvn` (one builder at a time).
- **ONE engine identity per release:** bump `REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py` unconditionally to the engine the release was built and gated with. It also derives `PINNED_SERVICE_TAG`. Local-mode installs get only what that constant names.
- **Paired release:** cut the engine tag FIRST (a tag cut is never blocked); the client release bumps the pin in the same release; the deploy fires before or at client-tag push, never "eventually". `scripts/check_engine_release_floor.py --client-precondition engine-service-vX.Y.Z` gates the DEPLOY, never the tag cut.
- Deploy and cloud-gate are conexus-side: surface "relay: deploy `engine-service-vX.Y.Z` + re-gate" to Sam, never as autonomous.
- After every cloud deploy run `tests/e2e/cloud-client-path-gate.sh` from a cloud-mode box with `NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only` (or `=enforce` after the flip); plain, it FAILS leg B3.

### Cutting a release (version bump + tag-push to PyPI)

**The `release` skill is the authority and the executable checklist.** Invoke it (`/conexus:release`, or `Skill` on `release`); this section is a pointer. `docs/contributing.md#release-process` is the long form for rollback and one-time setup. Full text: [cutting-a-release](docs/agents/cutting-a-release.md).

**Step 0 stays here because other sections cite it.** The engine-freshness gate is BLOCKING and is a command, not a prose eyeball-check (nexus-i5c2u):

```bash
uv run python scripts/check_engine_release_floor.py
```

Non-zero means STOP. It fails in both directions — a cloud behind the pinned identity, and a gated engine tag that was never pinned — and the remedy differs per direction. The skill's Step 0 carries both, the `--paired-deploy` form for a paired release (nexus-k1c08), and the post-tag verify obligation.

## Worktrees: one session, one worktree (2026-09-19)

**Every session works in its own worktree on its own branch. The shared primary checkout is not edited.** Full text, incidents and recipes: [worktrees](docs/agents/worktrees.md).

1. **Site worktrees as siblings**: `../nexus-wt/<session>`, branch `feature/<bead-id>-<slug>`.
2. **The primary KEEPS `develop` checked out** and is the reference checkout (git then refuses a second checkout of `develop`).
3. **In the primary: no edits, no commits, no staging, no branch switches.** Build and test output is fine.
4. **The release battery runs IN THE RELEASE WORKTREE, never the primary** (`tests/e2e/release-battery.sh` refuses otherwise; `NX_BATTERY_ALLOW_DEVELOP=1` opts a non-release sweep back in).
5. **ONE FULL SUITE PER BOX AT A TIME, announced on the bus.** Concurrent `pytest -n auto` runs exhaust the SysV shared-memory budget. Scoped runs are unaffected.
6. **A NEW WORKTREE RUNS `scripts/build-gate-jar.sh` BEFORE ITS FIRST SUBSTRATE-BACKED TEST.** A setup-error wall means shared-memory exhaustion (rule 5; check `ipcs -m`) or a jar rewritten mid-run.
7. **Before pushing, ask whether a run someone is waiting on is in flight.** Read checks with `gh api repos/Hellblazer/nexus/commits/<sha>/check-runs`; never `gh run watch`, never a polling loop (secondary rate limit).
8. **Push unchanged**: `NX_PUSH_SOURCE=HEAD scripts/git-push-develop.sh <sha>...` from INSIDE the worktree (it reads HEAD from cwd). `PUSH_REFUSED_LOCK_HELD` names the holder; `NX_PUSH_SKIP_LOCK='<reason>'` is the named escape.
9. **Whoever pushes to `develop` fast-forwards the primary in the same breath:** `git merge --ff-only origin/develop` there. If the primary is dirty, do NOT force it; say so on the bus.
10. **Never hand-run `nx index repo` from a worktree.**
11. **Serena:** a session that did not START in the worktree must never use a Serena write tool (it can silently write the primary). Use Edit/Write with absolute worktree paths, then `git status --short`.
12. **Default test host is `chas`** (`ssh chas-test`; `-n auto` = 32 workers, do not raise it); `hellmini` is the second. Recipes: T2 `nexus/chas-test-host-howto`, `nexus/hellmini-second-test-host-howto`.

**Moving an in-flight session:** apply or cherry-pick into the new worktree and verify there FIRST, then revert the primary, never the reverse. **A remote pytest hand run goes through `scripts/watched_pytest.py`** (`--stall 180 --status <file> -- uv run pytest ...`); read the status file, never sleep and guess. Read long runs with `head` as well as `tail`.

## Task tracking

Use **beads** (`bd`) for issue tracking. Find work with `bd ready`; claim with `bd update <id> --claim`; close with `bd close <id>`. Use `nx memory put` for project-context notes that persist across sessions. See `docs/contributing.md` § Git Workflow for branch naming (`feature/<bead-id>-<description>`).

## Settings

User-global permission settings live in `~/.claude/settings.json`. Never write to `settings.local.json` — it must remain `{}`.
