# 6. Agents and Skills

> **Time**: 5–7 minutes
> **Goal**: Viewer understands the top agents, sees debug + review in action

---

## VOICE

The plugin added 15 specialized agents. Here are the five you'll use most.

## OVERLAY

> **Your top 5 agents:**
> - `/nx:debug` — systematic debugging (Opus)
> - `/nx:review-code` — code quality review (Sonnet)
> - `/nx:create-plan` — break work into steps (Opus)
> - `/nx:implement` — build from a plan (Sonnet)
> - `/nx:analyze-code` — understand unfamiliar code (Sonnet)
>
> 10 more in the plugin README

## VOICE

You call them with slash commands. Opus handles reasoning. Sonnet handles implementation.

Let me show two.

### Debugging

[PAUSE 1s]

## VOICE

A test is failing intermittently. Instead of guessing:

## SCREEN [pre-recorded — 60–120s, trim to highlights]

```
/nx:debug

The test test_retry_on_timeout is failing intermittently. Sometimes it passes, sometimes it times out after 30 seconds.
```

*(Debugger agent traces call chain, forms hypotheses)*

## VOICE [OVER SCREEN]

The debugger traces the call chain. Checks for race conditions. Examines configuration. Forms hypotheses with evidence.

Systematic. Not trial-and-error. And there's a safety net — if the developer agent gets stuck on test failures, it automatically stops after two attempts and escalates to the debugger with a structured report.

### Code Review

[PAUSE 2s]

## VOICE

Now let's review some code. Here's a change with a problem — watch what the reviewer catches.

## SCREEN [5s]

*(Show the uncommitted change in editor:)*

```python
def process_file(path):
    try:
        data = open(path).read()
        return parse(data)
    except:
        pass
```

## SCREEN [pre-recorded — 30–60s, trim to highlights]

```
/nx:review-code
```

*(Code review agent flags the bare except/pass)*

## VOICE [OVER SCREEN]

It flagged the bare "except pass." That silently swallows every error. Runs on Sonnet, so it's fast. And the reviewer checks what the developer struggled with during the session — if the developer had a hard time with a section, the reviewer focuses extra attention there.

### Choosing an Agent

[PAUSE 1s]

## VOICE

You don't need to memorize the list. Describe what you need — Claude routes to the right agent. The cheatsheet has the full reference.
