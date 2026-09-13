# nexus beads usage

Verbs this project uses: `bd ready`; `bd show <id>`; `bd update <id> --claim`;
`bd create --title ... --description ... --type task|bug|feature|epic
--priority 0-4` (numeric priority, never high/medium/low); `bd dep add <issue>
<depends-on>` (issue depends on depends-on); `bd close <id> --reason "..."`.
Never `bd edit`, it opens `$EDITOR` and blocks.

Project records (decisions, findings, session context) go in T2 via
`memory_put` (project=nexus). Feedback on how to work with the user lives in
Claude Code's own auto-memory. `bd remember` is not used here.

Git workflow and session close follow this repo's AGENTS.md, do not restate
git commands here. Beads sync is `bd dolt push`, which the SessionEnd hook
already runs.
