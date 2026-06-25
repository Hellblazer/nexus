---
title: "Service-Mode Catalog Interface Conformance: Make HttpCatalogClient a Faithful Drop-In for the Local Catalog, Enforced by a Signature-Conformance Test"
id: RDR-168
type: Bug Fix
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-25
accepted_date: 2026-06-25
related_issues: [nexus-7y0ab]
related: [RDR-152, RDR-155, RDR-164, RDR-108, RDR-103]
---

# RDR-168: Service-Mode Catalog Interface Conformance

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

In service mode (the 6.0.0 default), the catalog is served by `HttpCatalogClient`
(93 public methods, talking to the Java `CatalogHandler` over HTTP). It is supposed
to be a drop-in substitute for the local `Catalog` so that callers — the indexer, the
`nx catalog` CLI, post-store hooks — work unchanged regardless of backend. It is not:
several method **signatures diverge** from the local interface. Callers written
against the local interface therefore raise `TypeError` in service mode, and because
those errors are caught and logged as warnings (or surface only in interactive CLI),
the failure is silent. The concrete, observed consequence is that **service-mode
`nx index repo` does not populate the catalog at all** — chunks land in pgvector, but
the catalog documents and the `catalog_document_chunks` manifest stay empty, breaking
document-level search, `nx catalog list`, and manifest-based GC. T3 vector chunk
search still works, which is exactly why this stayed hidden.

### Enumerated gaps to close

#### Gap 1: HttpCatalogClient method signatures diverge from the local catalog

Confirmed during the 6.0.0 validation pass:
- `collection_for`: local `Catalog.collection_for(content_type, owner, embedding_model)`;
  `HttpCatalogClient.collection_for(*, content_type, owner_id, embedding_model)`.
  `indexer.py:537` calls `collection_for(content_type=ct, owner=owner, ...)` →
  `TypeError: unexpected keyword argument 'owner'` (caught as `phase4_migration_failed`).
- `all_documents`: local accepts `content_type=`; `HttpCatalogClient.all_documents()`
  rejects it → `TypeError` in `nx catalog list` (`catalog.py:437`).

These were the first two found; the full audit (Research Findings) since enumerated the
class: of 87 caller-facing methods, **18 break**, 1 silently absorbs, 6 are benign, and
62 match.

#### Gap 2: Service-mode `nx index repo` silently does not populate the catalog

As a downstream consequence of Gap 1 (and possibly other divergences in the
registration/manifest path), a fresh `nx index repo` in service mode yields
`Documents: 0`, `Chunks: 0`, and `manifest_empty_skipping_gc` despite chunks being
written to pgvector. The catalog/document layer of the 6.0.0 product is effectively
non-functional for indexing. (RDR-152/164 moved catalog writes server-side; the
client-facing interface contract was not pinned.)

#### Gap 3: No interface-conformance test — the divergence is structurally invisible

The 173 passing catalog HTTP integration tests call `HttpCatalogClient` with *its own*
signatures, never the caller contract. So a method can diverge from the local
interface and every test stays green. There is no test asserting that `HttpCatalogClient`
satisfies the same interface as `Catalog`. This is the root test-gap: without it, the
class of bugs is undetectable and will recur with every new method.

## Context

### Background

Discovered when the substantive-critic, reviewing the 6.0.0 service-mode fixes
(`nexus-82ihm`), correctly flagged that the chash-backfill fix (`nexus-84gbt`) did not
explain the `Chunks:0` symptom. A follow-up empirical probe (service-mode `nx index
repo` against an isolated stack) surfaced the `collection_for(owner=...)` TypeError and
the empty catalog, then a second `all_documents(content_type=...)` TypeError in `nx
catalog list` — establishing this as a signature-parity class, not an isolated bug.
Filed as `nexus-7y0ab` (P1).

### Technical Environment

- Local: `src/nexus/catalog/catalog.py` (`Catalog` facade) + `catalog_docs.py`
  (`_DocumentOps`). Service: `src/nexus/catalog/http_catalog_client.py` →
  `service/src/main/java/dev/nexus/service/http/CatalogHandler.java`.
- Backend selection: `nexus.catalog.factory.make_catalog_reader/writer` +
  `nexus.db.storage_mode.storage_backend_for("catalog")`.
- Callers: `src/nexus/indexer.py` (registration + legacy migration + manifest),
  `src/nexus/commands/catalog.py` (CLI), post-store manifest hook in
  `src/nexus/mcp_infra.py`.

## Research Findings

### Investigation

**Full signature audit (completed 2026-06-25, introspection spike).** Compared
`inspect.signature` of every public method of `Catalog` (the caller-facing facade)
against `HttpCatalogClient`. Result: `Catalog` exposes **87** public methods,
`HttpCatalogClient` **93**; **0** are missing on the client; **62 match**; **25
diverge**. Classifying the 25 by caller impact (does a caller written against the local
signature break?):

- **18 BREAKING** — the client is missing, or has renamed, a parameter the caller
  passes, with no `**kwargs` to absorb it → `TypeError`: `all_documents`,
  `bulk_unlink`, `collection_for`, `collection_for_repo`, `ensure_owner_for_repo`,
  `graph`, `graph_many`, `is_initialized`, `link`, `links_from`, `links_to`,
  `list_by_collection`, `lookup_doc_id_by_collection_and_path`, `resolve_chash`,
  `resolve_span`, `supersede_collection`, `update_document_collection`,
  `update_documents_collection_batch`.
- **1 SILENT** — `link_if_absent` has a `**kwargs`. Gate-round runtime tracing refined
  this: `link_if_absent(**kwargs)` forwards to `self.link(...)`, and the client's `link`
  *does* accept `from_span`/`to_span`/`created_by`, so those forward correctly. The true
  residual gap is `allow_dangling` (and `meta`), which the client's `link` lacks — but
  no current caller passes `allow_dangling` (indexer call sites pass only `created_by`),
  so the practical damage today is zero. The lasting hazard is structural: the `**kwargs`
  makes the signature *look* compatible, so a naive conformance predicate would not catch
  a future explicit-param divergence here — which is why the predicate is specified to
  require explicit named params (see Technical Design).
- **6 BENIGN** — the client only adds extra optional params (`atomic_manifest_replace`,
  `link_query`, `register`, `register_collection`, `register_owner`,
  `rename_collection`); callers using the local signature are unaffected.

Several BREAKING methods sit directly in the indexing/registration path
(`collection_for`, `collection_for_repo`, `update_document_collection`,
`supersede_collection`), strongly supporting the hypothesis that the empty-catalog
symptom (Gap 2) is primarily caused by these signature divergences. Confirming there is
no *second* cause (manifest hook / `catalog_doc_id` threading) is the remaining
implementation-phase spike (CA-4).

Reproduction: `/tmp/catalog_conformance_spike.py` (full audit), `/tmp/catalog_breaking.py`
(breaking classification). Recorded in T2: `nexus_rdr/168-research-1`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `Catalog` / `_DocumentOps` (local) | Yes | 87 public methods; canonical caller-facing signatures |
| `HttpCatalogClient` | Yes | 93 public methods; 0 missing vs local; 62 match, 18 breaking, 1 silent, 6 benign |
| `CatalogHandler.java` | Partial | endpoints exist for all client methods; the gap is the Python client signature, not a missing endpoint |

### Key Discoveries

- **Verified** — the divergence is **18 breaking methods (+1 silent)**, not 2. Patching
  only the two known (`collection_for`, `all_documents`) would have left 16 breaking +
  1 silent unaddressed — a 9× under-estimate. The audit-first approach is vindicated.
- **Verified** — the introspection conformance test (~40 lines, pure `inspect`) catches
  every divergence including the two known. It needs no hand-maintained list; it is the
  recurrence guard (CA-2).
- **Verified** — `Catalog` has 87 public methods and `HttpCatalogClient` is missing
  none of them, so a `Protocol` over the caller-facing surface is feasible (CA-1).
- **Verified** — service-mode `nx index repo` leaves the catalog empty
  (`Documents:0`/`manifest_empty`); multiple breaking methods are in that path.
- **Assumed** — signature reconciliation alone fully restores catalog population (no
  second cause). Needs the implementation spike (CA-4).

### Critical Assumptions

- [x] **The caller-facing catalog interface can be expressed as a single Protocol/ABC**
  that both `Catalog` and `HttpCatalogClient` are intended to satisfy — **Status**:
  Verified — **Method**: Source Search (87 local methods, 0 missing on the client; the
  surface is bounded and enumerable)
- [x] **A signature-conformance test would have caught both confirmed divergences** and
  is cheap to maintain (introspection-based, not a hand-list) — **Status**: Verified
  — **Method**: Spike (`/tmp/catalog_conformance_spike.py` flags all 25 divergences incl.
  the 2 known; ~40 lines)
- [ ] **Reconciling the signatures (renaming params / adding params on the client) does
  not break the 173 existing HTTP integration tests** — **Status**: Unverified —
  **Method**: Source Search + run the suite (deferred to implementation; scope now known:
  18 breaking methods, most fixes additive — accept the local param name, derive the
  client param; the `link_if_absent` `**kwargs` swallow needs explicit handling)
- [ ] **After signature reconciliation, the client correctly SERIALIZES every accepted
  param into the HTTP payload the Java `CatalogHandler` expects** (signature parity ≠
  wire correctness: accepting `owner` and then sending it as the wrong key, or dropping
  it, is a distinct failure the Python-only conformance test cannot see) — **Status**:
  Unverified — **Method**: covered by the integration-level MVV, which exercises the
  real wire path end-to-end (deferred to implementation)
- [ ] **Once signatures match, service-mode `nx index repo` populates the catalog**
  (Documents/Chunks > 0 + manifest) with no second root cause — **Status**: Unverified
  — **Method**: Spike (the integration-level MVV; deferred to implementation)

## Proposed Solution

### Approach

1. **Audit** (research): enumerate the caller-facing catalog surface and produce the
   complete divergence table (the unknown size becomes known).
2. **Pin the contract**: define the catalog interface as a `typing.Protocol` (the
   caller-facing subset, not all 87 methods) that both backends satisfy. Prefer the
   **local** signatures as canonical (callers are written against them); the contract is
   a *minimum* — the client may carry extra service-only params (see Technical Design).
3. **Conformance test**: an introspection-based test that, for every method on the
   Protocol, asserts `HttpCatalogClient` has a matching name + compatible signature.
   This is the artifact that makes the whole class detectable and keeps it from
   recurring.
4. **Reconcile**: bring every divergent `HttpCatalogClient` method to the canonical
   signature (e.g. accept `owner` and derive `owner_id`; accept `content_type`).
5. **Re-verify**: the MVV — service-mode `nx index repo` of a fixture repo populates
   the catalog (Documents/Chunks > 0 + manifest), asserted by a test.

Direction is deliberately audit-first: the project's history with patch-thrash
(`feedback_exhaustive_surface_audit`, `feedback_root_cause_after_repeated_patches`)
says fix the class with a conformance test, not the two symptoms in hand.

### Technical Design

The audit is complete (see Research Findings). Interface intent: a `CatalogReader` /
`CatalogWriter` Protocol pair capturing the **caller-facing subset** of `Catalog`
(derived empirically from the indexer / CLI / post-store-hook call sites and the 18
breaking methods, not all 87 — freezing internal helpers into the contract is
undesirable). The conformance test is parametrized over the Protocol's methods using
`inspect.signature`.

**The compatibility predicate is the load-bearing detail** (gate finding): it must NOT
be "the client is call-compatible with the local arguments," because a `**kwargs` on
the client satisfies that for *any* keyword argument — which is exactly how the
`link_if_absent`-class divergence stays invisible. The predicate must instead require,
for every **explicit named** parameter on the local Protocol method, a matching
**explicit named** parameter on the client (a `VAR_KEYWORD` does NOT satisfy it):

```text
// Required predicate (verify during implementation)
// local_named  = explicit positional/keyword params of Catalog.<m> (excl. **kwargs)
// client_named = explicit positional/keyword params of HttpCatalogClient.<m> (excl. **kwargs)
// for p in local_named: assert p in client_named   // by NAME; **kwargs does not count
// // one-directional: client MAY have extra params (service-only); do NOT flag those
```

The contract is a **minimum**, one-directional: the client must satisfy every local
param by explicit name, but may extend the signature with service-only params (the 6
BENIGN methods — e.g. `cross_model`, `legacy_grandfathered`, `new_collection` — are
deliberate service capabilities, NOT to be removed). The test flags missing/renamed
local params; it does not flag extra client params.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Catalog interface Protocol | implicit duck-typing today | Add: make the contract explicit |
| Conformance test | none | Add: the missing root-cause guard |
| Signature reconciliation | `http_catalog_client.py` | Extend each divergent method |

### Decision Rationale

A Protocol + conformance test converts an invisible, recurring class of bugs into a
compile-time-ish guard with one cheap test. Preferring local signatures as canonical
minimizes caller churn (callers already target them). Patching only the two known
methods is explicitly rejected (Alternative 1) as patch-thrash.

## Alternatives Considered

### Alternative 1: Patch only `collection_for` + `all_documents`

**Description**: Fix the two confirmed divergences, ship.

**Pros**: Smallest diff; unblocks service-mode indexing fastest.

**Cons**: Leaves the other 16 breaking (+1 silent) divergences the audit found
unaddressed; no guard against recurrence; the next divergence ships silently.

**Reason for rejection**: Exactly the patch-thrash pattern the project's discipline
forbids; the test-gap (Gap 3) is the actual root cause and goes unaddressed.

### Briefly Rejected

- **Auto-generate the client from a shared schema**: larger refactor than warranted;
  revisit only if the audit shows pervasive divergence.

## Trade-offs

### Consequences

- (+) Service-mode indexing/catalog works; the document layer of 6.0.0 becomes functional.
- (+) The whole divergence class becomes test-visible and regression-proof.
- (−) A Protocol the two backends must keep satisfying (enforced by the test, so cheap).

### Risks and Mitigations

- **Risk**: the audit reveals deep divergence (many methods). **Mitigation**: scope the
  Protocol to the caller-facing surface; phase the reconciliation.
- **Risk**: a second root cause for the empty manifest beyond signatures.
  **Mitigation**: the MVV asserts the end state (catalog populated), not just signature parity.

### Failure Modes

- Signature divergence → conformance test fails at build, not in production.
- Manifest still empty after reconciliation → MVV fails loud, pointing at the second cause.

## Implementation Plan

### Prerequisites

- [ ] Critical Assumptions verified (esp. the audit + the conformance-test spike).

### Minimum Viable Validation

Service-mode `nx index repo` of a fixture repo yields `catalog stats` Documents > 0 and
a non-empty manifest, asserted by an automated test; AND a conformance test fails on
the two currently-known divergences before the fix and passes after. In scope.

**The index MVV MUST be an integration test against the live service stack** (real
`CatalogHandler.java` + Postgres), NOT a mocked HTTP client (gate finding). CA-4's
safety net — "fails loud, points at a second cause" — only holds when the test
exercises the real wire path; a mocked client cannot detect a second cause (manifest
hook / `catalog_doc_id` threading / wire serialization).

### Phase 1: Code Implementation

#### Step 1: Full signature audit → divergence table (research output)
#### Step 2: Define the CatalogReader/Writer Protocol (caller-facing surface)
#### Step 3: Add the introspection-based conformance test (fails on current divergences)
#### Step 4: Reconcile every divergent HttpCatalogClient signature
#### Step 5: Service-mode index MVV test (catalog populated)

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Conformance test | N/A | CI | N/A | In scope | N/A |

### New Dependencies

None expected (uses `typing.Protocol` + `inspect`).

## Test Plan

- **Scenario**: conformance test over the Protocol — **Verify**: fails on
  `collection_for`/`all_documents` pre-fix, passes post-fix.
- **Scenario**: service-mode `nx index repo` fixture — **Verify**: Documents/Chunks > 0,
  manifest non-empty.
- **Scenario**: `nx catalog list` in service mode — **Verify**: no TypeError, lists docs.
- **Scenario**: the 173 existing HTTP integration tests — **Verify**: still green after reconciliation.

## Validation

### Testing Strategy

1. **Scenario**: signature conformance. **Expected**: parity enforced for the caller surface.
2. **Scenario**: service-mode index end-to-end. **Expected**: catalog populated (the MVV).

### Performance Expectations

N/A (correctness fix). No estimates.

## Finalization Gate

### Contradiction Check

No contradictions between research findings, design, and proposed solution. The gate's
substantive-critic raised no contradiction; its three Significant findings (conformance
predicate must require explicit named params; MVV must be integration-level; the
contract is a one-directional minimum) were folded into the Technical Design, MVV, and
Critical Assumptions rather than left as conflicts. Directional alignment with RDR-152/164
(server-side catalog authority) confirmed complementary — this RDR pins the *consumer*
contract, those pin the *persistence* authority.

### Assumption Verification

The four Critical Assumptions must be Verified (the audit + conformance-test spike are
load-bearing) before Accept.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `collection_for` / `all_documents` / registration path | HttpCatalogClient ↔ CatalogHandler | Source Search + Spike |
| `inspect.signature` introspection | stdlib | Source Search |

### Scope Verification

The MVV (service-mode index populates the catalog + conformance test) is in scope for
Phase 1, not deferred.

### Cross-Cutting Concerns

- **Versioning**: client/handler stay wire-compatible; only Python client signatures change.
- **Incremental adoption**: Protocol covers the caller surface first; can grow.
- **IDE compatibility**: Protocol improves static checking.
- Others: N/A.

### Proportionality

Right-sized: an audit + one Protocol + one conformance test + signature reconciliation.
The auto-generation alternative is explicitly deferred.

## References

- nexus-7y0ab (the filed P1), nexus-82ihm (the review that surfaced it).
- `src/nexus/catalog/catalog.py`, `catalog_docs.py`, `http_catalog_client.py`;
  `service/.../CatalogHandler.java`; `indexer.py:537`, `commands/catalog.py:437`,
  `mcp_infra.py` (manifest hook).
- RDR-152/164 (server-side catalog), RDR-108/103 (catalog/T3 identity).

## Revision History

- 2026-06-25: Initial draft (from the 6.0.0 validation + stacked-review discovery).
- 2026-06-25 (research round 1): Full signature audit completed. 87 local methods, 18
  BREAKING + 1 SILENT + 6 BENIGN divergences (not 2). CA-1 (interface enumerable) and
  CA-2 (introspection conformance test catches all divergences) Verified by spike.
  CA-3 (no regression) and CA-4 (catalog populates after fix) remain for the
  implementation phase. T2: `nexus_rdr/168-research-1`.
- 2026-06-25 (gate round): substantive-critic — 0 Critical, 3 Significant, 3
  Observations → PASSED. Folded in: (1) the conformance predicate must require EXPLICIT
  named params (a `**kwargs` must not satisfy it) or the SILENT class escapes the guard;
  (2) the index MVV must be an integration test against the live service stack;
  (3) the Protocol is a one-directional MINIMUM contract — the client may carry
  service-only params (the 6 BENIGN), which must NOT be removed. Added CA (wire
  serialization ≠ signature parity). Corrected the SILENT classification (`link_if_absent`
  forwards most params via `link()`; residual is `allow_dangling`, unused by callers
  today). Stale "94 / unenumerated" references reconciled to the audited figures.
