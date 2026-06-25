---
title: "Service-Mode Catalog Interface Conformance: Make HttpCatalogClient a Faithful Drop-In for the Local Catalog, Enforced by a Signature-Conformance Test"
id: RDR-168
type: Bug Fix
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-25
accepted_date:
related_issues: [nexus-7y0ab]
related: [RDR-152, RDR-155, RDR-164, RDR-108, RDR-103]
---

# RDR-168: Service-Mode Catalog Interface Conformance

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

In service mode (the 6.0.0 default), the catalog is served by `HttpCatalogClient`
(94 public methods, talking to the Java `CatalogHandler` over HTTP). It is supposed
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

These are two confirmed instances of an unknown-sized class (94 methods). Some methods
match (`owner_for_repo`, `register`); the full divergent set is unenumerated.

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

To be completed during `/conexus:rdr-research`. The load-bearing task is the
**full signature audit**: enumerate every public method on `Catalog` (the caller-facing
interface) and compare against `HttpCatalogClient`, classifying each as
{match, name-divergence, missing-param, missing-method, extra-method}. Also trace the
service-mode registration + manifest-write path end-to-end to confirm whether Gap 2 is
fully explained by Gap 1 divergences or has additional causes (e.g. the manifest hook
not firing with a populated `catalog_doc_id`).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `Catalog` / `_DocumentOps` (local) | Partial | `collection_for(content_type, owner, embedding_model)`, `all_documents(content_type=...)` confirmed |
| `HttpCatalogClient` | Partial | `collection_for(*, content_type, owner_id, embedding_model)`, `all_documents()` (no content_type) confirmed |
| `CatalogHandler.java` | Partial | `/collections/for_tuple`, `/collections/rename`, manifest endpoints exist; map each to the client method |

### Key Discoveries

- **Verified** — two signature divergences reproduce live (`collection_for`,
  `all_documents`) and break the indexer + CLI in service mode.
- **Verified** — service-mode `nx index repo` leaves the catalog empty
  (`Documents:0`/`manifest_empty`).
- **Assumed** — the empty-catalog symptom is fully explained by signature divergences
  in the registration path; needs end-to-end tracing to confirm there is not a second
  cause (manifest hook / `catalog_doc_id` threading).

### Critical Assumptions

- [ ] **The caller-facing catalog interface can be expressed as a single Protocol/ABC**
  that both `Catalog` and `HttpCatalogClient` are intended to satisfy — **Status**:
  Unverified — **Method**: Source Search (enumerate the actual caller surface, not all 94 methods)
- [ ] **A signature-conformance test would have caught both confirmed divergences** and
  is cheap to maintain (introspection-based, not a hand-list) — **Status**: Unverified
  — **Method**: Spike (write the test against current code, confirm it fails on the 2 known gaps)
- [ ] **Reconciling the signatures (renaming params / adding params on the client) does
  not break the 173 existing HTTP integration tests** — **Status**: Unverified —
  **Method**: Source Search + run the suite
- [ ] **Once signatures match, service-mode `nx index repo` populates the catalog**
  (Documents/Chunks > 0 + manifest) with no second root cause — **Status**: Unverified
  — **Method**: Spike (the MVV)

## Proposed Solution

### Approach

1. **Audit** (research): enumerate the caller-facing catalog surface and produce the
   complete divergence table (the unknown size becomes known).
2. **Pin the contract**: define the catalog interface as a `typing.Protocol` (the
   caller-facing subset, not all 94 methods) that both backends satisfy. Prefer the
   **local** signatures as canonical (callers are written against them).
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

To be expanded in research after the audit. Interface intent: a `CatalogReader` /
`CatalogWriter` Protocol pair capturing the caller-facing methods; the conformance
test parametrized over the Protocol's methods using `inspect.signature`.

```text
// Illustrative — verify during implementation
// for name in protocol_methods(CatalogReader):
//     assert compatible(signature(getattr(HttpCatalogClient, name)),
//                        signature(getattr(Catalog, name)))
```

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

**Cons**: Leaves the unenumerated rest of the 94-method surface unverified; no guard
against recurrence; the next divergence ships silently.

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

To be completed at gate.

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
