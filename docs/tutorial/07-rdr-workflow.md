# 7. The RDR Process

> **Time**: 5–7 minutes
> **Goal**: Viewer understands what RDRs are and sees one created live

---

## VOICE

Ever come back to a codebase and asked "why did we do it this way?" That's the problem RDR solves.

RDR captures a technical decision. What the problem was. What you investigated. What you chose. What you rejected. It lives in your repo. Nexus indexes it.

## OVERLAY

> **Without RDR:** nobody remembers why, Claude re-proposes rejected ideas
> **With RDR:** decisions are searchable, Claude checks them automatically

## VOICE

After a few RDRs, agents check prior decisions before proposing new ones. Your project builds institutional memory.

[PAUSE 1s]

Let's create one.

### Create

## SCREEN [10s]

```
/nx:rdr-create API Rate Limiting Strategy
```

*(Claude creates file, assigns ID)*

## VOICE [OVER SCREEN]

Claude created a markdown file with a template and assigned it an ID.

## SCREEN [5s]

*(Open the created file briefly in editor)*

### Add Research

[PAUSE 1s]

## VOICE

Now we add a finding. Just describe what you found.

## SCREEN [10s]

```
/nx:rdr-research add <id>

I checked the express-rate-limit package source code. It supports sliding window rate limiting with Redis backing, and has an in-memory fallback. Verified by reading the source.
```

## VOICE [OVER SCREEN]

Claude recorded the finding. Notice "verified by reading the source" — that's an evidence label.

## OVERLAY

> **Evidence quality (convention, not enforced):**
> - **Verified** — checked source code or ran a test
> - **Documented** — read external docs only
> - **Assumed** — best guess, needs validation

## VOICE

This helps future readers know which conclusions are solid.

### The Lifecycle

## OVERLAY

> 1. `/nx:rdr-create` — draft
> 2. `/nx:rdr-research` — add findings (repeat)
> 3. `/nx:rdr-gate` — validate (optional)
> 4. `/nx:rdr-accept` — lock the decision
> 5. `/nx:rdr-close` — archive forever

## VOICE

You don't need every step. A bug fix? Create, write the root cause, close. A major architecture decision? Use the gate. It catches contradictions and unverified assumptions.

### Finding RDRs Later

[PAUSE 1s]

## SCREEN [5s]

```
/nx:rdr-list
```

## VOICE [OVER SCREEN]

All your RDRs with status.

## SCREEN [8s]

```
Search our previous decisions about rate limiting.
```

## VOICE [OVER SCREEN]

Six months from now — one search.

[PAUSE 1s]

## VOICE

An RDR can be three paragraphs. If the rationale is obvious from the code, skip it.
