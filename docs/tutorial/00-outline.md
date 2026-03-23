# Nexus Tutorial — Video Script Outline

A recorded video walkthrough for Claude Code users who want persistent memory and semantic search across their agent sessions. No prior experience with vector databases, embeddings, or uv required.

## Target Audience

- Claude Code users (beginners welcome)
- Comfortable with a terminal but not necessarily with Python packaging
- Don't care how embeddings work — just want things to be findable
- May be using Claude Code for the first time alongside this tutorial

## Runtime Estimate

30–45 minutes total

## Sections

| # | Section | Minutes | What happens |
|---|---------|---------|-------------|
| 0 | Cold Open | ~1 | Show the end state: Claude finding code, recalling decisions, answering from context |
| 1 | Prerequisites | 3–5 | Install uv, verify Python and git |
| 2 | Install Nexus | 3–4 | `uv tool install conexus`, `nx doctor`, quick tour |
| 3 | First Use — CLI | 5–7 | Memory, scratch, index a repo, search — all local, no keys |
| 4 | Install the Plugin | 2–3 | Marketplace install, preflight check |
| 5 | Nexus Inside Claude | 4–6 | Same operations through Claude — search, memory, auto-context |
| 6 | Agents and Skills | 5–7 | Top 5 agents, live demo of debug + review |
| 7 | The RDR Process | 5–7 | What decisions look like, live create → research |
| 8 | Cloud Mode (Optional) | 2–3 | When you'd want it, how to set it up |
| 9 | Tips and Next Steps | 2–3 | Daily workflow, hooks, updating, cheatsheet URL |

## Companion Materials

- `companion-cheatsheet.md` — one-page command reference viewers can bookmark
- Each section file has **TALK** (what to say), **DO** (what to type/show), and **OVERLAY** (text for post-production)

## Recording Notes

- Use a clean terminal with a visible font size (14pt+)
- Have a small test repo ready (10–20 files, with at least one function that has poor error handling for the review demo)
- Make a small uncommitted edit before section 6 (e.g., add a function that catches a broad exception and silently passes)
- Run `nx index repo .` on the test repo during section 3 — show the output live
- Clear nx state before recording (`rm -rf ~/.config/nexus ~/.local/share/nexus`)
- Start Claude Code fresh (no plugins installed)
- On Windows, use WSL or adapt the uv install command (PowerShell variant shown in section 1)
- Script section 5 questions to match what's actually in the test repo
- After RDR create in section 7, briefly show the created file in an editor
- Say the cheatsheet URL on camera at the end of section 9
