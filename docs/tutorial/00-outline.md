# Nexus Tutorial — Video Script Outline

A recorded video walkthrough for Claude Code users who want persistent memory and semantic search across their agent sessions. No prior experience with vector databases, embeddings, or uv required.

## Target Audience

- Claude Code users (beginners welcome)
- Comfortable with a terminal but not necessarily with Python packaging
- Don't care how embeddings work — just want things to be findable
- May be using Claude Code for the first time alongside this tutorial

## Runtime Estimate

35–50 minutes total

## Sections

| # | Section | Minutes | What happens |
|---|---------|---------|-------------|
| 1 | Prerequisites | 3–5 | Install uv, verify Python and git |
| 2 | Install Nexus | 3–4 | `uv tool install conexus`, `nx doctor`, quick tour of what got installed |
| 3 | First Use — CLI | 5–7 | Memory, scratch, index a repo, search — all local, no keys |
| 4 | Install the Plugin | 3–4 | Marketplace install, preflight check, what changed |
| 5 | Nexus Inside Claude | 5–7 | Same operations but through Claude — agents use nx automatically |
| 6 | Agents and Skills | 7–10 | What the 15 agents do, live demo of 2–3 (debug, review, plan) |
| 7 | The RDR Process | 7–10 | What decisions look like, live create → research → accept → close |
| 8 | Cloud Mode (Optional) | 3–5 | When you'd want it, how to set it up, free tiers |
| 9 | Tips and Next Steps | 2–3 | Daily workflow, updating, where to get help |

## Companion Materials

- `companion-cheatsheet.md` — one-page command reference viewers can bookmark
- Each section file has **TALK** (what to say), **DO** (what to type/show), and **OVERLAY** (text for post-production)

## Recording Notes

- Use a clean terminal with a visible font size (14pt+)
- Have a small test repo ready (doesn't matter what — something with 10–20 files)
- Clear nx state before recording (`rm -rf ~/.config/nexus ~/.local/share/nexus`)
- Start Claude Code fresh (no plugins installed)
