---
title: "Unify the Dim-Sharded Chunk Tables into One nexus.chunks with Nullable Typed Embedding Columns: Make the Manifest FK Expressible and Retire the Client-Side Integrity Apparatus"
id: RDR-191
type: Architecture
status: accepted
accepted_date: 2026-08-10
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-08-10
related_issues: []
related: [RDR-108, RDR-152, RDR-154, RDR-155, RDR-156, RDR-158, RDR-180, RDR-186, RDR-187]
supersedes_decision: "RDR-156 Decision 2 (Do NOT FK the manifest to the chunk tables)"
---

# RDR-191: Unify the Dim-Sharded Chunk Tables — Make the Manifest FK Expressible

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

`nexus.chunks_384` / `chunks_768` / `chunks_1024` are three tables holding
one logical entity. They are split for exactly one reason: the `embedding`
column is `vector(384)` / `vector(768)` / `vector(1024)`, three distinct
PostgreSQL types. Nothing else about a chunk varies by dimension — same PK
`(tenant_id, collection, chash)`, same `chunk_text`, same generated
`chunk_tsv`, same metadata, same RLS.

That split is not free. Because a foreign key targets exactly one table,
`catalog_document_chunks.chash` cannot reference the chunk it names. RDR-156
recorded this consequence explicitly (Decision 2: "Do NOT FK the manifest to
the chunk tables") and accepted the cost: every referential question between
a document's manifest and the chunks it claims becomes application code.

**This RDR is the revisit RDR-156 itself scheduled.** Its Alternatives entry
for the trigger-maintained parent table reads: *"Recorded as NOT worth it
today; revisit if orphan incidents recur post-RDR-153."* They have recurred,
repeatedly, and the accumulated cost is now measurable.

### Gap 1: The integrity apparatus that exists only because the FK cannot

With no FK, every integrity property is maintained by code that must be
written, tested, reviewed, and kept correct forever:

- `nexus.manifest_orphans(dim)` — manifest rows whose chash has no chunk row.
- `nexus.manifest_verify`/`manifest_verify_all` (catalog-020) — the same
  question narrowed to a document, or grouped by collection.
- `_check_dangling_manifests` and (2026-08-10) `_check_manifest_null_collection`
  in `health.py`, plus their engine routes, client methods, and tests.
- `manifest_backfill()` and its documented call-protocol trap ("run this
  FIRST or the orphan check reads a false-clean zero").
- `_prune_deleted_files`' entire completeness apparatus: the 422-not-truncate
  contract, the cap-boundary test, the alive-set count reconciliation, the
  per-collection isolation, the copy-then-delete quarantine discipline.

Every one of these exists to detect or survive a state the database could
refuse outright. Verified on pgvector 0.8.2 (this repo's bundle), an FK with
`ON DELETE RESTRICT` rejects both directions at the source:

```
insert a DANGLING manifest row   -> ERROR: violates foreign key constraint
delete a REFERENCED chunk        -> ERROR: still referenced from ...
```

### Gap 2: The recurring false-clean class is structural, not incidental

The dangling-manifest class has produced defects on a cadence:

- RDR-187 measured **292,230** drifted rows in `chash_index`, the router
  remnant of the same split-store architecture — "the single largest
  data-integrity debt in the system".
- 2026-08-10: `nx doctor`'s orphan check was reading **false-clean** — both
  `manifest_orphans` and `manifest_verify_all` filter `collection IS NOT NULL`
  first, so a collection whose manifest rows are entirely NULL-collection
  never appears in the output at all. Neither engine function's docs are
  wrong; the client simply never satisfied the documented precondition, and
  `manifest_backfill()` had **zero production callers**.
- The same day: `manifest_backfill()` turns out to be insufficient anyway —
  its predicate requires `physical_collection IS NOT NULL AND != ''`, so
  ghost/sourceless documents' manifest rows stay NULL **permanently** and sit
  outside every orphan and verify check forever (catalog-014 states this
  verbatim).

Each fix is correct and each leaves the class alive. RDR-156's own Finding
records the pattern from the other end: the production cutover validation
"ran against empty catalog tables, making the manifest-orphan check vacuous —
evidence that integrity checks living outside the schema degrade silently."

### Gap 3: The split forces full-corpus reads for set operations

`_prune_deleted_files` answers "which chunks does no manifest row reference"
by pulling **both** operands across the wire and differencing them in Python:
the alive set from the catalog, and every chunk's metadata from the store.
Measured on this repo's corpus (2026-08-10, cloud):

| | |
|---|---|
| `get_all_metadata` scaling | ~0.33s fixed + ~0.15 ms/row, linear in **collection** size |
| code collection today | 28,932 rows → 3.8s |
| hard ceiling | `GET_ALL_METADATA_MAX_ROWS` = 200,000, then 422 → paginated fallback |

The read is independent of how much changed: a one-file commit pays the same
as a thousand-file one. And the subsequent quarantine "move" — which only
changes the `collection` column, since quarantine is a sibling collection in
the same table — is implemented as `get(include=[... "embeddings"])` +
`get_embeddings()` + `_upsert_full()` + `delete()`: roughly 1,220 × (a
1024-float vector + document text) round-tripped **to change one string
column**.

These are both single SQL statements against co-resident tables. They are
Python loops because the client was written against ChromaDB, and RDR-155
moved the substrate without moving the shape (see T2
`nexus/chroma-residue-plan-2026-08-10`).

## Constraints and Verified Facts

Everything below was executed against this repo's bundled PostgreSQL +
pgvector **0.8.2**, not reasoned from documentation.

**V1 — Partitioning is impossible.** All three routes fail:

```
untyped `vector` parent   -> CREATE INDEX ... hnsw -> ERROR: column does not have dimensions
untyped on the partition  -> CREATE INDEX ... hnsw -> ERROR: column does not have dimensions
typed vector(768) child   -> ATTACH PARTITION      -> ERROR: child table has different type
```

RDR-156's rejection of the partitioned-table alternative was correct, and
this RDR does **not** revisit it.

**V2 — Three nullable typed columns in one table works, fully.**

```sql
CREATE TABLE nexus.chunks (
  tenant_id text NOT NULL, collection text NOT NULL, chash bytea NOT NULL,
  chunk_text text NOT NULL,
  embedding_384  vector(384),
  embedding_768  vector(768),
  embedding_1024 vector(1024),
  CONSTRAINT chunks_pk PRIMARY KEY (tenant_id, collection, chash),
  CONSTRAINT exactly_one_embedding
    CHECK (num_nonnulls(embedding_384, embedding_768, embedding_1024) = 1)
);
CREATE INDEX ON nexus.chunks USING hnsw (embedding_384 vector_cosine_ops);
--        ... and embedding_768, embedding_1024. FULL, NOT partial — see F13.
```

**REVISED BY F13 (measured): use THREE FULL HNSW INDEXES, not partial ones.**
The draft specified `WHERE embedding_N IS NOT NULL` partial indexes. Measured
on identical data, that predicate buys NOTHING and costs correctness-by-luck:

- **Size 241.31 MB (full) vs 241.04 MB (partial) — 0.11%.** HNSW does not
  index NULLs in the first place, so the partial predicate excludes nothing.
- **Build 3.83s (full) vs 4.33s (partial)** — full is not slower. No write
  penalty.
- **Planner: the FULL index was used in EVERY case**, including with no
  predicate and no filter. The PARTIAL 768 index **seq-scanned** when the
  query omitted the predicate — 63.4 ms vs 0.25 ms, ~250×, silently. The
  PARTIAL 1024 index was not used **even WITH** the predicate at default
  costs (`enable_seqscan=off` proved it usable; the planner simply costed it
  out — full's startup cost is ~39% cheaper).

Because which dim is adopted is deployment-dependent (cloud 1024 / local
768), a query missing the predicate would pass in one deployment and regress
250× in the other. **This deletes F7 risk 1 outright** and removes Open
Question 8.

Verified: table creates; all three HNSW indexes create (both shapes were
exercised; FULL is the adopted one per F13); the FK from a manifest-shaped
table on `(tenant_id, collection, chash)` creates; a dangling manifest insert
is rejected; deleting a referenced chunk is rejected; a two-embedding row is
rejected by the CHECK.

**V3 — This defeats both of RDR-156's stated objections.** It is not a
partition (so V1's type constraint does not apply) and it uses no trigger and
no separate parent table (so RDR-154's "triggers only for app-unfixable
invariants" discipline does not apply). RDR-156 evaluated exactly two shapes;
this third one was never considered.

**V4 — The write-ordering objection is substantially weakened.** RDR-156
objected that a manifest FK "imposes chunk-before-manifest write ordering on
the hot indexing path". The combined write now writes the manifest rows and
the chunk vectors in **one transaction** under advisory sweep gates
(`CatalogRepository`, the `resolvedChunks` path). Ordering inside a single
transaction is an implementation detail, not a coupling across calls.
`DEFERRABLE INITIALLY DEFERRED` remains available if ordering proves awkward.

**V5 — The migration choreography has precedent in this repo.**
`fk-002-collection-registry.xml` → `fk-002-0-backfill-stubs` →
`fk-002-validate.xml` is exactly the `ADD CONSTRAINT NOT VALID` → backfill →
`VALIDATE CONSTRAINT` sequence this needs, already shipped for the collection
registry FKs.

**C1 — RESOLVED, NOT a blocker (was: "the NULL-collection manifest population
is a hard migration blocker").** The draft asserted that a manifest row with
`collection IS NULL` could not satisfy the FK and therefore had to be
resolved or deleted before `VALIDATE`. That was wrong. Under PostgreSQL's
default `MATCH SIMPLE`, a row with a NULL in ANY column of the foreign key is
exempt from enforcement entirely. Verified end to end:

```
seed a NULL-collection manifest row, THEN:
  ALTER TABLE ... ADD CONSTRAINT ... NOT VALID   -> ALTER TABLE
  ALTER TABLE ... VALIDATE CONSTRAINT            -> ALTER TABLE   (succeeds!)
  insert a DANGLING non-NULL manifest row        -> REJECTED
  delete a REFERENCED chunk                      -> REJECTED
  insert ANOTHER NULL-collection row             -> accepted
```

So the permanently-ghost population is legal, unenforced, and imposes no
pre-flight requirement on the migration. `VALIDATE` runs with those rows in
place.

The honest consequence, which must not be lost: those rows remain
**unenforced**. The FK gives them no guarantee — it neither fixes nor worsens
them. They are the same population that is already invisible to
`manifest_orphans` and `manifest_verify_all` (both filter `collection IS NOT
NULL`). So the 2026-08-10 `manifest_null_collection_report` census stays the
ONLY visibility into that set and must NOT be retired with the rest of the
apparatus in Decision item 4 — a correction to that item's scope.

**C2 — Storage cost of NULL columns is a null-bitmap bit**, not a row-width
penalty. Total index size is unchanged from three tables: HNSW does not index
NULLs, so each per-dim index covers only that dim's populated rows whether it
is declared partial or full (F13 measured the two at 0.11% apart). This is why
FULL costs nothing and is the adopted shape.

## Decision

*(To be locked at gate. Draft position below.)*

1. **Create `nexus.chunks`** with the V2 shape: one table, three nullable
   typed embedding columns, `CHECK num_nonnulls(...) = 1`, **three FULL HNSW
   indexes — NOT partial (F13, measured)**, one FTS index, FORCE RLS matching
   the current tables. The draft said "partial"; F13 measured that the
   `WHERE embedding_N IS NOT NULL` predicate buys nothing (HNSW does not index
   NULLs, so the two indexes are 0.11% apart in size) and costs a silent ~250×
   seq-scan whenever a query omits the predicate. Full indexes throughout.
2. **ADOPT THE LARGEST DIM TABLE IN PLACE — do not copy all three.**
   (Revised by F9; the draft said "migrate dim by dim, then drop them", which
   is ~6× more expensive for no benefit.) Rename the largest `chunks_<dim>`
   into `nexus.chunks`, rename its `embedding` column to `embedding_<dim>`,
   `ADD COLUMN` the other two (metadata-only), and move only the smaller two
   dims' rows. **The pre-existing HNSW index survives both renames** and keeps
   being used, so the dominant cost of the copy strategy is never paid.
   Which table is largest is DEPLOYMENT-DEPENDENT (cloud = voyage-1024,
   local = bge-768), so the migration must choose dynamically and then
   canonicalise the inherited constraint/index names.

   **Canonicalise those names as hygiene, and append this RDR's changelog
   AFTER `fk-002-validate.xml` (F11a, corrected).** `fk-002-7/8/9` guard on
   CONSTRAINT NAME (`conname`) while their statements reference the TABLE
   NAME, so an inherited `chunks_<dim>_collection_fk` on the renamed table
   would satisfy the precondition and then fail on the vanished table — BUT
   Liquibase's strict declared order (zero `<includeAll>`; `fk-002-validate`
   at master line 100, new changelogs appended near line 392) means those
   changesets always run first, so this is an ordering REQUIREMENT on the new
   changelog's placement, NOT an inherent boot-brick. Contrast Decision item
   2's inherited-CHECK item above, which IS a genuine boot-brick (F14b).

   **MANDATORY, and the item that decides whether an un-converged box boots
   at all (F14b):** the adopted table carries an inherited `NOT VALID` octet
   CHECK, and `NOT VALID` exempts PRE-EXISTING rows only — so the
   `INSERT…SELECT` of the two non-adopted dims VIOLATES it on any un-rekeyed
   store, failing Liquibase at engine boot. The migration MUST **drop the
   check, move the rows, then re-add it `NOT VALID` under a canonical name.**
   Measured to work. Without this the release BRICKS un-rekeyed installs
   before the ladder rung is ever reached.
3. **Add the FK** `catalog_document_chunks (tenant_id, collection, chash) →
   nexus.chunks` — **`ON UPDATE CASCADE`** (F10: mandatory, not optional) and
   deferrable on the delete side (F8a). NOT `ON DELETE RESTRICT`. Note F10's
   finding that RESTRICT and NO ACTION are behaviourally IDENTICAL — the real
   axis is deferrability, not the action keyword. Choose between
   `INITIALLY DEFERRED` (simple) and `INITIALLY IMMEDIATE` + explicit
   `SET CONSTRAINTS ALL DEFERRED` at the two class-B sites (better error
   locality); both are correct. Via V5's NOT VALID → VALIDATE sequence — which
   F9 measured at 0.8 ms + 333 ms over 280,695 manifest rows. No C1
   disposition required (F6, re-confirmed at corpus scale by F9).

   **GATED ON TWO THINGS, NOT ONE (F15 — the second was missing and is
   measured):** (a) fixing F8c's two dangling-manifest PRODUCERS, and (b)
   REMEDIATING THE PRE-EXISTING dangling-manifest POPULATION before
   `VALIDATE` runs. (b) was absent from the draft and is not optional: this
   repo's live corpus already carries **37 manifest rows / 36 distinct chashes
   / 4 documents across 4 collections** whose chunks do not exist, all with
   NON-NULL `collection` — so the C1/F6 `MATCH SIMPLE` exemption does NOT
   cover them and `VALIDATE CONSTRAINT` fails on them. F9's "VALIDATE
   succeeded" evidence came from a corpus size-matched to production but NOT
   defect-matched, so it never exercised this case.
4. **Retire** what the constraint makes unreachable: `manifest_orphans`,
   `manifest_verify`/`manifest_verify_all`, `manifest_backfill` and its call
   protocol, `_check_dangling_manifests`, and the completeness apparatus
   around `_prune_deleted_files`.

   **EXPLICITLY NOT RETIRED: `_check_manifest_null_collection` and its
   `manifest_null_collection_report` route.** The FK does NOT cover the
   NULL-collection population — under `MATCH SIMPLE` those rows are exempt
   (F6) — so that census remains the ONLY visibility into a population that
   is permanently unenforced, and it is the instrument Decision item 6 needs
   to price the `NOT NULL` promotion (F12d: it has never run against a real
   corpus). Retiring it would delete the measurement while the thing it
   measures still exists. *(Gate Critical 1: the C1 correction was written in
   prose and never applied here.)*
5. **HARDEN THE UPGRADE LADDER IN THE SAME RELEASE — this is what decides
   whether an existing install survives (F14).** Three parts, all required:
   (a) existence-gate `chash_rekey.py`'s VALIDATE statements (`:128-136`) —
   zero-cost, and today an absent relation raises forever;
   (b) retarget `OCTET_CHECKS` (`:56-69`) and `_validated_probe` (`:663-691`)
   to `nexus.chunks` in the SAME engine release, forced by the
   `PRECONDITION_ENGINE → RUNG_CHASH_REKEY` edge — a two-release straddle
   strands every local box, not just un-converged ones;
   (c) carry the Decision-2 CHECK drop/re-add, without which Liquibase fails
   at boot before the rung is ever reached.
   REJECTED alternatives, with reasons: a self-retiring rung (silently
   abandons un-rekeyed data), and DDL outside Liquibase (every existing
   exemption is local-mode-only and pays for it with a hand-run cloud
   operator step). Liquibase `preConditions` work only with
   `onFail="CONTINUE"` (`MARK_RAN` is permanent, `HALT` bricks) and buy an
   unbounded dual-schema window — held in reserve, not adopted.

6. **Do NOT adopt `MATCH FULL`; promote `collection` to `NOT NULL` later
   instead** (Open Question 6). Order: fix the NULL-collection producers
   (F12b `StagingPromoteOps`, F12c upsert demotion), ship and RUN the C2
   census for the population size, then promote the column —
   `vectors-001-baseline.xml`'s own comment already anticipated this.

7. **Push prune into SQL** — an anti-join `UPDATE` that moves unreferenced
   chunks to the quarantine sibling collection in one statement, with expiry
   and restore as their own statements. Zero rows and zero embeddings cross
   the wire; the call returns counts. (Independently valuable and **not**
   gated on 1–4; see Phasing.)

## Alternatives Considered

- **Single partitioned `chunks` table (dim as partition key).** Rejected by
  RDR-156 and independently re-verified here (V1). Would cost HNSW indexing
  entirely.
- **`chunks_registry` trigger-maintained parent table.** RDR-156's recorded
  "NOT worth it": a trigger on the hottest write path plus write-ordering
  coupling. The V2 shape achieves the same FK with neither.
- **Status quo + better client-side checks.** This is what has been tried
  repeatedly. Gap 2 is the evidence: each fix is correct and the class
  survives. The 2026-08-10 doctor fix is the newest instance and is already
  known to be insufficient for the ghost subset.
- **Do only item 7 (prune into SQL), keep three tables.** Legitimate and much
  cheaper — it captures the measured performance win without a schema
  migration. It leaves the FK inexpressible and the integrity apparatus
  standing. Recommended as Phase 1 regardless, precisely so the perf win is
  not held hostage to the migration.

## Consequences

**Positive.** Referential integrity becomes authoritative-by-construction in
both directions. A whole defect class — the one that produced RDR-187's
292,230 drifted rows and this session's false-clean doctor — becomes
impossible rather than detected. A substantial body of detection code,
stored functions, engine routes, client methods, doctor checks, and their
tests becomes deletable. Prune stops reading the corpus and stops moving
embeddings over the wire, which also removes the 200,000-row cliff.

**Negative / risk.** A data migration over a live corpus (267,195 chunks
across 95 collections at time of writing) in both cloud and local
deployments, with an upgrade path for existing installs. `ON DELETE RESTRICT`
converts today's silent chunk deletion into a hard failure at any site that
deletes a still-referenced chunk — that is the point, but every such site
must be found first. The `exactly_one_embedding` CHECK forecloses a future
in which one chunk carries vectors for two models simultaneously; if that is
ever wanted, this is the wrong shape.

**The four risk items from the F7 census, in priority order** (item 1 has
since been WITHDRAWN by F13; numbering retained so cross-references hold):

1. ~~**SILENT PERFORMANCE REGRESSION — the partial-index predicate.**~~
   **WITHDRAWN — ELIMINATED BY DESIGN, not mitigated (F13, measured).** The
   risk existed only because the draft specified PARTIAL HNSW indexes: a
   `search()`/`hybridSearch()` query omitting `WHERE embedding_<dim> IS NOT
   NULL` would silently seq-scan (~250× measured). Decision item 1 now
   specifies FULL indexes, which the planner used in EVERY measured case
   including with no predicate and no filter, so no query shape can miss the
   index. Item number retained rather than renumbered so the existing "F7 risk
   1" references (V2 block, F9's caveat, F13) stay valid.
   **What survives:** nothing blocking. An EXPLAIN-asserting plan test remains
   worthwhile as cheap insurance against a future reintroduction of a
   predicate-dependent index, but it is no longer gating.
2. **`chash_rekey.py` `OCTET_CHECKS`** — its convergence counting goes from
   4 checks to 2. That is a design change to the upgrade ladder's rung, not
   a rename.
3. **`chash_tables.py`** (`CHASH_BEARING_TABLES` / `POISON_CHASH_TABLES`) is
   read in LOCKSTEP by doctor, the install-binary gate, and `forensics`.
   All three move together or the taxonomy lies.
4. **The 9 combined-query stored functions**
   (`search_metadata_scoped` / `graph_hop` / `topic_scoped` × 3 dims) —
   **the function COUNT does not collapse.** Each takes a typed
   `vector(N)` parameter, so nine remain; only each function's FROM clause
   and predicate change. A correction to any reading of Decision 4 that
   expects these to disappear.

## Open Questions (to resolve before gate)

1. ~~**C1 disposition**~~ — **CLOSED by F6.** No disposition required; see
   the C1 entry above. The ghost rows stay legal and unenforced, and their
   census must survive the Decision-4 subtraction.
2. ~~**Does MATCH SIMPLE let NULL-collection rows coexist?**~~ — **CLOSED,
   YES (F6).** Verified: `VALIDATE CONSTRAINT` succeeds with them present,
   and the FK still enforces every non-NULL row.
3. Cutover shape: dual-write both stores through one release, or a single
   offline migration inside the existing upgrade ladder (RDR-185)? **UNDER
   RESEARCH.**
4. ~~**Does any read path depend on the physical table name?**~~ — **CLOSED
   by F7.** Nothing fundamentally requires three physical tables. ~90 files
   touch the names; ~12 carry real logic coupling. `DimTables.java` already
   abstracts ~25 Java call sites behind a typed `ChunkTable` record and needs
   zero changes if repointed. Four risk items carried into Consequences.
5. ~~**Is `ON DELETE RESTRICT` right?**~~ — **ANSWERED BY F8: NO.** The
   Decision's `ON DELETE RESTRICT` is WRONG and must become **`NO ACTION
   DEFERRABLE INITIALLY DEFERRED`**. RESTRICT is checked immediately and
   cannot be deferred; multiple legitimate transactions delete chunks BEFORE
   removing the manifest rows within the same transaction, and RESTRICT would
   fail every one of them. DEFERRED checks at COMMIT, by which point the
   manifest rows are gone — while still catching a genuine dangling reference.
   (Pending the deep-analyst's independent synthesis; the census facts below
   are firm.)
7. **CONFIRMED AND WORSE THAN STATED (F14). STILL GATE-BLOCKING** until the
   inherited `NOT VALID` CHECK has an explicit disposition IN THE DECISION —
   that single item decides whether an un-converged box boots at all. The
   original framing below is preserved; read F14 for the two corrections
   (it strands EVERY local box, not only un-converged ones, and Liquibase
   fails FIRST so the engine never starts).
   `src/nexus/upgrade_ladder/rungs/chash_rekey.py`'s `OCTET_CHECKS` names
   `nexus.chunks_384` / `_768` / `_1024` explicitly and VALIDATEs constraints
   on them. That file's own comment records what happened last time a table
   was dropped from under it: the rung *"would VALIDATE against a missing
   relation on EVERY `nx upgrade` forever, `_pointer_debt` would silently
   degrade to unknowable, and `_validated_probe` could never count to five
   again — permanently un-converged."*

   RDR-191 drops those three tables. The conflict is structural: the unify
   DDL naturally belongs in Liquibase, which runs UNCONDITIONALLY at engine
   boot BEFORE any ladder rung walks — but the rekey rung requires those
   tables to exist, so any box that has not yet converged the rekey rung is
   STRANDED. Either the rekey machinery is retargeted to `nexus.chunks` in
   the SAME engine release, or the unify DDL is gated outside Liquibase,
   which collides with the all-DDL-through-Liquibase directive. This is a
   design decision, not derivable from the code.

8. ~~Raised by F9: the adopted dim table inherits a FULL HNSW index while the
   two added dims get PARTIAL ones.~~ — **CLOSED by F13.** The reconciliation
   is to make all three FULL, which is what Decision item 1 now specifies. The
   inherited index on the adopted dim is already full, so adopt-in-place
   produces a uniform index shape with no divergence to reconcile, and F7 risk
   1 (the predicate-dependent seq-scan) is withdrawn rather than mitigated.

6. **`MATCH FULL` vs `MATCH SIMPLE` — CANNOT BE PRICED YET (F12). DEFER, with
   a named unblock.** It is not a marginal choice: exactly ONE of the three
   FK columns (`collection`) is nullable, so every key is all-non-NULL or
   exactly-one-NULL, and **`MATCH FULL` therefore differs from `MATCH SIMPLE`
   on 100% of the NULL-collection population — it rejects every row.** It
   would re-instate the C1 blocker that F6 closed, and break TWO currently
   legal writers (F12b).

   **ANSWER (F14/F12): NO — and `MATCH FULL` is the wrong instrument
   entirely.** MEASURED: under `MATCH FULL`, `VALIDATE` fails AND a new
   NULL-collection INSERT is REJECTED **while the constraint is still
   `NOT VALID`**. There is no grace window, so V5's NOT VALID → VALIDATE
   choreography does not apply at all — the draft assumed it would.
   `StagingPromoteOps` writes those rows today (F12b), so it would start
   failing the moment the constraint is ADDED, not when it is validated.

   **The right instrument is to promote `collection` to NOT NULL** — which
   `vectors-001-baseline.xml`'s own comment already anticipated and deferred.
   Order: fix the producers (F12b, F12c), ship and RUN the C2 census to get
   the population size (F12d — it has never run against a real corpus), then
   promote the column. Use `MATCH SIMPLE` throughout.

## Phasing (draft)

- **Phase 1 — Prune into SQL.** No schema change. Anti-join UPDATE, expiry,
  restore. Deletes the wire copy and the full-corpus read. Independently
  shippable and independently valuable.

  **STANDING DIRECTIVE (Hal, 2026-08-10): key on the NATURAL ID, never on a
  metadata copy of it.** Today `_prune_deleted_files` tests membership using
  the `chunk_text_hash` METADATA field rather than the row's `chash`,
  justified by a pre-RDR-180 window in which live indexer writes still
  produced synthetic ids. RDR-180 is CLOSED: `chash` IS `sha256(chunk_text)`,
  32 bytes, and part of the PK `(tenant_id, collection, chash)`. A metadata
  field holding the same fact is a second source of truth, a drift site, and
  in SQL it forces a JSON read where an indexed PK column exists. The new SQL
  joins `chunks.chash` to `catalog_document_chunks.chash`. If any live row is
  found where the two diverge, that is a DATA defect to be fixed by a rekey —
  not a reason to carry the indirection forward. The legacy metadata branch
  and its `unsafe_skipped` accounting become dead code, to be deleted in
  their own reviewed diff.

  This directive generalizes past Phase 1: the unified table plus the FK make
  `chash` the load-bearing identity everywhere, so any surface still
  consulting a metadata copy is debt on the same axis.
- **Phase 2 — Ladder hardening + producer fixes. ENTRY GATE for everything
  after it.** Decision item 5 (existence-gate the rung's VALIDATE, retarget
  `OCTET_CHECKS`/`_validated_probe`) and the F8c/F12b/F12c NULL-collection
  and dangling-manifest producers: `_prune_misclassified_in_collection`'s two
  arms (`indexer.py:2635`, `:2681`), `StagingPromoteOps`' omitted
  `collection` column, and the upsert demotion arms
  (`CatalogRepository.java:3858`, `:3869`).
  **EXIT CRITERION: zero live producers of dangling-manifest or
  NULL-collection rows remain.** These are stated as preconditions in
  Decision item 3's trailing clause; they are restated HERE as a phase with
  an exit criterion because a precondition living only inside another item's
  prose is exactly what gets dropped when this is broken into beads (gate
  Significant 1, and this project's documented scope-reduction history).

- **Phase 3 — MEASURE THE CLOUD. Hard gate on the cloud DDL.** F9 could not
  establish the managed deployment's corpus size or its
  `maintenance_work_mem`, and the freeze there is GLOBAL across all tenants
  rather than per-tenant. **No cloud DDL runs until both numbers exist and a
  freeze-window estimate is derived from them.** Local installs are not
  gated on this. (Gate Significant 2.)

- **Phase 4 — `nexus.chunks` + migration** (adopt-in-place, inherited CHECK
  drop/re-add, constraint-name canonicalisation).
- **Phase 5 — REMEDIATE THE EXISTING POPULATION, then FK `NOT VALID` →
  `VALIDATE`.** Requires Phase 2's exit criterion met (zero live PRODUCERS)
  AND, separately, that the pre-existing dangling rows are gone.

  **THE GATING INSTRUMENT IS `nx catalog manifest-verify --list`, NOT
  `nx doctor` (F16).** The draft of this phase named the doctor check
  `_check_dangling_manifests`, which CANNOT gate anything: by RDR-129 B4
  design it never sets `fatal=True` on any path, and it returns `ok=True`
  both when the engine is unreachable (`health.py:3945-3947`, "skipped
  (catalog or engine unavailable)") and when zero collections had a readable
  manifest to compare. So a census that never ran, a census that failed, and
  a genuinely clean corpus are indistinguishable in `nx doctor`'s exit code.
  Gating on it would have reproduced the exact Gap-2 false-clean shape this
  RDR cites as its own motivating evidence — inside the step meant to prevent
  it. `nx catalog manifest-verify --list` fails loud in BOTH directions
  instead: `click.exceptions.Exit(1)` on damage (`commands/catalog.py:569`)
  and `ClickException` on a read failure (`:480`, `:499`).

  **Ordered steps, all required (F15, F16):**
  1. Run `nx catalog manifest-verify --list` and record the verdict AND the
     number of collections actually compared. It is NOT clean today: 37 rows
     / 36 chashes / 4 documents, measured 2026-08-10 on this repo's live
     corpus. Doctor's check may still be run for its human-readable detail,
     but it is NOT the gate.
  2. Remediate to zero. **THE INSTRUMENT IS NOT YET ESTABLISHED — establishing
     it is part of this phase, not a given (F17).** The two tools the doctor
     check suggests are both unproven against this specific damage class:
     - `nx catalog reconcile` **cannot fix it.** Its gap detection
       (`manifest_heal.py:130-134`) selects documents where
       `len(manifests) < chunk_count` — a row-COUNT deficit. This class
       preserves the count exactly (the manifest rows are all present; the
       chunks they name are missing), so `gapped` comes back empty and
       reconcile silently no-ops on precisely the rows it was assigned.
     - `nx index <path> --force` is plausible but not proven: the affected
       document reports a `chunk_count` and returns nothing, and re-indexing
       may silently no-op (nexus-5xn3k).

     So step 2 must first DEMONSTRATE a remediation that moves the count,
     on one affected document, before running it across the population. Do
     not treat either command's exit code as evidence — step 3's re-verify
     is the only evidence that counts.
  3. Re-run the verify and require a clean exit before `ADD CONSTRAINT`.

  **EXIT CRITERION, and it must be non-vacuous: `nx catalog manifest-verify
  --list` exits 0 on the target deployment immediately before `VALIDATE`, AND
  the number of collections it compared is greater than zero and equals the
  deployment's known collection count.** A zero-collections-compared run
  exits 0 while proving nothing — verified, not assumed: the clean path
  returns 0 whenever no collection reports `missing > 0`, including when the
  census compared none (F16b). That is why the count is part of the criterion
  and not a footnote. **`--json` does not expose the compared count today
  (F16b), and THIS PHASE ADDS IT**: emit `collections_checked` in
  `_manifest_verify_list`'s clean AND damaged payloads, then gate on that
  field. Picking the remedy here rather than leaving F16b's two options open,
  because an exit criterion that cannot be mechanically evaluated is not an
  exit criterion — text-scraping the human sentence is the alternative and it
  is worse. Re-run after `VALIDATE`. Stated as a phase
  step rather than a precondition inside Decision item 3's prose, because a
  precondition living only in another item's prose is precisely the class
  this RDR has already lost once (gate run 1, Significant 1).

  **This is a per-deployment gate, not a one-time fix.** The count above is
  this box's. Cloud carries its own, unmeasured (F12d's blocking gap applies
  to this census too: it must be RUN there, not inferred from here).
- **Phase 6 — Subtraction.** Delete the apparatus Decision item 4 lists —
  and ONLY that list. `_check_manifest_null_collection` /
  `manifest_null_collection_report` are explicitly EXCLUDED (item 4). This
  phase is the point of the RDR; if it is deferred, the debt is not repaid.
- **Phase 7 — `collection` → `NOT NULL`** (Decision item 6), once Phase 2's
  producers are fixed and the C2 census has RETURNED A NUMBER.

  "The census has run" is not a criterion — `_check_manifest_null_collection`
  is a doctor check with the same fail-open shape F16 documents, so a run that
  failed to reach the engine still counts as having run. Require a reported
  count (zero or otherwise) from a census that compared a non-zero number of
  collections. Unlike Phase 5, nothing downstream here can silently proceed on
  a bad number: the `SET NOT NULL` itself is the hard gate, and PostgreSQL
  fails the ALTER if any NULL remains. The census sizes the remediation; PG
  enforces it.

## Research Findings

- **F1 (VERIFIED, live pgvector 0.8.2):** partitioning routes all fail; the
  three-nullable-column shape works end to end including FK enforcement in
  both directions and the CHECK. Probe transcript in session scratch.
- **F2 (VERIFIED, measured):** `get_all_metadata` is linear at ~0.15 ms/row
  with a ~0.33s floor; 28,932-row collection reads in 3.8s; 200,000-row cap
  raises 422 rather than truncating.
- **F3 (VERIFIED, code):** quarantine's "move" round-trips embeddings and
  document text to change one column.
- **F4 (VERIFIED, code):** the combined write is one transaction.
- **F5 (VERIFIED, changelog):** `fk-002` establishes the NOT VALID →
  backfill-stubs → VALIDATE precedent.

- **F17 (VERIFIED, code, 2026-08-10) — the obvious remediation tool cannot
  fix the F15 population, and the reason generalises to any count-preserving
  damage.**

  `nx catalog reconcile`'s gap detection (`src/nexus/catalog/manifest_heal.py:130-134`)
  selects heal candidates as `len(manifests.get(tumbler, [])) < e.chunk_count`
  — a row-COUNT deficit — plus a ghost arm for `chunk_count == 0`. F15's
  damage is count-preserving by construction: every manifest row is present
  and correct in number; what is missing is the CHUNK each row names. The
  deficit is therefore zero, `gapped` is empty, and reconcile returns having
  done nothing, reporting success.

  This is the same shape as F8c's root cause (chunks deleted with no manifest
  action), which is why the producer and the would-be repair tool are blind in
  the same direction: both reason about manifest ROWS, neither joins to the
  chunk table. Any future repair for this class must be an anti-join against
  the chunk store, not a count comparison — the same operation Phase 1 just
  pushed into SQL for the mirror-image population (chunks no manifest
  references).

  **Consequence for Phase 5:** the remediation instrument is an OPEN item to
  be established in that phase, not a known quantity to be invoked. Recorded
  there explicitly so it cannot be read as "run reconcile and move on".

- **F15 (MEASURED on the LIVE corpus, 2026-08-10) — the pre-existing
  dangling-manifest population is NOT zero, so `VALIDATE` as drafted fails on
  first production run. GATE-BLOCKING, now dispositioned in Decision item 3
  and Phase 5.**

  Ran `nexus.health._check_dangling_manifests()` against this repo's real
  corpus (cloud, `api.conexus-nexus.com`, tenant `default`):

  ```
  4 collection(s) have manifest rows referencing chunks that do not exist:
    code__1-6__voyage-code-3__v1        1 of 31124
    code__1-9__voyage-code-3__v1       34 of  2526
    docs__1-6__voyage-context-3__v1     1 of   405
    knowledge__knowledge__...__v1       1 of  1095
  (37 manifest rows, 36 distinct chashes, 4 documents)
  Damaged documents: 1.11.391, 1.6.2114, 1.6.3, 1.9.164
  ```

  **Every one of these has a NON-NULL `collection`** — they are named
  collections — so the C1/F6 `MATCH SIMPLE` exemption does not reach them.
  `VALIDATE CONSTRAINT` scans existing rows and these are exactly the rows it
  rejects.

  **Why the RDR missed it.** F9's cutover measurements ran on a synthetic
  corpus size-matched to production (267,195 chunks / 280,695 manifest rows)
  but NOT defect-matched: it was internally consistent by construction, so
  `VALIDATE` trivially succeeded there. Size-matching a corpus proves the
  TIMING of a migration; only defect-matching proves it COMPLETES. This is the
  same shape as RDR-156's own recorded failure — its cutover validation "ran
  against empty catalog tables, making the manifest-orphan check vacuous."
  Cited in Gap 2 of this very document, and repeated anyway.

  **Consequence beyond this RDR:** these 37 rows are live damage right now,
  independent of the migration. Each affected document reports a `chunk_count`
  and returns nothing on read.

  **MEASUREMENT CAVEAT (see F16):** the number above came from calling
  `nexus.health._check_dangling_manifests()` directly and reading its
  `detail` string, which is sound for MEASURING. It is NOT sound for GATING —
  that function is warn-only and fails open. Phase 5 gates on
  `nx catalog manifest-verify --list` instead.

- **F16 (VERIFIED, code, 2026-08-10) — the census that measures this
  population cannot gate on it, and picking the wrong instrument would have
  rebuilt the very false-clean this RDR exists to kill.**

  `_check_dangling_manifests` (`src/nexus/health.py:3837`) is a `nx doctor`
  health check. Per RDR-129 B4 it never sets `fatal=True` on ANY return path,
  including its real positive-detection branch, so `nx doctor`'s exit code
  (`format_health_for_cli`, `commands/doctor.py:1663-1664`) cannot separate
  "37 dangling rows found" from "clean". Worse, two branches return
  `ok=True` outright: engine/catalog unavailable (`:3945-3947`) and zero
  collections compared. The second is even labelled NON-VACUITY in a comment
  and still renders as `ok` — the detail string says "skipped", but a detail
  string is not a gate.

  **The fail-loud instrument already exists in-tree and was not being used:**
  `nx catalog manifest-verify --list` (`commands/catalog.py:452-569`) raises
  `click.exceptions.Exit(1)` when damage is found and `ClickException` when a
  read fails, so both "damaged" and "could not tell" are non-zero exits. It
  also reports `incomplete_collections` explicitly rather than silently
  under-reporting a dim whose orphan population exceeded the enumeration
  ceiling.

  **F16b — the replacement is better but NOT vacuity-proof either, and its
  JSON mode cannot express the non-vacuity check.** Verified by reading
  `_manifest_verify_list` (`commands/catalog.py:476-569`):

  - read failure → `ClickException` (non-zero). Good.
  - damage found → **always** `Exit(1)`; `incomplete_collections` (row-cap or
    enumeration failure, counts a LOWER BOUND) and `unroutable_collections`
    are emitted as warnings INSIDE that already-failing path, so partial
    enumeration can never downgrade a damaged verdict to success. Good.
  - **clean path → exit 0, including when the census compared ZERO
    collections.** `rows = census.get("collections") or []` then
    `if not damaged: ... return` — an empty census prints
    `OK — no dangling manifest rows (0 collection(s) checked)` and exits 0.

  So the zero-compared vacuity hole survives into the better instrument. That
  is why Phase 5's exit criterion carries the collections-compared clause
  rather than just "exits 0".

  **The trap for whoever implements that clause:** the compared count exists
  ONLY in the human-readable string. The `--json` clean payload is
  `{"collections": [], "total_rows": 0, "unroutable_collections": [],
  "incomplete_collections": {}, "clean": true, ...}` — `collections` there is
  the DAMAGED list, not the checked count, and no field carries N. A gate
  script that parses `--json` therefore CANNOT implement the non-vacuity
  check and will pass vacuously on an empty census. Either add a
  `collections_checked` field to the JSON payload as part of this phase, or
  gate on the text output. Do not assume the JSON is the machine-readable
  superset of the text; here it is a strict subset in the one field that
  matters.

  **Generalises past this RDR:** any phase exit criterion in this document
  that names a doctor check as its gate is making the same mistake. Doctor
  checks are for humans reading output; gates need an instrument that exits
  non-zero on both damage AND on failure-to-determine — and "exits non-zero
  on damage" is not sufficient on its own when "found nothing to look at"
  also exits zero.
- **F14 (VERIFIED + MEASURED, 2026-08-10) — Q7 confirmed, and the RDR was
  wrong in two directions. GATE-BLOCKING.**

  **F14a — it strands EVERY local box, not just un-converged ones.**
  `chash_rekey.py`'s `_validated_probe` (`:663-691`) requires all four
  hardcoded `OCTET_CHECKS` names (`:56-69`) to be convalidated. Three of them
  cease to exist at the drop, so NO local box can ever report converged.
  Every box enters `converge()` and raises at the first VALIDATE —
  `validate_statements` (`:128-136`) has no existence gate and `run_admin_sql`
  raises. Net: `nx upgrade` exits non-zero FOREVER
  (`commands/upgrade.py:288`), and `backfill_install_mode_record()` plus
  three post-upgrade advisories never run.

  **F14b — NEW, and worse: Liquibase fails FIRST; the engine does not boot.**
  MEASURED on live PG 17.5 / pgvector 0.8.2: a `NOT VALID` CHECK exempts
  PRE-EXISTING rows ONLY. Adopt-in-place must `INSERT…SELECT` the two
  non-adopted dims INTO the adopted table, which still carries the inherited
  `NOT VALID` octet check (`rdr180-001-bytea-chash.xml:149-153`). On an
  un-rekeyed store those rows are 16 bytes → `ERROR: new row violates check
  constraint` → `MigrationException` → `Main` exits 1. **That is a BRICK,
  reached BEFORE the ladder rung ever runs.**
  Fix is one line and was also measured to work: DROP the check, move the
  rows, RE-ADD it `NOT VALID` under a canonical name.

  **F14c** — `SchemaMigrator.preflightChashConstraints` (`:198-264`) goes
  silently VACUOUS for the chunk tables post-drop. Existence-gated, so it
  stops scanning rather than crashing — no crash, no coverage.

  **NOT blockers (checked):** `fk-002-7/8/9` already carry
  `preConditions onFail=MARK_RAN` on `pg_constraint`; fresh installs replay
  history in order; managed cloud is `applicable=False`.

  **RECOMMENDATION:** retarget the rung in the SAME release — forced by the
  `PRECONDITION_ENGINE → RUNG_CHASH_REKEY` edge — PLUS the drop/re-add of the
  inherited CHECK, preceded by zero-cost existence-gating of the rung's
  VALIDATE. REJECT the self-retiring rung (it silently abandons un-rekeyed
  data) and REJECT DDL-outside-Liquibase (every existing exemption is
  local-mode-only and pays for it with a hand-run cloud operator step).
  Liquibase `preConditions` are feasible only with `onFail="CONTINUE"`
  (`MARK_RAN` is permanent, `HALT` bricks) and buy an unbounded dual-schema
  window — hold in reserve.

- **F13 (MEASURED, 2026-08-10) — use FULL HNSW indexes, not partial.**
  Inverts the draft's V2 index shape and DELETES F7 risk 1. Full detail at
  the V2 block above: identical size (0.11% apart, because HNSW does not
  index NULLs), full builds no slower, and the planner used FULL in every
  case while PARTIAL seq-scanned silently at ~250× without the predicate and
  was costed out even WITH it. NOT ESTABLISHED: the mechanism behind
  pgvector's higher cost estimate for a partial vs identical full HNSW index
  — reproducible, cause not isolated.

- **F12 (CENSUS, 2026-08-10) — pricing `MATCH FULL`, and two live defects in
  the manifest write paths.**

  **F12a — the axis is total, not marginal.** `catalog-001-baseline.xml:171-182`:
  `tenant_id` and `chash` are NOT NULL (both PK-adjacent; `chash` retyped
  TEXT→bytea at `rdr180-001:125-135` preserving NOT NULL), and `collection`
  is NULLABLE and was never promoted — `vectors-001-baseline.xml:221-236`
  added it and its header `:214-220` records the RDR-155 "no FK,
  application-enforced only" decision plus a NOT NULL promotion that never
  landed. So every FK key is all-non-NULL or exactly-one-NULL, and
  `MATCH FULL` rejects 100% of the NULL-collection population.

  **F12b — it would break two writers that are legal today.**
  (i) The ghost/sourceless-doc manifest write is a supported PUBLIC shape —
  `mcp/catalog.py:249,252` documents `physical_collection: str = ""` as
  "Ghost elements: physical_collection can be empty", and
  `CatalogRepository.java:3509-3511`/`:3752-3756` states the intent.
  (ii) **`StagingPromoteOps.java:684-700`'s promote `INSERT...SELECT` NEVER
  populates `collection`** — its column list simply omits it, with no
  follow-up stamp and no comment acknowledging the gap. Every promoted row is
  a partial-NULL key. That is NULL by OMISSION, not by policy, and is a
  probable latent defect in its own right, independent of this RDR.

  **F12c — upsert can DEMOTE an already-stamped row back to NULL.** The
  conflict arms at `CatalogRepository.java:3858` (append) and `:3869`
  (import) set `COLLECTION` from the excluded row, so a row that HAS a
  collection can lose it on a later upsert. Another independent producer of
  the population, and another reason its size cannot be inferred statically.

  **F12d — THE BLOCKING GAP: no production number exists anywhere.**
  `health.py:4168` states the route "has never shipped on ANY tag" and
  `:4176` gates on `REQUIRED_ENGINE_VERSION <= (0,1,69)` — so the census has
  never run against a real corpus. Only synthetic fixtures
  (`test_health_service_checks.py:2092`, total=3/backfillable=2) and
  relative-delta assertions exist. T2 `chroma-residue-plan-2026-08-10` §C2
  states the problem and records no counts. The nearest corpus-scale figure
  is F9's 280,695 manifest rows TOTAL, with no NULL subtotal.
  **Getting the number requires shipping the C2 census in an engine tag and
  running it against a live tenant.** This is the sequencing insight: the
  instrument to price Q6 ships in the same cut as the route it measures.

  **F12e — `manifest_backfill` can never fix THREE classes, not two**
  (`catalog-004-manifest-functions.xml:171-195`): ghost/sourceless docs
  (`physical_collection` NULL or `''`), rows under TOMBSTONED docs, and rows
  with NO matching `catalog_documents` row at all — the dangling-manifest
  class, where the join itself fails. The RDR previously recorded only the
  first.

- **F11 (CENSUS, 2026-08-10) — the rekey machinery is a SECOND, disjoint
  abstraction, and there is a checksum-frozen blocker. TEMPERS F7.**

  **F11a — RECONCILED WITH F14 (gate Critical 2). Both prior findings were
  partly right; neither identified the actual hazard.** I verified the
  changesets myself.

  F11a originally called `fk-002-validate.xml`'s name-based VALIDATEs a hard
  blocker; F14's "NOT blockers" list dismissed them via their preconditions.
  The preconditions are real — `fk-002-7/8/9` each carry
  `<preConditions onFail="MARK_RAN"><sqlCheck expectedResult="1">SELECT
  COUNT(*) FROM pg_constraint WHERE conname = '<name>'`. So on a box where
  the constraint genuinely does not exist, the changeset marks-ran and skips.
  F14 is right that a plain drop is survivable.

  **A structural asymmetry exists, but it is NOT a boot-brick — corrected
  after the re-gate caught me overstating it.** The guard keys on the
  CONSTRAINT NAME (`conname`) while the statement references the TABLE NAME.
  Adopt-in-place renames the table while the constraint name is inherited, so
  in isolation the precondition would pass and the statement would then fail
  on a vanished table.

  **Liquibase's ordering prevents that from ever happening.** VERIFIED:
  `db.changelog-master.xml` has ZERO `<includeAll>` and 68 strictly-ordered
  `<include>` entries; `fk-002-validate.xml` sits at line 100 while the
  newest changelog (Phase 1's `catalog-023`) sits at line 392 — this
  project's convention is to append at the tail. So `fk-002-7/8/9` always
  execute (or MARK_RAN-skip) BEFORE any RDR-191 rename changeset, on fresh
  installs and long-neglected upgrades alike; on already-migrated boxes they
  are in `DATABASECHANGELOG` and never re-run at all. My earlier claim that
  this was "the same boot-brick class as F14b" was WRONG — F14b is
  independently real, this is not.

  **What survives as a REAL requirement, narrower than the original claim:**
  the RDR-191 changelog MUST be appended AFTER `fk-002-validate.xml` in the
  master. Inserting it earlier would materialise the hazard — an
  implementation-ordering mistake, not an inherent property of adopt-in-place.

  **CONSEQUENCE — canonicalising the inherited constraint names is sound
  HYGIENE, not a boot-brick mitigation.** It remains worth doing (it stops
  `chunks_<dim>_*` names surviving on a table no longer called that), and it
  makes the ordering requirement above moot rather than load-bearing.

  **F11b — `RekeyOps` does NOT go through `DimTables`.** The rekey and the
  serving/write machinery are two DISJOINT abstractions over the same three
  tables. `RekeyOps.java` hardcodes the three-table set FIVE separate times
  in one file (`:98-104` `DIMS`, `:332-335` sweep-gate UNION ALL, `:364-366`
  + `:445-453` aliased siblings, `:761-786` `unionAllContentRowsDsl`), plus
  a raw-SQL string channel. Its javadoc (`:82-88`) records the divergence as
  deliberate: byte[]-typed chash rather than `DimTables.ChunkTable`'s
  hex-string accessor, because every join targets `chash_alias` byte columns.

  **This TEMPERS F7.** F7's "`DimTables` abstracts ~25 call sites and needs
  zero changes" holds for the SERVING path only. There are at least FOUR
  independent hardcodings of the dim set: `DimTables`, `RekeyOps` (×5
  internal), `StagingPromoteOps` (`:197-213`), and the raw-SQL channel — and
  `StagingPromoteOps`' `ChunkDim` record ALSO carries `Field<Vector>
  embedding` (`:198`), so the single-embedding-column assumption is baked in
  there too, not only in `ChunkTable`.

  **F11c — the repo already has the authoritative checklist for exactly this
  change.** `RawSqlGateTest.chunkTablesCanary_fourthDimNeedsAllSitesToldChecklistAbove`
  (`service/src/test/java/dev/nexus/service/db/RawSqlGateTest.java`) is the
  canonical "every site that must change when the dim set changes" list,
  referenced from `RekeyOps:79-80`, `ChashSqlIdioms:397-399`, and
  `StagingPromoteOps:194`. **Any RDR-191 scoping must start there.** It was
  NOT opened by the census.

  **F11d — the octet-check VALIDATE is Python's job, and its names are
  pinned.** Zero `octet_check` VALIDATE hits in `service/src/main`;
  `RekeyOps:64-66` states this explicitly, and nothing in Liquibase ever
  VALIDATEs them (deliberate — the GH #1390 crash-loop shape). The owner is
  `chash_rekey.py:56-69` / `:128-136`, and `:54-55` says the constraint names
  MUST mirror the `rdr180-001` XML and are PINNED BY TEST against it.
  Renaming them under RDR-191 breaks that pin — related to F11a, same class.

  **F11e — good news:** `ChashCensus` (`:161-178`) is SCHEMA-DERIVED via
  `information_schema` enumeration and would AUTO-DISCOVER a unified
  `nexus.chunks`; only its CI assertion set (`KNOWN_INVENTORY :113-116`)
  is hardcoded. And because `DimTables.of()` already resolves fields BY
  STRING COLUMN NAME (`:39-51`), the smallest serving-path change is
  `ChunkTable.of(table, dim)` selecting `embedding_<dim>`.

  **NOT ESTABLISHED:** `PgVectorRepository` (2900+ lines) was not read beyond
  locating its 21 `DimTables` call sites. Whether anything there assumes a
  single embedding column OUTSIDE the `ChunkTable` record is unknown.

- **F10 (VERIFIED, live PG 17.5 + pgvector 0.8.2, 2026-08-10) — the FK needs
  `ON UPDATE CASCADE`, and one live data-loss bug is already running.**

  **F10a — `ON UPDATE CASCADE` is MANDATORY.** The Decision specified only an
  `ON DELETE` action. But `chunks` is the PARENT, so collection rename
  (`UPDATE chunks SET collection = ...`) and the RDR-180 rekey
  (`UPDATE chunks SET chash = ...`) are PARENT-KEY UPDATES. Both are REJECTED
  while manifest rows reference them — under RESTRICT, under NO ACTION, and
  they survive `INITIALLY DEFERRED` only incidentally, because those
  transactions happen to update both sides. With `ON UPDATE CASCADE` the
  manifest auto-propagates; verified for both rename and rekey.
  `fk-002-5` already uses `ON UPDATE CASCADE` on `topic_assignments` for
  exactly this reason — house pattern, not a novel choice.

  **F10b — RESTRICT and NO ACTION are behaviourally IDENTICAL** on every
  deletion case; `SET CONSTRAINTS` reports both as "not deferrable". The
  axis was always DEFERRABILITY, not the action keyword. `purge_trash`'s
  exact shape was replicated and fails under both, succeeding only when
  deferrable. `DEFERRABLE INITIALLY IMMEDIATE` + an explicit
  `SET CONSTRAINTS ALL DEFERRED` at the two class-B sites gives the same
  outcome with statement-local error locality preserved elsewhere —
  including on the combined-write hot path, where `INITIALLY DEFERRED` would
  move a dangling-INSERT failure from the offending statement to COMMIT.
  `SET CONSTRAINTS ALL DEFERRED` was verified to work inside a plpgsql
  function body, so the `purge_trash` fix is one line either way.

  **F10c — A LIVE SILENT DATA-LOSS BUG, TODAY, independent of this RDR.**
  Identical chunk text in a collection collapses to ONE row shared by many
  manifest rows (RDR-108, by design). So deleting document A's chunks
  currently SUCCEEDS and destroys a chunk that document B still references.
  No error, no signal. The FK converts this into a refusal; the correct fix
  is anti-join-scoped deletion (verified working). **This is a present-tense
  defect, not a migration risk.**

  **F10d — RLS redacts FK error detail.** Under `FORCE ROW LEVEL SECURITY`
  with a non-owner role the FK still enforces correctly, but PostgreSQL
  redacts the message to `Key is still referenced from table "manifest"` with
  NO key values. Production failures under the `svc` role will not name the
  blocking chash — an operability cost to plan for, not a correctness one.

- **F9 (MEASURED, live PG 17.5 + pgvector 0.8.2 on a synthetic 267,195-row
  corpus matching this repo's, 2026-08-10) — the cutover.** Answers Open
  Question 3.

  **The row copy is NOT the dominant cost — the HNSW rebuild is.** Moving all
  267k rows *including vectors* within one PostgreSQL is **27.4 s**. The
  destination index builds are ~73% of the copy strategy's ~103 s total.

  **`maintenance_work_mem` is the largest source of variance and is
  untunable from this repo.** At PG's 64 MB default vs 512 MB: 768-dim build
  8.0× slower, 1024-dim 3.4× slower. An untuned local install pays ~5.3 min
  for the copy strategy. This is invisible in advance.

  **Hence Decision item 2's revision (adopt-in-place): ~17.6 s vs ~103 s**,
  and ~78 s vs ~5.3 min at default `maintenance_work_mem`. Verified: the
  pre-existing HNSW index SURVIVES table rename + column rename, its
  definition auto-re-reading `hnsw (embedding_1024 vector_cosine_ops)`, and
  the planner still uses it. `ADD COLUMN ... vector(N)` with no default is
  metadata-only (0.34 ms on 180k rows). Post-migration counts exact.
  **CAVEAT RESOLVED by F13: the inherited index is FULL** — and since
  Decision item 1 now specifies FULL for all three dims, the adopted dim's
  inherited index already has the target shape. Nothing to reconcile; this
  caveat and F7 risk 1 are both withdrawn.

  **Rollback is free: PostgreSQL does it.** Table rename + ADD COLUMN +
  CREATE INDEX + bulk DELETE in ONE transaction, failed mid-way, restored
  everything — table name, column, index, all 267,195 rows. No compensating
  migration needed. Cost is `ACCESS EXCLUSIVE` for the duration (~18-80 s
  local). Keep the source tables one release (RDR-176 copy-not-move) for
  post-COMMIT reversibility.

  **No VACUUM needed — correcting my own hint.** `DROP TABLE` on the 792 MB
  source took 8.8 ms and `n_dead_tup` was 0 after the rename strategy.
  `purge_trash`'s post-commit VACUUM exists because it DELETEs rows in place;
  a migration that DROPs its sources does not inherit that precedent.

  **No dual-write window, and do NOT reach for RDR-176/178.** That machinery
  (batched ETL, 502-retry routing, async job semantics) exists to survive a
  NETWORK between substrates. There is no network. The right machinery is
  RDR-185's ladder plus Liquibase.

  **Cloud needs a different shape.** One database serves all tenants, so the
  freeze is global and scales with the TOTAL cloud corpus, not any one
  tenant's. `CREATE INDEX CONCURRENTLY` is cheap (+11%) and non-blocking but
  cannot run in a transaction, forfeiting the atomic rollback above — so CIC
  for cloud, in-transaction for local. `chash_rekey.py` already implements
  exactly this local/managed split and should be copied, not redesigned.

  **NOT ESTABLISHED:** the managed cloud's actual corpus size and its
  `maintenance_work_mem` — both drive the cloud freeze window and neither is
  derivable from this repo. Measurements were on `fsync=off`; index builds
  are CPU/memory-bound and should transfer, row-copy figures are
  WAL-sensitive and will be slower with `fsync=on`.

- **F8 (CENSUS, 2026-08-10): 24 live chunk-delete sites + 1 dead.** 9 Java,
  3 Liquibase (`purge_trash`), 12 Python. Decides Open Question 5 and sizes
  the FK's real blast radius.

  **F8a — `ON DELETE RESTRICT` is wrong.** THREE different orderings coexist
  for the same invariant: `deleteCollectionTxn` (J4) and `purge_trash`
  (L1-L3) delete chunks BEFORE the manifest **within one transaction** (the
  manifest goes via `catalog_documents` DELETE → fk-001 CASCADE, and
  `catalog-003-soft-delete.xml:193-197` documents that ordering as
  deliberate); `RekeyOps` step-3 orphan drop (J8) deletes manifest first;
  and the `sweepChunks*` family (J1-J3) splits the two across transaction
  BOUNDARIES, leaving a committed window in which the chunk exists with no
  manifest row. RESTRICT is immediate and undeferrable, so it would fail J4
  and L1-L3 outright. `NO ACTION DEFERRABLE INITIALLY DEFERRED` accommodates
  chunk-before-manifest ordering inside a transaction and still fails a real
  dangling reference at COMMIT.

  **F8b — one method is the choke point.** `PgVectorRepository.delete`
  (J5, `:2132-2140`) has NO manifest leg at all — the javadoc assigns the
  obligation to the caller, citing T2 `nexus_rdr/155-manifest-fk-decision` —
  and it is the funnel for **NINE** distinct Python calling functions (both
  superseded sweeps, all three quarantine paths, the misclassified prune,
  `nx t3 gc`, `nx store delete`, and the TTL expiry). Of those nine, **SIX
  perform no catalog or manifest action whatsoever**; two rely on the
  manifest having been rewritten by a prior separate call; one only
  tombstones the parent document and leaves the manifest rows.

  Every dangling-manifest exposure in the system traces through this single
  Java method, which cannot detect or refuse it — the failure surfaces later
  and elsewhere, as `fetchDocumentChunks`' `IllegalStateException`. That
  makes it the single best place to enforce the invariant, and the single
  best explanation for why the class recurs.

  **F8c — two sites are active dangling-manifest PRODUCERS, and the state
  they create is invisible to every existing sweep.**
  `_prune_misclassified_in_collection`'s two arms (P7 `indexer.py:2635`,
  P8 `:2681`) delete chunks with ZERO manifest action, so a document
  reclassified `code__` → `docs__` has its old-collection chunks deleted
  while its manifest rows — carrying the old denormalized `collection` —
  survive.

  The divergence is supposed to close when the re-index rewrites that
  document's manifest in the same run, but **nothing enforces the ordering
  between prune and write**, and a document that is pruned but NOT re-indexed
  (reclassified and now excluded, or the run aborts in between) stays diverged
  INDEFINITELY.

  Worse, that state is invisible to all three existing sweeps: `purge_trash`
  keys on chunks that still exist (Steps 1-3 gate on `EXISTS(manifest row)`
  against a live chunk); `nx t3 gc` computes orphans in the OTHER direction
  (chunks no manifest references, not manifest rows referencing absent
  chunks); and it is precisely the state `nx doctor` reports as
  dangling-manifest. So the system produces this defect on one path and
  cannot clean it on any.

  These two sites must be fixed BEFORE the FK is validated, or they will
  begin failing at commit — which is the constraint doing its job, but on a
  hot indexing path.

  **F8d — a scoping asymmetry in the collection cascade.** J4 scopes chunks
  by `chunks_*.collection` but the manifest by
  `catalog_documents.physical_collection`. A manifest row whose parent
  document is homed elsewhere, but whose own `collection` column names this
  collection, is not reached by the cascade.

  **F8e — `fk-002`'s `chunks_*_collection_fk` are RESTRICT, not CASCADE**, and
  `chunks_*` has no FK to `catalog_documents`: no existing FK ever deletes a
  chunk row. The new constraint is additive, not a change to cascade shape.

- **F7 (CENSUS, 2026-08-10; full text in T3 `knowledge`, title
  `census-rdr191-chunks-dim-tables-dependency-blast-radius-2026-08-10`):**
  CLOSES Open Question 4. Nothing fundamentally requires three physical
  tables — RLS shape, vacuum behaviour and index size are identical or
  unaffected. Blast radius: ~90 files touch the names (literal or
  f-string-constructed), ~12 carry real non-doc/non-test logic coupling.
  **`DimTables.java` already abstracts ~25 Java call sites behind a typed
  `ChunkTable` record and needs zero changes if repointed**, and jOOQ codegen
  satisfies as-is (its `forcedType` matches any `vector`-typed column, not by
  name) — cheaper than the draft assumed FOR THE SERVING PATH ONLY.
  **TEMPERED BY F11: `RekeyOps` is a second, disjoint abstraction that does
  NOT use `DimTables` and hardcodes the dim set five times in one file;
  `StagingPromoteOps` is a fourth hardcoding whose record also carries a
  single `Field<Vector> embedding`. Read F11 before pricing this.** Dim routing does
  NOT disappear; it collapses from table-selection to column-selection plus a
  predicate. Most Liquibase DDL is a mechanical 3→1 collapse. Four risk items
  carried into Consequences above.
- **F6 (VERIFIED, live PG, 2026-08-10):** `MATCH SIMPLE` (the default) exempts
  any FK row with a NULL in the key. `VALIDATE CONSTRAINT` SUCCEEDS with
  pre-existing `collection IS NULL` manifest rows in place, while still
  rejecting dangling non-NULL rows and deletion of referenced chunks. This
  CLOSES Open Questions 1 and 2 and REMOVES the draft's only hard migration
  blocker. Consequence carried into C1: those rows remain unenforced, so the
  `manifest_null_collection_report` census is the only visibility into them
  and must survive the Decision-4 subtraction. It also raises Open Question 6
  (`MATCH FULL` as a deliberate alternative).
