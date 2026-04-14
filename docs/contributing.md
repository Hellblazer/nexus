# Contributing

## Development Setup

```bash
git clone https://github.com/Hellblazer/nexus.git
cd nexus
uv sync
scripts/reinstall-tool.sh   # install nx CLI (preserves optional extras)
nx hooks install             # auto-index this repo on every commit
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
nx/                  # Claude Code plugin (skills, agents, hooks)
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

T2 is split into four domain stores under `src/nexus/db/t2/`:
`memory_store.py`, `plan_library.py`, `catalog_taxonomy.py`, and
`telemetry.py`. See [architecture.md § T2 Domain Stores](architecture.md#t2-domain-stores)
for the map.

**Adding a method to an existing store** (the common case):

1. Add the method to the store's class in its own module — use the
   store's own connection via its internal methods; do not reach out
   to the facade.
2. If the feature needs a new table or column, add a per-store
   migration that runs the first time that store opens a database
   path (the existing stores show the pattern — a module-level
   `_migrated_paths: set[str]` guard + `_migrated_lock`, checked
   in `__init__`).
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
   with `PRAGMA busy_timeout = 5000`.
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

See `nx/README.md` for the plugin structure. Skills live in `nx/skills/<name>/SKILL.md`, agents in `nx/agents/<name>.md`, and both are registered in `nx/registry.yaml`.

**MCP tools in agents**: Agents do NOT declare a `tools:` or `disallowedTools:` field in frontmatter — Claude Code has confirmed bugs where these fields in plugin-defined agents filter out MCP tools or are silently ignored (see RDR-035, RDR-039). Agents inherit all tools from the parent session; the `settings.json` permissions list provides runtime enforcement. Agent body text references MCP tool syntax (not CLI commands) for storage tier operations. See `nx/README.md` § MCP Servers for tool names and parameters.

## Version Pinning

Two packages have known breaking incompatibilities and must be pinned to exact versions in `pyproject.toml`:

- `llama-index-core` (AST chunking dependency)
- `tree-sitter-language-pack` (parser compatibility)

Do not bump these without testing the full chunking pipeline.

## Git Workflow

- Branch naming: `feature/<bead-id>-<short-description>`
- Never push directly to `main` — all changes via PR (exception: version-bump release commits, see Release Process below)
- Use `bd` (beads, **≥ 1.0.0** — `brew install beads` or `brew upgrade beads`) for task tracking. Earlier 0.x versions reject the comma-separated `--status` flag the close-skill preamble uses; the bead advisory will silently report no open beads on stale installs.
- **Code review**: Plans include review tasks after implementation phases. Use `/nx:review-code` or dispatch `code-review-expert` at the designated plan steps

The `main` branch requires CI to pass before merging. Configure branch protection at
https://github.com/Hellblazer/nexus/settings/branches:

- **Rule**: `main`
- Require a pull request before merging
- Require status checks to pass before merging:
  - `pytest (3.12)`
  - `pytest (3.13)`
- Require branches to be up to date before merging
- Do not allow bypassing the above settings

## License

AGPL-3.0-or-later. For Python source files, use the SPDX header:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Agent files, skill files, config files: no header needed — the LICENSE file covers them.

## Release Process

Every step below is **required**. Missing any one of them has caused problems in the past — hence the explicit checklist.

### Step-by-step checklist

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
   scripts/reinstall-tool.sh   # preserves [mineru] and other extras
   nx --version   # must print X.Y.Z before proceeding
   ```
   `uv.lock` **must** be committed — the release pipeline pins exact versions from it.

5. **Update `CHANGELOG.md`**
   - Move everything under `## [Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section
   - Leave a fresh empty `## [Unreleased]` at the top
   - Group entries under `### Added`, `### Fixed`, `### Changed`, `### Removed`, `### Docs`

6. **Update `nx/CHANGELOG.md`** (plugin changelog — always, even if no plugin changes)
   Add a release entry. If there are no plugin-level changes, write:
   > Plugin version aligned with Nexus CLI X.Y.Z. No plugin-level functional changes.

7. **Update `.claude-plugin/marketplace.json`**
   Bump the `"version"` field in **both** the `nx` and `sn` plugin entries to match the new version.
   Also update `nx/.claude-plugin/plugin.json` and `sn/.claude-plugin/plugin.json` to match.
   Claude Code uses each plugin's `plugin.json` version to decide whether to refresh the cache — forgetting either one leaves stale skills/agents running.

8. **Commit all release artifacts directly to `main`**
   ```bash
   git add pyproject.toml uv.lock CHANGELOG.md nx/CHANGELOG.md nx/.claude-plugin/plugin.json sn/.claude-plugin/plugin.json .claude-plugin/marketplace.json docs/
   git commit -m "chore: bump version to X.Y.Z"
   git push
   ```
   Release version-bump commits go directly to `main` (not via PR) because the tag must point to `main`.

9. **Tag and push — this triggers the full release pipeline**
   ```bash
   git tag vX.Y.Z
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
| `uv.lock` | auto-updated by `uv sync` — **must be committed** |
| `CHANGELOG.md` | move Unreleased → `[X.Y.Z]`, add empty Unreleased |
| `nx/CHANGELOG.md` | add `[X.Y.Z]` entry |
| `.claude-plugin/marketplace.json` | bump `"version"` in both `nx` and `sn` plugin entries |
| `nx/.claude-plugin/plugin.json` | bump `"version"` to match — **controls nx cache refresh** |
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
