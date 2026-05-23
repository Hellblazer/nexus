# RDR-080 Migration Scope — Agent/Skill Files Touched in P2a/P3/P4

## P2a (DONE — commit 7a4fcbc)

| File | Action | Before | After |
|------|--------|--------|-------|
| conexus/skills/query/SKILL.md | Collapse | 256 lines, 3-path dispatch with scratch relay | 24 lines, nx_answer MCP pointer |
| conexus/agents/query-planner.md | Delete | 279 lines | — |
| conexus/agents/analytical-operator.md | Delete | 248 lines | — |
| conexus/retrieval-agents.txt | Reduce | 10 entries | 2 entries (deep-analyst, deep-research-synthesizer) |
| conexus/agents/strategic-planner.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| conexus/agents/architect-planner.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| conexus/agents/code-review-expert.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| conexus/agents/substantive-critic.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| conexus/agents/debugger.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| conexus/agents/plan-auditor.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| conexus/agents/codebase-deep-analyzer.md | Strip preamble | plan_match-first section (10 lines) | Removed |
| conexus/registry.yaml | Update | analytical-operator + query-planner entries | Deleted; query added as standalone skill |
| conexus/README.md | Update | 16-row agent table | 14-row agent table |
| conexus/skills/orchestration/reference.md | Update | analytical-operator in implementation table | Removed |
| conexus/skills/plan-first/SKILL.md | Update | "query-planner agent" reference | "nx_answer" reference |
| conexus/hooks/scripts/subagent-start.sh | Update | analytical-operator dispatch block | nx_answer + operator tools pointer |
| tests/test_plugin_structure.py | Update | query not in standalone | query in _STANDALONE_SKILLS |

## P2b/P2c (nexus-qo0.6)

| File | Action | Before | After |
|------|--------|--------|-------|
| conexus/skills/plan-first/SKILL.md | Add section | No internal enforcement section | Add §Internal enforcement explaining nx_answer gate |
| docs/architecture.md | Add section | No boundary rule | Add boundary rule + per-tool classification table |

## P3 (nexus-qo0.8) — nx_tidy, nx_enrich_beads, nx_plan_audit MCP tools

| File | Action | Before | After |
|------|--------|--------|-------|
| conexus/agents/knowledge-tidier.md | Collapse to stub | Full agent (~200 lines) | ≤15 lines: "use nx_tidy MCP tool" |
| conexus/agents/plan-enricher.md | Collapse to stub | Full agent (~200 lines) | ≤15 lines: "use nx_enrich_beads MCP tool" |
| conexus/agents/plan-auditor.md | Collapse to stub | Full agent (~200 lines) | ≤15 lines: "use nx_plan_audit MCP tool" |
| conexus/skills/knowledge-tidying/SKILL.md | Collapse | Full skill | Trigger pointer to nx_tidy |
| conexus/skills/enrich-plan/SKILL.md | Collapse | Full skill | Trigger pointer to nx_enrich_beads |
| conexus/skills/plan-validation/SKILL.md | Collapse | Full skill | Trigger pointer to nx_plan_audit |
| conexus/agents/deep-research-synthesizer.md | Update | Dispatches knowledge-tidier agent | Cites nx_tidy MCP tool |
| conexus/registry.yaml | Update | Agent entries for knowledge-tidier, plan-enricher | Reduced entries or standalone skills |
| src/nexus/mcp/core.py | Add tools | No nx_tidy/nx_enrich_beads/nx_plan_audit | 3 new operator-pool-backed MCP tools |

## P4 (nexus-qo0.10) — PDF processor deletion

| File | Action | Before | After |
|------|--------|--------|-------|
| conexus/agents/pdf-chromadb-processor.md | Delete | Full agent | — |
| conexus/skills/pdf-processing/SKILL.md | Delete | Full skill | — |
| conexus/agents/deep-research-synthesizer.md | Update | Dispatches pdf-chromadb-processor | Direct nx index pdf CLI reference |
| conexus/registry.yaml | Update | pdf-chromadb-processor entry | Deleted |

## SC-4 Post-Migration Grep

After P4 completes, this grep must return zero non-comment matches:

```bash
grep -r "plan-auditor\|knowledge-tidier\|pdf-chromadb-processor\|plan-enricher\|query-planner\|analytical-operator" \
  conexus/agents/ conexus/skills/ | grep -v "\.md:\s*#"
```
