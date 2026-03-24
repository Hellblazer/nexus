# 4. Install the Plugin

> **Time**: 2–3 minutes
> **Goal**: nx plugin installed in Claude Code, preflight passing

---

## VOICE

So far we've used nx from the terminal. Now let's give Claude Code access to all of it.

Two commands.

## SCREEN [5s]

```bash
claude
```

## SCREEN [5s]

```
/plugin marketplace add Hellblazer/nexus
```

## VOICE [OVER SCREEN]

That adds the marketplace source.

## SCREEN [5s]

```
/plugin install nx@nexus-plugins
```

## VOICE [OVER SCREEN]

If you see a "not found" error, run "claude update" first.

[PAUSE 2s]

Let's verify.

## SCREEN [5s]

```
/nx:nx-preflight
```

## VOICE [OVER SCREEN]

Green means good. Warnings for "beads" and "superpowers" are fine — those are optional.

## OVERLAY

> **If nx CLI shows red:** run `uv tool install conexus` in a separate terminal, then retry.

[PAUSE 1s]

## VOICE

Now restart Claude Code to see the session hooks.

## SCREEN [5s]

```bash
exit
claude
```

## VOICE [OVER SCREEN]

See the context at the top? That's nexus loading what it knows about your project. Memory entries, active work. Claude sees this before you type a word.

## OVERLAY

> **Plugin commands**
> - Install or update: `/plugin install nx@nexus-plugins`
> - Check health: `/nx:nx-preflight`
> - Remove: `/plugin uninstall nx`
