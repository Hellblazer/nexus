---
rdr: "058"
title: "Pipeline Orchestration and Plan Reuse"
status: closed
closed_date: 2026-04-09
reason: implemented
---

# RDR-058 Post-Mortem

## Outcome

Fully implemented. The obsolete orchestrator agent was retired and converted to reference documentation. Five pipeline pattern templates were stored in the T2 plan library. `plan_save`/`plan_search` MCP tools are wired into `mcp_server.py` and integrated into the skill workflow.

## What Worked

- **Scope reduction from operational reality**: RF-5 identified that the orchestrator agent was never invoked in practice — the main conversation already acts as the orchestrator. Cutting Phases 2-3 (typed pipeline DAGs, knowledge-validator agent) avoided building infrastructure for problems that don't exist.
- **Documentation-over-infrastructure decision**: Recognizing that pipeline routing is a documentation problem, not an infrastructure problem, kept the implementation lightweight and immediately useful.
- **Plan library reuse via MCP**: Wiring `plan_search` into the skill workflow before multi-agent dispatch means successful pipeline patterns accumulate and are consulted automatically, matching the episodic memory pattern from DeepEye (RF-1) and LitMOF (RF-3).
- **Orchestration reference skill**: Converting the orchestrator agent's routing tables and pipeline templates to `nx/skills/orchestration/reference.md` preserved institutional knowledge without maintaining dead agent code.

## What Didn't Work

- **Original scope was over-engineered**: The initial design proposed typed DAG validation and I/O schema consistency checking — solutions looking for problems. The subagent-cannot-spawn-subagent constraint made the orchestrator agent architecturally impossible from the start; this should have been caught earlier.

## Deliverables

- Orchestrator agent retired from agent registry and `plugin.json`
- Orchestration reference skill at `nx/skills/orchestration/reference.md`
- 5 pipeline pattern templates seeded in T2 plan library
- `plan_save()` and `plan_search()` MCP tools wired in `mcp_server.py`
- `using-nx-skills` updated to consult `plan_search` before multi-agent dispatch
