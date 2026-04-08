---
title: "Pipeline Orchestration and Plan Reuse"
id: RDR-058
type: Feature
status: accepted
accepted_date: 2026-04-07
priority: medium
reviewed-by: self
author: Hal Hildebrand
created: 2026-04-07
related_issues: [RDR-040, RDR-056, RDR-057]
---

# RDR-058: Pipeline Orchestration and Plan Reuse

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The Nexus orchestrator agent exists but is never invoked in practice. The main conversation acts as the de facto orchestrator — the user tells Claude what to do, Claude decides which agents to launch. This creates three gaps:

1. **No plan reuse**: `plan_save`/`plan_search` infrastructure exists in T2 but the orchestrating layer never calls `plan_search` before constructing pipelines. Successful pipeline patterns are lost.
2. **No typed pipeline validation**: The orchestrator emits prose Pipeline Plans. There is no typed DAG, no schema consistency check between stages, no automatic parallelism identification. Broken relay chains are discovered at runtime, not at plan time.
3. **No cross-validation against knowledge base**: Agent critics operate on artifacts in isolation. No "Inspector & Editor" role reconciles artifacts against indexed source documents.

The fundamental constraint: **subagents cannot spawn other subagents**. The orchestrator agent can only recommend routing — it cannot actually dispatch a pipeline. This was already acknowledged when `/nx:orchestrate` was removed (CHANGELOG: "routing tree in using-nx-skills replaces it").

### The Orchestrator's Real Location

The orchestrating intelligence must live at one of three levels:
- **Skill layer**: Skills that inject pipeline structure before agent dispatch
- **Hook layer**: SubagentStart hooks that enforce validation
- **Main conversation routing**: CLAUDE.md / using-nx-skills skill

## Research Findings

### RF-1: DeepEye's Database-Inspired Workflow Engine

**Source**: DeepEye (arxiv 2603.28889, indexed: docs__default)

Compiler translates intent → executable DAG with typed `{Key, Type}` I/O schemas per node. Validator runs DFS cycle detection + schema type consistency across edges. Optimizer performs topological sort → parallel execution layers. Executor manages failures, feeds error traces back to Planner for DAG restructuring. Episodic Memory archives successful DAGs for reuse.

### RF-2: LitMOF's Cross-Validation Pattern

**Source**: LitMOF (arxiv 2512.01693, indexed: docs__default)

Supervisor + 5 specialized agents with Unified Agent Template (LLM-driven head + node set with stored plan memory). Inspector & Editor performs multi-source reconciliation — architecturally separate from reading agents. Key insight: each information source requires a specialist reader, and reconciliation is a distinct stage.

### RF-3: Structured Memory with Reuse (Cross-Paper Convergence)

**Source**: DeepEye Episodic Memory, LitMOF reusable agent plans, HoldUp cluster cache

All three papers independently implement structured memory with reuse. DeepEye archives successful DAGs. LitMOF stores prior successful plans in head module memory. HoldUp caches representative clusters for reuse across similar datasets. Nexus has `plan_save`/`plan_search` but agents never read it proactively.

### RF-4: Pre-Execution Reconciliation (Cross-Paper Convergence)

**Source**: DeepEye Validator, LitMOF Inspector, HoldUp clustering

All three papers add a pre-execution step: DeepEye validates type compatibility across DAG edges. LitMOF reconciles heterogeneous sources before accepting corrections. HoldUp clusters before labeling to understand global distribution. Nexus processes in sequence without prior reconciliation.

### RF-5: The Orchestrator Agent Does Not Work and Should Be Retired

**Source**: Operational experience, user feedback (2026-04-07)

The orchestrator agent (`nx/agents/orchestrator.md`) is never invoked in practice. The main
conversation acts as the orchestrator — the user tells Claude what to do, Claude dispatches
agents directly. The fundamental constraint is architectural: **subagents cannot spawn other
subagents**, so the orchestrator can only recommend routing, not execute pipelines.

The `using-nx-skills` routing skill already replaced the orchestrator's dispatch logic
(documented in CHANGELOG when `/nx:orchestrate` was removed). The orchestrator agent's
remaining value is as **process documentation** — it describes standard pipeline patterns
(RDR chain, plan-audit-implement, research-synthesize) that the main conversation and skills
reference when deciding what to dispatch.

The RDR's original Phases 2-3 (typed pipeline schemas, knowledge-validator agent, DAG
validation) are solutions to problems that don't exist in practice. The main conversation
doesn't need a JSON schema to dispatch two agents — it needs to know which agents exist
and what order to use them. That's a documentation problem, not an infrastructure problem.

## Proposed Design

> **Scope note:** The original design proposed typed pipeline DAGs, I/O schema validation,
> and a knowledge-validator agent. Operational reality is simpler: the orchestrator agent
> doesn't work, the routing skill already handles dispatch, and what's missing is clear
> process documentation. The design is reduced to: retire the agent, preserve its knowledge
> as documentation, and ensure plan_search is wired into the skill layer.

### Phase 1: Retire Orchestrator Agent (hours)

**1a. Convert orchestrator agent to reference document**

Move `nx/agents/orchestrator.md` content to `nx/skills/orchestration/reference.md` (or similar).
Remove the agent frontmatter (name, model, color). Keep the routing tables, pipeline templates,
and standard workflow patterns as skill reference material that the main conversation and
using-nx-skills consult.

Update all references:
- `using-nx-skills` SKILL.md — remove orchestrator from agent routing table
- `plugin.json` — remove orchestrator from agent registry
- Any skill or hook that mentions the orchestrator agent

**1b. Wire plan_search into using-nx-skills**

Add to the using-nx-skills skill flow, before agent dispatch:
```
Before dispatching a multi-agent pipeline:
1. Call plan_search(query="<task description>")
2. If matching template found, present as suggested pipeline structure
3. After successful pipeline completion, call plan_save(content=<relay chain>)
```

This is lightweight — it adds one MCP call before dispatch and one after completion.
No typed schemas, no validation, no new agents.

### Phase 2: Document Standard Pipelines (days)

**2a. Pipeline pattern catalog**

Document the standard multi-agent pipelines that actually get used:

| Pipeline | Pattern | When to use |
|----------|---------|-------------|
| RDR chain | research → gate → accept → plan → implement | New feature design |
| Plan-audit-implement | strategic-planner → plan-auditor → developer | Planned implementation |
| Research-synthesize | deep-research → knowledge-tidier | Literature survey |
| Code review | developer → code-review-expert → test-validator | Feature completion |
| Debug | debugger → test-validator | Test failures |

Store as plan library templates via `plan_save` (already seeded 5 templates at `nx catalog setup`).

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Retiring orchestrator agent breaks references | Grep all references before removal; update to point at reference doc |
| plan_search returns irrelevant templates | 5 builtin templates are curated; user templates accumulate via plan_save |

## Success Criteria

- [ ] Orchestrator agent retired — no longer in agent registry
- [ ] Routing tables and pipeline patterns preserved as reference documentation
- [ ] plan_search called in using-nx-skills before multi-agent dispatch
- [ ] Standard pipeline patterns documented and stored in plan library
