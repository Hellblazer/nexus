# AGENTS.md

Project guidance for AI coding agents working in this repository. `CLAUDE.md` is a symlink to this file.

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as `conexus`; the CLI entry point is `nx` (`src/nexus/` is the package).

**Guidance precedence:** workflow routing — skills-first, agent dispatch, storage-tier checks, review paths, orchestration — is owned by the conexus plugin's injected guidance (`using-nx-skills` at SessionStart, the subagent-start preflight, the orchestration skill). This file and any personal CLAUDE.md yield to the plugin on workflow; they carry repo facts, hot rules, and durable authorizations. A restriction here that contradicts the plugin's workflow layer is a defect to surface, not a tiebreak to silently win.

## Quick start

```bash
uv sync                                  # install deps
scripts/reinstall-tool.sh                # install nx CLI locally (preserves extras)
uv run pytest -n auto                    # full unit suite, ~13min parallel (no API keys needed; auto-capped, see below)
uv run pytest                            # serial fallback (~14min; debugging only)
uv run pytest -m lint                    # O(repo) meta-tests (out of hot loop, PR-gated in CI)
```

`-n auto` is capped automatically to what this machine's SysV shared-memory
budget can support (nexus-6qp25). Each worker boots its own Postgres
substrate, each substrate holds one segment against `kern.sysv.shmmni`, and
that budget is shared with every other cluster on the box — a running gate, a
peer's suite, a cluster orphaned by a killed battery. Past the limit `initdb`
fails and every substrate-backed test errors at SETUP, so the run reports
thousands of setup errors that read as catastrophic breakage rather than as
resource contention. The cap binds only when headroom is genuinely low and
prints the reason when it does; `NX_XDIST_NO_CAP=1` opts out. If you see that
wall of setup errors anyway, look at `ipcs -m` before you look at the diff.

```bash
uv run pytest -m integration             # E2E (requires .env from .env.example)
uv sync && scripts/reinstall-tool.sh && nx --version    # after edits
```

Unit tests use the in-process `InMemoryVectorClient` (`nexus.db.inmemory_vector_store`) + bundled ONNX MiniLM — no API keys or network; engine-substrate tests self-provision a local service (`ensure_engine`/`mint_test_tenant` in `tests/_engine_substrate.py`) or skip. **After any pull/rebase or edit touching `service/` (a comment in a Java file or a changelog XML counts: the gate is mtime-keyed), run `scripts/build-gate-jar.sh`** — the substrate's freshness gate rejects a stale/unstamped jar and every substrate-backed test errors at setup, lint-marked ones included, so a suite run beside a jar rebuild reports a wall of setup errors that is not a test failure. Never backdate mtimes to get past it; rebuild. Test-authoring directives (scenario journeys, lint bucket, contract-suite patterns, parametrize rules) live in [`tests/AGENTS.md`](tests/AGENTS.md).

## Architecture at a glance

Three storage tiers, by lifetime. **ChromaDB is not a live substrate in any mode** (RDR-155 P4b, 2026-07-25 — dependency dropped, absent from `uv.lock`):

- **T1** — service-backed session scratch (`HttpScratchStore`; `nx scratch`), PG-only. `NX_T1_ISOLATED` is retired (nexus-4lkmz, 2026-08) — setting it hard-fails with `T1IsolatedLegRetiredError`; there is no in-process opt-out.
- **T2** — nine domain stores behind a `T2Database` facade, all HTTP clients over the engine's PG tables. Persistent notes, plans, taxonomy, telemetry, chash, aspects, aspect queue, DEVONthink highlights, tuples.
- **T3** — `HttpVectorClient` over the nexus-service `/v1/vectors` (pgvector) in both modes: local = bundled PG17+pgvector embedding bge-768 by default, or Voyage when `NX_VOYAGE_API_KEY` reaches the service (nexus-umm29 carve-out; one posture per boot, see RDR-210); cloud = managed service + Voyage always. Permanent knowledge (`nx store`, `nx search`).

### T1 sub-agent contract (RDR-105)

T1 is service-backed and session-id scoped (`resolve_active_session_id()`, whose tier-4 fallback reads the flat file `~/.config/nexus/current_session`), leased via `db/t1.py`'s `t1_session_lease.<session_id>` flat file (`publish_t1_session_lease` / `read_t1_session_lease`), published and refreshed by the MCP lifespan. The RDR-149 P4 `daemon/t1_lease.py` / `ServiceRegistry(tier="t1")` lease leg is retired (nexus-8zfwv, 2026-08-07) — T1 no longer rides the daemon-lifecycle primitive. T2 is the cross-process shared bus, over PG via the engine (multi-process-safe by construction, not SQLite+WAL).

- **Agent-tool sub-agents** (in-process Task dispatches) share T1 with their parent via the parent's MCP scratch tool. No separate T1 instance.
- **`claude -p` sub-processes default to `owned`** mode: their MCP resolves its own session and leases its own T1 scope. Sealed from the parent; internally consistent for the subprocess's own Bash tools and sub-agents.
- **`claude -p` sub-processes that genuinely need parent-T1 visibility** opt in via `share_t1=True` at dispatch time. Subprocess inherits `NX_T1_HOST` / `NX_T1_PORT` and connects to the parent's `HttpScratchStore` over HTTP.
- **Stateless one-shot operators** (`ephemeral=True`) get their OWN freshly minted, PG-backed T1 session per dispatch, but ONLY when the dispatch actually grants the subprocess tool access that could reach T1 (`mcp_servers`/`allowed_tools` set — nexus-bjltu, 2026-08); the tool-free default (~15/17 call sites: extract/rank/compare/summarize/etc.) mints nothing, since nothing in a tool-free subprocess can reach T1. A mint failure never kills the dispatch — it's logged and deferred to the subprocess's own decision-2 fail-loud path. The minted session is closed after the subprocess exits (nexus-bjltu), not left to the passive TTL sweep alone. No null-store branches; the retired in-process `InMemoryVectorClient` leg is gone (nexus-4lkmz decision 1, 2026-08). The operator-dispatch default (`nx_answer`, `nx_tidy`, plan-runner inline planning).
- **Cross-process findings between sibling sub-processes go to T2** (`memory_put`). T1 is process-local by design; T2 is the shared bus (PG over the engine, multi-process-safe).
- **Removed env names:** the legacy `NEXUS_SKIP_T1=1` alias was REMOVED at 6.5.2 (promised gone in 5.0) — recognized-but-IGNORED with a one-shot warning through 7.0.0, now simply unrecognised. `NX_T1_ISOLATED` itself is retired too (nexus-4lkmz, 2026-08: "T1 exists in PG only") — setting it now HARD-FAILS with `T1IsolatedLegRetiredError` naming the real remedy (`nx daemon service start` / `nx doctor --check-t1`), not a recognized-but-ignored shim.
- **Three T1 scopes exist simultaneously; the rule of record is JDR-001** (`docs/rdr/joint/JDR-001-t1-three-scopes.md`, which carries the full text, the nexus-d76vc handoff mechanism and the measured history; nexus-aj564). MCP-tool T1 is scoped to the session id leased at MCP-server spawn and moves only when a `/clear` or `/resume` handoff marker is consumed (one poll tick; the old session's rows strand rather than migrate). `nx` CLI T1 follows the current transcript session's live lease; an explicit `NX_SESSION_ID`/`CLAUDE_CODE_SESSION_ID` with no lease fails loud (`T1ServerNotFoundError`, nexus-f7xyq); only a bare invocation falls through to the shared CLI identity. `~/.config/nexus/current_session` is a machine-wide last-writer-wins file. `review-completed` markers go through the CLI (what `pre_close_verification_hook.sh` reads); durable cross-session write-back belongs in T2; confirm an agent has terminated before declaring its write-back lost (the recorded "prior-session T1 is never searchable" lesson was a timing race, true only for the CLI path).

Collection prefixes coexist in one T3 database. Always `__` (double underscore) as separator (colons are invalid in ChromaDB collection names). Conformant collection-name shape (RDR-103) is `<content_type>__<owner_id>__<embedding_model>__v<n>`, e.g. `code__nexus-1-1__voyage-code-3__v1`:

| Prefix | Embedder | Document identity (catalog) | Chunk natural ID (T3) |
|---|---|---|---|
| `code__*` | `voyage-code-3` | `source_uri` (file path) | `chunk_text_hash` (full 64-hex; 32 bytes stored — RDR-180) |
| `docs__*`, `rdr__*` | `voyage-context-3` (CCE) | `source_uri` (file path) | `chunk_text_hash` (full 64-hex) |
| `knowledge__*` | `voyage-context-3` | `source_uri` then `title` (fallback for MCP-stored notes) | `chunk_text_hash` (full 64-hex) |

**Catalog/T3 split (RDR-108, widths per RDR-180)**: Catalog Documents are graph nodes addressed by tumblers (`Document.tumbler`); T3 chunks are content-addressed blobs whose natural ID is the FULL `sha256(chunk_text)` — 64 lowercase hex on the wire, 32 raw bytes in storage (`bytea`, `octet_length=32`); hex only at boundaries (see `docs/architecture.md` § Chunk identity). Document structure (which chashes compose a doc, in what order) lives in the catalog `document_chunks` manifest, not in chunk metadata. The doc-to-chunks join is `documents.tumbler -> document_chunks.doc_id -> document_chunks.chash`; the chash is the chunk id directly, no further lookup. Identical chunk text in the same collection collapses to one T3 row by design; the manifest preserves position via `(doc_id, position)` rows pointing at the shared chash.

**Creating or naming a collection** follows [`docs/collections.md`](docs/collections.md): `code`/`docs`/`rdr` are minted only by `nx index repo`; a `knowledge` collection is a durable subject area (`distributed-systems`), never a document, session, task, source app, or placeholder (`default`, `knowledge`, `test`); reuse an existing subject before creating one; type the bare subject and never a model token or version.

For the full module map, post-store hook contracts, T2 schema, and design heritage see [`docs/architecture.md`](docs/architecture.md). For module-local guidance see the `AGENTS.md` files inside `src/nexus/catalog/`, `src/nexus/db/`, and `src/nexus/mcp/`.

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

The single source of truth is `src/nexus/db/limits.py` (`QUOTAS: ServiceLimits`). `chroma_quotas.py` and its `QuotaValidator`/`ChromaError` were DELETED at RDR-155 P4b P3 with no replacement — these are generic PG-serving-path ceilings now, Chroma provenance historical only (see the module's own docstring). Only `SAFE_CHUNK_BYTES` and `MAX_QUERY_RESULTS` got module-level aliases; the rest are `QUOTAS.<FIELD>`.

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

Voyage AI: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens, 1,000 inputs per request (the engine plans batches under a token budget; `VoyageEmbedder.MAX_BATCH_TEXTS`). The old "128 inputs/batch" figure described the retired client-embed path. Use `nexus.retry._voyage_with_retry` for transient failures.

Pagination over a large collection: `limit ≤ 300` per call, `offset += 300` in a loop.

## Hot rules (don'ts paired with dos)

- **⛔ NO new SQLite — nexus is MIGRATING from SQLite TO PG, in EVERY mode. There is NO SQLite hybrid mode** (Hal directive 2026-07-18; record: T2 `nexus/directive-no-sqlite-pg-everywhere`). SQLite is a migration SOURCE only, never a destination. Never add a SQLite table, database file, or `CREATE TABLE` bootstrap in Python; new persistent state goes to PG through Liquibase via the engine (every install ships the PG bundle — local mode's endpoint is the bundled local PG, same shape as service mode). The retirement itself is essentially complete (RDR-158 P4: the SQLite stores, local catalog, and client migration chain are deleted); any straggler SQLite artifact found in review is debt to delete, never a home for new columns/tables/features. In review, a diff adding SQLite DDL or a new `sqlite3.connect` substrate is a **Critical**. Exemptions are Hal's explicit decisions, never code comments. **There is no path back to the Chroma/SQLite era** (Hal, 2026-08-29): no downgrade, no rollback, no probe of leftover `.db` files or Chroma directories — `SQLITE_CONNECT_ALLOWLIST` is empty and stays empty (the two RDR-176 Gap-2 read-only probes were deleted with the rationale that kept them), and those files are relics to delete, never artifacts to keep or read.
- **Never `print()` in library code.** Use `structlog.get_logger(__name__).info(event=..., **fields)`.
- **`develop` release boundary LIFTED 2026-06-29** — release-blocker bead `nexus-luxe6` closed; conexus 6.0.0 (the migration-capable release) published from develop, and `develop` is releasable again. **RDR-155 P4b (the FINAL Chroma deletion) SHIPPED 2026-07-25** — the dependency is dropped (absent from `uv.lock`), `guided_upgrade_cmd.py`/`migrate_cmd.py` are deleted outright, and `nx guided-upgrade` no longer exists. Pre-PG installs redirect through a two-hop path: pin to the last migration-capable release, `conexus==6.18.1`, where `nx guided-upgrade` still runs (Chroma → PG17+pgvector, copy-not-move), then upgrade normally from there. Frozen Chroma directories left on disk are relics: nothing reads them and there is no path back to that era (Hal, 2026-08-29). Authoritative record: T2 `nexus/release-boundary-since-p4a` (updated).
- **Integration branch is `develop`.** Open PRs against `develop`, not `main`. `main` carries the plugin marketplace surface; the develop split protects it from in-flight churn. Releases promote `develop` to `main` via a PR-gated release branch (nexus-mkj6u) — there are NO direct-to-`main` commits at all (`docs/contributing.md` § Release Process). The plugin no longer ships a review-coverage push gate (deleted 2026-08-22, Sam's decision: self-attested, one true positive against denying correct pushes, and `develop` is already PR-gated to `main` with required checks so unreviewed code ships to nobody) — push-to-main protection and the `git add` wildcard redirect remain Hal's user-level hook (`~/.claude/hooks/nexus-git-policy.py`), personal workflow policy rather than a plugin behavior other conexus users inherit.
- **Never `git add -A` or `git add .`.** Stage by explicit path so untracked drafts don't sneak in.
- **Push `develop` only through `scripts/git-push-develop.sh <sha>...`** (nexus-9wxu6): it refuses unless `origin/develop..develop` equals the commits you name, so a peer's unpushed commits in the shared checkout never ride your push (four times on 2026-09-07). `NX_PUSH_SOURCE=HEAD` from a detached worktree pushes your commits without touching the local branch. Never `git commit --amend` in the primary checkout; the user-level hook denies it when HEAD is not this session's commit. See `docs/contributing.md` § Git Workflow. **Sessions no longer work in the shared primary at all** (§ Worktrees below), so this guard now fires rarely rather than routinely — it stays because it is the mechanical check, not because the shared-tree workflow it was written for is still the practice.
- **Never include AI attribution in commits.** No "Generated with Claude", no `Co-Authored-By: Claude`. Bead references and `Closes #N` only.
- **Never delete RDR files.** Closing an RDR is a frontmatter `status: closed` flip — the file stays. See [`docs/rdr/AGENTS.md`](docs/rdr/AGENTS.md).
- **Closed vocabularies (RDR status, and future ones) are CHECKED TABLES, not prose — see [`docs/rdr/AGENTS.md`](docs/rdr/AGENTS.md) § RDR lifecycle for the full story.** `src/nexus/tables/` (packaged, checked at load time); `docs/tables/` for repo-only tables (the release-choreography table both release gates resolve).
- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime.
- **`expectations_*` is an `nx-hook` VERB, not an MCP tool.** The RDR-184 background-teammate ledger (`expectations_expect` / `expectations_census` / `expectations_undeclared` / `expectations_reconcile`) moved from a sourced bash library to `nexus.hooks.expectations` at RDR-215 bead nexus-q02nx.14. Searching the MCP tool registry for it still returns nothing **by design** — the answer IS the exit code and an MCP tool has none, so these are command tier only; `nx expectations` / `nx orchestration` / `nx guard` do not exist (nexus-3ra9h).
  ```bash
  nx-hook expectations_census "$SESSION_ID"      # retro counts — NEVER hand-count (nexus-hybv1)
  nx-hook expectations_undeclared "$SESSION_ID"  # rc: 0 clean, 1 BLINDSPOT (EXPECT rows present but zero STARTs walked — the audit examined nothing, not a pass; nexus-houpu), 2 undeclared>0 deficit (nexus-suuja), 3 no ledger file for this session — nothing checkable, not evidence of cleanliness (nexus-ahl9v)
  ```
  No `source` step, and nothing to keep in sync. **`nx-hook` needs a conexus generation at or past the wheel that declares it** — it is a console script, so an older installed generation simply has no `nx-hook` shim and the command is not found (measured 2026-09-19 on this box: `~/.local/bin/` had `nx`, `nx-mcp`, `nx-mcp-catalog` and `nx-session-end-launcher`, no `nx-hook`; `scripts/reinstall-tool.sh` writes it, and a fresh install always carries it). In a dev checkout, `uv run nx-hook ...` works regardless. The plugin's bash copy is GONE and the wired `hooks.json` entries no longer run any shell: nexus-q02nx.21 (`f6fff5501`) deleted the twelve bash hook scripts together with the `expectations.sh` library they sourced, and nexus-q02nx.22 (`7ad666ebb`) deleted the last one, `_run_python_hook.sh`, both on 2026-09-19. Measured 2026-09-20: `git ls-files conexus` matches zero `.sh`/`.bash`/`.zsh`, and `hooks.json` names no shell at all — its entries are `nx-hook` verbs, `mcp_tool` registrations, and exec-form `python3` invocations of plugin-resident scripts. The deletion happened in the same change that re-pointed the entries, which is what the order was for: doing it the other way round would have stripped the library from the four hooks that sourced it, and two of them exited 0 in silence on a bare `|| exit 0`.
  **The EXPECT row is MECHANIZED and LIVE** (nexus-qc4p1, shipped at the 7.0.0 plugin pin; verified 2026-08-02: 70/70 recognized, 0 undeclared): a PreToolUse hook on the Agent tool writes it from the dispatch's own `subagent_type` + `run_in_background`. **Do NOT hand-write EXPECT rows** — the ledger matches N EXPECT rows of a type against N STARTs of that type, so a manual duplicate inflates the credit pool and can mask an undeclared start. Hand-call `nx-hook expectations_expect` ONLY for a dispatch the hook cannot see, keyed on the **subagent type verbatim, colon included** — never an invented name (the Agent tool has no `name` parameter; nexus-nu7fo). `conexus/skills/orchestration/SKILL.md` is canonical for the dispatch-time contract; this entry exists so the *surface* is discoverable — two 2026-07 sessions concluded the ledger was unavailable and skipped it, and it was available both times.
- **Worktree-dispatched agents run `scripts/agent-worktree-preflight.sh [required-sha]` as their FIRST action and stop on any `PREFLIGHT_FAIL` line.** The harness cuts `isolation:worktree` worktrees from the DEFAULT branch's tip, not the session's current branch, so a fresh worktree can be silently stale relative to `develop` by construction (nexus-5kwkf); the script also refuses outright if the agent turns out to be in the shared primary checkout, not a worktree at all. `required-sha` is optional — when omitted it defaults to local `develop` if that branch exists, else `origin/develop`, else refuses (`PREFLIGHT_FAIL_BAD_SHA`); local is checked first because this project's own batched-push workflow routinely runs local `develop` ahead of `origin/develop`. It recovers a stale-but-clean worktree via `git merge --ff-only`; a dirty or diverged worktree is refused untouched. The conexus SubagentStart hook injects this instruction for `isolation:worktree` dispatches once the plugin ships it (`conexus/PENDING_RELEASE.md`); until then this bullet is the delivery path.
- **Daemon-lifecycle fixes land in the shared primitive, never one tier's copy.** Discovery / single-writer / self-heal / version-skew for T1/T2/T3 all live in `src/nexus/daemon/service_registry.py` + the conformance suite `tests/daemon/test_rdr149_lifecycle_conformance.py` (RDR-149). Editing a single tier's lifecycle without touching both is the recurring bug class. Mechanically enforced by `tests/daemon/test_lifecycle_gate.py`. See [`src/nexus/daemon/AGENTS.md`](src/nexus/daemon/AGENTS.md).

## CI Cost Discipline

Project policy, cited by name from the workflows (nexus-jndz0 landed it here
so those citations resolve inside the repo; origin: the 2026-07-06 billing
incident — 24 CI runs + 4 engine tags in one day, macOS at ~10x rates, the
same tree tested four times en route to PyPI. Reference implementation:
PRs #1375/#1376):

- **Never test the same tree twice.** A tree that passed a PR's required
  checks does not re-run CI on the merge-push or at tag time. Tag/release
  workflows publish — they do not re-test.
- **Expensive jobs run only when their inputs changed.** Per-job path
  filters on native builds, from-source compiles, platform matrices.
  Required checks stay satisfied via job-level skip (skipped == success for
  branch protection) or always-run-report-skip WITH a non-vacuity assert.
- **Never rebuild deterministic artifacts.** Version-pinned compiles (PG
  bundles, models, toolchains) are cached or prebuilt, keyed on exact
  inputs; rebuild only on key miss.
- **macOS/premium runners only where the artifact requires the platform**
  (release/tag artifact builds), never in routine push/PR CI.
- **Every workflow has a concurrency group; superseded runs cancel.**
- **Full matrix breadth only at merge boundaries.** PRs run the full
  matrix; interior branch pushes run the minimal representative.
- **Tag cadence is a cost decision.** Batch related work into one cut.

## Workflows

### Adding a CLI command

1. Create `src/nexus/commands/your_cmd.py` with a Click group/command.
2. Register it in `src/nexus/cli.py` via `cli.add_command()`.
3. Add tests in `tests/test_your_cmd.py`.
4. Document the new flags/subcommands in `docs/cli-reference.md`.

### Release cadence policy (nexus-mkj6u)

Six rules borrowed from the global `marketplace-pinned-source-playbook`:

1. **Releases are hand-cut.** CI does not publish on merge. Tag-push triggers publish. Merges to main between releases do not affect installed users (marketplace.json's `source.ref` stays pinned to the previous tag).
2. **`source.ref` only ever points at immutable release tags.** Never at a branch, never at main HEAD. Optional `source.sha` for force-push protection.
3. **One channel until proven otherwise.** No `-dev` / `-rc` / `-canary` suffix variants. If a beta channel becomes necessary, file an RDR.
4. **Bump cadence matches user-visible impact, not commit volume.** Many internal PRs can land on develop and then on main without bumping the version. The version bumps when users would see something change.
5. **Releaser is human. AI prepares; human cuts.** AI can draft the release PR, bump manifests, write the CHANGELOG entry. The human runs `gh pr merge` + `git tag` + `git push origin vX.Y.Z`.
6. **Parity tests stay strict.** Any drift between `pyproject.toml` version and the other six version surfaces (`mcpb/pyproject.toml`, `mcpb/manifest.json`, marketplace.json's two `plugins[].version` fields, `conexus/.claude-plugin/plugin.json`, `sn/.claude-plugin/plugin.json`) — plus `source.ref` in marketplace.json and `uv.lock` — fails CI. No `# noqa` escape hatches.

### Independent plugin release channel (RDR-197)

A plugin cut ships plugin-surface content (paths under `conexus/` and
`sn/`, plus `.claude-plugin/marketplace.json`) without a client release:
it moves the changed plugin's `source.ref` to an anchored tag of the
shape `plugin-v{X.Y.Z}-{n}`, where `X.Y.Z` is the CURRENT released
client version and `n` is the cut's sequence number. The number comes
from git's own tag list at cut time (one more than the highest existing
`plugin-v{X.Y.Z}-*` tag); there is NO stored counter file, so there is
nothing to reset and nothing to read from the wrong place. The channel
publishes nothing to PyPI: the `plugin-v*` tag fires a verify-only
workflow (`.github/workflows/plugin-release.yml`), and the wheel-surface
proof asserts the cut's range touches no wheel content. The cut itself
is `scripts/cut_plugin_release.py` (a script with tests, never a
checklist); the post-cut back-merge is `scripts/plugin_cut_back_merge.sh`,
never a bare merge (`conexus/PENDING_RELEASE.md` conflicts by
construction on every cut); invariants R and W live in
`scripts/plugin_channel.py`'s docstring.

**Usage discipline** (was this cut warranted?):
1. Cut when accumulated plugin functionality is worth shipping and no
   client release is imminent. A client release ships the same content
   for free.
2. An open release PR always wins. Never cut while one is in flight.
3. Batch related plugin work into one cut, exactly as the release
   cadence rules above batch client releases.
4. **Rehearse before the real cut, every time:**
   `tests/e2e/plugin-cut-rehearsal/run.sh` (container by default, `--host`
   for a fast loop) walks the whole flow against a FAKE origin — the real
   cut script with its battery, the cut PR's CI on a synthetic merge ref
   with a `pull_request` payload, merge + anchored tag, the tag workflow's
   steps, back-merge — and commits the source's uncommitted changes onto
   its clone's develop, so machinery fixes are rehearsed before they land.
   Must end `PLUGIN-CUT REHEARSAL PASSED`. The first real cut (2026-08-30,
   nexus-a2wmi.12) refused three times on machinery that only runs at cut
   time, each round costing a PR to main; every one was reachable here.

**Sunset trigger:** if two years pass with zero cuts, delete the
workflow, the parity test's anchored-form branch, and the cut script.
RDR-197 (`docs/rdr/rdr-197-plugin-only-release-channel.md`) is the
record that keeps the design recoverable.

`docs/` is NOT in the channel allowlist, deliberately: docs-only commits
outnumber plugin-surface commits about 12 to 1, and admitting them would
make nearly every cut a docs cut.

### Engine-service release (a SECOND lifecycle — decoupled from the PyPI release)

The Java **engine-service** binary is a separate release artifact with its own cadence. Conflating it with the PyPI/marketplace release is how the cloud engine silently drifts behind develop (2026-06-26: 22 `service/` commits / 4 days un-deployed, un-cloud-tested).

Build or test the engine through `scripts/mvnw-leased.sh` (never a bare `./mvnw`/`mvn`) — one builder at a time; a concurrent `./mvnw` invocation against the same `service/target` corrupts jOOQ codegen mid-build (nexus-c00dw, see `scripts/lib/build-lease.sh`). The lease lives in the git common dir, so every worktree shares it, and a live holder is waited for rather than refused (`NX_BUILD_LEASE_WAIT`, nexus-g6xpa): one engine build or suite per box. `scripts/build-gate-jar.sh` caches the stamped jar on the exact `service/` content, so a fresh worktree with an unchanged tree gets a copy instead of a nine-minute rebuild. The Python suite reads the same lease at session start (nexus-pv93h): while a build holds it, `pytest` refuses the whole run with one line and exit 75 naming the holder, and `NX_BUILD_LEASE_WAIT=<seconds>` makes it wait instead; `NX_TEST_T2_SUBSTRATE=none` runs are never gated.

- **Artifact + trigger:** an `engine-service-vX.Y.Z` git tag fires `engine-service-release.yml`, which builds + cosign-signs the 3 native binaries (linux-amd64, linux-arm64, mac-arm64 — mac-arm64 unsmoked, no Docker on GH macOS, nexus-4xf5m; mac-amd64/Intel is not a supported target). It publishes **nothing to PyPI** and is **NOT gated by the luxe6 / RDR-155-P4a develop release boundary** (the workflow header says so explicitly). **The release is a DRAFT until every asset is attached** (nexus-cl14i, after v0.1.95 published PG bundles with no binary): a final `promote-release` job flips it only when both matrices succeeded and all 21 assets are present, so a tag is consumable roughly 35 to 65 minutes (v0.1.118 took 36, a single measurement) after push, never partially; a failed leg, mac-arm64 included, leaves a draft that `gh run rerun --failed` completes and promotes. So the engine can be refreshed in the cloud at any time, independent of the unreleasable-develop state.
- **Version is tag-stamped — there is NO manifest to bump.** `release.properties` `release_version` is blank in source and stamped at native-build time from the tag (the Maven `pom.xml` stays `1.0-SNAPSHOT`, the dev coordinate). The cut is NOT just suite-green-then-tag: the `engine-release` skill (Authority: this section) enforces a full pre-tag battery — full engine suite green on the tagged commit, `tests/e2e/migration-rehearsal/run.sh --shakeout` (must end `CANDIDATE SHAKEOUT PASSED`) — then human pushes `engine-service-vX.Y.Z`, followed by a post-publish `--acquire` gate against the published bytes. `scripts/check_client_release_precondition.py --engine-tag engine-service-vX.Y.Z` gates the **DEPLOY, never the tag cut** (Hal directive 2026-08-02 — its pre-tag wiring forced conexus 7.1.0 to ship pinned to a pre-fence engine, its own flagship feature inert on fresh local installs; a red exit means the deploy waits for the client tag carrying the listed commits, per the paired-release choreography below). **A tag gates DELIVERY, not work**: engine changes are fully testable end-to-end on develop (`scripts/mvnw-leased.sh test` + the Python suite's engine substrate + LSG against a `build-gate-jar.sh` dev jar) — "cannot deploy yet" is never "cannot do/test/tag it" (error recurred 3x: nexus-0ehwe thread 2026-07-31 twice, the 7.1.0/v0.1.62 inversion 2026-08-02). Use the `engine-release` skill as the executable checklist, not this summary.
- **PRE-TAG gate: release-workflow SHAPE, not just content** (nexus-xihsm). Three tags burned in one day (2026-09-05) on defects invisible to every gate above because those gates run in a DIFFERENT shape from `engine-service-release.yml` on two axes: `--shakeout` drives `service/native-smoke.sh`'s real-client probes from a `uv`-tool-INSTALLED WHEEL inside its container, where the nexus-a2qhz dev-checkout production-write guard is inert by construction, while the release workflow runs the byte-identical script from the CI CHECKOUT, where the guard fires; and every local Java leg (the host suite, `--shakeout`'s own `-Ob` build, `--candidate-migration`) runs WITH Docker, while the release build (`./mvnw -Pnative -Pprebuilt-jooq -DskipTests package`) runs on runners WITHOUT it. `scripts/check_release_workflow_shape.py` closes both: phase (a) runs native-smoke.sh's client probes from THIS checkout with a non-vacuity assert that `nexus.db.service_endpoint.is_dev_checkout_process()` is actually `True` for the process doing the asserting; phase (b) parses the release's EXACT Maven invocation out of the workflow YAML (never retyped: `extract_release_native_build_argv` is pinned against drift by `tests/scripts/test_check_release_workflow_shape.py::test_extraction_matches_the_real_workflow_file`) and runs it through `test-compile` with `DOCKER_HOST` pointed at a nonexistent socket. Self-sufficient, no manual pre-step: phase (b) populates `service/target/generated-sources/jooq` itself when absent, via `scripts/mvnw-leased.sh -q generate-sources` (the SAME command the release's separate `jooq-codegen` job runs, WITH Docker, exactly matching that job's two-stage split; routed through the leased wrapper, never a bare `./mvnw`, so it respects the one-builder-per-box lease above).

  **Wired into `--shakeout` automatically** (`tests/e2e/migration-rehearsal/lib/shakeout_shape_check.sh`, called from `run.sh` right after the native-build step, never from a manual invocation and never from inside `rehearse_shakeout.sh`): a run-by-nobody check rots exactly like the procedures it exists to catch, and `rehearse_shakeout.sh`'s own container is the WRONG place for it regardless, since that container is a `uv`-tool-installed wheel with no `.git`/`pyproject.toml` ancestor by design (phase (a)'s non-vacuity assert could only ever refuse there, and phase (b) has no `service/mvnw` or JDK inside that image to run at all). `run.sh` is the one place both preconditions hold: it is itself a checkout, and it is where the just-built `-Ob` native candidate and freshly generated jOOQ sources already sit on THIS host, right after the build step. Phase (a) runs against the native candidate (`service/target/nexus-service`, or `$ARTIFACTS/native/nexus-service` under `--artifacts`); on a host that cannot execute that Linux candidate (macOS, where the `-Ob` build runs in a container) it boots the same build's JVM jar through a shim, and a missing jar is a FAILED verdict. A failure prints `[shakeout] release-workflow SHAPE check: FAILED` and exits `run.sh` with a nonzero status before the container ever starts, so it gates `--shakeout`'s own exit code and its verdict line is visible in the same terminal output as everything else `--shakeout` prints. Tests: `tests/scripts/test_shakeout_release_workflow_shape_wiring.py`.

  **Standalone invocation still works** for other legs / ad hoc runs: `uv run python scripts/check_release_workflow_shape.py [--bin <BIN>]`: omit `--bin` to fall through to `native-smoke.sh`'s own default (`target/nexus-service`, relative to `service/`), or pass `--bin <path-to-a-shim-that-execs-java-jar>` (a JVM-jar shim via `build-gate-jar.sh` is far cheaper than a fresh native build, and legitimate here since phase (a)'s checkout-classification defect is orthogonal to native-image vs JVM). Must end `RELEASE WORKFLOW SHAPE CHECK PASSED`.

  **What remains uncovered even when wired** (critic finding, nexus-vpl9c riders review): this exercises the CANDIDATE binary and the Maven invocation shape only. It proves nothing about cosign signing (`engine-service-release.yml`'s keyless-Sigstore steps, tag-gated, never exercised by `--shakeout` at all), the `promote-release` job's all-21-assets gate (`scripts/promote_engine_release.sh`, its own separate forced-failure proof per `tests/scripts/test_promote_engine_release_sh.py`), or mac-arm64's genuine no-Docker GitHub-hosted runner (this check runs on a box that HAS Docker throughout; mac-arm64's `smoke: false` posture and its own coverage gaps are `nexus-4xf5m`'s territory, unrelated to and unclosed by this check).
- **Cut from develop tip; don't let it drift.** Cloud-relevant engine work (pooler/RLS, pgvector, catalog conformance, aspect queue, batch endpoints) lands on develop continuously. Cut + deploy + cloud-gate the engine on its own cadence. Rule of thumb: if `git log <last-engine-tag>..HEAD -- service/` is non-trivial AND cloud-relevant, cut a fresh engine **before** relying on cloud test results or pinning it into a PyPI release.
- **Prep (AI) vs cut (human).** AI preps: confirm the `service/` tree at the target commit equals a green-`service-ci` commit (the Java CI is advisory — it does not block auto-merge — so verify the full `scripts/mvnw-leased.sh test` + native build actually passed on that exact tree). The human pushes the tag.
- **Deploy + cloud-gate is conexus-side (passive bus).** After the tag publishes + signs, conexus deploys the signed binary and re-runs the cloud gate (recall + hybrid parity, xr7.8.9-style). Surface an explicit "relay: deploy `engine-service-vX.Y.Z` + re-gate" to Hal — never frame the cross-instance deploy as autonomous. **This line is a POINTER, not a terminus.** conexus owns pre-deploy instruments this repo does not document, because they are not this repo's to document. Two of them, confirmed 2026-08-27:
  - **Liquibase walk rehearsal against a PITR fork of production** (`deploy/RESTORE.md`, conexus repo) — a new Crunchy cluster restored to a point in time, ~6 min to ready, destroyed after, a few dollars. This is the pre-deploy gate for ANY tag carrying a changeset. It is not ceremonial: it caught `v0.1.78` leaving `nexus_diag` with ZERO grants, which is the only reason that was known before it was live. On `v0.1.86` (2026-08-27) it confirmed the predicted 1-changeset walk AND found two defects no bare-box gate can reach — `nexus-x0s52` (`schema_migration_complete` logged the PRE-update pending count under the name `applied_changesets`; reported 12 where 1 landed. FIXED on develop 2026-08-30: the line now reports `new_changesets` / `reexecuted_changesets` / `pending_at_start`, and the old field name is deliberately gone so stale greps find nothing instead of new semantics; engines ≤ the fix's first tag still log the lying field) and `nexus-rph82` (`databasechangelog.dateexecuted` was JVM-local against a GMT database, so an audit windowing on it reported "nothing applied" for a successful walk; fixed on develop dbc498cab). Both are invisible on an empty cluster, where pending and applied coincide and there is no prior wall-clock to reconcile against. Also: "N new changesets" is not "N things ran" — 12 `runAlways` changesets re-execute every walk, 10 of them in `grants-nexus-svc.xml` and `grants-nexus-diag.xml`, which is the surface v0.1.78's defect came from. A clean walk proves they executed, not that their content is right. The walk is CUMULATIVE — it replays every changeset the target cluster is behind on — so its size is a property of the CLUSTER's state, not of the changeset you added. Read the cloud's actual `release_version` from the engine to size it; never from records. A changeset that modifies or deletes existing rows must carry a `DATA EFFECT:` line in its `<comment>` (checksum-neutral, see `scripts/data_effect_lint.py`'s docstring), mechanically enforced by `tests/test_changelog_data_effect_lint.py`; `scripts/list_data_effects.py <from-tag> <to-tag>` lists every such changeset added between two engine tags as a markdown table for the deploy handoff. **The three fields have an exact identity, and counting is by changeset IDENTITY, not by row** (nexus-jl08t): `pending_at_start` is Liquibase's own `listUnrunChangeSets()` count BEFORE the walk — every genuinely-new changeset plus the full `runAlways` set, which Liquibase always counts as "unrun" regardless of prior execution — so `new_changesets + reexecuted_changesets + mark_ran_changesets` must equal `pending_at_start` EXACTLY (a clean walk with N new landings against this changelog's 12 `runAlways` changesets: `pending_at_start = N+12`, `reexecuted_changesets = 12`, `mark_ran_changesets = 0`). This was added investigating `engine-service-v0.1.118`'s own PITR-fork walk, which logged `new_changesets=5 pending_at_start=17 reexecuted_changesets=25` against a changelog whose 12 `runAlways` changesets predict `17-5=12`. **CONFIRMED CAUSE** (conexus-9a, 2026-09-14, read-only query of production): `public.databasechangelog` has no uniqueness constraint on `(id, author, filename)`, and production carries 13 extra rows across exactly two identities, both in `grants-nexus-diag.xml` — `grants-nexus-diag-1` (author `nexus-ykzbj.8`, 6 extra copies) and `grants-nexus-diag-2` (author `nexus-9bufb`, 7 extra copies), every copy identical (`RERAN`, one shared `deployment_id`, `dateexecuted`, `md5sum`). Liquibase's `RERAN` path (`MarkChangeSetRanGenerator`) issues one `UPDATE ... WHERE id=? AND author=? AND filename=?` per changeset, with no row-count limit, so it re-stamps every matching physical row while Liquibase itself believes it processed one changeset: 12 genuine `runAlways` identities + 13 duplicate extra rows = 25. A first hypothesis (a `dateexecuted` clock/timestamp-window defect, or a concurrent second walker) was investigated and ruled out — production has zero rows with `dateexecuted` in the future, and the fork's `orderexecuted` sequence advanced by exactly 17, matching one walker doing one thing per changeset. `SchemaMigrator.migrate()` now counts by scoping to a pre-walk `orderexecuted` watermark and DE-DUPLICATING by changeset identity before counting outcomes, so duplicate physical rows for the same identity count once; `SchemaMigratorIntegrationTest`'s "aged database" test seeds this exact production shape (6 + 7 extra copies) and pins both the wrong value (25, reproduced from the retired algorithm and independently from a non-distinct `deployment_id` count) and the fixed one (12). `migrate()` logs `event=schema_changelog_duplicate_rows` when it finds duplicate identities (naming the identity count and extra-row count) and `event=schema_migration_count_anomaly` if the three outcome counts ever fail to partition `pending_at_start` exactly — the same event the existing negative-raw-reading guard used. **The duplicate rows' origin is KNOWN, not a mystery** (substantive-critic finding, T2 [25635] CRITICAL 2): commit `585c8c20e` ("fix(schema): stop DATABASECHANGELOG growing a row per boot", nexus-ixsxa, 2026-07-27) explains that `grants-nexus-diag-1`/`-2` — the exact two identities production shows duplicated — previously carried `<preConditions onFail="MARK_RAN">` on a `runAlways` changeset, so while the precondition kept failing, Liquibase's `MarkChangeSetRanGenerator` issued an INSERT (not an UPDATE) on every boot, growing one extra row per boot until 585c8c20e converted both to body-guard `RERAN` updates and the growth stopped; `grants-nexus-diag.xml`'s own header (lines 48-76) repeats this, and rows accumulated before that fix were deliberately not swept. What is genuinely unrecoverable is narrower: which PHYSICAL BOOT produced which copy — every `RERAN` re-stamps every copy's `dateexecuted`/`deployment_id`/`orderexecuted` alike, erasing whatever distinguished them before. De-duplicating or deleting the accumulated rows in production is a data-hygiene decision for conexus and Sam, not something a migration performs, and `SchemaMigrator` must count correctly with them present indefinitely.
  - **`deploy/engine/image-smoke.sh`** (conexus repo) — pre-push, image-level: boots the built image, asserts `release_version`, and fails on any `/version` key outside the public contract. `push-engine-image.sh:129` (conexus repo) refuses an image that never proved it boots.
  What does NOT exist: a staged / shadow / non-prod deploy target. There is ONE environment (`dev`), and it carries the live estate, so deploying to "dev" IS the live deploy (conexus-vbti, OPEN). Say that precisely; do not generalise it into "the cloud cutover is unvalidated by construction".
  Measured 2026-08-27 (`engine-service-v0.1.86`): this section plus the engine-release skill's post-deploy-only gate list was read as "validated post-hoc by construction" and reported to Hal that way. The binary half of that was right; the WALK half was wrong, and the fork rehearsal was omitted entirely.
- **Their gate probes the engine DIRECT; it does not prove client-visibility.** After every cloud deploy (and before signing off a release shakeout), run `tests/e2e/cloud-client-path-gate.sh` from a cloud-mode box: it asserts the engine's pinned contracts (/version fields, ez5.1 /health, client embedding_mode probe, /v1 read path) survive the PUBLIC edge. 2026-07-23 (nexus-bwulw): the edge stubbed /version and auth-gated /health, silently disabling voyage threshold gating + dimension-orphan tooling and blocking guided migrations to cloud — three client features shipped green through every engine-direct gate.
- **A new engine bumps these downstream references:** `tests/e2e/migration-rehearsal/run.sh` `COLD_TAG` default; and — **unconditionally** — `REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py`. ONE engine identity per release: the engine it was built and gated with, on every install path. Not a compatibility minimum, not a range, no "only if the release needs the features" carve-out (Hal directive 2026-07-15, after the 14h GH #1402 incident; the identical 2026-07-14 v0.1.42 episode came from exactly that carve-out). Cloud users get whatever conexus deployed; **local-mode installs get ONLY what this constant names**, so a tag that is cut, gated, and never pinned reaches nobody. That single constant also drives `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py` DERIVES it, not an independent literal), so there is nothing separate to bump there. **Sequencing — the PAIRED-RELEASE choreography (Hal directive 2026-08-02, supersedes the old "bump only after deploy, floor lags a release" reading):** when a client release carries client halves of engine features, the engine tag is cut FIRST (tag-cut is never blocked — see 3b above), the client release gates its battery against that engine and bumps the floor to it IN the same release, and the deploy fires at client-tag push, in PARALLEL with the ~90s PyPI publish workflow — any client-release precondition is satisfied the instant the client tag exists, and the engine is live before any user can install the client that requires it. Zero refusal window (GH #1402: a floor-bumped client published with NO deploy armed makes cloud clients refuse the managed service as below-identity — the deploy must fire at tag push, not "eventually") and zero inert-window (the 7.1.0/v0.1.62 inversion: floor lagging a release ships a client whose pinned engine lacks the engine halves of its own features). `scripts/check_engine_release_floor.py` fails the release in BOTH directions. **nexus-1emxn refinement (measured twice, 2026-08-29: "fires at tag push" was a human relay, PyPI published in ~90s, and the window sat open 48+ min):** (a) when every wire-ledger `## Unshipped` entry carries the `[additive]` direction-safety token (old client + new engine safe), deploy the engine BEFORE the client tag push — `check_client_release_precondition.py` accepts an all-`[additive]` ledger with the pairing named, so the client tag can never open a window at all; (b) when any entry is not additive, the relay must be ARMED with conexus (image built, redeploy staged on a named trigger) and that arming CONFIRMED before the client tag pushes — an unarmed non-additive pairing does not tag, enforced by `check_release_arming` at the end of the paired battery against conexus's attestation in `docs/release-arming/` — which the ATTENDED `--paired-deploy` run reaches and the tag-push `--paired-deploy-auto` run does not always, by ruling (nexus-h0fo3 landed it, nexus-jv9h3 is the accepted gap; contract in that directory's README); (c) on a cloud-mode box, the post-release local reinstall waits for the bare floor verify to pass against the live cloud (exit 1 = the cloud is still behind = new spawns would refuse the service — wait, don't reinstall).

### Cutting a release (version bump + tag-push to PyPI)

**The `release` skill is the authority and the executable checklist.** Invoke it
(`/conexus:release`, or `Skill` on `release`); this section is a pointer, not a
parallel copy. `docs/contributing.md#release-process` is the long form for
rollback and one-time setup.

The authority used to run the other way and the two drifted, which is why it was
inverted (2026-09-20). Measured on 2026-09-19: this file recorded the
remediation-commit gate as RETIRED while the skill listed it as a live preflight
leg 57 lines after carrying that retirement inside an HTML comment; the skill's
`git add` block ended in a trailing backslash that swallowed its own
`git commit`, so it staged files and never committed; the skill described a
SQLite backend deleted at RDR-158 in the present tense; and "the human pushes
the tag" had widened in the skill into "human by default, OR the AI pushes it
when authorized". Two copies of a procedure decay until the stale one wins an
argument, and a reader had no way to tell which was current.

What did NOT move, and why this is not a mechanical rule: § Engine-service
release ABOVE is not a pointer and should not become one. It is not a summary
of its skill — it is the incident archive that skill defers to, and the
`engine-release` skill's own back-references name this file as the authority on
wording conflicts. A pointer cannot win a wording conflict.

**Step 0 stays here because other sections cite it.** The engine-freshness gate
is BLOCKING and is a command, not a prose eyeball-check (nexus-i5c2u):

```bash
uv run python scripts/check_engine_release_floor.py
```

Non-zero means STOP. It fails in both directions — a cloud behind the pinned
identity, and a gated engine tag that was never pinned — and the remedy differs
per direction. The skill's Step 0 carries both, the `--paired-deploy` form for a
paired release (nexus-k1c08), and the post-tag verify obligation. `ONE engine
identity per release` and the `REQUIRED_ENGINE_VERSION` -> `PINNED_SERVICE_TAG`
derivation are stated in § Engine-service release above.

## Worktrees: one session, one worktree (2026-09-19)

**Every session works in its own worktree on its own branch. The shared
primary checkout is not edited.** Agreed by the three sessions sharing this
box after a day in which every coordination failure traced to one cause: a
commit that nearly carried a peer's uncommitted draft, `PUSH_REFUSED_FOREIGN`
in both directions, a deadlock where a fix could not reach origin because a
peer's unpushed commit was its ancestor, subagent briefs restricted to named
files purely because the tree was shared, and a branch pointer nobody could
reconcile while anyone's tree was dirty. In separate worktrees these are not
things to avoid carefully; they are impossible.

1. **Site worktrees as siblings**: `../nexus-wt/<session>`, branch
   `feature/<bead-id>-<slug>`.

2. **The primary KEEPS `develop` checked out** and is the reference
   checkout. Not detached: git refuses to check out a branch that is
   already checked out elsewhere, so the primary holding `develop` makes
   rule 1 self-enforcing at zero discipline.

3. **In the primary: no edits, no commits, no staging, no branch
   switches.** Build and test output (`.venv/`, `service/target/`,
   `__pycache__`, coverage) is expected and fine. "Read-only" would be
   false the first time someone runs a suite there, and a rule that is
   false on first contact becomes advisory.

4. **The release battery runs IN THE RELEASE WORKTREE, never the primary**
   (nexus-57cvk; this said "in the primary" until 2026-09-22). It keys
   artifacts on the working tree's identity and refuses artifacts whose
   manifest identity is not this checkout's (nexus-mbeke), so the tree it
   runs in must not move underneath it — and the primary moves by
   construction, because rule 9 fast-forwards it on every push to
   `develop`. On 2026-09-22 that ended 7.57.0's battery on its twelfth leg
   with a tree-identity mismatch naming two hashes: eleven legs green,
   nothing wrong with the code, and neither session having done anything
   the rules did not tell it to. A release worktree holds the release
   branch, which a `develop` push cannot move at all, so the collision
   stops existing rather than being avoided carefully.

   Rule 4 is the one that gave way, because the two are not the same kind
   of rule. Rule 9's reason is correctness: a stale primary answers
   questions wrongly and looks complete doing it. Rule 4's reason was
   cost: the primary was where the artifacts already were, so rotating
   worktrees rebuilt the wheel and the native candidate every time. Cost
   yields to correctness.

   The rebuild is worth paying on its own merits anyway. The version bump
   lands in the release branch, so a battery run in the primary gates a
   tree that is NOT the tree that ships. One artifact rebuild per release
   buys "we gated what we shipped", which is the whole point of a battery.

   The engine build needs neither tree: the lease lives in the git common
   dir and the jar cache is keyed on `service/` content, so any worktree
   gets a copy.

5. **ONE FULL SUITE PER BOX AT A TIME, announced on the bus.** Scoped runs
   are unaffected. This rule exists because worktrees DESTROY an accidental
   serialisation: three sessions in one tree cannot comfortably run three
   suites at once, so they take turns without agreeing to. Three worktrees
   make three concurrent `pytest -n auto` runs easy, and that exhausts the
   machine-wide SysV shared-memory budget — each xdist worker boots its own
   Postgres substrate holding a segment against `kern.sysv.shmmni`
   (nexus-6qp25). The build lease does not cover this; it is a separate
   budget.

6. **A NEW WORKTREE RUNS `scripts/build-gate-jar.sh` BEFORE ITS FIRST
   SUBSTRATE-BACKED TEST.** `service/target/` is untracked build output, so
   a fresh worktree has no service jar and every substrate-backed test
   errors at setup. The fix is seconds rather than a nine-minute rebuild
   because the cache key is on `service/` CONTENT and lives in the git
   common dir, so every worktree on the box shares one build.

   **This produces the SAME SYMPTOM as rule 5's exhaustion** — thousands of
   setup errors that read as catastrophic breakage. Two causes, one
   symptom. Check `ipcs -m` first because it is one command; if segments
   are clear, it is the jar. Measured 2026-09-19: 20673 setup errors in a
   fresh worktree, zero shared-memory segments, missing jar.

7. **Before pushing, `gh run list --limit 1`.** Ask whether a verdict
   someone is waiting on is in flight — not whether the slot is free.
   Worktrees split the tree; CI remains one shared resource with one queue,
   and a push destroys a running verdict.

8. **Push unchanged**: `NX_PUSH_SOURCE=HEAD scripts/git-push-develop.sh <sha>...`
   from INSIDE the worktree. It reads HEAD from the shell's cwd, so `cd`
   in; `git -C` does not cover it. Direct to `develop` per the project
   rule; the feature branch is a local name that never reaches origin.

9. **Whoever pushes to `develop` fast-forwards the primary in the same
   breath.** `cd` to the primary and `git merge --ff-only origin/develop`.
   Rule 2 makes the primary the reference checkout — the one place to read
   what is on `develop` without a fetch dance — and nothing kept it
   current, so it silently stopped doing that job. Found 2026-09-19 twelve
   commits behind: `nx rdr preamble` run there omitted an RDR that had
   been on `develop` for hours, because the tool reads the RDR directory
   relative to cwd and returned a confident, complete-looking, stale list.

   Attach it to the push rather than to a schedule or a habit, because the
   push is the event that creates the staleness and is already a thing
   someone does deliberately. It is unconditional: rule 4 moved the
   release battery out of the primary precisely so that nothing you have
   to check for can be running in there. If the primary is dirty, do NOT force it:
   a dirty primary is a rule-3 violation someone is mid-way through, and
   clobbering it is worse than a stale read. Say so on the bus instead.

   Same failure shape, for the same reason, one layer up: a stale checkout
   and a glob that matches nothing both answer confidently with a wrong
   maximum. `conexus/skills/rdr-create/SKILL.md` Step 2 told sessions to
   hand-scan for `[0-9][0-9][0-9]-*.md`, which matches none of this repo's
   `rdr-NNN-*.md` files, so a session following it literally found no
   maximum, fell through to the step's "start at 001" clause, and would
   have collided with RDR-001. Fetching fixes neither; each needs its own
   fix.

10. **Never hand-run `nx index repo` from a worktree.** The post-commit hook
   already refuses worktree indexing by construction (nexus-ws67k, comparing
   `--git-dir` to `--git-common-dir`), but the guard lives in the HOOK and
   not in the command, so a hand-run bypasses it entirely.

11. **Serena differs between a session STARTED in a worktree and one that
    RELOCATED into it.** A session started there gets its own server rooted
    at the worktree via `--project-from-cwd` and keeps symbol editing. A
    session that relocates mid-flight keeps the server rooted at the
    primary, so it must use Edit/Write with absolute worktree paths and not
    Serena write tools.

**Moving an in-flight session.** Cherry-pick or apply into the new worktree
FIRST and verify there, and only then revert the primary — never the
reverse. Two methods, and the right one depends on the starting state:
uncommitted work moves with `git diff origin/develop -- <paths>` then
`git apply` in the worktree (checking that diff-vs-origin and diff-vs-HEAD
are identical first, which is what proves the paths rebase cleanly BEFORE
anything moves); work that is already committed is cherry-picked, because a
commit is recoverable where an applied-but-unverified diff is not.

**A note on reading long runs.** Preconditions are warned at the TOP of a
run (the stale-jar banner above is one). Whether such a warning reaches you
depends on how much output follows it, which inverts against its value: the
longer and more expensive the run, the further the warning sits from the
tail. `head` as well as `tail`, or grep the warning shape.

## Task tracking

Use **beads** (`bd`) for issue tracking. Find work with `bd ready`; claim with `bd update <id> --claim`; close with `bd close <id>`. Use `nx memory put` for project-context notes that persist across sessions. See `docs/contributing.md` § Git Workflow for branch naming (`feature/<bead-id>-<description>`).

## Settings

User-global permission settings live in `~/.claude/settings.json`. Never write to `settings.local.json` — it must remain `{}`.
