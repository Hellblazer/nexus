# 6. Agents and Skills

> **Time**: 7–10 minutes
> **Goal**: Viewer understands the agent roster, sees 2–3 agents in action

---

## TALK

The nx plugin comes with 15 specialized agents. Each one is good at a specific job — debugging, code review, planning, research, architecture. You don't usually call them directly. Instead, you use skills — slash commands that route your request to the right agent with the right context.

Think of it like a team. You say what you need, and the right specialist picks it up.

### The Agent Roster

## OVERLAY

> **Agents by purpose:**
>
> | When you need to... | Use this | Agent |
> |---|---|---|
> | Debug a failure | `/nx:debug` | debugger (opus) |
> | Review code quality | `/nx:review-code` | code-review-expert (sonnet) |
> | Plan a feature | `/nx:create-plan` | strategic-planner (opus) |
> | Design architecture | `/nx:architecture` | architect-planner (opus) |
> | Implement from a plan | `/nx:implement` | developer (sonnet) |
> | Understand a codebase | `/nx:analyze-code` | codebase-deep-analyzer (sonnet) |
> | Research a topic | `/nx:research` | deep-research-synthesizer (sonnet) |
> | Validate tests | `/nx:test-validate` | test-validator (sonnet) |
> | Critique a design | `/nx:substantive-critique` | substantive-critic (sonnet) |
> | Audit a plan | `/nx:plan-audit` | plan-auditor (sonnet) |
> | Deep problem analysis | `/nx:deep-analysis` | deep-analyst (opus) |
> | Enrich plan with context | `/nx:enrich-plan` | plan-enricher (sonnet) |
> | Index PDFs | `/nx:pdf-process` | pdf-chromadb-processor (haiku) |
> | Organize knowledge | `/nx:knowledge-tidy` | knowledge-tidier (haiku) |
> | Route to the right agent | *(automatic)* | orchestrator (sonnet) |

## TALK

You don't need to memorize this. The skill system is loaded into every session — Claude knows which agent to use based on what you're doing. The agents at the top of this list are the ones you'll use most often.

Let me show a few in action.

### Demo 1: Code Review

## TALK

Let's say you just finished some work and want a review before committing. Let me make a quick edit first so there's something to review.

## DO

```bash
# Make a small change to a file in the test repo
# (edit something — add a function, change some logic)
```

## TALK

Now let's ask for a review:

## DO

```
/nx:review-code
```

## TALK

The code-review-expert agent analyzed the changes we just made. It checks for bugs, security issues, style consistency, and architectural fit. It gives you findings ranked by priority — critical things first, minor observations last.

This runs on Sonnet, so it's fast. If it finds something serious, you fix it now instead of discovering it in a PR review.

### Demo 2: Debugging

## TALK

Now let's say a test is failing and you can't figure out why. Instead of guessing and retrying:

## DO

```
/nx:debug

The test test_retry_on_timeout is failing intermittently. Sometimes it passes, sometimes it times out after 30 seconds.
```

## TALK

The debugger agent runs on Opus — the most capable model — because debugging requires deep reasoning. It doesn't just look at the test. It traces the call chain, checks for race conditions, examines timeout configurations, and forms hypotheses. It tells you what it thinks is wrong and what evidence supports that conclusion.

This is systematic, not trial-and-error. It uses sequential thinking to work through possibilities methodically.

### Demo 3: Planning

## TALK

Before building anything significant, you want a plan. Not because Claude can't figure it out, but because you want to agree on the approach before committing to it.

## DO

```
/nx:brainstorming-gate

I want to add rate limiting to our API endpoints.
```

## TALK

This is the brainstorming gate — it's the entry point for any new feature. It asks clarifying questions, proposes approaches, and presents a design for your approval. Nothing gets built until you say yes.

After you approve, it hands off to the strategic planner, which breaks the work into concrete tasks with tests and dependencies.

### Standard Pipelines

## OVERLAY

> **Common workflows (multi-agent):**
>
> **New feature:**
> brainstorming → strategic-planner → plan-auditor → plan-enricher → architect-planner → developer → code-review → test-validator
>
> **Bug fix:**
> debugger → developer → code-review → test-validator
>
> **Research:**
> deep-research-synthesizer → knowledge-tidier
>
> **Understanding a new codebase:**
> codebase-deep-analyzer → strategic-planner
>
> **Architecture design:**
> codebase-deep-analyzer → deep-analyst → strategic-planner → plan-auditor → architect-planner

## TALK

These pipelines aren't rigid — you can jump in at any point. If you already know what to build, skip brainstorming and go straight to `/nx:create-plan`. If a review finds issues, loop back to the developer. The agents hand context to each other through scratch and memory, so nothing gets lost between steps.
