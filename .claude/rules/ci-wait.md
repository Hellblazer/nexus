---
paths:
  - "scripts/git-push-develop.sh"
  - "scripts/push_check_gate.py"
  - "src/nexus/commands/ci_cmd.py"
  - "conexus/hooks/scripts/stop_wake_guard.py"
---

## Waiting on develop CI

nexus's CI board posts a `board/ci/<sha>--done` completion row. That post wakes a session only when the session was launched with the conexus channel; a session without it is not woken by the post. Run `nx ci wait <sha> --branch develop` as a background command (`run_in_background`): its exit is the wake and its one line is the result. This is the command `.nexus/project.json` names as `ci_wait`, so the plugin's Stop hook quotes it in a turn that ends on a CI wait here and quotes nothing in a repository that sets none.

The plugin carries only the generic wake mechanisms and the project's `ci_wait` text. The board, the channel and the develop branch are nexus facts; the full contract, exit codes and the push-train rules are in [worktrees](../../docs/agents/worktrees.md) (worktree rule 7 in `AGENTS.md`).
