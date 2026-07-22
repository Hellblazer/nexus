# RDR-180 Post-Mortem: Content-Address Chunks by a Canonical 32-Byte Binary chash

**Closed:** 2026-07-20 · **Accepted:** 2026-07-18 · **Drafted:** 2026-07-04
**Shipped in:** conexus 6.14.0 + engine-service-v0.1.49
**Epic:** `nexus-jxizy` (10/10 children closed)

## What shipped

`chash` is now the full 32-byte SHA-256 of chunk text, everywhere it serves: binary in
storage (`bytea`, `octet_length = 32`), 64 lowercase hex at every boundary, and nothing
in between. The pre-existing state — `sha256(text)[:32]`, half a digest stored as text,
while the citation grammar advertised the full 64 hex — is gone, along with the
collision class it implied.

Production data followed the schema on 2026-07-20: both cloud tenants rekeyed, 254,846
rows, zero lost, 248,791 permanent `chash_alias` entries, and independent verification
that `chash:<64-hex>` and `chash:<32-hex legacy>` resolve to the identical chunk.

## Approach cross-walk (all nine items, no silent reduction)

| Item | Deliverable | Bead | Outcome |
|------|-------------|------|---------|
| 1 | Canonical definition (full digest, binary storage, hex interchange) | `.1` | Shipped |
| 2 | `BYTEA` + `CHECK octet_length=32` on the five poison columns | `.2` | Shipped |
| 3 | `Chash` accept/reject INVERSION; two-tier contract collapses to one strict tier | `.7` | Shipped |
| 4 | Producer emits full digest; `[:32]` retired | `.3` | Shipped + tripwire |
| 5 | Handler bind-time boundary validation | `.8` | Shipped |
| 6 | Offline reindex/remap ETL + permanent `chash_alias` | `.6` | Shipped + **run in production** |
| 6a | Inventory addendum, `CHASH_BEARING_TABLES` alignment | `.5` | Shipped |
| 7 | Grammar stays 64-hex + resolver proof | `.9` | Shipped |
| 8 | Three-way null/orphaned disposition | `.4` | Shipped (production residue 0, as A-1 measured) |

**Scope addition, not reduction:** `.10` (land-then-transform guided migration) was added
mid-flight and closed with ten sub-beads. The RDR's Item6 said "reuse the RDR-185/186
machinery"; what shipped is more than reuse — the guided path became bulk-load-into-staging
then one transactional in-DB re-id/promote, which retired the per-leg in-flight rewrite
class the original vehicle still carried.

## Divergences from the written design (all deliberate, all recorded)

1. **VALIDATE is 3-of-5, not 5-of-5.** Technical Design says "VALIDATE the new checks
   post-backfill". In production, `chunks_384/768/1024` validated; `catalog_document_chunks`
   and `chash_index` were deliberately left `NOT VALID` because the store carries 292,656
   **pre-existing** orphan pointers (zero alias entries; created 2026-04-18..07-06 — content
   that stopped existing months before the cutover). The octet CHECK is table-grain, so
   VALIDATE cannot succeed while they exist; that is arithmetic, not judgement. New writes
   to both tables are enforced regardless (`NOT VALID` gates existing rows only). Cleanup
   and the final two VALIDATEs are tracked as `nexus-uu4ue`.
2. **`chash_remap.new_chash` stayed TEXT.** Item6a said it "widens to the new canonical
   width". It did not: changeset `rdr180-13` widened its CHECK to `length IN (32, 64)`
   instead. `chash_remap` is the migration-provenance facts table (`old_id` free-form by
   design), not a serving chash column, so converting it would have been width theatre.
3. **Legacy-debt TEXT columns stayed TEXT** (`topic_assignments.doc_id`,
   `frecency`/`relevance_log.chunk_id`). Consistent with the RDR's inventory intent (they
   are *remap* targets, repointed via cascade — not *conversion* targets), but worth naming:
   `topic_assignments.doc_id` is a genuinely mixed identity space (chunk chashes AND memory
   note titles), so a bytea conversion would have made 16-byte values ambiguous. Retiring
   these to FK-anchored identity is a filed RDR candidate.

## What the process caught, and what only production caught

The gates earned their keep. The `--guided` container gate alone caught **four** product
bugs the 14k-test hermetic suite structurally could not see — including one where every
migrated chunk was invisible to `chash:` citations because the promote path never stamped
`chunk_text_hash`. Budget three to five gate iterations, not one.

But three defect classes were **only** reachable at production scale, and all three arrived
in the last 48 hours:

- **BUG-0148** (conexus-xpg7): the `ALTER TYPE` rewrite silently reset planner statistics;
  the stale-stats planner flipped sparse-gate hybrid queries onto a budget-bounded plan that
  returned **zero rows** while every health probe, `/version` handshake, and the aggregate
  cloud gate stayed green. Root-caused by the other instance in one query after three local
  reproductions came back healthy. Fix: changeset `rdr180-16` ANALYZEs the rewritten tables
  in the same changelog pass.
- **F2, the second-tenant planner catastrophe**: autoanalyze fired the moment tenant 1
  committed, freezing `chash_alias` statistics at "100% tenant 1". Tenant 2's alias rows are
  inserted inside its own transaction and therefore invisible to the planner, which estimated
  one row and chose a triple nested loop against ~134k × 466k actual. 101 minutes, aborted
  clean. `enable_nestloop=off` proved the same work takes 461 seconds. Fix (`nexus-339xv`):
  in-transaction `ANALYZE` of `chash_alias` — an in-txn ANALYZE sees its own uncommitted rows.
  Single-tenant stores never hit this, which is exactly why the rehearsal could not.
- **F1, the 504-that-committed**: the engine's TLS sidecar shares the engine's network
  namespace and imposes ~120s `proxy_read_timeout`, so a production-scale rekey returns a
  gateway timeout *while its transaction runs on and commits*. The operator sees failure over
  a store that silently changed — the GH #1390 hazard class arriving through a proxy instead
  of through ordering. Filed `nexus-b878d`.

**The unifying lesson**: three of these are one class — *the planner is blind to rows that
were just written*. Rewrites reset stats; in-transaction inserts are invisible; autoanalyze
freezes the wrong distribution between tenants. Any migration that mutates a large table
owes its planner an ANALYZE at the right moment, and "the right moment" is sometimes inside
the transaction.

## Process observations worth keeping

- **Coverage must enumerate wire endpoints × era states, not client journeys.**
  `/v1/vectors/hybrid-search` had zero nexus-side callers — conexus is its only consumer — so
  no nexus journey could ever have driven hybrid-in-window, and BUG-0148's surface was
  structurally unreachable from our test suite. Ask "who calls every engine endpoint, and did
  any journey drive it in every era state" at gate-design time.
- **A failed reproduction is data.** Three healthy local reproductions constrained out chash
  width, tsv health, and scan starvation, and moved the prime suspect to planner statistics —
  which is what the other instance then confirmed in a single query. Report reproduction
  *failures* promptly; they narrow the search.
- **Integration-marked tests are invisible to `uv run pytest`.** The strict cohort boundary
  met a season of stale 32-char fixtures all at once at release time (392 passed → 486 passed
  across five gate runs). After a producer-identity change, grep the integration modules
  explicitly.
- **Verify the benign explanation before accepting a reported premise.** A critic's "the
  stamps go stale after rekey" was wrong — producers had always stamped the full 64-hex — and
  the fix's rationale changed even though the fix survived.
- **An amnesty is a ceiling, not an exemption.** The first implementation of "leave those two
  CHECKs NOT VALID" became "these two tables do not gate" — categorical and permanent, under
  which a future rekey introducing 50,000 brand-new orphans would still report PASS. Caught by
  Hal on review. The correct shape pins the *measured* debt as a ceiling and fails on any
  increase. This is the silent-scope-reduction shape wearing a different hat, and it would have
  propagated into the client rung for every fleet install (`nexus-noa8d`).
- **Transport outcome is not operation outcome.** Twice in one night, in two different systems:
  an nginx 504 over a committed transaction, and `curl` exiting 0 on an engine 500 so a
  cancelled, rolled-back rekey was recorded as complete. Both were caught only because the
  *store* disagreed with the report. Gate on the operation's own envelope, never the pipe.

## Residuals at close (all tracked, none blocking)

| Bead | What |
|------|------|
| `nexus-339xv` (P1) | F2: in-transaction ANALYZE of `chash_alias` — next engine tag |
| `nexus-noa8d` (P1) | Rung VALIDATE policy as a measured ceiling, for lived-in local stores |
| `nexus-b878d` | F1: rekey endpoint vs the 120s proxy ceiling (async, or in-namespace-only) |
| `nexus-kmd5b` | F3: `ChashCensus` dangling check is width-blind to its own target (1 vs 292,230) |
| `nexus-uu4ue` | 292,656 pre-existing orphan pointers: attribute → cleanup → VALIDATE the last two |
| `nexus-84tr4` (P3) | `resolve_chash` full-width-only vs `resolve_chash_globally` alias-chaining |
| `nexus-leunq` (P3) | `legacy_ids` suppresses measured-dim override |
| — | FK-anchored chash identity / retire the legacy-debt TEXT columns (RDR candidate) |

Cross-instance (conexus): `conexus-b3rs` (oracle re-baseline — map through `chash_alias`,
do not re-capture), `conexus-3ilh` (managed store lacks the `nexus_diag` diagnostic path).

## Verification of record

- Engine suite 1353/0/0; client suite 14112/0; local-service gate 486/0.
- `--guided` land-then-transform gate PASSED (75/75 checklist items with exact-number teeth).
- `--cold` cosign-verified acquire PASSED; `--chash-window` M1 PASSED against the published pair.
- `--package-upgrade` convergence MVV PASSED (a real 6.13.1 + v0.1.47 box converges itself).
- Production: both tenants rekeyed, conformance 0/0/0 on all three chunk tables, three octet
  CHECKs VALIDATED, `dangling_alias = 0`, both citation widths verified live by an independent
  consumer.
