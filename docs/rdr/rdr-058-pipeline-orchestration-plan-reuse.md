---
title: "Pipeline Orchestration and Plan Reuse"
id: RDR-058
type: Feature
status: draft
priority: medium
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

## Proposed Design

### Key Decision: Where Does Orchestration Live?

Given the subagent-cannot-spawn-subagent constraint, the orchestrator agent should be **retired as an agent** and its intelligence redistributed:

**Option A: Skill-Based Orchestration (recommended)**
- A new `pipeline-dispatch` skill that the main conversation invokes before launching multi-agent work
- The skill calls `plan_search` for matching templates, validates I/O compatibility, and emits a structured dispatch plan
- The main conversation then dispatches agents per the plan

**Option B: Hook-Based Enforcement**
- SubagentStart hook checks if the dispatched agent's expected inputs match available context
- Blocks dispatch with a warning if inputs are missing

**Option C: Enhanced using-nx-skills Routing**
- Fold pipeline templates directly into the using-nx-skills skill
- Add plan_search call to the skill's process flow

### Phase 1: Plan Reuse (hours-days)

**1a. plan_search before pipeline construction**

Add to using-nx-skills or create a lightweight pipeline-dispatch skill:
```
Before dispatching a multi-agent pipeline:
1. Call plan_search(query="<task description>")
2. If matching template found, use as starting pipeline structure
3. After successful pipeline completion, call plan_save(content=<relay chain>)
```

**1b. Retire orchestrator agent → reference document**

Convert `nx/agents/orchestrator.md` from an agent definition to a reference document that skills and the main conversation consult. Remove from registry.yaml agent list. Keep the routing tables, pipeline templates, and failure relay protocol as documentation.

### Phase 2: Typed Pipeline Plans (weeks)

**2a. Pipeline Plan JSON schema**

Define a JSON schema for pipeline plans:
```json
{
  "goal": "string",
  "stages": [
    {
      "agent": "strategic-planner",
      "inputs": {"type": "user-request", "format": "text"},
      "outputs": {"type": "plan", "format": "relay"},
      "depends_on": []
    },
    {
      "agent": "plan-auditor",
      "inputs": {"type": "plan", "format": "relay"},
      "outputs": {"type": "audit-report", "format": "relay"},
      "depends_on": ["strategic-planner"]
    }
  ],
  "parallel_groups": [["agent-a", "agent-b"]]
}
```

**2b. Validation step in pipeline-dispatch skill**

Before dispatching, check:
- Each stage's input type matches its predecessor's output type
- No cycles in depends_on graph
- Parallel groups have no inter-dependencies

### Phase 3: Normalized Result Envelope (weeks)

**3a. Search result metadata propagation**

Extend search result format to always include `{tumbler, corpus, content_type, distance}` alongside chunk_text. The catalog already computes these — search just doesn't propagate them.

**3b. Knowledge-validator agent**

New agent implementing LitMOF's Inspector & Editor pattern: for a T3 collection, validate stored entries against source documents (catalog resolve → search → compare), detect divergent claims, surface via link_audit, auto-repair or flag for human review.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Retiring orchestrator agent breaks existing references | Grep for all references; update to point at documentation |
| Typed pipeline schema adds friction | Keep optional — prose plans still work; schema is for validation only |
| plan_search returns irrelevant templates | Tune similarity threshold; manual curation of plan library |
| Knowledge-validator produces false-positive divergence reports | Start with conservative thresholds; human-in-the-loop flag rather than auto-repair |

## Open Questions

1. **Should the orchestrator agent be fully retired**, or kept as a "routing advisor" that returns recommendations without dispatching? The current system already works without it — the question is whether the reference material is more useful as an agent prompt or as documentation.
2. **Where should pipeline templates live** — T2 plan library (TTL-based, session-scoped), T3 knowledge (permanent), or as markdown files checked into the repo?
3. **How much validation is worth the latency?** A full Compiler→Validator→Optimizer pass per DeepEye adds overhead. Worth it for 6-stage pipelines, probably not for 2-stage.

## Success Criteria

- [ ] plan_search called before every multi-agent pipeline dispatch
- [ ] Successful pipelines saved to plan library for reuse
- [ ] Pipeline plans validated for I/O type consistency
- [ ] Orchestrator agent either retired or has clear invocation path
- [ ] At least one pipeline template exists in plan library per standard workflow
