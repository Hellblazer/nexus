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

### Where to Get Help

## OVERLAY

> **Resources:**
> - `nx --help` and `nx <command> --help` — built-in reference
> - [Getting Started guide](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md)
> - [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md)
> - [Plugin README](https://github.com/Hellblazer/nexus/blob/main/nx/README.md) — agent and skill reference
> - [GitHub Issues](https://github.com/Hellblazer/nexus/issues) — bug reports and feature requests

## TALK

That's nexus. Persistent memory, semantic search, and specialized agents — all working together so Claude Code gets smarter about your project over time. Install takes two minutes, local mode needs zero configuration, and everything compounds the more you use it.

Thanks for watching.
