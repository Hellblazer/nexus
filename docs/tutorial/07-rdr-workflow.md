# 7. The RDR Process

> **Time**: 5–7 minutes
> **Goal**: Viewer understands what RDRs are, why they matter, and sees one created live

---

## TALK

If you've ever come back to a codebase after a few weeks and asked "why did we do it this way?" — that's the problem RDR solves.

RDR captures a technical decision: what the problem was, what you investigated, what you chose, and what you rejected. It lives in your repo and nexus indexes it so you can search it later.

### Why Bother?

## OVERLAY

> **Without RDR:** nobody remembers why, Claude re-proposes rejected ideas
> **With RDR:** decisions are searchable, Claude checks them automatically

## TALK

The payoff is with Claude. After a few RDRs, agents check prior decisions before proposing new ones. Your project builds institutional memory that actually gets used.

Let's create one.

### Live Demo

## DO

```
/nx:rdr-create API Rate Limiting Strategy
```

## TALK

Claude created a markdown file in `docs/rdr/` with a template and assigned it an ID. Let me show the file briefly.

*(Open the created file in editor for ~5 seconds)*

Now let's add a research finding:

## DO

```
/nx:rdr-research add <id>

I checked the express-rate-limit package source code. It supports sliding window
rate limiting with Redis backing, and has an in-memory fallback for single-process
setups. Verified by reading the source.
```

## TALK

Claude recorded the finding. Notice the "verified by reading the source" part — that's an evidence label.

## OVERLAY

> **Evidence quality (a convention, not enforced):**
> - **Verified** — checked source code or ran a test
> - **Documented** — read external docs only
> - **Assumed** — best guess, needs validation

## TALK

This helps future readers know which conclusions are solid. You write it naturally — the tool doesn't enforce it.

### The Lifecycle

## OVERLAY

> 1. `/nx:rdr-create` — draft
> 2. `/nx:rdr-research` — add findings (repeat)
> 3. `/nx:rdr-gate` — validate (optional)
> 4. `/nx:rdr-accept` — lock the decision
> 5. `/nx:rdr-close` — archive forever

## TALK

You don't need every step. A bug fix might just be create, write the root cause, close. A major architecture decision — use the gate, which catches contradictions and unverified assumptions.

### Finding RDRs Later

## DO

```
/nx:rdr-list
```

## TALK

That shows all your RDRs. You can also search them by meaning — just ask Claude:

## DO

```
Search our previous decisions about rate limiting.
```

## TALK

Six months from now, when someone asks "did we consider rate limiting?" — one search.

Don't overthink it. An RDR can be three paragraphs. If the rationale is obvious from the code, skip it.
