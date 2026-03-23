# 4. Install the Plugin

> **Time**: 3–4 minutes
> **Goal**: nx plugin installed in Claude Code, preflight passing

---

## TALK

So far we've been using nx from the terminal. Now let's give Claude Code access to everything we just set up. The nx plugin connects Claude's agents to nexus's search, memory, and scratch — plus it adds over a dozen specialized agents and 28 workflow skills.

Two commands inside Claude Code:

## DO

```bash
# Start Claude Code
claude

# Inside Claude Code, run:
/plugin marketplace add Hellblazer/nexus
```

## TALK

That adds the nexus marketplace source. You should see a confirmation message. Now install the plugin:

## DO

```
/plugin install nx@nexus-plugins
```

## TALK

You should see output listing the installed components — agents, skills, hooks. If you get a "not found" error, make sure Claude Code is up to date with `claude update` and try again.

Now let's verify everything is wired up:

## DO

```
/nx:nx-preflight
```

## TALK

Preflight checks that the nx CLI is available, that the plugin's hooks are loaded, and that optional dependencies are present. Green checkmarks mean everything is good. You'll likely see warnings for beads and superpowers — those are optional extras, not required. The important thing is that the nx CLI check passes.

## OVERLAY

> **Expected preflight output:**
> - `nx CLI` — should be green (we installed this in section 2)
> - `beads` — yellow warning is OK (optional task tracker)
> - `superpowers plugin` — yellow warning is OK (optional workflow plugin)
>
> **If nx CLI shows red:** run `uv tool install conexus` in a separate terminal, then retry preflight.

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
> - **Install or update**: `/plugin install nx@nexus-plugins` (same command for both)
> - **Uninstall**: `/plugin uninstall nx`
> - **Preflight**: `/nx:nx-preflight` — run after install or update
