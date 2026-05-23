---
description: Write a paste-ready continuation prompt to /tmp capturing session state, branch, beads, T2 entries, and next-step pointers
argument-hint: [topic-or-arc-slug] (optional; defaults to current branch)
---

# Continuation prompt builder

Generates a paste-ready handoff document in `/tmp/` that a future Claude Code session can read to pick up cold. Use this at the END of a productive session, or at a phase boundary, to capture state cheaply before context is lost.

!{
  set +e

  # ---- Resolve repo + slug -------------------------------------------------
  REPO=$(basename "$(pwd)")
  TODAY=$(date +%Y-%m-%d)

  if [ -n "$ARGUMENTS" ]; then
    SLUG=$(echo "$ARGUMENTS" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')
    TITLE_TOPIC="$ARGUMENTS"
  else
    BR=$(git branch --show-current 2>/dev/null || echo "no-branch")
    SLUG=$(echo "$BR" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')
    TITLE_TOPIC="current branch $BR"
  fi
  [ -z "$SLUG" ] && SLUG="session"

  OUT="/tmp/${REPO}-continuation-${SLUG}-${TODAY}.md"
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

  # ---- Optional: Claude Code auto-memory (Hal's pattern; silent if absent) -
  echo "### Feedback memories (auto-memory dir, if present)"
  PROJ_DIR=$(ls -td ~/.claude/projects/*"$(pwd | tr '/' '-' | sed 's/^-//')"* 2>/dev/null | head -1)
  if [ -n "$PROJ_DIR" ] && [ -d "$PROJ_DIR/memory" ]; then
    ls "$PROJ_DIR/memory/" 2>/dev/null | grep -E '^feedback_' | head -10 | sed 's|^|- |'
  else
    echo "(none; auto-memory not configured for this repo)"
  fi
  echo ""
}

## Action

Compose a paste-ready continuation prompt and write it to the **Target file** path above. Match the canonical 10-section structure exactly. Each section is load-bearing for a cold pickup; skipping one loses information the next session needs.

### Required sections (in order)

1. **Title** — `# Continuation: <topic> (YYYY-MM-DD)` matching the working-state topic line.

2. **What just shipped / state at handoff** — concrete commits with SHAs from the recent-commits block, beads closed in the session, T2 entries written, releases tagged. Name everything explicitly.

3. **Where state lives** — bullet list of branch, RDR file paths (if applicable), T2 memory keys (`project=X, title=Y`), bead IDs of the epic and root work, PR numbers.

4. **Literal first actions** — numbered, copy-paste-runnable commands. Always include:
   - `cd <abs path>` from working-state cwd
   - `git fetch && git checkout <branch> && git pull` (or equivalent)
   - `git status --short` clean-state check
   - `bd ready` or specific `bd show <id>` if a bead is the entry point
   - Any specific T2 reads (`mcp__plugin_nx_nexus__memory_get(project=..., title=...)`)
   - Any specific file reads required to come up to speed

5. **Open work / bead graph** — if multiple beads are in flight, draw the dependency graph as an ASCII tree. Mark the READY entry point.

6. **Active blockers** — any `in_progress` beads that gate the next step, or external blockers (CI red, dependent PR pending).

7. **Locked contracts / invariants** — anything settled in this session that the next session MUST honour. Pull from RDR §Out-of-scope, prior decisions, locked-in shapes, version-bump parity rules.

8. **What you should NOT do** — explicit ban list. If feedback-memory filenames appeared in the working-state block, reference them by filename (don't paraphrase, point).

9. **Workflow lessons from this session** — anything that bit you this session that next-session-you should pre-empt.

10. **Compressed continuation prompt** — a single blockquote (5-10 lines) that a future Claude session can paste verbatim to bootstrap. References the full file path.

### Style rules

- **No em dashes (`—`).** Use commas, colons, periods, or parentheses. Grep your output before writing.
- **Imperative voice.** "Read X first. Run Y. Pick up Z." not "you might want to consider...".
- **Concrete over general.** file:line, commit SHA, bead ID, date. Never "the relevant file" or "the recent work".
- **Tool names in full form.** `mcp__plugin_nx_nexus__memory_get`, not "the memory tool".

### Output

Write the document to **Target file**. After writing, output exactly one line for easy copy:

```
Continuation prompt written: /tmp/<repo>-continuation-<slug>-<date>.md
```

Then a one-paragraph human summary of what the next session is picking up. Nothing else.

If `$ARGUMENTS` is empty AND no obvious in-progress arc exists, stop and ask the user what the handoff scope should cover, rather than guessing.
