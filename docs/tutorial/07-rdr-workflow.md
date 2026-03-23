# 7. The RDR Process

> **Time**: 7–10 minutes
> **Goal**: Viewer understands what RDRs are, why they matter, and sees one created live

---

## TALK

If you've ever come back to a codebase after a few weeks and asked "why did we do it this way?" — that's the problem RDR solves.

RDR stands for Research, Design, Review. It's a document that captures a technical decision: what the problem was, what you investigated, what you chose, and what you rejected. It lives in your repo alongside the code, and nexus indexes it so you can search it later.

You don't need RDR for every change. It's for decisions where the "why" isn't obvious from the code — when you chose between options, when you hit a constraint, when something surprising happened.

### Why Bother?

## OVERLAY

> **Without RDR:**
> - "Why did we use connection pooling here?" → nobody remembers
> - New team member makes the same mistake you already tried
> - Claude re-proposes a solution you rejected last month
>
> **With RDR:**
> - Decision is searchable: `nx search "connection pooling"`
> - Claude finds it automatically before proposing changes
> - New team members read the reasoning, not just the result

## TALK

The real payoff is with Claude. When you've been using RDR for a while, Claude's agents check prior decisions before proposing new ones. They won't suggest something you already tried and rejected. Your project builds institutional memory that actually gets used.

Let's create one.

### Live Demo: Create an RDR

## DO

```
/nx:rdr-create
```

## TALK

It asks for a title, a type, and a priority. Let's say we're deciding how to handle API rate limiting.

## DO

```
Title: "API Rate Limiting Strategy"
Type: Feature
Priority: medium
```

## TALK

That created a markdown file in `docs/rdr/` with a template — problem statement, research findings, proposed solution, alternatives considered. It also registered the RDR in nexus's memory so agents can find it.

Now let's add some research:

## DO

```
/nx:rdr-research add <id>

Finding: Express middleware express-rate-limit supports sliding window
with Redis backing. In-memory fallback available for single-process deployments.
Classification: Verified — Source Search (package docs + source code)
```

## TALK

Each research finding is tagged with how you verified it. "Verified — Source Search" means you checked the actual source code, not just the README. "Documented" means you read docs but didn't verify. "Assumed" means it's a guess. This matters because agents and future-you can see which findings are solid and which need more work.

### The Lifecycle

## OVERLAY

> ```
> /nx:rdr-create     → Draft
>      |
> /nx:rdr-research   → add findings (repeat)
>      |
> /nx:rdr-gate       → validate (optional but recommended)
>      |
> /nx:rdr-accept     → decision locked
>      |
> /nx:rdr-close      → archived, searchable forever
> ```

## TALK

You don't have to use every step. For a simple bug fix, create the RDR, write the root cause and fix, and close it. For a major architecture decision, use the gate — it runs three layers of validation: structure check, assumption audit, and AI critique. The gate catches contradictions and unverified assumptions before you commit to a design.

### Querying RDRs

## DO

```
/nx:rdr-list
```

## TALK

This shows all your RDRs with their status. You can filter by status or type. And because RDRs are indexed, you can search them semantically:

## DO

```bash
nx search "rate limiting" --corpus rdr
```

## TALK

That found our new RDR by meaning. Six months from now, when someone asks "did we consider rate limiting?", the answer is one search away.

### Right-Sizing

## OVERLAY

> **Match depth to the decision:**
>
> | Scenario | What to write |
> |---|---|
> | Bug fix with obvious cause | Problem + root cause + fix (3 paragraphs) |
> | Choosing between two libraries | Problem + research + chosen option + rejected option |
> | Architecture change | Full template — problem, research, design, alternatives, trade-offs |
>
> If the rationale is obvious from the code, skip the RDR.

## TALK

Don't overthink it. An RDR can be three paragraphs. The point is capturing the "why" that the code doesn't show. If the code is self-explanatory, you don't need one.
