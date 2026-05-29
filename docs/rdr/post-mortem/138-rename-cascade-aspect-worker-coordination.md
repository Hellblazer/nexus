# Post-mortem: RDR-138 Rename-Cascade / Aspect-Worker Coordination

**RDR:** [RDR-138](../rdr-138-rename-cascade-aspect-worker-coordination.md)
**Closed:** 2026-05-29 (Implemented)
**Tracker:** nexus-u0u8a · **Epic:** nexus-9vkil

## What shipped

A coarse `threading.RLock` (`RENAME_LOCK`) on `T2Database`, injected into
`AspectExtractionQueue`, serializing `rename_collection_cascade` against all 7
queue mutators and `complete_aspect`. Approach A: the cascade keeps its single
dedicated connection for all stores (preserving K4 cross-store atomicity); every
queue writer acquires the same lock. Two layers:

- **Layer 1** (this RDR): the product-side coordination fix — T1.1 (primitive +
  cascade hold), T1.2 (7 mutators + `complete_aspect` whole-call), T2 (race
  regression suite), T3 (code review, PASS), T4 (test-validate + phase-review
  gate). CA2 throughput spike verified the coarse lock costs <1% amortized.
- **Layer 2** (shipped earlier, commit `4801675b`): the autouse
  `_reset_aspect_worker_singleton` fixture confining a spawned worker to its own
  test, which removed the test-suite manifestation.

## Lessons

### 1. The recurring "flake" was a real concurrency canary.

`test_collection_rename`'s aspect-cascade assertion failed intermittently on CI
(PR #997, #1006) with `aq_old=0 AND aq_new=0` — the queued row gone from both
collections. It was dismissed as flaky and "mitigated" with a 5s poll
(nexus-989e1). The poll was ineffective: a genuinely-deleted row never
reappears, so polling only confirms absence. A dedicated debugger run reproduced
the mechanism at 74–95% and proved it a real race: `rename_collection_cascade`
had no coordination with the `aspect_worker`. **Treat a recurring "flake" on a
cascade/concurrency path as a suspect, not noise — especially when the
mitigation is a poll/sleep that can only ever observe, not fix.**

### 2. The fix's actual guarantee diverged from the initial bead assertions.

The T2 bead's literal Scenario assertions did not match what the shipped T1.2
fix guarantees, and this was caught only by running empirical probes (150×
loops) *before* writing the suite:

- **Scenario 1** ("queued row never lost, `aq_new==1`, never `(0,0)`") is only
  deterministic for an *in-flight* (claimed, not-completed) row. A *completing*
  worker (`claim_next`+`mark_done`) racing the cascade legitimately yields
  `(0,0)` — that is completion, not loss. The lock neither stops nor should stop
  it. So Scenario 1 is a guardrail, not a fix-discriminating test.
- The original `(0,0)` total-loss came from a *leaked* worker `mark_done`-ing
  unsupported `code__` work it never extracted; `RENAME_LOCK` does not stop that
  (Layer 2's fixture did).

**Lesson:** verify what a fix actually guarantees against the running code
before encoding test assertions — a plausible spec can assert a guarantee the
implementation does not provide. A bead's DESIGN is a contract, but contracts
written before the fix can be wrong about the fix.

### 3. The Gap-3 stale-collection drift is accepted self-healing residue.

The lock prevents the cascade from interleaving *between* `complete_aspect`'s
`upsert` and `mark_done`. It does **not** close the
`cascade-fully-before-complete_aspect` ordering: `complete_aspect` writes
`record.collection` (the OLD name captured at claim time) after the cascade
already moved everything to NEW, leaving `document_aspects` under OLD and the
queue row orphaned `in_progress` under NEW. This drifts, then self-heals:
`reclaim_stale` re-pends the orphan → re-extraction under NEW. The RDR's own
Failure Modes paragraph documents this. The regression suite
(`TestGap3StaleCollectionResidue`) locks the residue in as a KNOWN exact state
with exact assertions rather than hiding it behind a weakened test. **When a fix
closes a window partially, encode the residue as a tested, named state — do not
weaken the assertion to make it look fully closed.**

### 4. A' was the seductive wrong answer (K4).

The initial research named A' (delegate the queue rename to the store's own
`rename_collection` on its own connection) as the leading candidate. The gate
caught that it splits the cascade across two SQLite connections, breaking the K4
/ nexus-nhyh cross-store single-transaction atomicity that RDR-129 relies on.
The narrowest *correct* change kept all stores on one cascade connection and
added a coarse lock around it.

## Follow-ups filed during the arc

- `nexus-k44w4` — guarded the legacy `AspectExtractionQueue.rename_collection`
  (no production caller, superseded by the inline cascade). **Closed.**
- `nexus-2evpz` — CA2 throughput spike. **Closed, verified** (<1% amortized).
- `nexus-a9oho` — robust assertion for the LLM-wording-flaky `nx_answer`
  plan-miss integration test. **Closed.**
- `nexus-alnpa` — `chromadb.EphemeralClient` shared-backend order-flake in
  `test_projection_quality` (cleared-on-entry fixture). **Closed.**

The last two were pre-existing issues surfaced by the T4 full + integration
suite runs, unrelated to RDR-138 but fixed during cleanup.

## Evidence

- T3 knowledge: `debug-aspect-queue-rename-cascade-worker-race-canary-2026-05-28`
- T2: `rdr138-t2-regression-suite-2026-05-29`, `rdr138-t3-code-review-2026-05-29`,
  `rdr138-t4-gate-2026-05-29`, `rdr-138-ca2-throughput-spike`
- Tests: `tests/test_rename_lock_t1_1.py`, `tests/test_rename_lock_t1_2.py`,
  `tests/test_rename_lock_t2_race.py`
