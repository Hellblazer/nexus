# 5. Nexus Inside Claude

> **Time**: 5–7 minutes
> **Goal**: Viewer sees Claude using nexus tools naturally — search, memory, scratch

---

## TALK

Now that the plugin is installed, Claude can use nexus directly. You don't have to tell it to — the agents know when to search, when to check memory, and when to store findings. But you can also ask explicitly. Let me show both.

### Automatic: Claude Searches Before Answering

## TALK

Let's ask Claude something about our codebase. Watch what happens:

## DO

```
How does the retry logic work in this project?
```

## TALK

Notice what Claude did — before answering, it searched your indexed code. It found the relevant files, read the matching chunks, and gave you an answer grounded in your actual code. It's not guessing from training data — it's reading your repo.

That search happened through the MCP server that the plugin set up. Claude's agents talk directly to nexus without using bash commands.

### Explicit: Storing and Retrieving Context

## TALK

You can also tell Claude to remember things:

## DO

```
Remember that we decided to use connection pooling with a max of 10 connections for the database layer.
```

## TALK

Claude just stored that in T2 memory. Next session, when you or an agent asks about the database, that decision will be right there. Let's verify:

## DO

```
What do we know about the database configuration?
```

## TALK

It found the note we just stored. This is how decisions persist across sessions — you don't have to re-explain context every time you start Claude Code.

### Scratch: Sharing Between Agents

## TALK

When Claude spawns sub-agents — say, a debugger and a code reviewer working in parallel — they need to share findings without stepping on each other. That's what scratch does.

## DO

```
Search the codebase for how errors are handled, and also check if there are any error-related tests.
```

## TALK

When Claude sends multiple agents to work on this, they write their findings to scratch. Each agent can see what the others found. At the end of the session, scratch is cleaned up — it's working space, not permanent storage.

### The Three Tiers in Practice

## OVERLAY

> **How the tiers work together in Claude Code:**
>
> | Tier | What | When | Lasts |
> |------|------|------|-------|
> | **Scratch (T1)** | Agent working notes | During a session, auto-shared | Session only |
> | **Memory (T2)** | Project decisions and context | Stored explicitly or by agents | Survives restarts |
> | **Search (T3)** | Indexed code, docs, knowledge | After `nx index repo` | Permanent |
>
> Claude's agents use all three automatically. You just work normally.

## TALK

You don't have to think about which tier to use. Tell Claude to remember something — it goes to memory. Agents share findings through scratch. You index a repo — it goes to the knowledge store.

One tip: storing decisions explicitly — "remember that we chose X because Y" — works better than hoping Claude infers them.

## OVERLAY

> **This compounds over time:**
> - Session 1: Claude searches your code
> - Session 10: Claude knows your decisions
> - Session 50: Claude knows your project better than a new team member
