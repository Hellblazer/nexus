### Added

- **Threads board threads take `due:` and `worktree:` lines.** `due:` is an ISO date (due from local midnight) or datetime; once it has come, every SessionStart prints one line per due thread of the session's own board entry, and `nx console` highlights it. `worktree:` names where the thread's work lives (an absolute path, artifact slug or file path) and shows under the thread. Field reference: `docs/agents/threads-board.md`.
