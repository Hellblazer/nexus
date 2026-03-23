# 9. Tips and Next Steps

> **Time**: 2–3 minutes
> **Goal**: Viewer knows how to stay current and where to go for help

---

## TALK

Let me leave you with a few practical tips for daily use.

### Keeping Nexus Updated

## DO

```bash
# Update the CLI
uv tool update conexus

# Inside Claude Code, update the plugin
/plugin install nx@nexus-plugins
```

## TALK

Run these periodically. The CLI and plugin are released together, so update both at the same time.

### Daily Workflow

## OVERLAY

> **Start of day:**
> 1. Start Claude Code — session hooks load your project context automatically
> 2. Ask "what were we working on?" — Claude checks memory and beads
>
> **During work:**
> - Use `/nx:brainstorming-gate` before building anything new
> - Use `/nx:debug` when a test fails (don't guess-and-retry)
> - Use `/nx:review-code` before committing
> - Store important decisions: "Remember that we chose X because Y"
>
> **End of day:**
> - Re-index if you made significant changes: `nx index repo .`
> - Or install git hooks to do it automatically: `nx hooks install`

## TALK

The git hooks are nice — they re-index your repo automatically after every commit. Set it up once and forget about it:

## DO

```bash
nx hooks install
```

### Cheatsheet and Help

## TALK

Everything we covered today is in a one-page cheatsheet. I'll put the link on screen — bookmark it.

## OVERLAY

> **Cheatsheet:** github.com/Hellblazer/nexus/blob/main/docs/tutorial/companion-cheatsheet.md
>
> **More help:**
> - `nx --help` — built-in reference
> - [Plugin README](https://github.com/Hellblazer/nexus/blob/main/nx/README.md) — all 15 agents
> - [GitHub Issues](https://github.com/Hellblazer/nexus/issues) — bugs and features

## TALK

That's nexus. Persistent memory, semantic search, and specialized agents — so Claude gets smarter about your project over time. Install takes two minutes, and everything compounds the more you use it.

Thanks for watching.
