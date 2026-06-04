# Post-mortem: RDR-146 — Catalog Store Behind the Existing T2 Daemon

**Closed:** 2026-06-04 · **Epic:** `nexus-5p2ci` (GH #1046) · **Outcome:** implemented

## What shipped

Closed the GH #1046 `catalog.db` starvation (an interactive `nx dt index`
starved ~30 min by hook-spawned `nx index repo` on the shared SQLite catalog
writer). Three phases over `develop`:

- **Phase 1** (#1106) — items 1+2. The rich `Catalog` is hosted in the
  existing T2 daemon behind a write-only 22-op whitelist; all 49 consumer
  `Catalog(...)` write sites cut over to typed `make_catalog_reader` /
  `make_catalog_writer` factories; reads stay local. Boundary lint floor
  `CATALOG_CONSTRUCTION_BASELINE = 0`, enforced.
- **Phase 2** (#1107) — items 3+4 (PC-5-collapsed). Interactive-vs-batch
  fairness as producer back-pressure: an interactive write opens an in-memory
  deadline window on the daemon; the background indexer polls
  `catalog.is_interactive_write_pending` and yields over a bounded budget.
  `--on-locked` retargeted from the per-repo advisory lock to the
  writer-availability probe.
- **Phase 3** — item 5. `_LinkOps.graph()` Python BFS replaced with a single
  `WITH RECURSIVE` CTE (nexus-41otf, folded).

## What went right

- **The PC-4 pivot (RF-7) shrank the RDR mid-flight.** The load-bearing
  planning claim was "build a 4th catalog daemon." Ground-truth verification
  found the T2 daemon *already* serves the catalog store; the work was a
  client-cutover, not a daemon build. This evaporated the gate's headline risk
  (4th-daemon election) and turned a large architecture change into a bounded
  cutover. Lesson reaffirmed: verify the load-bearing "already serves / already
  bypasses" claim against `file:line` before sizing.
- **Stacked review earned its keep twice.** Phase 1: code-review-expert caught a
  lint-invisible direct writer in `catalog/store_hook.py` (allowlisted dir) and
  a mixed read+write helper the AST scan missed. Phase 2: substantive-critic
  caught two *absent gate tests* (the #1046-inverted batch-storm proof and the
  no-queue invariant) plus an MCP-path priority misclassification — none visible
  from a green 8947-test suite.

## What to watch

- **AST write-scans miss two classes** (recorded as a standing lesson): writes
  through a helper callee, and writers in lint-allowlisted dirs (`catalog/`).
  Cross-check helper callees and every `catalog/` consumer-hot-path file when
  verifying "all writers routed." Candidate follow-up: tighten the lint to catch
  write-method calls inside `catalog/` consumer files, or relocate `store_hook`.
- **The phase-review-gate parser only matches single-line `N. **…[SIZE].**`
  headings.** §Approach items 1-2 wrapped their `[MEDIUM]` marker to a second
  line and were silently skipped by the cross-walk — the very silent-drop the
  gate exists to prevent. Keep §Approach item headings single-line through the
  size marker.

## Tracked follow-ons (carved out at planning, NOT gates)

- `nexus-c341x` (P3) — pipeline.db fd-leak (connection-per-unit-of-work leaks
  20+ duplicate fds in `nx dt index`). §Approach item 6, explicitly SPLIT: an
  orthogonal resource-lifecycle concern, not lock contention.
- `nexus-3neyw` (P2) — Chroma T3 duplicate-on-rewrite (content-hash-keyed upsert
  on re-store). Sibling bug surfaced during RDR-146 work; separate fix.

## Carried unknown (resolved)

The denylisted `transaction()` / `bulk_load_documents()` batch paths did not
need a JSON-shaped coarse RPC op (RF-3: the indexer hot path is per-record).
The 22-op whitelist plus `make_catalog_admin` (daemon-quiesced deep-maintenance
escape hatch for `dedupe-owners` / `undelete`) covered every cutover site.
