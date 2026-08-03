# AGENTS.md

Project guidance for AI coding agents working in this repository. `CLAUDE.md` is a symlink to this file.

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as `conexus`; the CLI entry point is `nx` (`src/nexus/` is the package).

**Guidance precedence:** workflow routing — skills-first, agent dispatch, storage-tier checks, review paths, orchestration — is owned by the conexus plugin's injected guidance (`using-nx-skills` at SessionStart, the subagent-start preflight, the orchestration skill). This file and any personal CLAUDE.md yield to the plugin on workflow; they carry repo facts, hot rules, and durable authorizations. A restriction here that contradicts the plugin's workflow layer is a defect to surface, not a tiebreak to silently win.

## Quick start

```bash
uv sync                                  # install deps
scripts/reinstall-tool.sh                # install nx CLI locally (preserves extras)
uv run pytest                            # full unit suite (no API keys needed)
uv run pytest -m integration             # E2E (requires .env from .env.example)
uv sync && scripts/reinstall-tool.sh && nx --version    # after edits
```

Unit tests use the in-process `InMemoryVectorClient` (`nexus.db.inmemory_vector_store`) + bundled ONNX MiniLM — no API keys or network; engine-substrate tests self-provision a local service (`ensure_engine`/`mint_test_tenant` in `tests/conftest.py`) or skip.

## Architecture at a glance

Three storage tiers, by lifetime. **ChromaDB is not a live substrate in any mode** (RDR-155 P4b, 2026-07-25 — dependency dropped, absent from `uv.lock`):

- **T1** — service-backed session scratch (`HttpScratchStore`; `nx scratch`). `NX_T1_ISOLATED=1` runs an in-process `InMemoryVectorClient` instead.
- **T2** — eight domain stores behind a `T2Database` facade, all HTTP clients over the engine's PG tables. Persistent notes, plans, taxonomy, telemetry, chash, aspects, aspect queue, DEVONthink highlights.
- **T3** — `HttpVectorClient` over the nexus-service `/v1/vectors` (pgvector) in both modes: local = bundled PG17+pgvector, cloud = managed service + Voyage. Permanent knowledge (`nx store`, `nx search`).

### T1 sub-agent contract (RDR-105)

T1 is service-backed and session-id scoped (`resolve_active_session_id()` / `current_session()`), leased through `daemon/t1_lease.py` (RDR-149 P4) and published by the MCP lifespan. T2 is the cross-process shared bus, over PG via the engine (multi-process-safe by construction, not SQLite+WAL).

- **Agent-tool sub-agents** (in-process Task dispatches) share T1 with their parent via the parent's MCP scratch tool. No separate T1 instance.
- **`claude -p` sub-processes default to `owned`** mode: their MCP resolves its own session and leases its own T1 scope. Sealed from the parent; internally consistent for the subprocess's own Bash tools and sub-agents.
- **`claude -p` sub-processes that genuinely need parent-T1 visibility** opt in via `share_t1=True` at dispatch time. Subprocess inherits `NX_T1_HOST` / `NX_T1_PORT` and connects to the parent's `HttpScratchStore` over HTTP.
- **Stateless one-shot operators** (`ephemeral=True`) get an in-process `InMemoryVectorClient` only (no service lease). The operator-dispatch default (`nx_answer`, `nx_tidy`, plan-runner inline planning).
- **Cross-process findings between sibling sub-processes go to T2** (`memory_put`). T1 is process-local by design; T2 is the shared bus (PG over the engine, multi-process-safe).
- **Removed env name:** the legacy `NEXUS_SKIP_T1=1` alias was REMOVED at 6.5.2 (promised gone in 5.0). It is recognized-but-IGNORED with a one-shot warning; use `NX_T1_ISOLATED=1`.

Collection prefixes coexist in one T3 database. Always `__` (double underscore) as separator (colons are invalid in ChromaDB collection names). Conformant collection-name shape (RDR-103) is `<content_type>__<owner_id>__<embedding_model>__v<n>`, e.g. `code__nexus-1-1__voyage-code-3__v1`:

| Prefix | Embedder | Document identity (catalog) | Chunk natural ID (T3) |
|---|---|---|---|
| `code__*` | `voyage-code-3` | `source_uri` (file path) | `chunk_text_hash` (full 64-hex; 32 bytes stored — RDR-180) |
| `docs__*`, `rdr__*` | `voyage-context-3` (CCE) | `source_uri` (file path) | `chunk_text_hash` (full 64-hex) |
| `knowledge__*` | `voyage-context-3` | `source_uri` then `title` (fallback for MCP-stored notes) | `chunk_text_hash` (full 64-hex) |

**Catalog/T3 split (RDR-108, widths per RDR-180)**: Catalog Documents are graph nodes addressed by tumblers (`Document.tumbler`); T3 chunks are content-addressed blobs whose natural ID is the FULL `sha256(chunk_text)` — 64 lowercase hex on the wire, 32 raw bytes in storage (`bytea`, `octet_length=32`); hex only at boundaries (see `docs/architecture.md` § Chunk identity). Document structure (which chashes compose a doc, in what order) lives in the catalog `document_chunks` manifest, not in chunk metadata. The doc-to-chunks join is `documents.tumbler -> document_chunks.doc_id -> document_chunks.chash`; the chash is the chunk id directly, no further lookup. Identical chunk text in the same collection collapses to one T3 row by design; the manifest preserves position via `(doc_id, position)` rows pointing at the shared chash.

For the full module map, post-store hook contracts, T2 schema, and design heritage see [`docs/architecture.md`](docs/architecture.md). For module-local guidance see the `AGENTS.md` files inside `src/nexus/catalog/`, `src/nexus/db/`, and `src/nexus/mcp/`.

## Critical conventions

- **Python 3.12+** — use `match/case`, `tomllib`, `typing.Protocol`, walrus freely.
- **Type hints on every public API.** Module-level constants too.
- **No ORM.** Raw SQL. (Existing T2 SQLite code: raw `sqlite3`, WAL on open — maintenance only, see the NO-SQLITE hot rule.)
- **Composition over inheritance.** Protocols, not deep hierarchies. Constructor injection — no global singletons, no service locators.
- **TDD.** Test file before implementation. Deterministic: seeded randomness, fixed clocks, `port=0` for dynamic allocation.
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

Voyage AI: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens, 128 inputs/batch. Use `nexus.retry._voyage_with_retry` for transient failures.

Pagination over a large collection: `limit ≤ 300` per call, `offset += 300` in a loop.

## Hot rules (don'ts paired with dos)

- **⛔ NO new SQLite — nexus is MIGRATING from SQLite TO PG, in EVERY mode. There is NO SQLite hybrid mode** (Hal directive 2026-07-18; record: T2 `nexus/directive-no-sqlite-pg-everywhere`). SQLite is a migration SOURCE only, never a destination. Never add a SQLite table, database file, or `CREATE TABLE` bootstrap in Python; new persistent state goes to PG through Liquibase via the engine (every install ships the PG bundle — local mode's endpoint is the bundled local PG, same shape as service mode). The retirement itself is essentially complete (RDR-158 P4: the SQLite stores, local catalog, and client migration chain are deleted); any straggler SQLite artifact found in review is debt to delete, never a home for new columns/tables/features. In review, a diff adding SQLite DDL or a new `sqlite3.connect` substrate is a **Critical**. Exemptions are Hal's explicit decisions, never code comments.
- **Never `print()` in library code.** Use `structlog.get_logger(__name__).info(event=..., **fields)`.
- **`develop` release boundary LIFTED 2026-06-29** — release-blocker bead `nexus-luxe6` closed; conexus 6.0.0 (the migration-capable release) published from develop, and `develop` is releasable again. **RDR-155 P4b (the FINAL Chroma deletion) SHIPPED 2026-07-25** — the dependency is dropped (absent from `uv.lock`), `guided_upgrade_cmd.py`/`migrate_cmd.py` are deleted outright, and `nx guided-upgrade` no longer exists. Pre-PG installs redirect through a two-hop path: pin to the last migration-capable release, `conexus==6.18.1`, where `nx guided-upgrade` still runs (Chroma → PG17+pgvector, copy-not-move), then upgrade normally from there. Frozen Chroma directories on disk remain untouched rollback artifacts, not a live migration source in this version. Authoritative record: T2 `nexus/release-boundary-since-p4a` (updated).
- **Integration branch is `develop`.** Open PRs against `develop`, not `main`. `main` carries the plugin marketplace surface; the develop split protects it from in-flight churn. Releases promote `develop` to `main` via a PR-gated release branch (nexus-mkj6u) — there are NO direct-to-`main` commits at all; the push guard enforces it (`docs/contributing.md` § Release Process).
- **Never `git add -A` or `git add .`.** Stage by explicit path so untracked drafts don't sneak in.
- **Never include AI attribution in commits.** No "Generated with Claude", no `Co-Authored-By: Claude`. Bead references and `Closes #N` only.
- **Never delete RDR files.** Closing an RDR is a frontmatter `status: closed` flip — the file stays. See [`docs/rdr/AGENTS.md`](docs/rdr/AGENTS.md).
- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime.
- **`expectations_*` is a SOURCED SHELL LIB, not a tool and not an `nx` verb.** The RDR-184 background-teammate ledger (`expectations_expect` / `expectations_census` / `expectations_undeclared`) is bash. Searching the MCP tool registry for it returns nothing **by design**, and `nx expectations` / `nx orchestration` / `nx guard` do not exist (nexus-3ra9h).
  ```bash
  source tests/e2e/lib/expectations.sh                    # in this checkout
  source ~/.claude/plugins/marketplaces/*/conexus/hooks/scripts/expectations.sh   # anywhere else
  expectations_census "$SESSION_ID"      # retro counts — NEVER hand-count (nexus-hybv1)
  expectations_undeclared "$SESSION_ID"  # rc: 0 clean, 1 BLINDSPOT (recognized==0, false-clean, not a pass), 2 undeclared>0 deficit (nexus-suuja)
  ```
  The two copies are kept byte-identical by `tests/hooks/test_subagent_stop_hook.py`; edit `tests/e2e/lib/expectations.sh` and copy it over, never the reverse.
  **The EXPECT row is MECHANIZED and LIVE** (nexus-qc4p1, shipped at the 7.0.0 plugin pin; verified 2026-08-02: 70/70 recognized, 0 undeclared): a PreToolUse hook on the Agent tool (`conexus/hooks/scripts/agent-dispatch-expect.sh`) writes it from the dispatch's own `subagent_type` + `run_in_background`. **Do NOT hand-write EXPECT rows** — the ledger matches N EXPECT rows of a type against N STARTs of that type, so a manual duplicate inflates the credit pool and can mask an undeclared start. Hand-call `expectations_expect` ONLY for a dispatch the hook cannot see, keyed on the **subagent type verbatim, colon included** — never an invented name (the Agent tool has no `name` parameter; nexus-nu7fo). `conexus/skills/orchestration/SKILL.md` is canonical for the dispatch-time contract; this entry exists so the *surface* is discoverable — two 2026-07 sessions concluded the ledger was unavailable and skipped it, and it was available both times.
- **Daemon-lifecycle fixes land in the shared primitive, never one tier's copy.** Discovery / single-writer / self-heal / version-skew for T1/T2/T3 all live in `src/nexus/daemon/service_registry.py` + the conformance suite `tests/daemon/test_rdr149_lifecycle_conformance.py` (RDR-149). Editing a single tier's lifecycle without touching both is the recurring bug class. Mechanically enforced by `tests/daemon/test_lifecycle_gate.py`. See [`src/nexus/daemon/AGENTS.md`](src/nexus/daemon/AGENTS.md).

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

### Engine-service release (a SECOND lifecycle — decoupled from the PyPI release)

The Java **engine-service** binary is a separate release artifact with its own cadence. Conflating it with the PyPI/marketplace release is how the cloud engine silently drifts behind develop (2026-06-26: 22 `service/` commits / 4 days un-deployed, un-cloud-tested).

- **Artifact + trigger:** an `engine-service-vX.Y.Z` git tag fires `engine-service-release.yml`, which builds + cosign-signs the 3 native binaries (linux-amd64, linux-arm64, mac-arm64 — mac-arm64 unsmoked, no Docker on GH macOS, nexus-4xf5m; mac-amd64/Intel is not a supported target). It publishes **nothing to PyPI** and is **NOT gated by the luxe6 / RDR-155-P4a develop release boundary** (the workflow header says so explicitly). So the engine can be refreshed in the cloud at any time, independent of the unreleasable-develop state.
- **Version is tag-stamped — there is NO manifest to bump.** `release.properties` `release_version` is blank in source and stamped at native-build time from the tag (the Maven `pom.xml` stays `1.0-SNAPSHOT`, the dev coordinate). The cut is NOT just suite-green-then-tag: the `engine-release` skill (Authority: this section) enforces a full pre-tag battery — full engine suite green on the tagged commit, `tests/e2e/migration-rehearsal/run.sh --shakeout` (must end `CANDIDATE SHAKEOUT PASSED`) — then human pushes `engine-service-vX.Y.Z`, followed by a post-publish `--acquire` gate against the published bytes. `scripts/check_client_release_precondition.py --engine-tag engine-service-vX.Y.Z` gates the **DEPLOY, never the tag cut** (Hal directive 2026-08-02 — its pre-tag wiring forced conexus 7.1.0 to ship pinned to a pre-fence engine, its own flagship feature inert on fresh local installs; a red exit means the deploy waits for the client tag carrying the listed commits, per the paired-release choreography below). **A tag gates DELIVERY, not work**: engine changes are fully testable end-to-end on develop (`./mvnw test` + the Python suite's engine substrate + LSG against a `build-gate-jar.sh` dev jar) — "cannot deploy yet" is never "cannot do/test/tag it" (error recurred 3x: nexus-0ehwe thread 2026-07-31 twice, the 7.1.0/v0.1.62 inversion 2026-08-02). Use the `engine-release` skill as the executable checklist, not this summary.
- **Cut from develop tip; don't let it drift.** Cloud-relevant engine work (pooler/RLS, pgvector, catalog conformance, aspect queue, batch endpoints) lands on develop continuously. Cut + deploy + cloud-gate the engine on its own cadence. Rule of thumb: if `git log <last-engine-tag>..HEAD -- service/` is non-trivial AND cloud-relevant, cut a fresh engine **before** relying on cloud test results or pinning it into a PyPI release.
- **Prep (AI) vs cut (human).** AI preps: confirm the `service/` tree at the target commit equals a green-`service-ci` commit (the Java CI is advisory — it does not block auto-merge — so verify the full `./mvnw test` + native build actually passed on that exact tree). The human pushes the tag.
- **Deploy + cloud-gate is conexus-side (passive bus).** After the tag publishes + signs, conexus deploys the signed binary and re-runs the cloud gate (recall + hybrid parity, xr7.8.9-style). Surface an explicit "relay: deploy `engine-service-vX.Y.Z` + re-gate" to Hal — never frame the cross-instance deploy as autonomous.
- **Their gate probes the engine DIRECT; it does not prove client-visibility.** After every cloud deploy (and before signing off a release shakeout), run `tests/e2e/cloud-client-path-gate.sh` from a cloud-mode box: it asserts the engine's pinned contracts (/version fields, ez5.1 /health, client embedding_mode probe, /v1 read path) survive the PUBLIC edge. 2026-07-23 (nexus-bwulw): the edge stubbed /version and auth-gated /health, silently disabling voyage threshold gating + dimension-orphan tooling and blocking guided migrations to cloud — three client features shipped green through every engine-direct gate.
- **A new engine bumps these downstream references:** `tests/e2e/migration-rehearsal/run.sh` `COLD_TAG` default; and — **unconditionally** — `REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py`. ONE engine identity per release: the engine it was built and gated with, on every install path. Not a compatibility minimum, not a range, no "only if the release needs the features" carve-out (Hal directive 2026-07-15, after the 14h GH #1402 incident; the identical 2026-07-14 v0.1.42 episode came from exactly that carve-out). Cloud users get whatever conexus deployed; **local-mode installs get ONLY what this constant names**, so a tag that is cut, gated, and never pinned reaches nobody. That single constant also drives `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py` DERIVES it, not an independent literal), so there is nothing separate to bump there. **Sequencing — the PAIRED-RELEASE choreography (Hal directive 2026-08-02, supersedes the old "bump only after deploy, floor lags a release" reading):** when a client release carries client halves of engine features, the engine tag is cut FIRST (tag-cut is never blocked — see 3b above), the client release gates its battery against that engine and bumps the floor to it IN the same release, and the deploy fires at client-tag push, in PARALLEL with the ~90s PyPI publish workflow — any client-release precondition is satisfied the instant the client tag exists, and the engine is live before any user can install the client that requires it. Zero refusal window (GH #1402: a floor-bumped client published with NO deploy armed makes cloud clients refuse the managed service as below-identity — the deploy must fire at tag push, not "eventually") and zero inert-window (the 7.1.0/v0.1.62 inversion: floor lagging a release ships a client whose pinned engine lacks the engine halves of its own features). `scripts/check_engine_release_floor.py` fails the release in BOTH directions.

### Cutting a release (version bump + tag-push to PyPI)

**Engine-freshness gate (step 0 — BEFORE the numbered steps).** There is ONE engine-version number, `REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — not two. `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`, the exact tag a fresh local `nx init --service` install downloads) is DERIVED from it, not an independent literal — bumping `REQUIRED_ENGINE_VERSION` moves the release's engine identity AND the fresh-install pin together, by construction. (Prior to 2026-07-12 these were two separately hand-typed constants that silently drifted apart — pinned at v0.1.36 while the floor had already moved to a verified, cloud-deployed v0.1.39 — the identical failure class `nexus-b6qlf` already unified once before for a different pair of constants; see `engine_version.py`'s docstring.)

This is a BLOCKING command, not a prose eyeball-check (nexus-i5c2u — the eyeball version of this step was routinely skipped, letting the cloud engine sit at v0.1.17 for 9+ days across releases while the floor moved to v0.1.34):

```bash
uv run python scripts/check_engine_release_floor.py
```

If it exits non-zero, STOP — do not proceed with the PyPI release; cut a fresh `engine-service` tag via the `engine-release` skill (see "Engine-service release" above), bump `REQUIRED_ENGINE_VERSION` to that tag's version (this alone also moves `PINNED_SERVICE_TAG`), gate the release battery against that engine, and pair the deploy with THIS release (deploy relay fires at client-tag push, parallel with the PyPI publish — paired-release choreography, Hal directive 2026-08-02). For a paired release, cloud-behind pre-tag is the EXPECTED state, not drift: re-run the gate with `--paired-deploy engine-service-vX.Y.Z` naming the exact tag this release pairs with (nexus-k1c08). The flag accepts a below-floor cloud ONLY when the named tag independently verifies as (a) a published, non-draft GH release with assets, (b) exactly equal to `REQUIRED_ENGINE_VERSION`, and (c) the newest published `engine-service-v*` tag — any miss stays red with a named reason, never a silent pass. Post-tag, re-run the gate WITHOUT the flag as the deploy-window VERIFY; escalate loudly if it is still behind. `git log <pinned-engine-tag>..HEAD -- service/` remains useful supplementary context for judging whether recent `service/` work is cloud-relevant, but the script above — not the eyeball — is the actual gate. Shipping the PyPI release on a stale, un-cloud-validated engine is exactly the gap this gate closes.


1. **Run unit + integration suite.** `uv run pytest` and `uv run pytest -m integration`. Both must pass — integration is excluded from CI and is your last line of defense.
1b. **Run the fresh-install MVV.** `./tests/e2e/fresh-install-mvv.sh` (nexus-nolqs). The VIRGIN-journey gate — every other E2E gate tests the upgrade axis from a populated install. The unit suite then pinned the SQLite opt-out backend (since retired at RDR-158), which is how the 2026-07-21 fresh-box defect class (f1itv/e9ru2/kmo9h/r5f3c/9xfx5) shipped unseen; today the suite pins the engine substrate instead, and the MVV still covers the virgin journey no unit test walks. Builds the wheel under test, then on a scrubbed-env virgin HOME: local init (engine + portable PG + bge-768), ladder converged at init, store put + index md with ENGINE-CATALOG registration asserted (not just T3 chunks), semantic search returns both, doctor with zero ✗ and an empty warnings allowlist. Must end `FRESH-INSTALL MVV PASSED`.
2. **Audit docs against changes since last tag.** `git log --oneline v<prev>..HEAD` then check `docs/cli-reference.md`, `docs/architecture.md`, `README.md` for user-visible drift.
3. **Bump version in all seven version surfaces AND both `source.ref` fields** (CI enforces parity — see `docs/contributing.md` § Release Process step 7 for the canonical list):
   - `pyproject.toml` — `version = "X.Y.Z"`
   - `mcpb/pyproject.toml` — `version` (plus its `conexus[local]>=X.Y.Z` dependency pin; `tests/test_plugin_structure.py::test_mcpb_pins_conexus_local_extra` enforces the pin tracks the version)
   - `mcpb/manifest.json` — `version` (`tests/test_plugin_structure.py::test_mcpb_manifest_version_matches_pyproject`)
   - `.claude-plugin/marketplace.json` — both `version` fields AND both `plugins[].source.ref` fields (must be `"vX.Y.Z"` — the tag form). The `source.ref` is what decouples installed users from main HEAD: plugins are fetched from the pinned tag, not from whatever main currently is. CI test `TestMarketplaceVersion::test_marketplace_source_ref_matches_pyproject` enforces this.
   - `conexus/.claude-plugin/plugin.json` — `version`
   - `sn/.claude-plugin/plugin.json` — `version`
   - `conexus/PENDING_RELEASE.md` — cleared (`tests/test_plugin_release_drift_ledger.py` fails on a stale entry)
4. **Update changelogs.** Add a new section to `CHANGELOG.md` and `conexus/CHANGELOG.md` with the date and the changes since last release.
5. **Refresh `uv.lock`.** Run `uv sync` — the lock file MUST be committed.
6. **Run sandbox smoke.** `./tests/e2e/release-sandbox.sh smoke` (~2 min). Required for any change touching `pyproject.toml`, `uv.lock`, `src/nexus/mcp/**`, `conexus/**`, `.claude-plugin/**`, `src/nexus/commands/{doctor,upgrade}.py`. The reinstall this drives is genuinely isolated (fixed 2026-07-01, `137d2688`) — it runs cleanly with live Claude Code sessions/MCP servers active, no `--force`/`--cycle-daemons` needed. If it ever refuses again with a live-holder error, suspect a step-ordering regression (the sandbox `HOME` must be activated *before* the reinstall runs, since `uv tool install` resolves its install location off `$HOME`) before reaching for `--force`.
7. **Commit on a release branch + PR to main** (nexus-mkj6u: replaces direct-to-main convention).
   Base on **develop** (a release promotes develop to main — hot rule above; a main-based
   branch releases main's stale tree), then pre-merge `origin/main` to resolve the
   release-only conflicts on the branch (a conflicting release PR gets NO CI checks —
   release skill Step 7).
   ```
   git checkout develop && git pull && git checkout -b release/vX.Y.Z
   git merge origin/main
   <bump all manifests, refresh uv.lock, update CHANGELOGs>
   git commit -m "chore(release): conexus X.Y.Z"
   git push -u origin release/vX.Y.Z
   gh pr create --base main --title "release: conexus X.Y.Z"
   ```
   Wait for CI green. Then `gh pr merge <N> --merge` (NOT `--squash` — preserves the release commit SHA for the optional `source.sha` pin in Step 8a).
8. **Tag the merge commit IMMEDIATELY after PR lands.**
   ```
   git checkout main && git pull
   git tag -a vX.Y.Z -m "conexus X.Y.Z" $(git rev-parse HEAD)
   git push origin vX.Y.Z
   ```
   Tag-push triggers the Release workflow → PyPI auto-publish via OIDC. Order matters: marketplace.json's `source.ref` points at `vX.Y.Z`, which must exist on origin before any user runs `/plugin install`. Push commit (via PR merge), then push tag, in tight succession.
8b. **Back-merge `main` into `develop` (MANDATORY, zero-change releases included).**
   ```
   git checkout develop && git pull
   git merge origin/main --no-edit   # trivially clean right after a release
   git push origin develop
   ```
   Skipping this is how develop drifts behind the release-only commits and the next release branch conflicts (2026-07-23 incident; `docs/contributing.md` step 11b).
9. **Reinstall locally.** `scripts/reinstall-tool.sh && nx --version` — `pyproject.toml` is bumped but the local `nx` shim still points at the old wheel until reinstall.

Full checklist with rollback / one-time setup steps lives in [`docs/contributing.md` § Release Process](docs/contributing.md#release-process).

## Task tracking

Use **beads** (`bd`) for issue tracking. Find work with `bd ready`; claim with `bd update <id> --claim`; close with `bd close <id>`. Use `nx memory put` for project-context notes that persist across sessions. See `docs/contributing.md` § Git Workflow for branch naming (`feature/<bead-id>-<description>`).

## Settings

User-global permission settings live in `~/.claude/settings.json`. Never write to `settings.local.json` — it must remain `{}`.
