---
paths:
  - "src/nexus/commands/**"
  - "src/nexus/cli.py"
  - "docs/cli-reference.md"
---

## Adding a CLI command

1. Create `src/nexus/commands/your_cmd.py` with a Click group/command.
2. Register it in `src/nexus/cli.py` via `cli.add_command()`.
3. Add tests in `tests/test_your_cmd.py`.
4. Document the new flags/subcommands in `docs/cli-reference.md`.
