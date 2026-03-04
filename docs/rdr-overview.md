# RDR: Research-Design-Review

An RDR records a technical decision: problem, evidence, chosen solution, rejected alternatives. It exists so decisions can be reproduced, searched, and fed directly to agents as context.

## Quick Start

1. `/rdr-create` — creates a new file with metadata prefilled, status set to Draft
2. `/rdr-research add <id>` — appends a finding with an evidence classification tag
3. `/rdr-gate <id>` — runs 3-layer validation: structure check, assumption audit, AI critique (optional but recommended for irreversible decisions)
4. `/rdr-accept <id>` — locks the decision, sets status to Accepted
5. `/rdr-close <id> --reason implemented` — archives the RDR and decomposes it into implementation beads

## When to Write One

- A design choice has non-obvious trade-offs
- You investigated two or more options before deciding
- A bug required root-cause analysis, not just a patch
- A decision will be hard to reverse or expensive if wrong
- External constraints (API limits, vendor behavior, third-party behavior) shaped the solution
- A previous decision turned out to be wrong and you're correcting it
- You're about to refactor something others depend on
- The "why" won't be obvious from the code or commit history alone
- You discovered something during implementation that changes the original plan

## Right-Sizing an RDR

Not every RDR needs every section. Match depth to complexity.

| Scenario | Sections needed | Example |
|---|---|---|
| **Minimal** (bug, 1 option) | Problem + Root Cause + Fix | AST line-range bug: splitter returns empty metadata |
| **Full** (architecture, multiple options) | All sections | Four-store T3 architecture with quota enforcement |

**The rule**: if you can state the problem, root cause, and fix in one paragraph, that IS the RDR. Don't add sections to look thorough.

## Evidence Classification

Each research finding is tagged so readers know what is solid and what is a guess.

| Classification | Meaning | Example |
|---|---|---|
| **Verified** | Confirmed via source code search or working spike | "grep confirms the API accepts batch writes" |
| **Documented** | Supported by external documentation only | "Vendor docs state 10k RPS limit" |
| **Assumed** | Unverified belief based on experience or inference | "Serialization overhead assumed negligible" |

Flag assumptions that your design depends on. Low-stakes assumptions need no verification; load-bearing ones should be visible.

## The Iterative Pattern

The Nexus project produced 18 RDRs over two weeks. Here's what that looks like in practice:

| RDRs | Theme |
|---|---|
| 001–002 | Foundation: process validation, T2 status synchronization |
| 004–007 | Architecture: four-store layout, quota enforcement, scoring, agent session context |
| 008–013 | Workflow integration, API cleanup, T1 cross-process sessions, PDF ingest tiers, memory simplification |
| 014–016 | Retrieval quality: code context prefixes, pipeline rethink (cross-repo learning from Arcaneum), AST line-range bug |
| 017–018 | Operational: progress bars, replace polling server with git hooks |

RDR-015 exists because implementing RDR-014 exposed that Arcaneum had already solved the same indexing problems. RDR-016 exists because fixing RDR-014 uncovered a latent bug in the AST chunker. Each RDR is a step, not a plan.

## What an Agent Sees

When you run `nx search "topic" --corpus rdr`, the agent retrieves the Problem Statement, Proposed Solution, and evidence classifications for matching RDRs. A well-written Problem Statement and Proposed Solution are the most valuable parts — they give the agent enough context to implement or extend without reading the full document. The evidence classification tells the agent which parts of the design are verified facts versus assumptions it should check before relying on them.

## Statuses

```
Draft --> Accepted --> Implemented
                           |
                       Reverted / Abandoned / Superseded
```

- **Draft**: skeleton created, research in progress
- **Accepted**: gate passed; decision formally accepted
- **Implemented**: implementation complete, archived to T3
- **Reverted**: implementation was rolled back
- **Abandoned**: decision dropped before implementation
- **Superseded**: replaced by a newer RDR (linked via `superseded_by` field)

## Types

- **Feature**: new capability or user-facing behavior
- **Bug Fix**: root-cause analysis and fix strategy for a defect
- **Technical Debt**: refactoring or cleanup of existing code
- **Framework Workaround**: mitigation for a known framework limitation
- **Architecture**: cross-cutting structural decision

## Optional Rigor

`/rdr-gate` runs a structural check, assumption audit, and AI critique before you commit. Use it when the decision is expensive to reverse. `/rdr-close` optionally generates a post-mortem comparing what was decided to what was built — useful for improving future RDRs. Neither is required for routine work.
