### Fixed

- A subagent dispatched by a session that works in a linked worktree can commit there: the subagent git-write guard admits one literal `git -C <absolute path> <verb> ...` command when git proves the path is a linked worktree (verbs that run no command of their own; no chaining, expansion, second repository or `rebase -x`). Before, the subagent's cwd was always the primary checkout, so every commit was refused and the session had to ask before committing the agent's work itself.
