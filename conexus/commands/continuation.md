---
description: Write a handoff document under /tmp capturing session state, branch, beads, T2 entries, and next-step pointers. Chat response is `cat <path>`; user copies it and pastes after /clear to bootstrap the next session.
argument-hint: [topic-or-arc-slug] (optional; defaults to current branch)
---

# Continuation prompt builder

Generates a handoff document under `/tmp/` that a future Claude Code session can read to pick up cold. Stores under `/tmp` (purged on reboot by macOS) so we don't accumulate stale handoffs in `~/.cache`. The chat response is one literal line: `cat <Target file>`. The user copies that line (mouse-select + cmd-C, or whatever their terminal supports), runs `/clear`, pastes, hits return. The new session reads the handoff and resumes.

!{
  set +e

  # ---- Resolve repo + slug -------------------------------------------------
  # Sanitize REPO (basename can contain spaces) the same way as the SLUG so
  # the final filename has no whitespace and no character that would need
  # shell quoting downstream.
  REPO_SAFE=$(printf '%s' "$(basename "$(pwd)")" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')
  [ -z "$REPO_SAFE" ] && REPO_SAFE="repo"
  TODAY=$(date +%Y-%m-%d)
  # /tmp is purged on reboot (macOS) or periodically (Linux). Don't accumulate
  # handoffs under ~/.cache or ~/.claude; they're throwaway state.
  OUT_DIR="/tmp"

  # Slug pipeline: printf strips the trailing newline that echo adds, so the
  # tr -c result has no trailing-dash artefact that the later s/-$//g would
  # have to clean up. Explicit > implicit.
  if [ -n "$ARGUMENTS" ]; then
    SLUG=$(printf '%s' "$ARGUMENTS" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')
    TITLE_TOPIC="$ARGUMENTS"
  else
    BR=$(git branch --show-current 2>/dev/null || echo "no-branch")
    SLUG=$(printf '%s' "$BR" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')
    TITLE_TOPIC="current branch $BR"
  fi
  [ -z "$SLUG" ] && SLUG="session"

  # If a same-day file already exists at the base path, suffix with HHMM so
  # successive invocations on the same day don't silently overwrite.
  BASE="${OUT_DIR}/nexus-continuation-${REPO_SAFE}-${SLUG}-${TODAY}.md"
  if [ -e "$BASE" ]; then
    OUT="${OUT_DIR}/nexus-continuation-${REPO_SAFE}-${SLUG}-${TODAY}-$(date +%H%M).md"
  else
    OUT="$BASE"
  fi

  # No clipboard auto-prime. Tried pbcopy (writes to remote pasteboard if
  # over SSH/mosh), launchctl asuser pbcopy (same), OSC 52 (mosh and many
  # tmux/terminal combos drop the escape). Bash-tool side cannot reliably
  # reach the user's local NSPasteboard from a remote tmux-over-mosh
  # session. Leave the copy to the user; they have terminal text selection
  # which works everywhere.

  echo "**Target file:** \`$OUT\`"
  echo ""
  echo "**Topic:** $TITLE_TOPIC"
  echo ""

  # ---- Working state -------------------------------------------------------
  echo "## Working state"
  echo ""
  echo "- **cwd:** \`$(pwd)\`"
  if git rev-parse --git-dir > /dev/null 2>&1; then
    echo "- **branch:** \`$(git branch --show-current 2>/dev/null)\`"
    echo "- **HEAD:** \`$(git log --oneline -1 2>/dev/null)\`"
    UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || echo "(no upstream)")
    echo "- **upstream:** $UPSTREAM"
    AHEAD=$(git rev-list --count @{u}..HEAD 2>/dev/null || echo "?")
    BEHIND=$(git rev-list --count HEAD..@{u} 2>/dev/null || echo "?")
    echo "- **ahead/behind:** $AHEAD / $BEHIND"
  else
    echo "- **branch:** (not a git repo)"
  fi
  echo ""

  echo "### Uncommitted"
  echo '```'
  git status --short 2>/dev/null | head -20 || echo "(no git status)"
  echo '```'
  echo ""

  echo "### Recent commits (last 10 on this branch)"
  echo '```'
  git log --oneline -10 2>/dev/null || echo "(no log)"
  echo '```'
  echo ""

  echo "### Open PRs from this branch"
  echo '```'
  if command -v gh >/dev/null 2>&1; then
    gh pr list --head "$(git branch --show-current 2>/dev/null)" --state open \
      --json number,title,baseRefName \
      --jq '.[] | "PR #\(.number) -> \(.baseRefName): \(.title)"' 2>/dev/null | head -5
  else
    echo "(gh not installed)"
  fi
  echo '```'
  echo ""

  if command -v bd >/dev/null 2>&1; then
    echo "### In-progress beads"
    echo '```'
    bd list --status=in_progress --limit=10 2>/dev/null || echo "(none)"
    echo '```'
    echo ""
    echo "### Ready beads (top 10)"
    echo '```'
    bd ready --limit=10 2>/dev/null | head -25 || echo "(none)"
    echo '```'
    echo ""
  fi

  if command -v nx >/dev/null 2>&1; then
    PROJ_ACTIVE="${REPO}_active"
    echo "### nx memory ($PROJ_ACTIVE) titles"
    echo '```'
    nx memory get --project "$PROJ_ACTIVE" --title "" 2>/dev/null | head -15 || echo "(no active-project memory)"
    echo '```'
    echo ""
  fi

  # ---- Optional: Claude Code auto-memory (silent if absent) ----------------
  # Claude Code names session dirs by replacing every non-alphanumeric char in
  # the absolute cwd with a dash. Use printf to strip pwd's trailing newline
  # (otherwise tr -c converts it to a trailing dash and the glob over-matches
  # adjacent repos with prefix-equal paths, e.g. nexus vs nexus-rdr-125).
  echo "### Feedback memories (auto-memory dir, if present)"
  PWD_KEY=$(printf '%s' "$(pwd)" | tr -c 'A-Za-z0-9' '-')
  PROJ_DIR="${HOME}/.claude/projects/${PWD_KEY}"
  if [ -d "$PROJ_DIR/memory" ]; then
    ls "$PROJ_DIR/memory/" 2>/dev/null | grep -E '^feedback_' | head -10 | sed 's|^|- |'
  else
    echo "(none; auto-memory not configured for this repo)"
  fi
  echo ""
}

## Action

Two steps. Write the handoff file, then emit one line of chat. Nothing else.

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
