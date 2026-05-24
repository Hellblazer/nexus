# Brainstorming brief — adjacent design space for the workflow engine

12 angles surfaced by architect-planner agent (run abe8e36d) on the workflow-engine design doc, May 2026.

## 1. nx plan library *is* the BlueprintStore
Collapse `BlueprintStore` into nx plan library when engine ships inside nexus. T1→T2→T3 promotion maps onto draft→tested→battle-hardened blueprint maturity. `plan_match` becomes "did somebody already write this?" lookup. Cost: hard nx dependency; standalone-engine story weakens.

## 2. Catalog tumblers as workflow-scoped identifiers
Promote workflow runs to catalog Documents, steps as linkable entities, parent_step_id/branch_id as graph edges. Buys queryability through existing catalog APIs ("what workflows touched this file"). Cost: catalog write pressure during execution.

## 3. Effect-shadow static analysis
From blueprint + per-tool effect annotations, compute the effect shadow (tables touched, side effects, idempotency, parallel-branch conflicts) before execution. Buys CI-style production warnings, idempotency proofs, feeds capability-aware permissions. Cost: depends on MCP effect-annotation ecosystem that doesn't exist yet.

## 4. Semantic blueprint diff
Normalize step IDs by topological position, compute graph isomorphism on data-flow DAG. Buys meaningful PR review, safe auto-merge of equivalent blueprints. Cost: non-trivial; partial implementations mislead.

## 5. Workflow-as-test-fixture (record/replay)
Audit log becomes record/replay substrate. Capture (tool, params)→result during real runs, replay with shadowed calls. Buys regression tests, blueprint refactoring without re-spending tool budgets, reproducible bugs, v1→v2 migration test substrate. Cost: result-fidelity fragility (timestamps, UUIDs); needs canonicalization.

## 6. Cross-blueprint composition (engine.run_workflow as tool)
Blueprints become composable units. Hard part: recursion, cycle detection, naming/visibility across projects. Required machinery: call-stack depth limit, BlueprintStore cycle detection at save time. Buys real composition, library-of-blueprints culture. Cost: debugging across nested boundaries needs trace tree.

## 7. OpenTelemetry trace-tree by construction + cost roll-up
parent_step_id/branch_id already in audit log shape. Emit OTel spans natively → workflow becomes single trace tree by construction. Cost attribution rolls up free. Buys drop-in observability compatibility. Cost: OTel dep in hot path; audit-log-vs-trace overlap needs resolution (probably unify).

## 8. Agent-side describe_workflow tool
Workflows expose as tools to agents; can agent UNDERSTAND one before calling? describe_workflow returns plain-English explanation, effect shadow, human-touch points, expected duration. Buys better agent tool-selection, risk-aware execution, LLM-driven blueprint editing. Cost: derived descriptions can lie, cache-invalidation on edit.

## 9. Capability-aware blueprints (workflow-as-privilege-bracket)
Workflow is one tool that fans out to N tool calls — transitive permission expansion. Two design moves: (a) blueprints declare required capabilities, host gates run_workflow against union; (b) engine subject-shifts — workflow runs under blueprint's identity, not agent's, with separately-granted capability bundle (like sudo). Buys meaningful workflow-level permissions, grant agents access to outcomes without underlying tools.

## 10. UI surfaces beyond ext-apps
Voice (Twilio), Slack thread, mobile push, email all fit "deliver prompt → external system → return input → resume" shape. Forces yield over blocking for anything beyond seconds. Buys engine as interaction substrate, not just iframe substrate. Cost: v1 blocking model breaks; multi-channel routing logic.

## 11. Blueprint mining from observed agent traces
Inverse of authoring. Mine recurring tool-call sequences from prior agent traces, propose blueprints automatically. Buys cold-start solution, quantifies blueprint value via shadow execution, aligns with plan_match philosophy. Cost: trace privacy, false-positive blueprints. Constrains v1 audit-log schema (must be mineable).

## 12. The "no v2 durability ever" steelman
Never build v2. Constraints: every tool idempotent (or carries dedup key); long-human-time workflows use yield with agent re-issue; engine crash loses in-flight but blueprints durable so agent re-runs. Buys massive simplification (no ExecutionStore SQLite, no checkpoint serialization, no race conditions, no version-skew). Engine genuinely stateless, horizontal scaling trivial. Cost: idempotency-as-invariant is ecosystem tax; loses "park for 3 days" use case.

## Cross-cutting themes
- Yield wins on multiple independent vectors (HITL taxonomy, multi-channel UI, idempotent steelman)
- Effect annotations on MCP tools are missing ecosystem piece for #3, #9, #11
- MCP Tasks primitive + effect annotations + nx plan library = three external substrate questions that could simplify the doc
