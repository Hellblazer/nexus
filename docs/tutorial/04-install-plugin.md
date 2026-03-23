# 4. Install the Plugin

> **Time**: 2–3 minutes
> **Goal**: nx plugin installed in Claude Code, preflight passing

---

## TALK

So far we've been using nx from the terminal. Now let's give Claude Code access to everything we just set up.

Two commands:

## DO

```bash
# Start Claude Code
claude

# Inside Claude Code, run:
/plugin marketplace add Hellblazer/nexus
```

## TALK

That adds the nexus marketplace source. Now install:

## DO

```
/plugin install nx@nexus-plugins
```

## TALK

If you get a "not found" error, run `claude update` first and try again. Let's verify it worked:

## DO

```
/nx:nx-preflight
```

## TALK

Green means good. Warnings for "beads" and "superpowers" are fine — those are optional extras. The important one is the nx CLI check.

## OVERLAY

> **If nx CLI shows red:** run `uv tool install conexus` in a separate terminal, then retry.

## TALK

Now restart Claude Code to see the new session hooks in action:

## DO

```bash
exit
claude
```

## TALK

See the context at the top? That's nexus automatically loading what it knows about your project — memory entries, active work items. Claude sees this before you type a word.

## OVERLAY

> **Plugin commands**
> - Install or update: `/plugin install nx@nexus-plugins`
> - Check health: `/nx:nx-preflight`
> - Remove: `/plugin uninstall nx`
