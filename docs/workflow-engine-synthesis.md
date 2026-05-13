# Workflow Engine Landscape Synthesis

Deep-research-synthesizer (run a78c5a1c) cross-source synthesis on workflow-engine design doc, May 2026.

## Landscape position
Sparse quadrant: declarative/data-first AND natively MCP-aware. Nobody else there.

Four populated neighbors:
- **Code-first durable**: Temporal, Restate, Inngest, Azure Durable Functions. Polyglot, production-grade, rich HITL. None MCP-aware.
- **Code-first LLM**: LangGraph, CrewAI. First-class LLM nodes; not searchable/diffable.
- **Data-first infrastructure**: AWS Step Functions/ASL, Argo, Tekton, Nextflow, Serverless Workflow Spec. Not LLM-authorable; expression languages bolted on.
- **Data-first no-code**: n8n, Zapier, Prefect. Visual; not LLM-emittable.

## Data-first prior-art lessons (every system learned these)
1. **In-flight versioning is painful.** AWS shipped immutable versions+aliases in 2023 for this. Engine: what happens to parked workflow when its blueprint changes? No policy yet.
2. **Conditional logic escapes the DSL.** ASL added intrinsic functions; jq-vs-JSONPath debate in Serverless Workflow Spec. Parmar's "re-engage design phase" is principled but breaks on data-driven conditionals.
3. **Sub-workflow composition is table-stakes.** Step Functions, Argo, Temporal, BPMN all have it. 5-primitive DSL doesn't.
4. **Saga/compensating transactions absent.** Continue-or-abort only.

## HITL taxonomy — 5 mechanically distinct shapes
| Shape | Example | Connection held? |
|---|---|---|
| Blocking RPC | Camunda BPMN sync; doc v1 | Yes |
| Task token/callback | Step Functions .waitForTaskToken | No |
| Durable promise/awakeable | Restate ctx.awakeable() | No |
| waitForEvent | Inngest, Cloudflare Workflows | No (bus-bound) |
| Yield/parking | Doc v2 default | No |

Task-token and durable-promise are the same pattern at different infrastructure layers. Doc's engine.resume(workflow_id, checkpoint_id, payload) IS task-token-on-SQLite. **MCP Tasks primitive (SEP-1686, experimental)** is the protocol-level task-token. If matures (gaps: retry, expiry, idempotency), eliminates need for SQLite parking layer.

## MCP ecosystem state (2026)
- **2026 roadmap**: Streamable HTTP preferred; Tasks primitive experimental; no workflow orchestration in roadmap — engine doesn't duplicate.
- **ext-apps spec (2026-01-26)**: Claude + ChatGPT support; VS Code + Goose joining; Java SDK not yet. Security via iframe sandbox + postMessage, no formal CSP spec.
- **Mediator pattern in MCP space**: Parmar's framing doesn't appear in other published MCP work. Genuine novelty within MCP ecosystem.

## Convergent evidence (where the design aligns with mature systems)
- Snapshot+audit-log split is right (matches LangGraph, Temporal, every mature system)
- Per-step timeout on human-approval is mandatory (universal)
- ~300 LoC SQLite checkpointer estimate consistent with LangGraph's ~400 LoC Python

## Genuine novelties (defensible claims)
1. LLM-authored JSON workflow as first-class retrievable artifact (no prior system)
2. MCP Mediator architecture (no other published MCP work frames it this way)
3. plan_search/plan_match/plan_promote lifecycle (no traditional workflow system indexes its definitions)

Novelty is "nobody else had this constraint" (LLMs + MCP standardization 2024-2025), not "nobody else would."

## 5 pre-commit experiments
1. **Measure Claude Code's stdio tool-call timeout.** One afternoon. Gates whether v1 blocking await_user is viable without HTTP+SSE.
2. **Verify TS interpreter is flat-loop or recursive-descent.** Architectural question. Determines whether "state is data" is discipline or refactor.
3. **Find JMESPath wall in 10 real blueprints.** The wall comes when templates need to CONSTRUCT new objects from parts of multiple prior outputs. Decide JSONata/intrinsics/re-engage from evidence.
4. **Spike await_user on MCP Tasks (SEP-1686)** as alternative v2 path.
5. **Prototype multi-iframe routing** for parallel { await_user; await_user }. Spec gap is real.

## Sources cited
Parmar 2026 paper; MCP 2026 roadmap; ext-apps spec; Restate/Inngest/Step Functions docs; LangGraph checkpointer code; Serverless Workflow Spec; Kai Waehner durable-execution survey; Temporal blog; Argo Workflows suspend/resume; Zigflow YAML-DSL-for-Temporal article.

## RDR-110/111/112 triad relationship (2026-05-13)

The workflow engine slots in as another consumer of the RDR-110 tuple
space, the RDR-111 ORB bus, and the RDR-112 daemons — no retrofit
required on any of the three. One load-bearing constraint: cross-
container coordination (workflow steps that span sandbox boundaries)
is gated on `NX_STORAGE_MODE=daemon` per the RDR-112 sequencing
decision. Same-host single-FS deployments can use direct-file mode
for early prototyping; any deployment that crosses an overlayfs bind
mount must run the T2 daemon. Checkpointer surfaces compose as
tuple-space subspaces; durable-execution wake semantics land on the
RDR-112 `EventStream` RPC. Host trust inherited from RDR-113.
