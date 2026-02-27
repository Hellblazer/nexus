# RDR: Research-Design-Review

## What RDRs Are

Agentic coding is like driving on slick ice — powerful, fast, and easy to
lose control of. Without structure, it produces sprawl: disjointed threads,
frameworks built with missing purpose, decisions that nobody can reconstruct
a week later. RDRs are the traction control that makes it manageable:
a short document where you state the problem, record what you know, describe
the plan, and note what you rejected. They give humans a structured way to
steer, respond to discoveries and failures, and maintain coherent direction
across the speed and complexity of LLM-driven development.

An LLM that receives a well-structured RDR can implement the decision
directly, because it has the context, constraints, and design in a single
document. But RDRs are not just input to agents — they are the feedback loop.
Each one captures what was learned, what broke, what changed, feeding that
back into the next decision. They are how humans stay in control of systems
that move faster than any individual can track unaided.

You can write one RDR for a single decision, or write a batch of them upfront
to map out a whole project before any code is written. Use what fits.

## RDRs Are Iterative

RDRs are not front-loaded specs that you write once and implement. They are an
ongoing conversation with your codebase. You write one, build it, learn something,
and write another.

A real project's RDR history might look like this:

- **001-007**: Foundation decisions — project structure, storage, indexing, search
- **008-012**: Discovery pivot — "we need full-text search too." New engine, new
  dual-indexing strategy, re-integration with existing CLI.
- **013**: Performance optimization — real usage revealed bottlenecks
- **014-016**: Quality refinements — new content types, token waste discovered
  from actual data, normalization improvements
- **017-020**: Operational needs — migration, logging, GPU stability fixes
  discovered in production
- **021**: Search quality — chunks lose structural context, prepend metadata

Each RDR builds on knowledge from implementing earlier ones. RDR-016 refines
RDR-004 because real indexing data revealed 47% token waste. RDR-020 fixes a
GPU fallback mechanism from RDR-013 that failed under real workloads. RDR-009
synthesizes seven earlier RDRs into a unified strategy.

This is the normal pattern. You don't know everything at the start. RDRs give
you a structured way to capture what you learn as you go — and on a team,
they give everyone a shared record of *why* things changed. When a new
developer joins mid-project or a teammate picks up where you left off, the
RDR chain is the fastest way to understand the current state and how it got
there.

## Evidence Classification

Each research finding is tagged with how you know it, so readers can judge
which parts of the decision are solid and which are educated guesses:

| Classification | Meaning | Example |
|---|---|---|
| **Verified** | Confirmed via source code search or working spike | "grep confirms the API accepts batch writes" |
| **Documented** | Supported by external documentation only | "The vendor docs state 10k RPS limit" |
| **Assumed** | Unverified belief based on experience or inference | "We assume the serialization overhead is negligible" |

The more load-bearing an assumption is, the more it matters to flag it.
Not every assumption needs formal verification — but the ones your design
depends on should be visible.

## Document Structure

Each RDR lives at `docs/rdr/NNN-kebab-title.md` with a `## Metadata` section
containing status, type, priority, and dates. The sequential ID (`001`, `002`, ...)
is assigned automatically at creation time by scanning the `docs/rdr/` directory.
A project prefix derived from the repository name scopes IDs across projects.

## Statuses

```
Draft --> Final --> Implemented
                       |
                   Reverted / Abandoned / Superseded
```

- **Draft**: initial skeleton created, research in progress
- **Final**: gate passed, decision approved for implementation
- **Implemented**: implementation complete, archived to T3
- **Reverted**: implementation was rolled back
- **Abandoned**: decision was dropped before implementation
- **Superseded**: replaced by a newer RDR (linked via `superseded_by` field)

## Types

- **Feature**: new capability or user-facing behavior
- **Bug Fix**: root-cause analysis and fix strategy for a defect
- **Technical Debt**: refactoring or cleanup of existing code
- **Framework Workaround**: mitigation for a known framework limitation
- **Architecture**: cross-cutting structural decision

## Optional Rigor: Gates and Post-Mortems

For decisions that carry real risk, Nexus provides two additional layers:

- **Gate** (`/rdr-gate`): structural check, assumption audit, and AI critique.
  Forces you to confront what you don't actually know before committing. Use this
  when the decision is expensive to reverse.
- **Post-mortem** (created by `/rdr-close`): drift analysis comparing what was
  decided to what was actually built. Useful for improving future RDRs.

Neither is required. A quick RDR that captures the decision and moves on is
perfectly valid. Add gates when the stakes justify them.

## How Is This Different from a Design Doc?

A design doc describes *what* to build. An RDR also records *what you looked at*
and *how confident you are* in the key assumptions. This matters most when
working with LLMs — the evidence classification gives the LLM (and future
humans) a way to judge which parts of the spec are firm and which are soft.
