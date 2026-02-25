# Contributing

## Development Setup

```bash
git clone https://github.com/Hellblazer/nexus.git
cd nexus
uv sync
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

- **Python 3.12+**: use `match/case`, `tomllib`, `typing.Protocol`, walrus operator
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

## Adding an Agent or Skill

See `nx/README.md` for the plugin structure. Skills live in `nx/skills/<name>/SKILL.md`, agents in `nx/agents/<name>.md`, and both are registered in `nx/registry.yaml`.

## Version Pinning

Two packages have known breaking incompatibilities and must be pinned to exact versions in `pyproject.toml`:

- `llama-index-core` (AST chunking dependency)
- `tree-sitter-language-pack` (parser compatibility)

Do not bump these without testing the full chunking pipeline.

## Git Workflow

- Branch naming: `feature/<bead-id>-<short-description>`
- Never push directly to `main` — all changes via PR
- Use `bd` (beads) for task tracking

The `main` branch requires CI to pass before merging. Configure branch protection at
https://github.com/Hellblazer/nexus/settings/branches:
- Require status checks: `pytest (3.12)` and `pytest (3.13)`
- Require branches to be up to date before merging

## License

AGPL-3.0-or-later. For Python source files, use the SPDX header:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Agent files, skill files, config files: no header needed — the LICENSE file covers them.

## Release Process

1. **Verify tests pass**
   ```bash
   uv run pytest tests/
   ```

2. **Update version in `pyproject.toml`**
   Change the `version` field (e.g. `"1.0.0-rc1"` → `"1.0.0"`).

3. **Update `CHANGELOG.md`**
   - Rename `[Unreleased]` section to `[X.Y.Z] - YYYY-MM-DD`
   - Add a new empty `[Unreleased]` section at the top
   - Update the comparison links at the bottom

4. **Update plugin versions** (if plugin changed)
   - `nx/CHANGELOG.md`: add release entry
   - `.claude-plugin/marketplace.json`: bump `"version"` field

5. **Commit the release**
   ```bash
   git add pyproject.toml CHANGELOG.md
   git commit -m "Release vX.Y.Z"
   ```

6. **Create an annotated tag** (message becomes the GitHub release body)
   ```bash
   git tag -a vX.Y.Z -m "Release X.Y.Z

   [Paste the CHANGELOG section for this version here]"
   ```

7. **Push branch and tag**
   ```bash
   git push origin main
   git push origin vX.Y.Z
   ```

8. **CI publishes automatically**
   The `release.yml` workflow triggers on `v*` tags, runs tests, builds the wheel, publishes to PyPI via OIDC trusted publisher, and creates a GitHub release.

9. **Yank pre-release versions** (if applicable)
   Go to https://pypi.org/manage/project/conexus/releases/ and yank any versions that should not be resolved by `pip install conexus`.

### One-time PyPI Trusted Publisher Setup

Before the first release, configure PyPI to trust the GitHub Actions OIDC token:

1. Go to https://pypi.org/manage/project/conexus/settings/publishing/
2. Click "Add a new publisher"
3. Fill in:
   - **Owner**: `Hellblazer`
   - **Repository**: `nexus`
   - **Workflow filename**: `release.yml`
   - **Environment name**: (leave blank)
4. Click "Add"

This eliminates the need for a `PYPI_API_TOKEN` secret. GitHub Actions authenticates directly via OIDC.
