---
allowed-tools: Bash
description: Write a handoff document under /tmp capturing session state, branch, beads, T2 entries, and next-step pointers. Chat response is `cat <path>`; user copies it and pastes after /clear to bootstrap the next session.
argument-hint: [topic-or-arc-slug] (optional; defaults to current branch)
---

# Continuation prompt builder

Generates a handoff document under `/tmp/` that a future Claude Code session can read to pick up cold. Stores under `/tmp` (purged on reboot by macOS) so we don't accumulate stale handoffs in `~/.cache`. The chat response is one literal line: `cat <Target file>`. The user copies that line (mouse-select + cmd-C, or whatever their terminal supports), runs `/clear`, pastes, hits return. The new session reads the handoff and resumes.

!`nx command-context continuation`

## Action

Three steps. Run the session-retro audits, write the handoff file, then emit one line of chat. Nothing else.

### Step 0: session-retro audits (RDR-184 — multi-agent sessions only; skip if zero Agent dispatches)

Any tripped threshold files a mechanization bead (`bd create`) — never just a handoff note.

1. **Directive-diff (Gap 2)**: diff sent scope-updates vs each hand-back's addressed items; count `nx scratch` tag `crossed-resend`. >2 crossed-resends → bead.
2. **Commit-pathspec (Gap 4)**: `bash tests/e2e/lib/commit_scope_audit.sh <session-start-ref>.. <intended pathspecs...>`. Any `!!! FOREIGN FILE(S)` → bead.
3. **Declaration-completeness (Gap 1)**: `source tests/e2e/lib/expectations.sh; expectations_undeclared <session_id>` — flags named-morphology dispatches with no EXPECT row (either mode suppresses; unnamed/sync dispatches never flagged). Any UNDECLARED → bead. Census: `expectations_census <session_id>` — the SCRIPTED per-agent classification + ROWS/CLASSIFIED summary (nexus-hybv1: never hand-count; `BLOCKED_RESOLVED` = guard success, bare `BLOCKED_UNRESOLVED` = real idle). Paste its two summary lines into the handoff.
4. **Payload-morphology tripwire**: after any Claude Code version change, rerun `./tests/cc-validation/runner.sh --scenario 27` (the `a<name>-<hash>` encoding is production-load-bearing).

### Step 1: write the full handoff to `<Target file>`

Compose a 9-section handoff document. Each section is load-bearing for a cold pickup; skipping one loses information the next session needs.

1. **Title** (`# Continuation: <topic> (YYYY-MM-DD)`) matching the working-state topic line.

2. **What just shipped / state at handoff.** Concrete commits with SHAs from the recent-commits block, beads closed in the session, T2 entries written, releases tagged. Name everything explicitly.

3. **Where state lives.** Bullet list of branch, RDR file paths (if applicable), T2 memory keys (`project=X, title=Y`), bead IDs of the epic and root work, PR numbers.

4. **Literal first actions.** Numbered, copy-paste-runnable commands. Always include:
   - `cd <abs path>` from working-state cwd
   - `git fetch && git checkout <branch> && git pull` (or equivalent)
   - `git status --short` clean-state check
   - `bd ready` or specific `bd show <id>` if a bead is the entry point
   - Any specific T2 reads (`mcp__plugin_conexus_nexus__memory_get(project=..., title=...)`)
   - Any specific file reads required to come up to speed

5. **Open work / bead graph.** If multiple beads are in flight, draw the dependency graph as an ASCII tree. Mark the READY entry point.

6. **Active blockers.** Any `in_progress` beads that gate the next step, or external blockers (CI red, dependent PR pending).

7. **Locked contracts / invariants.** Anything settled in this session that the next session MUST honour. Pull from RDR §Out-of-scope, prior decisions, locked-in shapes, version-bump parity rules.

8. **What you should NOT do.** Explicit ban list. If feedback-memory filenames appeared in the working-state block, reference them by filename (don't paraphrase, point). The block lists filenames only, not contents; if you need to quote a specific rule, Read the file first rather than guessing at the body from the slug.

9. **Workflow lessons from this session.** Anything that bit you this session that next-session-you should pre-empt.

### Step 2: chat response is three lines, plain text

The entire chat response is:

    Paste this in the next session after /clear:

    cat <Target file>

Three lines: a one-line hint reminding the user what the command is for, a blank separator, and the bare `cat` command on its own line. The blank line matters: it isolates the command so triple-click selects only the command, not the hint.

Where `<Target file>` is the absolute path from the working-state block. No fence, no backticks, no bullet, no bold around the command. Nothing after the third line.

Example complete chat response:

    Paste this in the next session after /clear:

    cat /tmp/nexus-continuation-luciferase-main-2026-05-23.md

The user selects the third line (mouse drag or triple-click), copies (`cmd-C` or terminal-specific binding), `/clear`s, pastes, hits return. The next session sees `cat <path>` as its first user message, reads the handoff, resumes.

Clipboard auto-prime was tried (pbcopy, launchctl asuser pbcopy, OSC 52 plain and DCS-wrapped) and abandoned: remote tmux over mosh, the canonical nexus-developer environment, doesn't reliably forward any of them to the user's local NSPasteboard. Plain text on its own line is the only universally portable affordance.

### Style rules for the handoff file (not the chat response)

- **No em dashes (`—`).** Use commas, colons, periods, or parentheses. Grep before writing.
- **Imperative voice.** "Read X first. Run Y. Pick up Z." not "you might want to consider...".
- **Concrete over general.** file:line, commit SHA, bead ID, date. Never "the relevant file" or "the recent work".
- **Tool names in full form.** `mcp__plugin_conexus_nexus__memory_get`, not "the memory tool".

### Bail-out

If the rendered "Topic:" line above reads `current branch no-branch` (no arguments AND not a git repo) AND the in-progress beads block is empty or absent, stop and ask the user what the handoff scope should cover rather than guessing.
