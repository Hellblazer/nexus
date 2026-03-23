# 6. Agents and Skills

> **Time**: 5–7 minutes
> **Goal**: Viewer understands the top agents, sees debug + review in action

---

## TALK

The plugin added 15 specialized agents. You don't need to know all of them — here are the five you'll use most.

## OVERLAY

> **Your top 5 agents:**
> - `/nx:debug` — systematic debugging (Opus)
> - `/nx:review-code` — code quality review (Sonnet)
> - `/nx:create-plan` — break work into steps (Opus)
> - `/nx:implement` — build from a plan (Sonnet)
> - `/nx:analyze-code` — understand unfamiliar code (Sonnet)
>
> 10 more in the [plugin README](https://github.com/Hellblazer/nexus/blob/main/nx/README.md)

## TALK

You call them with slash commands. Claude picks the right model — Opus for reasoning-heavy tasks like debugging, Sonnet for implementation. Let me show two of them.

### Demo 1: Debugging

## TALK

Let's say a test is failing and you've been going in circles. Instead of guessing:

## DO

```
/nx:debug

The test test_retry_on_timeout is failing intermittently. Sometimes it passes, sometimes it times out after 30 seconds.
```

## TALK

The debugger doesn't just look at the test. It traces the call chain, checks for race conditions, examines configuration. It forms hypotheses and tells you which evidence supports each one. Systematic, not trial-and-error.

### Demo 2: Code Review

## TALK

Now let's review some code. I've made a change to this file — watch what the reviewer catches.

## DO

*(Show the uncommitted change: a function with a broad except/pass, e.g.:)*

```python
def process_file(path):
    try:
        data = open(path).read()
        return parse(data)
    except:
        pass
```

## DO

```
/nx:review-code
```

## TALK

It flagged the bare `except: pass` — that silently swallows every error, including things you'd want to know about. It runs on Sonnet, so it's fast. Catch issues before they reach a PR review.

### When to use which

## TALK

You don't need to memorize the full list. If you're not sure which agent to use, just describe what you need — Claude routes to the right one. The cheatsheet at the end of this tutorial has the complete reference.
