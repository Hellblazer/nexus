# 4. Install the Plugin

> **Time**: 3–4 minutes
> **Goal**: nx plugin installed in Claude Code, preflight passing

---

## TALK

So far we've been using nx from the terminal. Now let's give Claude Code access to everything we just set up. The nx plugin connects Claude's agents to nexus's search, memory, and scratch — plus it adds 15 specialized agents and 28 workflow skills.

Two commands inside Claude Code:

## DO

```bash
# Start Claude Code
claude

# Inside Claude Code, run:
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

## TALK

The first command adds the nexus marketplace to Claude's plugin sources. The second installs the nx plugin from it. Let's verify everything is wired up:

## DO

```
/nx:nx-preflight
```

## TALK

Preflight checks that the nx CLI is available, that the plugin's hooks are loaded, and that optional dependencies like beads are present. Green checkmarks mean everything is good. Warnings are fine — they just mean optional features aren't set up yet.

## OVERLAY

> **What the plugin adds:**
> - **MCP servers** — agents can search, store, and retrieve without using bash
> - **Session hooks** — memory context is loaded automatically when a session starts
> - **15 agents** — specialized for debugging, code review, planning, research, etc.
> - **28 skills** — workflow guidance that keeps agents on track
> - **Permission auto-approval** — safe nx commands don't need manual confirmation

## TALK

Let's see what changed. When you start a new Claude Code session now, you'll notice it automatically loads context from your project's memory. That's the SessionStart hook — it checks what you've been working on and gives Claude a head start.

## DO

```bash
# Exit and restart Claude to see the session hooks in action
exit
claude
```

## TALK

See that context at the top? That's nexus surfacing what it knows about your project. Any memory entries, any active work items — Claude sees them before you type a single word.

## OVERLAY

> **Plugin lifecycle**
> - **Install once**: `/plugin install nx@nexus-plugins`
> - **Update**: reinstall from marketplace when a new version is released
> - **Uninstall**: `/plugin uninstall nx`
> - **Preflight**: `/nx:nx-preflight` — run after install or update
