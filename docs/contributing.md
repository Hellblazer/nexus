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

## License

AGPL-3.0-or-later. For Python source files, use the SPDX header:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Agent files, skill files, config files: no header needed — the LICENSE file covers them.
