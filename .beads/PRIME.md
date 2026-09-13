<!-- conexus-managed beads PRIME v1 sha256:9db60a53886666e360bda7b224ab488d4ca566b052120472233ae21f777d1e80 -->
# beads usage

Verbs: `bd ready`; `bd show <id>`; `bd update <id> --claim`; `bd create
--title ... --description ... --type task|bug|feature|epic --priority 0-4`
(numeric priority, never high/medium/low); `bd dep add <issue> <depends-on>`
(issue depends on depends-on); `bd close <id> --reason "..."`. Never `bd
edit`, it opens `$EDITOR` and blocks.

When a durable project-memory tool is available (for example, the conexus
`memory_put` tool), send project records there instead of `bd remember`.
Otherwise `bd remember` is fine to use.

Repo workflow and session close follow this project's own AGENTS.md or
CLAUDE.md, if either exists; nothing here restates that.

## This repo (nexus)

Git workflow and session close follow AGENTS.md. `bd dolt push` (the
SessionEnd hook already runs it) syncs beads. `bd remember` is not used
here; project records go to T2 via `memory_put`.
