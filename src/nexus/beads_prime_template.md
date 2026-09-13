<!-- conexus-managed beads PRIME v1 -->
# beads usage

Verbs: `bd ready`; `bd show <id>`; `bd update <id> --claim`; `bd create
--title ... --description ... --type task|bug|feature|epic --priority 0-4`
(numeric priority, never high/medium/low); `bd dep add <issue> <depends-on>`
(issue depends on depends-on); `bd close <id> --reason "..."`. Never `bd
edit`, it opens `$EDITOR` and blocks.

Project records (decisions, findings, session context) belong in this
project's own durable memory store when it has one (for example Nexus T2 via
`memory_put`), not in `bd remember`.

Git workflow and session close follow this project's own AGENTS.md or
CLAUDE.md, if either exists; do not restate git commands here.
