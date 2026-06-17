# Post-mortem: RDR-154 — Deliberate Postgres-Capability Leverage

**Closed:** 2026-06-16 (accepted 2026-06-08). Epic `nexus-4ft44`. `close_reason: implemented`.

## What shipped

Four phases, each driven TDD → both reviewers (code-review-expert AND substantive-critic) → test-validator → full mvn suite → phase-review-gate. All four phase gates and the epic-level scope cross-walk PASSED.

- **P0 — `doc_count` trigger** (`nexus-i7ivk`, PR #1214): two statement-level `SECURITY INVOKER` triggers on `topic_assignments` make `topics.doc_count` authoritative-by-construction as the sole writer. Closed the cascade/purge-delete hole that application code structurally cannot see. Removed every competing app-side writer, dropped `doc_count` from the ETL `GREATEST` merge, and retired the `context.py` read-side dedup band-aid.
- **P1 — `security_invoker` views** (`nexus-h9qyp`, PR #1216): six read-shape views, each `WITH (security_invoker = true)` (caller RLS is the isolation boundary), replacing the hand-assembled Java/Python read paths and an N+1 (`coverage_by_content_type` via `count(*) FILTER`). A changelog-grep standing-rule guard enforces the invoker discipline going forward.
- **P2 — `updated_at` triggers** (`nexus-2zv75`, PR #1217): a shared `BEFORE UPDATE` trigger on exactly `document_aspects` and `topics`.
- **P3 — capability-selection doc** (`nexus-w3gix`, PR #1218): the declarative > view > trigger ladder, the "NOT worth it" trigger list, and the matview deferral recorded in `src/nexus/db/AGENTS.md`.

Post-epic follow-ons (same session): `nexus-eh89h` (batch per-topic assignment INSERTs so the P0 trigger fires once per topic instead of O(N²), PR #1219); `nexus-slcn7` (root-cause the duplicate root topics the P0 band-aid removal exposed — dedup at the labeler source + cleanup migration + unique index, PR #1220); `nexus-agsq7` (age-based `stale_source_ratio`, PR #1221).

## What was deliberately deferred

- `nexus-aeceu` — Python local-mode `Catalog.stats()` omits `chunk_count` (a pre-existing Java/PG parity gap). RDR-158-adjacent (it retires the SQLite backend). Not RDR-154 core scope; the P3 scope cross-walk PASSED.

## Lessons

- **The stacked reviewer pair earned its keep — twice — on findings green tests AND the code-reviewer missed.** P2's `stale_source_ratio` was *structurally vacuous*: `source_mtime` is captured at index time, so it is always `<= indexed_at`, making a "modified since indexing" ratio identically zero in production. The test only passed because its fixture seeded an impossible future mtime. The substantive-critic caught it; we reverted and later redid it age-based. In `slcn7`, the critic caught that the new unique index would crash `nx taxonomy rebuild` because `compute_rebuild_plan` was not deduped. Neither was visible from a green suite.
- **A self-imposed constraint can quietly make a feature pointless.** P2's "DB-only, no filesystem access" framing is exactly what made the source-staleness ratio vacuous. Naming the constraint as a *feature* ("no parse-throw") hid that it also removed the only way to compute the intended signal.
- **Forward-tag SQLite migrations at the next version.** `slcn7`'s cleanup migration is tagged at a version above the current package so it runs at the next release; the compute-side dedup prevents new duplicates regardless of when the migration lands.
- **Trigger backstops require protecting every insert path.** Adding a unique index is only safe once every code path that inserts the constrained shape is deduped — the index turns a latent dup into a hard crash on the un-protected path.
