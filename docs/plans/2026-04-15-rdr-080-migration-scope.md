# RDR-080 Migration Scope — Agent/Skill Files Touched in P2a/P3/P4

## P2a (DONE — commit 7a4fcbc)

| File | Action | Before | After |
|------|--------|--------|-------|
| nx/skills/query/SKILL.md | Collapse | 256 lines, 3-path dispatch with scratch relay | 24 lines, nx_answer MCP pointer |
| nx/agents/query-planner.md | Delete | 279 lines | — |
| nx/agents/analytical-operator.md | Delete | 248 lines | — |
| nx/retrieval-agents.txt | Reduce | 10 entries | 2 entries (deep-analyst, deep-research-synthesizer) |
| nx/agents/strategic-planner.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| nx/agents/architect-planner.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| nx/agents/code-review-expert.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| nx/agents/substantive-critic.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| nx/agents/debugger.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| nx/agents/plan-auditor.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| nx/agents/codebase-deep-analyzer.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| nx/registry.yaml | Update | analytical-operator + query-planner entries | Deleted; query added as standalone skill |
| nx/README.md | Update | 16-row agent table | 14-row agent table |
| nx/skills/orchestration/reference.md | Update | analytical-operator in implementation table | Removed |
| nx/skills/plan-first/SKILL.md | Update | "query-planner agent" reference | "nx_answer" reference |
| nx/hooks/scripts/subagent-start.sh | Update | analytical-operator dispatch block | nx_answer + operator tools pointer |
| tests/test_plugin_structure.py | Update | query not in standalone | query in _STANDALONE_SKILLS |

## P2b/P2c (nexus-qo0.6)

| File | Action | Before | After |
|------|--------|--------|-------|
| nx/skills/plan-first/SKILL.md | Add section | No internal enforcement section | Add §Internal enforcement explaining nx_answer gate |
| docs/architecture.md | Add section | No boundary rule | Add boundary rule + per-tool classification table |

## P3 (nexus-qo0.8) — nx_tidy, nx_enrich_beads, nx_plan_audit MCP tools

| File | Action | Before | After |
|------|--------|--------|-------|
| nx/agents/knowledge-tidier.md | Collapse to stub | Full agent (~200 lines) | ≤15 lines: "use nx_tidy MCP tool" |
| nx/agents/plan-enricher.md | Collapse to stub | Full agent (~200 lines) | ≤15 lines: "use nx_enrich_beads MCP tool" |
| nx/agents/plan-auditor.md | Collapse to stub | Full agent (~200 lines) | ≤15 lines: "use nx_plan_audit MCP tool" |
| nx/skills/knowledge-tidying/SKILL.md | Collapse | Full skill | Trigger pointer to nx_tidy |
| nx/skills/enrich-plan/SKILL.md | Collapse | Full skill | Trigger pointer to nx_enrich_beads |
| nx/skills/plan-validation/SKILL.md | Collapse | Full skill | Trigger pointer to nx_plan_audit |
| nx/agents/deep-research-synthesizer.md | Update | Dispatches knowledge-tidier agent | Cites nx_tidy MCP tool |
| nx/registry.yaml | Update | Agent entries for knowledge-tidier, plan-enricher | Reduced entries or standalone skills |
| src/nexus/mcp/core.py | Add tools | No nx_tidy/nx_enrich_beads/nx_plan_audit | 3 new operator-pool-backed MCP tools |

## P4 (nexus-qo0.10) — PDF processor deletion

| File | Action | Before | After |
|------|--------|--------|-------|
| nx/agents/pdf-chromadb-processor.md | Delete | Full agent | — |
| nx/skills/pdf-processing/SKILL.md | Delete | Full skill | — |
| nx/agents/deep-research-synthesizer.md | Update | Dispatches pdf-chromadb-processor | Direct nx index pdf CLI reference |
| nx/registry.yaml | Update | pdf-chromadb-processor entry | Deleted |

## SC-4 Post-Migration Grep

After P4 completes, this grep must return zero non-comment matches:

```bash
grep -r "plan-auditor\|knowledge-tidier\|pdf-chromadb-processor\|plan-enricher\|query-planner\|analytical-operator" \
  nx/agents/ nx/skills/ | grep -v "\.md:\s*#"
```
