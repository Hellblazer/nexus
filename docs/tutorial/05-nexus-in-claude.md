# 5. Nexus Inside Claude

> **Time**: 4–6 minutes
> **Goal**: Viewer sees Claude using nexus tools naturally

---

## VOICE

Now that the plugin is installed, Claude uses nexus automatically. It searches before answering, checks memory for context, and stores findings. You can also ask explicitly.

Let me show both.

### Automatic Search

## VOICE

Watch what happens when we ask about the codebase.

## SCREEN [15s]

```
How does the retry logic work in this project?
```

*(Claude searches indexed code, finds files, answers from actual code)*

## VOICE [OVER SCREEN]

Before answering, Claude searched your indexed code. Found the relevant files. Gave an answer grounded in your actual repo — not training data.

### Storing Decisions

[PAUSE 1s]

## VOICE

You can also tell Claude to remember things.

## SCREEN [8s]

```
Remember that we decided to use connection pooling with a max of 10 connections for the database layer.
```

## VOICE [OVER SCREEN]

That's now in persistent memory.

[PAUSE 1s]

Let's verify.

## SCREEN [10s]

```
What do we know about the database configuration?
```

## VOICE [OVER SCREEN]

It found the note. Next session, next week — that decision is still there.

### Agent Coordination

[PAUSE 1s]

## VOICE

When Claude spawns multiple agents — a debugger and a reviewer working in parallel — they share findings through scratch. Each agent sees what the others found.

## SCREEN [10s]

```
Search the codebase for how errors are handled, and also check if there are any error-related tests.
```

## VOICE [OVER SCREEN]

Multiple agents working, sharing results through scratch. When the session ends, scratch is cleaned up.

### The Three Tiers

## OVERLAY

> | Tier | What | Lasts |
> |------|------|-------|
> | **Scratch** | Agent working notes | Session only |
> | **Memory** | Project decisions | Survives restarts |
> | **Search** | Indexed code and docs | Permanent |

## VOICE

You don't have to think about which tier to use. Claude handles routing. One tip — storing decisions explicitly works better than hoping Claude infers them.

## OVERLAY

> **This compounds over time:**
> - Session 1: Claude searches your code
> - Session 10: Claude knows your decisions
> - Session 50: Claude knows your project better than a new team member
