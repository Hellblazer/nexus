# Nexus Tutorial — Recording Guide

Screen recording + voice-over tutorial for YouTube. Natural delivery from cue cards, not scripted word-for-word.

**Runtime target**: Aim for ~22 minutes. 28 minutes is fine — don't rush. The original outline planned 30-45 min across 10 sections; this guide consolidates to 8 sections by merging prerequisites into install and dropping cloud mode to a brief mention in the wrap.

## Recording Profile

Lock these settings before the first take. Every section uses the same profile.

### Terminal.app Profile: "Nexus Tutorial"

Create once via Terminal > Settings > Profiles > (+):

| Setting | Value |
|---------|-------|
| Font | SF Mono 18pt (or Menlo 18pt) |
| Window size | 100 cols x 30 rows |
| Background | #1a1a2e (dark navy) |
| Text color | #e0e0e0 (soft white) |
| Cursor | Block, blinking |
| Bold text color | #00d4aa (nexus teal) |
| Scrollback | Unlimited |
| Bell | Off |

### Shell Setup

```bash
# ~/.nexus-tutorial-rc (source this before every recording session)
export PS1='$ '
export TERM=xterm-256color
unset RPROMPT
unset PROMPT_COMMAND
export HISTCONTROL=ignoreboth
clear
```

### OBS Configuration

| Setting | Value |
|---------|-------|
| Canvas | 1920x1080 |
| Output | 1920x1080, CRF 18, x264 |
| Source | Window Capture → Terminal (Nexus Tutorial profile) |
| Audio | Mic input only, no desktop audio |
| Hotkey | Start/stop recording: Cmd+Shift+R |

### Desktop

- Hide dock (System Settings > Desktop & Dock > Automatically hide)
- Notifications: Do Not Disturb on
- Menu bar: hide clock if possible
- Wallpaper: solid dark (#1a1a2e to match terminal)
- Close all other apps

### Session Launcher

Before every recording session:

```bash
# 1. Open Terminal.app with the "Nexus Tutorial" profile
#    Terminal > Settings > General > set "Nexus Tutorial" as default profile
#    Then: Shell > New Window

# 2. Source the recording shell setup
source ~/.nexus-tutorial-rc

# 3. Verify the look
echo "Ready to record"
# Should see: clean dark terminal, '$ ' prompt, 18pt font, 100x30 window
```

---

## Master Prep

Do this once before the recording session.

### Demo Repository

Create a small, realistic Python project:

```bash
mkdir ~/demo-repo && cd ~/demo-repo
git init
```

Contents (10-15 files):
- `src/app.py` — main entry point, Click CLI
- `src/db.py` — SQLite helper with a few functions
- `src/api.py` — simple REST handler (Flask or just functions)
- `src/utils.py` — 2-3 utility functions
- `tests/test_app.py` — a few passing tests, one deliberately failing (for debug demo)
- `README.md`, `pyproject.toml`, `.gitignore`
- **Deliberately add** in `src/api.py` one function with a broad `except: pass` (for review demo in Section 5)
- **Deliberately add** one TODO comment about caching in `src/db.py` (for RDR demo in Section 6)
- **Deliberately add** a failing test in `tests/test_app.py` — e.g., asserts a function returns a specific error type but the broad except swallows it (for debug demo in Section 5)

Commit everything. This repo is the star of sections 2-6.

### Clean Nexus State

```bash
rm -rf ~/.config/nexus ~/.local/share/nexus
nx doctor  # should show fresh state
```

### Plugin State

Start Claude Code fresh — no nx plugin installed. Install it live in section 3.

### Pre-record Claude Code Sections

Sections 4-6 involve Claude Code responses that take 30-120s.

**Recommended**: Record live, trim dead time in FCP. More authentic than pre-recording. When an agent is thinking, narrate what it's doing ("it's searching the codebase now...") — this fills dead air and educates. Speed up in post if it takes longer than ~15s.

**Claude responses are non-deterministic.** Success for sections 4-6 means the agent addresses the target artifact (the right file, the right concept), not that it produces specific wording. If the agent goes off-track, stop and retake — don't try to salvage.

### Cold Open Setup (do after recording all other sections)

The cold open uses artifacts created during section 4. After recording section 4, these will exist:
- demo-repo indexed (from section 2)
- Memory entry: the "architecture" note and the "connection pooling" note

**Pre-store this exact entry for the cold open** (if not already present from section 4):

```bash
nx memory put "Database uses WAL mode for concurrent reads. Connection pooling needed for production." \
  --project demo-repo --title "architecture.md"
```

**Cold open queries to use** (verified against the pre-stored data):
1. Ask Claude: "What functions handle database connections in this project?" → semantic search finds `src/db.py`
2. Ask Claude: "What architectural decisions have we made?" → memory retrieval returns the WAL mode + pooling note

Test both queries before recording. If either returns empty, re-index or re-store.

---

## Section Cue Cards

### Section 0: Cold Open (0:30)

**Prep**: Claude Code open, nx plugin active, demo-repo indexed, memory entries pre-stored (see Cold Open Setup above). Test both queries before hitting record.

**Show**:
- Ask Claude: "What functions handle database connections?" → instant semantic result from T3
- Ask Claude: "What architectural decisions have we made?" → pulls from T2 memory
- Cut. Title card: "Let me show you how to set this up."

**Key points to hit**:
- This is the payoff — persistent memory and semantic search
- No API keys needed for local mode
- Works with any codebase

**Success**: Claude answers from context it shouldn't "know" — the hook that keeps viewers watching.

---

### Section 1: Install (2-3 min)

**Prep**: Clean machine state. No nexus installed. Python 3.12+ and git already present.

**Show**:
- `python3 --version` → confirm 3.12+ (expected: `Python 3.12.x` or `3.13.x`)
- `git --version` → confirm present
- Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (expected: "installed successfully")
- `uv tool install conexus` (expected: "Installed 2 executables: nx, nx-mcp")
- `nx --version` (expected: `nx, version X.Y.Z`)
- `nx doctor` → show the health check (expected: green checks for local mode)

**Key points to hit**:
- uv is the only prerequisite you might not have
- conexus is the PyPI package name, nx is the CLI
- nx doctor tells you what's working
- Everything runs local — no accounts, no API keys

**Success**: `nx doctor` shows green checks.

---

### Section 2: CLI Basics (4-5 min)

**Prep**: demo-repo exists, nexus freshly installed, cd into demo-repo.

**Show**:
- **Memory (T2)**: `nx memory put "Database uses WAL mode for concurrent reads" --project demo-repo --title "architecture.md"`
- `nx memory get --project demo-repo --title "architecture.md"` → see it back
- **Scratch (T1)**: `nx scratch put "Investigating slow query in db.py" --tags "hypothesis"`
- `nx scratch list` → see the entry
- **Index + Search (T3)**: `nx index repo .` → watch it classify, chunk, embed
- `nx search "database helper" --corpus code` → find db.py
- `nx search "error handling" --corpus code` → find the broad except

**Pause here and explain the three tiers explicitly:**
- T1 scratch — this session only. Agents use this to talk to each other. Gone when session ends.
- T2 memory — per-project, persists across sessions. Your decisions, notes, context.
- T3 store — permanent knowledge base. Indexed code, research, documents. What you just searched.
- Map each command you just ran to its tier: scratch = T1, memory = T2, search = T3.

**Key points to hit**:
- Three tiers with different lifetimes — session, project, permanent
- Indexing is local — uses ONNX model, no API call
- Search is semantic — finds by meaning, not just keywords
- Show the difference: grep finds exact text, nx search finds concepts

**Transition**: "So that's the CLI. Now let's wire this into Claude Code so it can use all of this automatically."

**Success**: Search returns relevant results, and viewer can name all three tiers.

---

### Section 3: Plugin Install (2-3 min)

**Prep**: Claude Code running, no nx plugin installed. Verify commands before recording:

```bash
# Test these in a scratch Claude Code session first
/plugin marketplace add Hellblazer/nexus
# Expected: marketplace added or "already added"

/plugin install nx@nexus-plugins
# Expected: "Installed nx. Run /reload-plugins to apply."

/plugin install sn@nexus-plugins
# Expected: "Installed sn. Run /reload-plugins to apply."

/reload-plugins
# Expected: "Reloaded: 12 plugins ... 4 plugin MCP servers"

/nx:nx-preflight
# Expected: checks pass
```

If any command produces a confirmation prompt or unexpected output, note it and handle it naturally on camera.

**Show**:
- `/plugin marketplace add Hellblazer/nexus`
- `/plugin install nx@nexus-plugins`
- `/plugin install sn@nexus-plugins` — "sn gives your subagents access to Serena for code navigation and Context7 for library docs"
- `/reload-plugins`
- `/nx:nx-preflight` → show the check

**Key points to hit**:
- Two plugins: nx (knowledge management), sn (code intelligence for subagents)
- sn is optional but recommended — makes subagents smarter about using tools
- Preflight tells you if anything is missing
- Mention: 15 agents, 28 skills, session hooks — all included

**Success**: Preflight passes, `/mcp` shows the MCP servers connected.

---

### Section 4: Nexus in Claude (4-5 min)

**Prep**: Plugin installed, demo-repo indexed from section 2, memory entry exists.

**Show**:
- Ask Claude: "Search for database-related code in this project"
  - Claude uses `nx search` → finds db.py, shows results
- Ask Claude: "What do we know about the architecture?"
  - Claude uses `nx memory get` → retrieves the WAL mode note
- Ask Claude: "Save a note that we need to add connection pooling"
  - Claude uses `nx memory put` → stores it
- Show that a new session picks up the context: `/clear` then note the SessionStart hook surfaces T2 memory automatically

**Key points to hit**:
- Claude uses the same tools you just used on the CLI
- Memory persists across sessions — this is the killer feature
- The SessionStart hook automatically surfaces relevant context
- No special syntax — just ask naturally

**Success**: Claude addresses the right artifacts (db.py, architecture note). Exact wording will vary — that's fine.

---

### Section 5: Agents & Skills (5-7 min)

**Prep**: demo-repo with the deliberate broad-except bug in `src/api.py`. The failing test in `tests/test_app.py` is ready. Verify before recording:

```bash
# Confirm the review agent will find the broad except
# In a test Claude Code session:
/nx:review-code
# Should flag the except: pass in api.py

# Confirm the debug agent has a test to work with
uv run pytest tests/test_app.py
# Should show 1 failure from the deliberately broken test
```

**Show**:
- "Let me show you the review agent." → `/nx:review-code`
  - Agent finds the broad except, suggests specific exception handling
- "What about when tests fail?" → `/nx:debug`
  - Mention: "I have a failing test — let's see what the debug agent does"
  - Agent uses sequential thinking, forms hypothesis, investigates
- `nx scratch list` → "Look — the agents wrote their findings to scratch. Other agents can pick up where these left off."
- "Before implementing anything..." → mention `/nx:brainstorming-gate`
  - "This is the design gate — you explore the problem before writing code. Every time."
- Briefly mention the agent roster: "There are 15 agents — developer, architect, researcher, and more. You don't need to memorize them. The skills route you to the right one."

**Key points to hit**:
- Agents are specialized — review agent reviews, debug agent debugs
- They share context via T1 scratch (show scratch list as proof)
- The brainstorming gate prevents premature implementation
- Skills are the entry point — you don't pick agents directly

**Success**: Agent finds the planted bug (broad except) or investigates the failing test. Exact output varies.

---

### Section 6: RDR Workflow (4-5 min)

**Prep**: Plugin active, working in demo-repo.

**Concept explanation** (say this before showing commands — 30-45 seconds):
- "Sometimes you face a decision with real consequences — which database to use, whether to add caching, how to handle auth."
- "An RDR — Research, Design, Review — is how you think it through with evidence instead of gut feel."
- "It's not a design doc. It's not required for every change. It's for the decisions that matter."

**Show**:
- `/nx:rdr-create` → create an RDR for "Add query caching to db.py"
  - Show the generated template in the editor
- `/nx:rdr-research` → **you type the finding manually**: "SQLite read performance is already excellent for our workload size. App-level caching adds complexity without measurable benefit."
  - Note to presenter: this is YOUR finding, not agent-generated. You're showing how to record evidence.
- Briefly show the RDR file structure
- "The full lifecycle is: create, research, gate, accept, close. The gate is an AI review of your evidence before you commit to a decision."

**Key points to hit**:
- RDRs are for decisions that need evidence, not just opinions
- They persist — future sessions can reference why you chose X over Y
- The gate checks your work before you commit to implementation
- Not every decision needs an RDR — use judgment

**Success**: Viewer understands what an RDR is, when to use one, and that it's evidence-driven.

---

### Section 7: Wrap + Next Steps (1-2 min)

**Prep**: None.

**Say**:
- Quick recap: "Three tiers — scratch for this session, memory for your project, store for permanent knowledge. Your agents use all of them."
- Cloud mode: "Everything today was local and free. If you're on a team and want shared knowledge across machines, there's a cloud mode that uses Voyage embeddings and hosted ChromaDB. Check the docs for setup — it's a config change, not a rewrite."
- "There's a cheatsheet in the repo — companion-cheatsheet.md — with every command we covered today."
- "Star the repo if this was useful. File issues if something breaks. PRs welcome."
- Sign off.

**Key points to hit**:
- Everything you saw today was local and free
- Cloud mode exists for teams — one sentence on what it adds, link to docs
- The cheatsheet has every command you need
- Community links

**Success**: Viewer knows where to go next and feels equipped to start.

---

## Recording Order

Not necessarily 0-7. Recommended batch order based on state dependencies:

1. **Sections 1-2 first** — pure CLI, no Claude Code, easiest to get right. Section 2 creates the indexed repo and memory entries that later sections depend on.
2. **Section 3** — plugin install, quick. After this, Claude Code has the plugins.
3. **Sections 4-6** — Claude Code interaction, longest takes, may need multiple attempts. Record in order (4, 5, 6) because each builds on the prior state.
4. **Section 0** — cold open last. You now know exactly what data exists and what queries work. Use the Cold Open Setup to verify before recording.
5. **Section 7** — wrap, record last. You know the total flow and can recap naturally.

## Post-Production (FCP)

### Video
- Trim dead time (waiting for responses, typos, retakes)
- Add section title cards (simple text on dark background, 2-3 seconds each)
- Add lower-third labels for key concepts ("T1: Session Scratch", "T2: Project Memory", "T3: Knowledge Store")
- Speed up long operations (indexing, agent responses >15s) with a subtle fast-forward indicator or "x2" badge

### Audio
- Normalize to -14 LUFS (YouTube loudness standard)
- Noise reduction pass on all voice tracks
- Check for mic pops and clipping, especially during sections 4-6 where long agent silences may be followed by loud narration
- If a take has good video but bad audio on one segment, re-record the voice-over for that segment and splice in FCP

### Export
- 1080p, H.264, YouTube-ready
- Thumbnail: terminal screenshot with title overlay
