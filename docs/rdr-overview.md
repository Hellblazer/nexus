**Reading order:** Overview (this page) | [Workflow](rdr-workflow.md) | [Nexus Integration](rdr-nexus-integration.md) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)

---

# RDR: Research-Design-Review

An RDR is a specification document written *before* implementation. It captures the problem, the research journey, competing options, and the chosen approach — then locks that decision so implementation has a stable target.

The core insight: complex features are bigger than what fits in working memory — yours or an LLM's. Without a locked specification, purpose drift sets in as new problems emerge during coding, side-quests derail the original vision, and you end up coding your way out of corners instead of designing your way around them. An RDR front-loads the thinking so implementation can focus on execution.

**The rule:** revise the RDR during planning; lock it at acceptance. If implementation proves the design wrong, abandon the code and iterate the RDR — not the other way around.

## Quick start

1. `/nx:rdr-create` — creates a new file with metadata prefilled, status set to Draft
2. `/nx:rdr-research add <id>` — appends a finding with an evidence classification tag
3. `/nx:rdr-gate <id>` — runs 3-layer validation: structure check, assumption audit, AI critique (optional, recommended for irreversible decisions)
4. `/nx:rdr-accept <id>` — locks the decision, sets status to Accepted
5. `/nx:rdr-close <id> --reason implemented` — archives the RDR, creates a post-mortem template, indexes to T3

Steps 3–5 add rigor for high-stakes decisions. For a straightforward bug fix, steps 1–2 plus writing the solution may be all you need.

## When to write one

Write an RDR *before implementing* when any of these apply:

- The problem has multiple viable solutions and you need to choose
- External constraints (API limits, vendor behavior, library quirks) will shape the design
- You're about to make a change that others depend on
- A bug requires root-cause analysis, not just a patch
- A previous decision turned out to be wrong and you're correcting course
- The feature is complex enough that you'd lose track of why you're making specific choices mid-implementation

Not every decision needs an RDR. If the rationale is self-evident from the code, skip it. But if you find yourself three hours into implementation wondering "why did I go this way?" — that's the RDR you should have written first.

## Right-sizing

Match depth to the decision's complexity.

| Scenario | Sections needed | Example |
|---|---|---|
| **Minimal** (bug, single option) | Problem + Root Cause + Fix | AST line-range bug: splitter returns empty metadata |
| **Full** (architecture, multiple options) | All sections | Four-store T3 architecture with quota enforcement |

If you can state the problem, root cause, and fix in one paragraph, that IS the RDR. Don't add sections to look thorough.

## Evidence classification

Each research finding is tagged so readers — both human and agent — know what is solid and what needs further validation.

| Classification | Meaning |
|---|---|
| **Verified** | Confirmed via source code search or working spike |
| **Documented** | Supported by external documentation only |
| **Assumed** | Unverified belief based on experience or inference |

Flag assumptions that your design depends on. Low-stakes assumptions need no verification; load-bearing ones should be explicitly visible so they can be challenged or validated later.

## The iterative pattern

RDRs are iterative across a project, not within a single document. Write one, lock it, build against it, learn from what you find. If the design was wrong, don't patch the code — abandon it and write a new RDR with what you learned. Each RDR builds on what earlier ones established, and the corpus grows into institutional memory.

Research may reveal that one RDR needs to split into several — that's normal. Cross-reference related RDRs to maintain conceptual integrity. Stack them by dependency so implementation order is clear.

The Nexus project has produced over 35 RDRs across its development. The corpus is searchable, so when starting a new design, prior decisions surface automatically — preventing contradictions and avoiding redundant investigation.

## Statuses and types

```
Draft --> Accepted --> Implemented
                           |
                       Reverted / Abandoned / Superseded
```

| Status | Meaning |
|---|---|
| **Draft** | Created, research in progress |
| **Accepted** | Gate passed, decision formally accepted |
| **Implemented** | Implementation complete, archived to T3 |
| **Reverted** | Implementation rolled back |
| **Abandoned** | Dropped before implementation |
| **Superseded** | Replaced by a newer RDR (linked via `superseded_by`) |

**Types**: Feature, Bug Fix, Technical Debt, Framework Workaround, Architecture.

## Using RDR in your project

RDR works in any repository — it doesn't require the Nexus CLI or plugin. The tooling amplifies RDRs with search, validation, and agent context, but the core value is the document itself.

**Minimal setup (no tooling):**

1. Create `docs/rdr/` in your repo
2. Copy the [template](rdr-templates.md) into `docs/rdr/TEMPLATE.md`
3. Write your first RDR — Problem Statement + Research Findings + Proposed Solution is enough

**With Nexus CLI + plugin:**

1. `/nx:rdr-create` bootstraps the directory, templates, and README automatically on first use
2. `/nx:rdr-research`, `/nx:rdr-gate`, `/nx:rdr-accept`, `/nx:rdr-close` manage the full lifecycle
3. RDRs are auto-indexed by `nx index repo` and searchable via `nx search --corpus rdr`

See [Nexus Integration](rdr-nexus-integration.md) for how agents and storage tiers work with RDRs.

---

**Reading order:** Overview (this page) | [Workflow](rdr-workflow.md) | [Nexus Integration](rdr-nexus-integration.md) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)
