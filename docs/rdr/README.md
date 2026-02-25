# RDR: Research-Design-Review

RDR is a control and feedback mechanism for agentic coding. You state the
problem, record what you know and how you know it, describe the plan, and
note what you rejected. The document gives humans a structured way to steer
LLM-driven development — and each RDR feeds what was learned back into the
next decision.

RDRs are iterative. You write one, build it, learn something, and write
another. A project might produce dozens over its lifetime.

## Documents

| Document | What it covers |
|----------|---------------|
| [Overview](overview.md) | What RDRs are, evidence classification, the iterative pattern |
| [Workflow](workflow.md) | Create, research, gate, close — slash commands and operations |
| [Nexus Integration](nexus-integration.md) | How Nexus storage tiers and agents amplify RDRs |
| [Templates](templates.md) | Document and post-mortem template reference |

**Start with the [Overview](overview.md)** if you're new to RDRs. The
[Workflow](workflow.md) is reference material for the slash commands.

## Quick Start

```
/rdr-create          # scaffold a new RDR
/rdr-research 001    # add classified findings
/rdr-list            # see all RDRs
/rdr-show 001        # detailed view of one RDR
```

Gates and post-mortems are optional — use `/rdr-gate` and `/rdr-close` when
the stakes justify formal validation or long-term archival.

## External Reference

The RDR process was created by Chris Wensel. See
[Arcaneum via RDRs](https://chris.wensel.net/post/arcaneum-via-rdrs/) for a
case study of 21 RDRs driving iterative development of a real project.
