**Reading order:** Overview (this page) | [Workflow](rdr-workflow.md) | [Nexus Integration](rdr-nexus-integration.md) | [Templates](rdr-templates.md) | [RDR Index](rdr/README.md)

---

# RDR: Research-Design-Review

An RDR records a technical decision: problem, evidence, chosen solution, rejected alternatives. It exists so decisions can be reproduced, searched, and fed directly to agents as context.

## Quick start

1. `/nx:rdr-create` — creates a new file with metadata prefilled, status set to Draft
2. `/nx:rdr-research add <id>` — appends a finding with an evidence classification tag
3. `/nx:rdr-gate <id>` — runs 3-layer validation: structure check, assumption audit, AI critique (optional, recommended for irreversible decisions)
4. `/nx:rdr-accept <id>` — locks the decision, sets status to Accepted
5. `/nx:rdr-close <id> --reason implemented` — archives the RDR, creates a post-mortem template, indexes to T3

Steps 3–5 add rigor for high-stakes decisions. For a straightforward bug fix, steps 1–2 plus writing the solution may be all you need.

## When to write one

Write an RDR when the "why" behind a decision won't be obvious from the code or commit history alone:

- A design choice has non-obvious trade-offs or you evaluated multiple options
- A bug required root-cause analysis, not just a patch
- External constraints (API limits, vendor behavior) shaped the solution
- A previous decision turned out to be wrong and you're correcting it
- You're refactoring something others depend on
- Something discovered during implementation changes the original plan

Not every decision needs an RDR. If the rationale is self-evident from the code, skip it.

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

RDRs are not waterfall documents written once before implementation. They're iterative — write one, build, learn from what you find, write the next one. Each RDR builds on what earlier ones established, and sometimes an implementation reveals that a prior decision needs revisiting.

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
