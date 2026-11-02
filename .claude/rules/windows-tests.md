---
paths:
  - "src/nexus/hooks/**"
  - "conexus/hooks/**"
  - "sn/hooks/**"
  - "src/nexus/daemon/**"
  - "src/nexus/_install/**"
  - "src/nexus/util/**"
  - "src/nexus/__init__.py"
  - "src/nexus/session.py"
  - "mcpb/**"
  - "web/install.ps1"
  - "tests/daemon/**"
  - "tests/hooks/**"
---

## Windows before the push

This file is in or near the code CI's Windows set covers, and that set runs only after a push. Before pushing, run `uv run scripts/win_hand_test.py --check`; when it exits 1, run the listed tests (or `--set`) on native Windows with `uv run scripts/win_hand_test.py --session <name> -- <tests>`. AGENTS.md rule 14; full text in [test-running](../../docs/agents/test-running.md) § Native Windows.
