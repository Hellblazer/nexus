# Contributing

## Development Setup

```bash
git clone https://github.com/Hellblazer/nexus.git
cd nexus
uv sync
scripts/reinstall-tool.sh           # install nx CLI (preserves optional extras)
nx init                              # provision + start the local service backend (RDR-174 collapsed flow)
nx hooks install                     # auto-index this repo on every commit
```

The unit suite is self-contained — `uv run pytest` uses an in-process
`chromadb.EphemeralClient` and a tmp-path SQLite, so it needs **no** running
daemon or service. `nx init` is only required for shell CLI usage
(`nx memory`, `nx index`, `nx search`) against persistent state; it provisions
and starts the nexus-service that serves every tier in the default config, and
offers to register the OS autostart unit (accept it, or use `--no-autostart`
for a session-only supervisor).

If you work with the opt-in SQLite T2 backend (`NX_STORAGE_BACKEND=sqlite`) and
want to hack on the T2 daemon itself, stop the autostart-managed instance and
run it in the foreground:

```bash
launchctl bootout gui/$(id -u)/com.nexus.t2     # macOS
# or
systemctl --user stop nexus-t2.service          # Linux
nx daemon t2 start                              # foreground, ^C to stop
```

After every conexus version bump (including local edits), restart the
LaunchAgent-managed daemon so it picks up the new code:

```bash
launchctl kickstart -k gui/$(id -u)/com.nexus.t2
```

## Running Tests

```bash
uv run pytest                         # full suite, no API keys needed
uv run pytest -m integration          # E2E tests (requires real API keys)
uv run pytest --cov=nexus             # with coverage
uv run pytest tests/test_indexer.py   # single file
uv run pytest -k "test_frecency"      # by name pattern
```

Unit tests use `chromadb.EphemeralClient` + bundled ONNX MiniLM model — no accounts needed.

For integration tests: copy `.env.example` to `.env`, fill in your keys, then:

```bash
set -a && source .env && set +a
uv run pytest -m integration
```

### Storage-stack sandbox gate (T1 + T2 + T3)

The HTTP storage-tier suites (`tests/db/test_http_*_integration.py` and the Java
serving contract tests) run entirely in-sandbox: each spins up its own ephemeral
PG17 + a fresh service JAR with an isolated bearer — no production data, no live
daemon, no API keys. Because they are `@pytest.mark.integration` (excluded from the
default CI/unit run), storage-stack regressions can rot unseen. One button-press
runs the whole tier stack:

```bash
scripts/validate/integration-stack.sh               # build jar, run T1+T2+T3
scripts/validate/integration-stack.sh --no-build    # reuse the existing jar
scripts/validate/integration-stack.sh --python-only # T1/T2/catalog suites only
scripts/validate/integration-stack.sh --java-only   # T3 + repo-layer Java tests only
```

Run it after any change to the HTTP stores, the Java service handlers/schema, or
the token/RLS model. Prereqs (dev box): a JDK/GraalVM and pg17 binaries. When the
prereqs are absent the suites self-skip and the gate reports **inconclusive**
(non-zero exit), never a false green.

## Code Conventions

- **Python 3.12–3.13**: use `match/case`, `tomllib`, `typing.Protocol`, walrus operator
- **Type hints everywhere**: all public functions, methods, module-level variables
- **No ORM**: raw `sqlite3` for T2
- **Logging**: `structlog` — never `print()` in library code
- **TDD**: write tests before implementation
- **Package manager**: `uv` (not pip directly)

## Project Structure

```
src/nexus/           # Core Python package
  commands/          # Click CLI commands (one file per group)
  db/                # Storage tier implementations (t1, t2, t3)
conexus/                  # Claude Code plugin (skills, agents, hooks)
tests/               # pytest test suite
docs/                # Documentation
```

See [architecture.md](architecture.md) for the full module map.

## Adding a CLI Command

1. Create `src/nexus/commands/your_cmd.py` with a Click group or command
2. Register it in `src/nexus/cli.py` via `cli.add_command()`
3. Add tests in `tests/test_your_cmd.py`
4. Document in `docs/cli-reference.md`

## Adding a T2 Domain Feature

T2 is split into eight domain stores under `src/nexus/db/t2/`:
`memory_store.py`, `plan_library.py`, `catalog_taxonomy.py`,
`telemetry.py`, `chash_index.py`, `document_aspects.py`,
`aspect_extraction_queue.py`, and `document_highlights.py`. See
[architecture.md § T2 Domain Stores](architecture.md#t2-domain-stores)
for the map (note: `chash_index`, `taxonomy`, `document_aspects`, and
`aspect_queue` are reached directly via their attributes, not through
facade delegates).

**Adding a method to an existing store** (the common case):

1. Add the method to the store's class in its own module — use the
   store's own connection via its internal methods; do not reach out
   to the facade.
2. If the feature needs a new table or column, add a per-store
   migration that runs the first time that store opens a database
   path (the existing stores show the pattern — a module-level
   `_migrated_paths: set[str]` guard + `_migrated_lock`, checked
   in `__init__`).
   - **Substrate boundary (RDR-120 §A8):** the migration body must
     ship DDL only. Any work beyond DDL (per-row backfills, sweeps,
     content seeding) belongs in a consumer verb under the matching
     `nx <area>` command group, not in `migrations.py`. The narrow
     set of exceptions lives in RDR-120 §Research Findings ("§A8-
     exempt substrate-owned writes"); if your migration is not on
     that list, it ships DDL-only and the data work moves to a
     consumer verb.
3. If external callers should be able to use the method via the
   `T2Database` facade for backward compatibility, add a one-line
   delegate on `T2Database` in `src/nexus/db/t2/__init__.py`.
   Otherwise prefer the domain call style: `db.memory.your_method(...)`.
4. Tests go in the matching file — `tests/test_memory.py`,
   `tests/test_plan_library.py`, `tests/test_taxonomy.py`,
   or `tests/test_t2.py` for cross-domain cases.

**Adding a whole new domain store** (rare):

1. Create `src/nexus/db/t2/<your_domain>.py` with a store class that
   takes a `Path` and opens its own `sqlite3.Connection` in WAL mode
   with `PRAGMA busy_timeout = 30000` (the canonical serving value,
   `nexus.db.t2._tuning.SERVING_BUSY_TIMEOUT_MS`, raised from 5000 in
   RDR-129 B1).
2. Add a `threading.Lock` on the store and guard every write with it.
3. Add the store to `T2Database.__init__` in construction order
   (stores created later may depend on earlier ones — `CatalogTaxonomy`
   holds a reference to `MemoryStore`, for example).
4. Make sure `T2Database.close()` tears your store down in reverse
   construction order.
5. If your store registers cross-domain expiry work, add it to
   `T2Database.expire()`.
6. Add concurrency coverage to `tests/test_t2_concurrency.py`.

**Concurrency rules**:

- Never share a connection across threads outside of that store's own
  lock — the whole point of Phase 2 is that each store owns its own
  connection and coordinates with other domains at the SQLite WAL
  layer, not through a shared Python mutex.
- Do not add a global T2 lock. If two domains genuinely need to
  coordinate (rare), prefer a targeted SQLite transaction at a single
  store and document the constraint in that store's module.
- Tests that exercise multi-store behaviour should use a temp file
  path, not `":memory:"`. `:memory:` databases are per-connection, so
  the four stores would each see their own empty database.

## Adding an Agent or Skill

See `conexus/README.md` for the plugin structure. Skills live in `conexus/skills/<name>/SKILL.md`, agents in `conexus/agents/<name>.md`, and both are registered in `conexus/registry.yaml`.

**MCP tools in agents**: Agents do NOT declare a `tools:` or `disallowedTools:` field in frontmatter — Claude Code has confirmed bugs where these fields in plugin-defined agents filter out MCP tools or are silently ignored (see RDR-035, RDR-039). Agents inherit all tools from the parent session; the `settings.json` permissions list provides runtime enforcement. Agent body text references MCP tool syntax (not CLI commands) for storage tier operations. See `conexus/README.md` § MCP Servers for tool names and parameters.

## Version Pinning

Two packages have known breaking incompatibilities and must be pinned to exact versions in `pyproject.toml`:

- `llama-index-core` (AST chunking dependency)
- `tree-sitter-language-pack` (parser compatibility)

Do not bump these without testing the full chunking pipeline.

## Git Workflow

- Branch naming: `feature/<bead-id>-<short-description>`
- **Integration branch is `develop`.** Open PRs against `develop`, not `main`. `main` carries the plugin marketplace surface; the develop split protects it from in-flight churn. Releases promote `develop` to `main` via merge (or merge-then-tag).
- Direct pushes to `main` are reserved for the version-bump commit during a release. See Release Process below.
- Use `bd` (beads, **≥ 1.0.0**: `brew install beads` or `brew upgrade beads`) for task tracking. Earlier 0.x versions reject the comma-separated `--status` flag the close-skill preamble uses; the bead advisory will silently report no open beads on stale installs.
- **Code review**: Plans include review tasks after implementation phases. Use `/conexus:review-code` or dispatch `code-review-expert` at the designated plan steps.

Both `main` and `develop` carry branch protection. Configure at
https://github.com/Hellblazer/nexus/settings/branches:

- **Rules** (apply to both `main` and `develop`):
  - Require a pull request before merging
  - Require status checks to pass before merging:
    - `pytest (Python 3.12)`
    - `pytest (Python 3.13)`
  - Require branches to be up to date before merging
  - Do not allow force-pushes (the develop reset on 2026-05-21 was a one-time bypass via the API; routine resets are not permitted).

## License

AGPL-3.0-or-later, dual-licensed with a commercial option (see
[LICENSING.md](../LICENSING.md)). For Python source files, use the SPDX header:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Agent files, skill files, config files: no header needed — the LICENSE file covers them.

Contributions are accepted under the terms in
[LICENSING.md § Contributions](../LICENSING.md#contributions): contributed
code is AGPL-3.0-or-later and may also be included in commercially licensed
editions. This is what keeps the dual-license offer viable.

## Release Process

Every step below is **required**. Missing any one of them has caused problems in the past — hence the explicit checklist.

### Step-by-step checklist

0. **Engine-freshness gate (BLOCKING — run before everything else)**
   ```bash
   uv run python scripts/check_engine_release_floor.py
   ```
   Non-zero exit = STOP. Do not proceed with the PyPI release: cut, deploy,
   and cloud-gate a fresh `engine-service-v*` tag first (the `engine-release`
   skill / AGENTS.md § Engine-service release), bump
   `REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py` to that tag
   (this alone also moves the fresh-install pin, `PINNED_SERVICE_TAG`), then
   re-run the script until it exits 0. This is a command gate, not an
   eyeball check — the prose version of this step was routinely skipped and
   let releases ship against a stale, un-cloud-validated engine
   (nexus-i5c2u).

1. **Verify the full test suite passes (unit + integration)**
   ```bash
   uv run pytest tests/                    # unit tests (no API keys needed)
   uv run pytest -m integration            # E2E tests (requires real API keys)
   ```
   Both must pass. Integration tests are excluded from CI — they are your last
   line of defense before release. Do not skip them.

2. **Audit docs against changes since last release**
   Run `git log --oneline v<prev>..HEAD` and check each feature/fix against the docs:
   - `docs/cli-reference.md` — new or changed CLI flags, subcommands
   - `docs/architecture.md` — new modules, changed module responsibilities
   - `docs/repo-indexing.md` — indexing pipeline changes, new languages, chunking behavior
   - `docs/configuration.md` — new config keys or tuning parameters
   - `docs/storage-tiers.md` — new storage capabilities (export, import, etc.)
   - `README.md` — high-level feature descriptions, command table

   Every user-visible feature must be documented before release. This step has been skipped
   in the past and required patch releases to fix — hence it is now mandatory.

3. **Bump the version in `pyproject.toml`**
   Change the `version` field (e.g. `"1.2.0"` → `"1.3.0"`).
   Semver: `MAJOR` for breaking changes, `MINOR` for new features, `PATCH` for bug fixes.

4. **Regenerate `uv.lock` and reinstall the local tool**
   ```bash
   uv sync
   scripts/reinstall-tool.sh   # preserves [local] and other extras (mineru is now a default dep)
   nx --version   # must print X.Y.Z before proceeding
   ```
   `uv.lock` **must** be committed — the release pipeline pins exact versions from it.

5. **Update `CHANGELOG.md`**
   - Move everything under `## [Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section
   - Leave a fresh empty `## [Unreleased]` at the top
   - Group entries under `### Added`, `### Fixed`, `### Changed`, `### Removed`, `### Docs`

6. **Update `conexus/CHANGELOG.md`** (plugin changelog — always, even if no plugin changes)
   Add a release entry. If there are no plugin-level changes, write:
   > Plugin version aligned with Nexus CLI X.Y.Z. No plugin-level functional changes.

7. **Bump every manifest in lock-step (CI enforces parity)**
   All seven version surfaces must equal the new `X.Y.Z`, and **both** `source.ref` fields must become `vX.Y.Z`:
   - `pyproject.toml` — `version`
   - `mcpb/pyproject.toml` — `version`
   - `mcpb/manifest.json` — `version`
   - `.claude-plugin/marketplace.json` — both `plugins[].version` (nx + sn) **and both `plugins[].source.ref`** (the pinned tag that decouples installed users from main HEAD; CI test `TestMarketplaceVersion::test_marketplace_source_ref_matches_pyproject` enforces `source.ref == "v" + pyproject.version`)
   - `conexus/.claude-plugin/plugin.json` — `version` (controls nx plugin cache refresh)
   - `sn/.claude-plugin/plugin.json` — `version` (controls sn plugin cache refresh)

   Forgetting any one fails CI parity; forgetting `source.ref` ships a release that installed Claude Code users never receive.

7a. **Run the fresh-install MVV** (~3-5 min; downloads on first run)
   ```bash
   ./tests/e2e/fresh-install-mvv.sh
   ```
   The virgin-journey gate (nexus-nolqs): wheel under test → scrubbed-env
   virgin HOME → local init (ladder converged) → store/index with
   engine-catalog registration asserted → search → doctor (zero ✗, empty
   warnings allowlist). Complements the upgrade-axis gates (rehearsal,
   era-hop, guided) which all start from a populated install — the
   2026-07-21 fresh-box defect class was invisible to every one of them.
   Must end `FRESH-INSTALL MVV PASSED`.

7b. **Run the sandbox smoke** (~2 min)
   ```bash
   ./tests/e2e/release-sandbox.sh smoke
   ```
   Required for any change touching `pyproject.toml`, `uv.lock`,
   `src/nexus/db/migrations.py`, `src/nexus/mcp/**`, `conexus/**`,
   `.claude-plugin/**`, or `src/nexus/commands/{doctor,upgrade}.py` — which
   a release always does (the version bumps alone qualify). The reinstall it
   drives is genuinely isolated and runs cleanly with live Claude Code
   sessions/MCP servers active; if it ever refuses with a live-holder error,
   suspect a step-ordering regression before reaching for `--force`
   (AGENTS.md § Cutting a release, step 6).

8. **Commit on a release branch and PR to `main`** (branch protection requires a PR; do NOT direct-push)
   ```bash
   git checkout main && git pull && git checkout -b release/vX.Y.Z
   git add pyproject.toml mcpb/pyproject.toml mcpb/manifest.json uv.lock \
           CHANGELOG.md conexus/CHANGELOG.md \
           conexus/.claude-plugin/plugin.json sn/.claude-plugin/plugin.json \
           .claude-plugin/marketplace.json docs/
   git commit -m "chore(release): conexus X.Y.Z"
   git push -u origin release/vX.Y.Z
   gh pr create --base main --title "release: conexus X.Y.Z"
   ```
   Wait for CI green, then `gh pr merge <N> --merge` (NOT `--squash` — preserves the release commit SHA). The tag in step 9 points at the merge commit. The human cuts the release; AI prepares the branch.

9. **Tag the merge commit and push — this triggers the full release pipeline**
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "conexus X.Y.Z" $(git rev-parse HEAD)
   git push origin vX.Y.Z
   ```
   The `release.yml` workflow:
   - Runs tests on Python 3.12 and 3.13
   - Verifies the tag matches `pyproject.toml` version
   - Extracts release notes from the matching `## [X.Y.Z]` section in `CHANGELOG.md`
   - Builds wheel + sdist
   - Publishes to PyPI via OIDC trusted publisher
   - Creates a GitHub release with the extracted notes and build artifacts

10. **Verify the release**
    ```bash
    gh run watch   # watch CI until green
    gh release view vX.Y.Z
    uv pip compile --no-deps conexus==X.Y.Z  # confirm version resolves on PyPI
    ```

11. **Yank pre-release versions** (if applicable)
    Go to https://pypi.org/manage/project/conexus/releases/ and yank any `rcN`, `alpha`, or `beta` versions that should not be resolved by `pip install conexus`.

### Quick reference — files that change every release

| File | What to update |
|------|----------------|
| `pyproject.toml` | `version` field |
| `mcpb/pyproject.toml` | `version` field |
| `mcpb/manifest.json` | `version` field |
| `uv.lock` | auto-updated by `uv sync` — **must be committed** |
| `CHANGELOG.md` | move Unreleased → `[X.Y.Z]`, add empty Unreleased |
| `conexus/CHANGELOG.md` | add `[X.Y.Z]` entry |
| `.claude-plugin/marketplace.json` | bump both `plugins[].version` (nx + sn) **and both `plugins[].source.ref` to `vX.Y.Z`** (parity-tested) |
| `conexus/.claude-plugin/plugin.json` | bump `"version"` to match — **controls nx cache refresh** |
| `sn/.claude-plugin/plugin.json` | bump `"version"` to match — **controls sn cache refresh** |
| `docs/cli-reference.md` | new/changed CLI flags and subcommands |
| `docs/architecture.md` | new/changed modules |
| `docs/repo-indexing.md` | indexing pipeline changes |
| `docs/configuration.md` | new config keys or tuning parameters |
| `docs/storage-tiers.md` | new storage capabilities |
| `README.md` | high-level feature descriptions |
| `src/nexus/db/migrations.py` | verify `PRE_REGISTRY_VERSION` matches previous release; new T2 migrations in `MIGRATIONS` list; new T3 steps in `T3_UPGRADES` |

### Pre-push release checklist

Before pushing the version-bump commit, verify:

```bash
git diff --name-only HEAD          # uv.lock must appear here
nx --version                       # must print the new X.Y.Z
grep "^version" pyproject.toml    # must match the tag you'll push
```

If `uv.lock` is not in the diff, you forgot to run `uv sync` or forgot to stage it.
**Do not push the tag until `uv.lock` is committed.**

### One-time Release Infrastructure Setup

Two things to configure before the first automated release:

#### 1. GitHub `pypi-release` Environment

The release workflow uses a GitHub Actions environment named `pypi-release` to gate PyPI publishing. Create it at https://github.com/Hellblazer/nexus/settings/environments:

1. Click "New environment"
2. Name: `pypi-release`
3. Optionally add required reviewers (manual approval gate before publish)
4. Save

#### 2. PyPI Trusted Publisher

Configure PyPI to accept OIDC tokens from the `pypi-release` environment:

1. Go to https://pypi.org/manage/project/conexus/settings/publishing/
2. Click "Add a new publisher"
3. Fill in:
   - **Owner**: `Hellblazer`
   - **Repository**: `nexus`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi-release`
4. Click "Add"

The environment name in PyPI must match exactly — `pypi-release` — or OIDC authentication will fail. This eliminates the need for a `PYPI_API_TOKEN` secret.
