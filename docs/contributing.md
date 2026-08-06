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
`InMemoryVectorClient` (chromadb is not a dependency) over the suite's
self-provisioned engine substrate, so it needs **no** running daemon or
service. `nx init` is only required for shell CLI usage
(`nx memory`, `nx index`, `nx search`) against persistent state; it provisions
and starts the nexus-service that serves every tier in the default config, and
offers to register the OS autostart unit (accept it, or use `--no-autostart`
for a session-only supervisor).

The SQLite T2 daemon is retired (nexus-i711w), so there is no daemon to hack
on, restart after a version bump, or run in the foreground. T2 is served by
the nexus-service; for that stack use `nx daemon service start|stop|status`.

## Running Tests

```bash
uv run pytest                         # full suite, no API keys needed
uv run pytest -m integration          # E2E tests (requires real API keys)
uv run pytest --cov=nexus             # with coverage
uv run pytest tests/test_indexer.py   # single file
uv run pytest -k "test_frecency"      # by name pattern
```

Unit tests use `InMemoryVectorClient` + bundled ONNX MiniLM model — no accounts needed.

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

T2 is split into domain stores under `src/nexus/db/t2/`, each an HTTP
client against the engine's Postgres (the SQLite twins were deleted in
RDR-158 P4, nexus-i711w): `http_memory_store.py`,
`http_plan_library.py`, `http_taxonomy_store.py`,
`http_telemetry_store.py`, `http_chash_index.py`,
`http_document_aspects_store.py`, `http_aspect_queue.py`, and
`http_document_highlights_store.py`. See
[architecture.md § T2 Domain Stores](architecture.md#t2-domain-stores)
for the map (note: `chash_index`, `taxonomy`, `document_aspects`, and
`aspect_queue` are reached directly via their attributes, not through
facade delegates).

**Adding a method to an existing store** (the common case):

1. Add the method to the store's class in its own module — use the
   store's own HTTP session / engine routes via its internal methods;
   do not reach out to the facade.
2. If the feature needs a new table or column, that is a Liquibase
   changeset in the engine — the client-side migration chain is
   DELETED (RDR-158 P4 Stage 4; there are no per-store SQLite
   migrations in any mode).
   - **Substrate boundary (RDR-120 §A8, restated for the engine
     era):** the changeset ships DDL only. Any work beyond DDL
     (per-row backfills, sweeps, content seeding) belongs in a
     consumer verb under the matching `nx <area>` command group or
     an upgrade-ladder rung. The narrow set of exceptions lives in
     RDR-120 §Research Findings ("§A8-exempt substrate-owned
     writes"); if your change is not on that list, it ships
     DDL-only and the data work moves to a
     consumer verb.
3. If external callers should be able to use the method via the
   `T2Database` facade for backward compatibility, add a one-line
   delegate on `T2Database` in `src/nexus/db/t2/__init__.py`.
   Otherwise prefer the domain call style: `db.memory.your_method(...)`.
4. Tests go in the matching file — `tests/test_memory.py`,
   `tests/test_plan_library.py`, `tests/test_taxonomy.py`,
   or `tests/test_t2.py` for cross-domain cases.

**Adding a whole new domain store** (rare):

1. Schema first: the store's tables are Liquibase changesets in the
   engine (`service/`), DDL-only per the substrate boundary above.
   There is no client-side DDL in any mode (NO-SQLITE directive,
   2026-07-18).
2. Add the engine routes (Java) and gate them with the engine's own
   test suite.
3. Create `src/nexus/db/t2/http_<your_domain>_store.py` following the
   existing `Http*Store` twins (bearer auth, tenant header,
   `_refreshable_client` session handling).
4. Add the store to `T2Database.__init__` in construction order and
   tear it down in `T2Database.close()`.
5. If your store registers cross-domain expiry work, add it to
   `T2Database.expire()`.

**Concurrency rules**:

- Concurrency is arbitered by Postgres in the engine — the client
  stores are stateless HTTP clients and need no cross-store locking.
- Do not add a global T2 lock client-side. If two domains genuinely
  need to coordinate (rare), that coordination belongs in the engine
  (one transaction, one route), not in Python.

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
- `main` is fully PR-gated, release version-bumps included (nexus-mkj6u replaced the prior direct-to-main carve-out). See Release Process below.
- Use `bd` (beads, **≥ 1.0.0**: `brew install beads` or `brew upgrade beads`) for task tracking. Earlier 0.x versions reject the comma-separated `--status` flag the close-skill preamble uses; the bead advisory will silently report no open beads on stale installs.
- **Code review**: Plans include review tasks after implementation phases. Use `/conexus:review-code` or dispatch `code-review-expert` at the designated plan steps.

Both `main` and `develop` carry branch protection. Configure at
https://github.com/Hellblazer/nexus/settings/branches:

- **Rules** (apply to both `main` and `develop`):
  - Require a pull request before merging
  - Require status checks to pass before merging:
    - `pytest-gate` (one required check on both `main` and `develop`; it fans
      in over the sharded pytest matrix — the swap from the prior
      `pytest (Python 3.12)` / `pytest (Python 3.13)` two-check shape
      happened at nexus-n0ful)
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

   **Paired release** (Hal directive 2026-08-02): when this release bumps the
   floor to an engine tag whose deploy fires AT client-tag push, cloud-behind
   pre-tag is EXPECTED, not drift. Re-run with `--paired-deploy` naming the
   exact tag (nexus-k1c08):
   ```bash
   uv run python scripts/check_engine_release_floor.py --paired-deploy engine-service-vX.Y.Z
   ```
   The flag accepts a below-floor cloud only when the named tag independently
   verifies as a published (non-draft, with assets) GH release, exactly equal
   to `REQUIRED_ENGINE_VERSION`, and the newest published engine tag — any
   single miss stays red with a named reason. On acceptance it prints a
   "PAIRED MODE" acknowledgment and a POST-TAG VERIFY obligation: once the
   deploy lands, re-run the same command WITHOUT `--paired-deploy` to confirm
   convergence; escalate loudly if it is still behind at that point.

   The reverse direction — an engine deploying ahead of the client commits
   it requires — is a separate gate, `scripts/check_client_release_precondition.py`,
   run from the `engine-release` skill before a new `engine-service-v*` tag
   deploys (nexus-9ssih deploy order); it is not part of this PyPI checklist.

1. **Verify the full release test battery passes**
   ```bash
   uv run pytest                                             # unit suite (no API keys)
   tests/e2e/local-service-gate.sh                           # integration incl. the local-service functional gate
   tests/e2e/migration-rehearsal/run.sh --package-upgrade    # ONE-engine convergence MVV (nexus-cfgo9)
   tests/e2e/fresh-install-mvv.sh                             # VIRGIN-journey gate (nexus-nolqs), LOCAL WHEEL layer
   ```
   All must pass. Bare `uv run pytest -m integration` is not enough on its
   own: the local-service round-trip family self-provisions inside
   `local-service-gate.sh` and otherwise skip-gates silently on an absent
   service (the 74/516 ambient-degradation class the gate was built to end).
   Integration is excluded from CI — this battery is your last line of
   defense before tag-push. See `.claude/skills/release/SKILL.md` Step 1 for
   the authoritative, up-to-date version of this list.

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
   engine-catalog registration asserted → search → doctor (zero ✗,
   warnings allowlist reviewed for new drift). Complements the upgrade-axis
   gates (rehearsal, era-hop, guided) which all start from a populated
   install — the 2026-07-21 fresh-box defect class was invisible to every
   one of them. Must end `FRESH-INSTALL MVV PASSED — ... (LOCAL WHEEL,
   release-battery layer)`.

   This is the LOCAL WHEEL layer: it builds and installs the tree under
   test, so it proves the release candidate works, but it resolves
   dependencies from `uv.lock`/the wheel's own metadata, not PyPI. It does
   NOT exercise a fresh `uv tool install`'s independent resolution — that
   is a separate, POST-publish layer:
   ```bash
   ./tests/e2e/fresh-install-mvv.sh --published [X.Y.Z]   # omit X.Y.Z for latest
   ```
   `--published` installs the ACTUAL PyPI artifact via
   `uv tool install conexus[==X.Y.Z]`, isolated to a scrubbed sandbox HOME
   exactly like the default layer (never touches the live `~/.local/share/uv`
   or `~/.local/bin`). This is the layer nexus-l2ku5 broke (`mcp>=1.0`
   resolved `mcp` 2.0.0 fresh from PyPI and killed both MCP servers for 4
   days while every pre-existing gate ran pinned to the dev venv's
   `uv.lock` and saw nothing) — it belongs to the POST-publish shakedown
   (T2 `nexus/shakedown-playbook` §2 S1), not this pre-tag battery, since
   there is nothing on PyPI yet to install at this point in the checklist.
   Run it manually after a tag publishes to verify what PyPI is actually
   serving; see the shakedown playbook for the standing T1 trigger.

7b. **Run the sandbox smoke** (~2 min)
   ```bash
   ./tests/e2e/release-sandbox.sh smoke
   ```
   Required for any change touching `pyproject.toml`, `uv.lock`,
   `src/nexus/mcp/**`, `conexus/**`,
   `.claude-plugin/**`, or `src/nexus/commands/{doctor,upgrade}.py` — which
   a release always does (the version bumps alone qualify). The reinstall it
   drives is genuinely isolated and runs cleanly with live Claude Code
   sessions/MCP servers active; if it ever refuses with a live-holder error,
   suspect a step-ordering regression before reaching for `--force`
   (AGENTS.md § Cutting a release, step 6).

7c. **Run the sandbox shakedown** (~5-10 min warm cache, +10-15 min cold)
   ```bash
   ./tests/e2e/release-sandbox.sh shakedown
   ```
   Required on every release. Smoke (7b) only reinstalls and runs `nx
   doctor` checks; it never calls `nx index pdf`, so it cannot catch an
   indexing regression. The shakedown does — including MinerU end-to-end
   through the production `nx index pdf` path (step 3b of 11, the
   `bft-to-smr.pdf` formula fixture) — and it is the ONLY gate that does:
   the slow-marked `test_mineru_path_preserves_formulas` pytest test is
   not part of any default or scheduled run (nexus-6xkdu). Cold cache
   pays MinerU's ~2-3 GB model download once. All four indexing steps
   (2, 3a, 3b, 4) can fail the run — the `|| true` that previously made
   them theatre was removed at nexus-6xkdu — and the run ends with an
   explicit `SHAKEDOWN PASSED`/`SHAKEDOWN FAILED` verdict line. Halt on
   any failure.

8. **Commit on a release branch and PR to `main`** (branch protection requires a PR; do NOT direct-push).
   Base the release branch on **develop**, not main — a release PROMOTES develop's accumulated
   state to main (§ Git Workflow above); branching off main would release main's stale tree with
   new version numbers, omitting everything on develop. Then pre-merge `origin/main` so the
   always-conflicting release-only files (CHANGELOGs, manifests) are resolved on the branch —
   a release PR that still conflicts gets NO CI checks (see `.claude/skills/release/SKILL.md`
   Step 7 for the changelog-union conflict resolution).
   ```bash
   git checkout develop && git pull && git checkout -b release/vX.Y.Z
   git merge origin/main   # resolve release-only conflicts here, not in the PR
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

11b. **Back-merge `main` into `develop` (MANDATORY, zero-change releases included)**
    ```bash
    git checkout develop && git pull
    git merge origin/main --no-edit    # trivially clean right after a release:
                                       # the release branch just CONTAINED develop
    git push origin develop
    ```
    Earned by the 2026-07-23 incident: from 6.12.0 through 6.17.0 no release
    was ever merged back, so develop's seven manifests froze at 6.11.0 — all
    stale TOGETHER, so the parity tests stayed green on a coherent lie. Every
    dev-tree install self-identified as 6.11.0, doctor nagged, and the
    downgrade guard misfired on reinstalls. Running this step immediately
    after tag-push is the moment the merge is conflict-free by construction
    (see `.claude/skills/release/SKILL.md` Step 11b).

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
| `src/nexus/upgrade_ladder/` | new DATA-convergence axes land as rungs registered in `registry.py` (the client-side T2 migration chain is deleted — RDR-158 P4 Stage 4; schema is Liquibase in the engine) |
| `conexus/PENDING_RELEASE.md` | empty the pending-drift list for every entry this release ships — advancing `source.ref` is what makes the declared plugin changes live; a stale entry fails `tests/test_plugin_release_drift_ledger.py` |

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
