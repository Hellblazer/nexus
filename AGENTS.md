# AGENTS.md

Project guidance for AI coding agents working in this repository. `CLAUDE.md` is a symlink to this file.

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as `conexus`; the CLI entry point is `nx` (`src/nexus/` is the package).

## Quick start

```bash
uv sync                                  # install deps
scripts/reinstall-tool.sh                # install nx CLI locally (preserves extras)
uv run pytest                            # full unit suite (no API keys needed)
uv run pytest -m integration             # E2E (requires .env from .env.example)
uv sync && scripts/reinstall-tool.sh && nx --version    # after edits
```

Unit tests use `chromadb.EphemeralClient` + bundled ONNX MiniLM — no API keys or network.

## Architecture at a glance

Three storage tiers, by lifetime:

- **T1** — `chromadb` Ephemeral or per-session HTTP server. Session scratch (`nx scratch`).
- **T2** — seven domain stores behind a `T2Database` facade. Persistent notes, plans, taxonomy, telemetry, chash, aspects, aspect queue. **Substrate: MIGRATING SQLite → PG (directive 2026-07-18, see Hot rules). The SQLite+FTS5 implementation is the migration SOURCE, not the architecture.**
- **T3** — `chromadb.PersistentClient` + local ONNX (local mode) **or** `chromadb.CloudClient` + Voyage (cloud mode). Permanent knowledge (`nx store`, `nx search`).

### T1 sub-agent contract (RDR-105)

T1 is the per-MCP-process "working memory" tier. T2 is the cross-process shared bus. Discovery is hybrid: env passdown for MCP-dispatched subprocesses, single-writer `~/.config/nexus/t1_addr.<claude_pid>` for Claude-Code-spawned siblings.

- **Agent-tool sub-agents** (in-process Task dispatches) share T1 with their parent via the parent's MCP scratch tool. No separate T1 instance.
- **`claude -p` sub-processes default to `owned`** mode: their MCP spawns its own session-scoped chroma + writes its own `~/.config/nexus/t1_addr.<own_claude_pid>` file. Sealed from the parent; internally consistent for the subprocess's own Bash tools and sub-agents.
- **`claude -p` sub-processes that genuinely need parent-T1 visibility** opt in via `share_t1=True` at dispatch time. Subprocess inherits `NX_T1_HOST` / `NX_T1_PORT` and connects to the parent's chroma via HTTP.
- **Stateless one-shot operators** (`ephemeral=True`) get an in-process `EphemeralClient` only (no chroma spawn). The operator-dispatch default (`nx_answer`, `nx_tidy`, plan-runner inline planning).
- **Cross-process findings between sibling sub-processes go to T2** (`memory_put`). T1 is process-local by design; T2 is the shared bus (SQLite + WAL is multi-process-safe).
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

The single source of truth is `src/nexus/db/chroma_quotas.py` (the `QUOTAS` dataclass and `QuotaValidator`). Violating any of these at runtime produces `ChromaError: Quota exceeded`.

| Operation | Limit | Constant |
|---|---|---|
| `coll.get(limit=N)` | N ≤ 300 | `_PAGE` |
| `coll.query(n_results=N)` | N ≤ 300 | `MAX_QUERY_RESULTS` |
| `coll.upsert/add(ids=[...])` | ≤ 300 records | `MAX_RECORDS_PER_WRITE` |
| Concurrent reads / writes per coll | ≤ 10 each | `MAX_CONCURRENT_READS/WRITES` |
| Document size | ≤ 16384 bytes | `MAX_DOCUMENT_BYTES` (use `SAFE_CHUNK_BYTES = 12288`) |
| Query string | ≤ 256 chars | `MAX_QUERY_STRING_CHARS` |
| `where` predicates | ≤ 8 top-level | `MAX_WHERE_PREDICATES` |
| Embedding dims | ≤ 4096 | `MAX_EMBEDDING_DIMENSIONS` |

Voyage AI: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens, 128 inputs/batch. Use `nexus.retry._voyage_with_retry` for transient failures.

Pagination over a large collection: `limit ≤ 300` per call, `offset += 300` in a loop.

## Hot rules (don'ts paired with dos)

- **⛔ NO new SQLite — nexus is MIGRATING from SQLite TO PG, in EVERY mode. There is NO SQLite hybrid mode** (Hal directive 2026-07-18; record: T2 `nexus/directive-no-sqlite-pg-everywhere`). SQLite is a migration SOURCE only, never a destination. Never add a SQLite table, database file, or `CREATE TABLE` bootstrap in Python; new persistent state goes to PG through Liquibase via the engine (every install ships the PG bundle — local mode's endpoint is the bundled local PG, same shape as service mode). Existing client SQLite stores (`db/t2/*`, `db/migrations.py`, `ladder.db`, `chash_remap.db`, `pipeline.db`, strays) are retirement debt — never a home for new columns/tables/features. In review, a diff adding SQLite DDL or a new `sqlite3.connect` substrate is a **Critical**. Exemptions are Hal's explicit decisions, never code comments.
- **Never `print()` in library code.** Use `structlog.get_logger(__name__).info(event=..., **fields)`.
- **`develop` release boundary LIFTED 2026-06-29** — release-blocker bead `nexus-luxe6` closed; conexus 6.0.0 (the migration-capable release) published from develop, and `develop` is releasable again. `nx guided-upgrade` carries an existing install across (Chroma → PG17+pgvector, copy-not-move). **⛔ What's still blocked: RDR-155 P4b (the FINAL Chroma deletion, which also deletes the migration tool itself) — DO NOT START.** This is the two-release deprecation window's second half: 6.0.0 shipped both the new substrate AND the migration tool; Chroma stays intact as the migration source until the release AFTER this deprecation window closes. Authoritative record: T2 `nexus/release-boundary-since-p4a` (updated). Engine-side production migration is complete (`nexus_rdr/155-production-migration-complete`); Chroma sources remain untouched rollback targets in the interim.
- **Integration branch is `develop`.** Open PRs against `develop`, not `main`. `main` carries the plugin marketplace surface; the develop split protects it from in-flight churn. Releases promote `develop` to `main` via merge. The only direct-to-`main` commit allowed is the version-bump during a release (`docs/contributing.md` § Release Process).
- **Never `git add -A` or `git add .`.** Stage by explicit path so untracked drafts don't sneak in.
- **Never include AI attribution in commits.** No "Generated with Claude", no `Co-Authored-By: Claude`. Bead references and `Closes #N` only.
- **Never delete RDR files.** Closing an RDR is a frontmatter `status: closed` flip — the file stays. See [`docs/rdr/AGENTS.md`](docs/rdr/AGENTS.md).
- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime.
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
6. **Parity tests stay strict.** Any drift between `pyproject.toml` version and the four other manifests (plus `source.ref` in marketplace.json) fails CI. No `# noqa` escape hatches.

### Engine-service release (a SECOND lifecycle — decoupled from the PyPI release)

The Java **engine-service** binary is a separate release artifact with its own cadence. Conflating it with the PyPI/marketplace release is how the cloud engine silently drifts behind develop (2026-06-26: 22 `service/` commits / 4 days un-deployed, un-cloud-tested).

- **Artifact + trigger:** an `engine-service-vX.Y.Z` git tag fires `engine-service-release.yml`, which builds + cosign-signs the 4 native binaries. It publishes **nothing to PyPI** and is **NOT gated by the luxe6 / RDR-155-P4a develop release boundary** (the workflow header says so explicitly). So the engine can be refreshed in the cloud at any time, independent of the unreleasable-develop state.
- **Version is tag-stamped — there is NO manifest to bump.** `release.properties` `release_version` is blank in source and stamped at native-build time from the tag (the Maven `pom.xml` stays `1.0-SNAPSHOT`, the dev coordinate). The cut is purely: full engine suite green on the tagged commit → human pushes `engine-service-vX.Y.Z`.
- **Cut from develop tip; don't let it drift.** Cloud-relevant engine work (pooler/RLS, pgvector, catalog conformance, aspect queue, batch endpoints) lands on develop continuously. Cut + deploy + cloud-gate the engine on its own cadence. Rule of thumb: if `git log <last-engine-tag>..HEAD -- service/` is non-trivial AND cloud-relevant, cut a fresh engine **before** relying on cloud test results or pinning it into a PyPI release.
- **Prep (AI) vs cut (human).** AI preps: confirm the `service/` tree at the target commit equals a green-`service-ci` commit (the Java CI is advisory — it does not block auto-merge — so verify the full `./mvnw test` + native build actually passed on that exact tree). The human pushes the tag.
- **Deploy + cloud-gate is conexus-side (passive bus).** After the tag publishes + signs, conexus deploys the signed binary and re-runs the cloud gate (recall + hybrid parity, xr7.8.9-style). Surface an explicit "relay: deploy `engine-service-vX.Y.Z` + re-gate" to Hal — never frame the cross-instance deploy as autonomous.
- **A new engine bumps these downstream references:** `tests/e2e/migration-rehearsal/run.sh` `COLD_TAG` default; and `REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py` when the PyPI release hard-requires the new engine's features **OR advertises engine-side fixes** (for local service-mode installs the floor/pin is the ONLY fix-delivery vehicle — a changelog claiming an engine fix without the floor bump ships a broken promise to local installs and pins fresh installs to the still-broken engine). That single constant now ALSO drives `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py` derives it, not an independent literal) — bumping the floor moves the fresh-install pin with it, so there is nothing separate to bump there.

### Cutting a release (version bump + tag-push to PyPI)

**Engine-freshness gate (step 0 — BEFORE the numbered steps).** There is ONE engine-version number, `REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — not two. `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`, the exact tag a fresh local `nx init --service` install downloads) is DERIVED from it, not an independent literal — bumping `REQUIRED_ENGINE_VERSION` moves the compatibility floor AND the fresh-install pin together, by construction. (Prior to 2026-07-12 these were two separately hand-typed constants that silently drifted apart — pinned at v0.1.36 while the floor had already moved to a verified, cloud-deployed v0.1.39 — the identical failure class `nexus-b6qlf` already unified once before for a different pair of constants; see `engine_version.py`'s docstring.)

This is a BLOCKING command, not a prose eyeball-check (nexus-i5c2u — the eyeball version of this step was routinely skipped, letting the cloud engine sit at v0.1.17 for 9+ days across releases while the floor moved to v0.1.34):

```bash
uv run python scripts/check_engine_release_floor.py
```

If it exits non-zero, STOP — do not proceed with the PyPI release; cut + deploy + cloud-gate a fresh `engine-service` tag first via the `engine-release` skill (see "Engine-service release" above), bump `REQUIRED_ENGINE_VERSION` to that tag's version (this alone also moves `PINNED_SERVICE_TAG`), then re-run the script until it exits 0. `git log <pinned-engine-tag>..HEAD -- service/` remains useful supplementary context for judging whether recent `service/` work is cloud-relevant, but the script above — not the eyeball — is the actual gate. Shipping the PyPI release on a stale, un-cloud-validated engine is exactly the gap this gate closes.


1. **Run unit + integration suite.** `uv run pytest` and `uv run pytest -m integration`. Both must pass — integration is excluded from CI and is your last line of defense.
1b. **Run the fresh-install MVV.** `./tests/e2e/fresh-install-mvv.sh` (nexus-nolqs). The VIRGIN-journey gate — every other E2E gate tests the upgrade axis from a populated install, and the unit suite pins the SQLite opt-out backend, which is how the 2026-07-21 fresh-box defect class (f1itv/e9ru2/kmo9h/r5f3c/9xfx5) shipped unseen. Builds the wheel under test, then on a scrubbed-env virgin HOME: local init (engine + portable PG + bge-768), ladder converged at init, store put + index md with ENGINE-CATALOG registration asserted (not just T3 chunks), semantic search returns both, doctor with zero ✗ and an empty warnings allowlist. Must end `FRESH-INSTALL MVV PASSED`.
2. **Audit docs against changes since last tag.** `git log --oneline v<prev>..HEAD` then check `docs/cli-reference.md`, `docs/architecture.md`, `README.md` for user-visible drift.
3. **Bump version in all four manifests AND both `source.ref` fields** (CI enforces parity):
   - `pyproject.toml` — `version = "X.Y.Z"`
   - `.claude-plugin/marketplace.json` — both `version` fields AND both `plugins[].source.ref` fields (must be `"vX.Y.Z"` — the tag form). The `source.ref` is what decouples installed users from main HEAD: plugins are fetched from the pinned tag, not from whatever main currently is. CI test `TestMarketplaceVersion::test_marketplace_source_ref_matches_pyproject` enforces this.
   - `conexus/.claude-plugin/plugin.json` — `version`
   - `sn/.claude-plugin/plugin.json` — `version`
4. **Update changelogs.** Add a new section to `CHANGELOG.md` and `conexus/CHANGELOG.md` with the date and the changes since last release.
5. **Refresh `uv.lock`.** Run `uv sync` — the lock file MUST be committed.
6. **Run sandbox smoke.** `./tests/e2e/release-sandbox.sh smoke` (~2 min). Required for any change touching `pyproject.toml`, `uv.lock`, `src/nexus/db/migrations.py`, `src/nexus/mcp/**`, `conexus/**`, `.claude-plugin/**`, `src/nexus/commands/{doctor,upgrade}.py`. The reinstall this drives is genuinely isolated (fixed 2026-07-01, `137d2688`) — it runs cleanly with live Claude Code sessions/MCP servers active, no `--force`/`--cycle-daemons` needed. If it ever refuses again with a live-holder error, suspect a step-ordering regression (the sandbox `HOME` must be activated *before* the reinstall runs, since `uv tool install` resolves its install location off `$HOME`) before reaching for `--force`.
7. **Commit on a release branch + PR to main** (nexus-mkj6u: replaces direct-to-main convention).
   ```
   git checkout main && git pull && git checkout -b release/vX.Y.Z
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
9. **Reinstall locally.** `scripts/reinstall-tool.sh && nx --version` — `pyproject.toml` is bumped but the local `nx` shim still points at the old wheel until reinstall.

Full checklist with rollback / one-time setup steps lives in [`docs/contributing.md` § Release Process](docs/contributing.md#release-process).

## Task tracking

Use **beads** (`bd`) for issue tracking. Find work with `bd ready`; claim with `bd update <id> --claim`; close with `bd close <id>`. Use `nx memory put` for project-context notes that persist across sessions. See `docs/contributing.md` § Git Workflow for branch naming (`feature/<bead-id>-<description>`).

## Settings

User-global permission settings live in `~/.claude/settings.json`. Never write to `settings.local.json` — it must remain `{}`.
