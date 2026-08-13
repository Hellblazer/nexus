/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import dev.nexus.service.vectors.DimTables;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

/**
 * RDR-180 Item6, engine half (nexus-jxizy.6): the per-tenant full-digest
 * rekey. Executed INSIDE the freeze window by the client rung via
 * {@code POST /v1/remap/rekey}; runs as nexus_svc under
 * {@link TenantScope#withTenant} (per-tenant RLS — the reason this is an
 * endpoint and not Liquibase DML, which silently sees zero rows under
 * FORCE RLS as the non-BYPASSRLS owner).
 *
 * <p>PREDICATE (design amendment 2, T2 nexus_rdr/180-engine-cohort-design-
 * amendments): a row needs rekeying when {@code chash IS DISTINCT FROM
 * sha256(chunk_text)} — digest-mismatch, NOT width. A width predicate
 * would miss 32-ASCII-char legacy ids that converted to exactly 32 bytes.
 * Idempotent by construction: a second run finds every content row equal
 * to its digest and no-ops.
 *
 * <p>ORDER, one transaction per tenant (single atomic cutover — RDR-180
 * Failure Modes: no dual-width window within a tenant):
 * <ol>
 *   <li>Conflict pre-check: one recovered old_ref mapping to two distinct
 *       digests fails LOUD (realized 128-bit collision / corpus
 *       corruption — mirrors the client build_content_map refusal).</li>
 *   <li>chash_alias build: (old_ref per the reversibility lemma,
 *       old_bytes, new digest) for every mismatched content row.</li>
 *   <li>Item8 disposition for empty-text rows: reference-only rows
 *       resolve through the alias built from content siblings; orphans
 *       get the per-run policy (drop cascades their manifest
 *       pointers in the same transaction; synthesize mints the
 *       deterministic surrogate and stamps
 *       {@code metadata.chash_origin='synthetic'}).</li>
 *   <li>Two-phase chunk rekey per dim (RDR-185 PK-collision-under-
 *       collapse): keep one row per (collection, digest) — preferring a
 *       row already AT the digest key — delete the rest (all aliased),
 *       then UPDATE survivors.</li>
 *   <li>Cascade via the alias: manifest (plain — chash not in its PK),
 *       topic_assignments (TEXT doc_id via old_ref match, two-phase),
 *       frecency (GREATEST-merge on collapse), relevance_log (plain).</li>
 * </ol>
 * VALIDATE of the octet CHECKs is deliberately NOT here: the client rung
 * runs it via the local admin connection after count-verify (table owner,
 * RLS-exempt scan).
 */
public final class RekeyOps {

    private static final Logger log = LoggerFactory.getLogger(RekeyOps.class);

    /**
     * Typed table accessor for content rows (nexus-4okz4 increment 2,
     * RDR-191 repoint nexus-o8dil.17).
     *
     * <p>byte[]-typed {@code chash} deliberately — NOT {@code
     * dev.nexus.service.vectors.DimTables.ChunkTable}'s hex-string accessor.
     * Every join in this class is against {@code chash_alias.old_bytes} /
     * {@code new_chash}, both byte[]-typed generated fields; a hex-string
     * comparison would silently never match (different wire representation
     * of the same bytes, not comparable via {@code =}). {@link
     * dev.nexus.service.jooq.nexus.Tables#CHUNKS}'s generated {@code CHASH}
     * field is ALREADY byte[]-typed natively (unlike {@code ChunkTable},
     * which wraps it through the {@code ChashHex} converted type), so no
     * wrapper is needed at all post-unification — this record is built
     * directly from the generated table.
     *
     * <p>{@code name} is {@link DimTables#CHUNKS_TABLE_NAME} — used for the
     * still-raw {@link ChashSqlIdioms#contentCollapseDelete(String)} call
     * (no jOOQ DSL form, see step 4 below) and the synthetic-alias
     * {@code source} column.
     */
    private record Dim(String name, Table<?> table, Field<byte[]> chash, Field<String> collection,
                        Field<String> chunkText, Field<JSONB> metadata) {
    }

    /**
     * RDR-191 unification (nexus-o8dil.17, nexus-xtmtf): {@code
     * nexus.chunks_384/768/1024} collapsed into ONE table, {@code
     * nexus.chunks}, keyed {@code (tenant_id, collection, chash)} —
     * dim-independent identity (exactly one of {@code
     * embedding_384/768/1024} is non-null per row, which this class never
     * touches — {@link Dim} carries no embedding field, only chash/
     * collection/chunkText/metadata; every operation below is an
     * UPDATE/DELETE keyed on chash or an INSERT into {@code chash_alias},
     * never a content INSERT into a chunk table).
     *
     * <p>{@code DIMS} therefore now names exactly ONE physical partition,
     * not three — kept as a ONE-ELEMENT list (not inlined) rather than
     * eliminating the {@code for (Dim d : DIMS)} loops entirely, so every
     * loop body below stays behavior-identical and every loop still runs
     * exactly once. This is not a cosmetic minimal-diff choice: looping 3x
     * over the SAME table with identical predicates would now
     * triple-process every row — the DimTables D1 hazard finding (table
     * membership no longer implies dim; T2 nexus/rdr-191-batch-D1-2026-08-13
     * [22460]) applies here directly, and a one-element list is the
     * correctness fix, not merely a convenience.
     */
    private static final List<Dim> DIMS = List.of(
        new Dim(DimTables.CHUNKS_TABLE_NAME, CHUNKS, CHUNKS.CHASH, CHUNKS.COLLECTION,
            CHUNKS.CHUNK_TEXT, CHUNKS.METADATA));

    private final TenantScope tenantScope;

    public RekeyOps(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /** Thrown when one legacy id maps to two distinct content digests. */
    public static final class RekeyConflictException extends RuntimeException {
        public RekeyConflictException(String message) {
            super(message);
        }
    }

    /**
     * Typed rendering of {@code current_setting('nexus.tenant', true)}
     * (nexus-4okz4 increment 2) — the SQL-side tenant read every writer in
     * this class deliberately uses INSTEAD OF the Java {@code tenant}
     * parameter (even though it is in scope via the enclosing lambda
     * capture), so the value always matches whatever {@link
     * TenantScope#withTenant} actually stamped on the connection, not what
     * the caller merely intended to pass. Preserved verbatim by this
     * conversion — no site was rebound to a Java-side value.
     */
    private static Field<String> currentTenantSetting() {
        return DSL.function("current_setting", String.class, DSL.val("nexus.tenant"), DSL.val(true));
    }

    /** {@code encode(bytes, 'hex')} — used at every alias-driven repoint. */
    private static Field<String> hex(Field<byte[]> bytes) {
        return DSL.function("encode", String.class, bytes, DSL.val("hex"));
    }

    /**
     * Run the full rekey for *tenant*. {@code synthesizeOrphans} selects the
     * Item8 policy for orphaned empty-text rows (default caller: drop).
     * Returns the disposition + per-table counts (the auditable envelope).
     */
    // NOT SANCTIONED RAW (nexus-4okz4 increment 5 — REMOVED from
    // RawSqlGateTest.SANCTIONED_STATEMENTS): this method contains no raw
    // execute()/fetch() call of its OWN — every statement (advisory lock,
    // conflict pre-check, alias INSERT...SELECT...ON CONFLICT, the step-3
    // Item8 UPDATE/DELETE/INSERT statements, phase-B content rekey, and
    // every step-5 cascade UPDATE/DELETE) is typed jOOQ DSL. The two
    // raw-SQL-touching primitives this method calls — the in-transaction
    // ANALYZE + privilege probe (ChashSqlIdioms.refreshAliasStats) and the
    // two-phase content rekey's phase-A collapse (ChashSqlIdioms.
    // contentCollapseDelete's ctid/array_agg ORDER BY keeper idiom) — are
    // METHOD CALLS into ChashSqlIdioms, their single true home (registered
    // there, not duplicated here). Empirically verified at increment 5:
    // RawSqlGateTest's RAW_EXECUTE pattern matches nothing anywhere in this
    // file (falsification: temporarily adding a raw ctx.execute("...")
    // literal here fails the gate immediately, since this method carries no
    // registration to shelter it — see RawSqlGateTest's own javadoc).
    public Map<String, Object> rekey(String tenant, boolean synthesizeOrphans) {
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> counts = new LinkedHashMap<>();

            // Cross-path mutual exclusion (critic-p1 High): this writer and
            // StagingPromoteOps touch the SAME tables (chash_alias, chunks_*,
            // pointer stores) — sharing the 'staging:'||tenant advisory-lock
            // namespace serializes a mis-sequenced concurrent rekey against
            // any in-flight promote/finalize instead of interleaving under
            // READ COMMITTED (the GH #1390 class via a cross-endpoint path).
            // jOOQ DSL rendering (nexus-4okz4 increment 2): mirrors
            // CatalogRepository.acquireSweepGateShared/Exclusive's
            // DSL.function(pg_advisory_xact_lock/hashtext) shape exactly.
            // DSL.val("staging:").concat(currentTenantSetting()) renders as
            // Postgres's native `||` operator (verified: jOOQ's concat()
            // renders `||` for the POSTGRES dialect family, not a
            // NULL-coalescing CONCAT() function) — so a missing
            // nexus.tenant GUC still yields a NULL lock key and the same
            // silent-no-lock behavior the raw SQL had (pg_advisory_xact_lock
            // is STRICT: NULL argument, no-op, no exception), not a
            // behavior change.
            ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                       DSL.function("hashtext", Integer.class,
                           DSL.val("staging:").concat(currentTenantSetting()))))
               .fetch();

            // (1) conflict pre-check across all dims: same old_ref, two digests.
            // jOOQ DSL rendering (nexus-4okz4 increment 2): unionAllContentRowsDsl
            // replaces the deleted unionAllContentRows() string method,
            // identical UNION ALL shape (old_ref/old_bytes/new_chash/source
            // per dim, filtered to mismatched content rows).
            var conflictUnion = unionAllContentRowsDsl(ctx);
            Field<String> conflictOldRef = conflictUnion.field("old_ref", String.class);
            Field<byte[]> conflictNewChash = conflictUnion.field("new_chash", byte[].class);
            Integer conflicts = ctx.selectCount()
                .from(ctx.select(conflictOldRef)
                    .from(conflictUnion)
                    .groupBy(conflictOldRef)
                    .having(DSL.countDistinct(conflictNewChash).gt(1))
                    .asTable("q"))
                .fetchOne(0, Integer.class);
            if (conflicts != null && conflicts > 0) {
                throw new RekeyConflictException(
                    conflicts + " legacy id(s) map to more than one content digest "
                    + "(realized 128-bit collision or corpus corruption) — refusing "
                    + "to pick silently (GH #1390: correct addresses only)");
            }

            // (2) alias facts for every mismatched CONTENT row (all dims).
            // jOOQ DSL rendering (nexus-4okz4 increment 2): INSERT...SELECT...
            // ON CONFLICT DO NOTHING over a fresh unionAllContentRowsDsl
            // instance (a second, independent derived-table build — pure
            // expression tree, no shared state with the conflict-check one
            // above; same SQL shape either way).
            var aliasUnion = unionAllContentRowsDsl(ctx);
            int aliased = ctx.insertInto(CHASH_ALIAS,
                    CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF, CHASH_ALIAS.OLD_BYTES,
                    CHASH_ALIAS.NEW_CHASH, CHASH_ALIAS.SOURCE)
                .select(ctx.select(
                        currentTenantSetting(),
                        aliasUnion.field("old_ref", String.class),
                        aliasUnion.field("old_bytes", byte[].class),
                        aliasUnion.field("new_chash", byte[].class),
                        aliasUnion.field("source", String.class))
                    .from(aliasUnion))
                .onConflict(CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF)
                .doNothing()
                .execute();
            counts.put("alias_rows", aliased);

            // (2a) UN-BLIND THE PLANNER before anything joins the rows we just
            // wrote. F2, production 2026-07-20: autoanalyze fires the moment
            // tenant 1 commits and freezes chash_alias statistics at "100%
            // tenant 1" (most_common_vals={t1}, freqs=[1.0], n_distinct=1).
            // Tenant 2's alias rows are inserted INSIDE this transaction and
            // are therefore invisible to the planner, so `tenant_id = 't2'`
            // estimates ONE row and Postgres picks a triple nested loop
            // against ~134k x 466k actual: 101 MINUTES, versus 461 seconds
            // once the estimate is right. An in-transaction ANALYZE samples
            // this transaction's own uncommitted rows, which is exactly the
            // property needed and the reason this cannot be deferred to a
            // post-commit maintenance pass.
            //
            // Every step below (Item8 disposition, the two-phase rekey, and
            // above all the step-5 cascades) joins chash_alias, so this sits
            // immediately after the INSERT rather than just before the
            // cascades.
            //
            // Requires MAINTAIN on the table for nexus_svc (grants-nexus-svc,
            // PG17+): Postgres does NOT error when a non-owner analyzes — it
            // WARNs and SKIPS, so an ungranted ANALYZE is a silent no-op that
            // leaves the planner exactly as blind. The outcome therefore rides
            // the envelope instead of being assumed; RekeyOpsIntegrationTest
            // asserts the resulting statistics, never the statement.
            counts.put("alias_stats_refreshed", ChashSqlIdioms.refreshAliasStats(ctx));

            // (2b) nexus-t76bp REWORK (critic-p1 Critical, T2 nexus/critique-
            // t76bp-rekey-gate-2026-08-08 [21807]): round 1 of this bead
            // gated ONLY step 5's manifest UPDATE. The critic traced the
            // full method body and found steps 3 (Item8 disposition) and 4
            // (two-phase content rekey — the step that makes rekeyed
            // content physically live under its NEW digest) ran entirely
            // UNGATED, for a span the class javadoc itself documents as
            // potentially MINUTES at real scale (F2: 101min pre-fix). A
            // concurrent, unrelated document's ordinary sweep sharing that
            // exact (collection, chash) pair could delete the physical
            // content during that window; step 5's then-only gate-resolution
            // query ran AFTER the damage, correctly found nothing left to
            // protect, and its UPDATE repointed the manifest unconditionally
            // — a silent dangling reference. Fixed per the critic's
            // Option 1: acquire every target-collection gate HERE, before
            // step 3 begins, using the alias map step 2 just built, and hold
            // through step 5's commit (xact-scoped advisory locks release
            // only at commit/rollback — no explicit release needed).
            //
            // COST (code-review-expert T2 review-11gh6-gate-2026-08-08
            // [21797], T76BP REWORK DELTA D3): holding these gates SHARED
            // from here through commit means every concurrent sweep on a
            // touched collection fails open to sweep_skipped (39upx
            // accounting) for this rekey's FULL duration — potentially
            // minutes at real scale (F2: 101min pre-fix; 461s post-fix).
            // This is the deliberate tradeoff (finite writer-side stall,
            // sweeps simply retry later), not an accident.
            //
            // Resolved via `old_bytes`, not `new_chash`: at THIS point in
            // the method no physical row has been rekeyed yet, so a
            // `new_chash` join (the shape round 1 used, correct only
            // immediately before step 5) would find nothing. `old_bytes` is
            // every mismatched row's CURRENT physical chash by construction
            // of step 2's INSERT — and it is coextensive with what steps 3
            // (Item8's `reference_only_resolved`, which also joins on
            // `a.old_bytes = c.chash`) and 4 (`ChashSqlIdioms.
            // contentCollapseDelete`/`contentRekeyUpdateDsl`, whose predicate
            // is exactly step 2's alias-generating predicate) are about to
            // touch: neither the collapse phase nor the rekey phase ever
            // changes a physical row's `collection` column, only its
            // `chash` — so "every collection currently holding a row at
            // `old_bytes`" IS "every collection steps 3-4 are about to
            // mutate," by construction, not by proxy. (A collapse-loser
            // that is already AT the digest key, not itself mismatched, is
            // never gate-relevant on its own: a collapse GROUP only forms
            // when count(*) > 1 for a (collection, digest) pair, which
            // requires at least one MISMATCHED sibling in that same group
            // — so the group's collection is always reachable via that
            // sibling's `old_bytes`, never missed.)
            //
            // NAMED RESIDUAL (not closed by this query — stating exactly
            // what remains open, matching the kl2z6/document_restore/
            // promoteCollection discipline rather than claiming closure):
            // step 3's ORPHAN-SYNTHESIZE sub-branch (below) mints alias
            // rows for rows that, by `orphanCond`'s own definition, have NO
            // alias fact yet at this point — so their collections are NOT
            // resolved by this query and are NOT gated by it. What
            // actually protects that sub-branch is a DIFFERENT mechanism:
            // the synthesized surrogate chash exists only on rows this
            // transaction itself UPDATEs, which stay row-locked through
            // commit — a racing sweep's DELETE blocks on the row lock and
            // then EvalPlanQual-rechecks its quals against the new row
            // version, matching zero rows. The step-6 abort-on-nonzero
            // below is the BACKSTOP, not the shield for this residual: it
            // detects pre-existing corruption and races committed before
            // its own snapshot, and structurally cannot see a sweep that
            // commits after this transaction (critic Option 2, deliberately
            // COMBINED with Option 1 here rather than substituted for it —
            // prevention where structural, detection where prevention is
            // impossible).
            // jOOQ DSL rendering (nexus-t76bp representation-only pass, Hal
            // directive 2026-08-08): identical to the prior raw SQL —
            // DISTINCT phys.collection, inner JOIN chash_alias to the
            // physical content rows on phys.chash = a.old_bytes. No bind
            // values in this statement (no user input reaches it either
            // way); the rewrite is about typed columns and generated
            // tables, not injection risk.
            // RDR-191 repoint (nexus-o8dil.17): the former three-dim UNION
            // ALL (chunks_384/768/1024, each dim's own physical table)
            // collapses to a single SELECT — nexus.chunks now IS the union
            // of what those three tables held, dim-independent identity
            // (tenant_id, collection, chash). No unionAll needed; "phys"
            // still names the same (collection, chash) pair set as before.
            var phys = ctx.select(CHUNKS.COLLECTION, CHUNKS.CHASH).from(CHUNKS).asTable("phys");
            var targetCollections = ctx.selectDistinct(phys.field("collection", String.class))
                .from(CHASH_ALIAS)
                .join(phys).on(phys.field("chash", byte[].class).eq(CHASH_ALIAS.OLD_BYTES))
                .fetch();
            for (String c : targetCollections.stream()
                    .map(r -> r.get(0, String.class))
                    .filter(name -> name != null && !name.isBlank())
                    .sorted()
                    .distinct()
                    .toList()) {
                CatalogRepository.acquireSweepGateShared(ctx, tenant, c);
            }

            // (3) Item8: empty-text rows. Reference-only rows resolve through
            // the alias just built from content-bearing siblings; the rest are
            // orphans under the per-run policy.
            // jOOQ DSL rendering (nexus-4okz4 increment 2): every UPDATE /
            // DELETE / INSERT below is typed DSL over the per-dim Dim
            // accessor; predicates are unchanged from the deleted raw SQL,
            // statement for statement.
            int refResolved = 0;
            int orphansDropped = 0;
            int orphansSynthesized = 0;
            // nexus-4okz4 increment 2 REWORK (C1 fix — see orphanCond's
            // comment below for the full scoping explanation): an aliased
            // sibling copy of the content table, built ONCE and reused
            // across every dim iteration's orphanCond (pure expression-tree
            // object, no per-iteration state — safe to share).
            //
            // RDR-191 repoint (nexus-o8dil.17): collapsed from three
            // per-dim aliases (sib384/768/1024, one per former physical
            // table) to ONE — nexus.chunks is now the single table every
            // dim's content lives in, so there is only one sibling copy to
            // alias. See orphanCond's comment below for why aliasing this
            // is now MANDATORY on every iteration (not just the historical
            // "same-dim branch").
            var sibContent = CHUNKS.as("sib_content");
            for (Dim d : DIMS) {
                // self-join alias ("k") for the two-phase resolve/dedupe
                // guard below — the SAME physical table referenced a second
                // time within one statement needs a distinguishing alias;
                // the primary reference (d.table(), unaliased) stays the
                // update/delete target throughout.
                var k = d.table().as("k");
                Field<byte[]> kChash = k.field(d.chash());
                Field<String> kCollection = k.field(d.collection());

                // chunk_text_hash mirrors the resolved key (same RDR-086
                // parity as contentRekeyUpdateDsl — critic-1010).
                Field<JSONB> refStamp = ChashSqlIdioms.jsonbBuildObject(
                    "chunk_text_hash", hex(CHASH_ALIAS.NEW_CHASH));
                refResolved += ctx.update(d.table())
                    .set(d.chash(), CHASH_ALIAS.NEW_CHASH)
                    .set(d.metadata(), ChashSqlIdioms.mergeMetadata(d.metadata(), refStamp))
                    .from(CHASH_ALIAS)
                    .where(d.chunkText().eq(""))
                    .and(CHASH_ALIAS.OLD_BYTES.eq(d.chash()))
                    .and(d.chash().isDistinctFrom(CHASH_ALIAS.NEW_CHASH))
                    // two-phase guard: skip if the resolved key already exists
                    // in this collection (shared-content collapse — the row is
                    // a duplicate reference; delete instead below).
                    .and(DSL.notExists(ctx.selectOne().from(k)
                        .where(kCollection.eq(d.collection()))
                        .and(kChash.eq(CHASH_ALIAS.NEW_CHASH))))
                    .execute();
                // duplicate reference rows whose resolved key already exists
                ctx.deleteFrom(d.table())
                    .using(CHASH_ALIAS)
                    .where(d.chunkText().eq(""))
                    .and(CHASH_ALIAS.OLD_BYTES.eq(d.chash()))
                    .and(DSL.exists(ctx.selectOne().from(k)
                        .where(kCollection.eq(d.collection()))
                        .and(kChash.eq(CHASH_ALIAS.NEW_CHASH))))
                    .execute();
                // ORPHAN CRITERION (width-free — the same 32-byte-ASCII
                // blindspot fix as the rekey predicate): an empty-text row is
                // an orphan when NO alias fact covers its key AND no
                // content-bearing row anywhere shares that key (a same-key
                // content sibling makes it a legitimate reference — either
                // already-canonical, needing no change, or legacy, resolved
                // via the alias above).
                //
                // nexus-4okz4 increment 2 REWORK (code-review-expert C1, T2
                // review-4okz4-increment2 [21863], critique [21864] — pin
                // fixture: RekeyOpsIntegrationTest's same-dim orphanCond
                // correlation rows on TA/TB): the sibling NOT EXISTS
                // subquery below MUST use an ALIASED copy (sibContent),
                // never a bare generated-Tables constant. An unaliased
                // `.from(CHUNKS)` here would introduce a SECOND unaliased
                // reference to the identical table already in scope from
                // the enclosing statement's UPDATE/DELETE/INSERT target
                // (d.table(), also CHUNKS, unaliased). PostgreSQL resolves
                // the qualified column reference inside the subquery
                // against the SUBQUERY'S OWN (innermost) range-table entry,
                // not the outer row — d.chash() and the subquery's own
                // CHASH column would become the SAME shadowed reference, so
                // the predicate degenerates from "does a content row with
                // THIS row's chash exist" to the self-comparison
                // "chash = chash AND chunk_text <> ''", i.e. "does this
                // table have ANY content row for the tenant" — true or
                // false for every orphan candidate uniformly, independent
                // of which key is being checked.
                //
                // RDR-191 repoint (nexus-o8dil.17): this is now the
                // UNCONDITIONAL case, not merely a "same-dim branch". Before
                // unification, d.table() cycled across three distinct
                // physical tables, and only the branch where the sibling's
                // table happened to equal d's OWN table needed aliasing.
                // Now d.table() is ALWAYS CHUNKS (DIMS has exactly one
                // entry — see its javadoc), so every iteration is the
                // same-dim case by construction; aliasing sibContent is no
                // longer a special case to get right per-branch, it is the
                // only shape that exists.
                //
                // EQUIVALENCE (collapsed IN PLACE, not merged into a shared
                // helper — RDR-191 repoint-batch execution plan D3, "collapse
                // in place or prove equivalent"): the former three-conjunct
                // form was "NOT EXISTS(content sibling in chunks_384) AND
                // NOT EXISTS(chunks_768) AND NOT EXISTS(chunks_1024)" — i.e.
                // no content-bearing row in ANY of the three tables (any
                // collection — chash-only scope, no collection filter,
                // deliberately unlike CatalogRepository.sampleMissingChashes'
                // tenant-AND-collection-scoped fourth copy) shares this
                // chash. nexus.chunks now physically IS the union of what
                // those three tables held, so a single NOT EXISTS against
                // it is the byte-for-byte same predicate over the same row
                // set — not an approximation.
                Condition orphanCond = d.chunkText().eq("")
                    .and(DSL.notExists(ctx.selectOne().from(CHASH_ALIAS)
                        .where(CHASH_ALIAS.OLD_BYTES.eq(d.chash()))))
                    // a row already AT an aliased NEW key is a reference the
                    // step-3a resolve just produced (content rows still hold
                    // their OLD keys until step 4) — never an orphan.
                    .and(DSL.notExists(ctx.selectOne().from(CHASH_ALIAS)
                        .where(CHASH_ALIAS.NEW_CHASH.eq(d.chash()))))
                    .and(DSL.notExists(ctx.selectOne().from(sibContent)
                        .where(sibContent.field(CHUNKS.CHASH).eq(d.chash()))
                        .and(sibContent.field(CHUNKS.CHUNK_TEXT).ne(""))));
                if (synthesizeOrphans) {
                    // Alias the surrogates FIRST so the step-5 cascade
                    // repoints their surviving pointers (RDR-180 Failure
                    // Modes: a preserved pointer must follow the surrogate,
                    // never dangle at the old key).
                    Field<String> synthOldRef = ChashSqlIdioms.oldRefField(d.chash());
                    Field<String> synthSeed = DSL.val("nexus:synthetic-chash:v1|")
                        .concat(currentTenantSetting())
                        .concat(DSL.val("|"))
                        .concat(d.collection())
                        .concat(DSL.val("|"))
                        .concat(synthOldRef);
                    Field<byte[]> synthChash = ChashSqlIdioms.digestField(synthSeed);
                    ctx.insertInto(CHASH_ALIAS,
                            CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF, CHASH_ALIAS.OLD_BYTES,
                            CHASH_ALIAS.NEW_CHASH, CHASH_ALIAS.SOURCE)
                        .select(ctx.select(
                                currentTenantSetting(), synthOldRef, d.chash(), synthChash,
                                DSL.val(d.name() + ":synthetic"))
                            .from(d.table())
                            .where(orphanCond))
                        .onConflict(CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF)
                        .doNothing()
                        .execute();
                    // chunk_text_hash mirrors the SURROGATE key (RDR-086
                    // parity, critic-1010 — same as the staging synthesize).
                    Field<JSONB> synthStamp = ChashSqlIdioms.jsonbBuildObject(
                        "chash_origin", "synthetic", "chunk_text_hash", hex(CHASH_ALIAS.NEW_CHASH));
                    orphansSynthesized += ctx.update(d.table())
                        .set(d.chash(), CHASH_ALIAS.NEW_CHASH)
                        .set(d.metadata(), ChashSqlIdioms.mergeMetadata(d.metadata(), synthStamp))
                        .from(CHASH_ALIAS)
                        .where(d.chunkText().eq(""))
                        .and(CHASH_ALIAS.OLD_BYTES.eq(d.chash()))
                        .and(CHASH_ALIAS.SOURCE.eq(d.name() + ":synthetic"))
                        .and(d.chash().isDistinctFrom(CHASH_ALIAS.NEW_CHASH))
                        .execute();
                } else {
                    // drop: cascade the manifest pointers FIRST
                    // (same transaction — RDR-180 Failure Modes: dangling
                    // manifest pointer), then the orphan rows.
                    ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                        .using(d.table())
                        .where(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(d.chash()))
                        .and(orphanCond)
                        .execute();
                    // (chash_index cascade RETIRED — RDR-187/nexus-piwya.9:
                    // the router table is dropped at boot, before any rung
                    // runs; there is no router row left to cascade.)
                    orphansDropped += ctx.deleteFrom(d.table())
                        .where(orphanCond)
                        .execute();
                }
            }
            counts.put("reference_only_resolved", refResolved);
            counts.put("orphans_dropped", orphansDropped);
            counts.put("orphans_synthesized", orphansSynthesized);

            // (4) two-phase content rekey per dim.
            int collapsed = 0;
            int rekeyed = 0;
            for (Dim d : DIMS) {
                // phase A: delete collapse-losers. Keeper per (collection,
                // digest): a row already AT the digest key wins, else min ctid.
                // STAYS raw (ChashSqlIdioms.contentCollapseDelete): the
                // ctid/array_agg ORDER BY keeper-selection idiom — an
                // array-subscript of an ordered array_agg — has no jOOQ DSL
                // form (nexus-4okz4 increment 2 evaluation).
                collapsed += ctx.execute(ChashSqlIdioms.contentCollapseDelete(d.name()));
                // phase B: rekey survivors whose key mismatches their digest.
                // jOOQ DSL rendering (nexus-4okz4 increment 2):
                // contentRekeyUpdateDsl replaces the deleted string form.
                rekeyed += ChashSqlIdioms.contentRekeyUpdateDsl(
                    ctx, d.table(), d.chash(), d.chunkText(), d.metadata());
            }
            counts.put("collapsed_duplicates", collapsed);
            counts.put("rehashed", rekeyed);

            // (5) cascades via the alias map.
            // manifest: chash not in its PK — plain rewrite.
            //
            // nexus-t76bp (RekeyOps UPDATE-verb catalog_document_chunks
            // writes were outside the sweep gate — same defect class as
            // nexus-11gh6, different SQL verb; REWORKED per critic-p1
            // Critical, T2 nexus/critique-t76bp-rekey-gate-2026-08-08
            // [21807]): every target collection's sweep gate was already
            // acquired at step (2b) above, BEFORE steps 3-4 ran, and is
            // held continuously through this statement's commit (xact-
            // scoped advisory locks release only at commit/rollback). See
            // step (2b)'s comment for the freeze-window investigation, why
            // the resolution moved there and switched to an `old_bytes`
            // join, and the named orphan-synthesize residual that a wider
            // gate does NOT reach: that sub-branch is shielded by
            // row-locks-through-commit + EvalPlanQual (see step (2b)'s
            // comment), with the step-6 abort below serving only as the
            // BACKSTOP for pre-existing corruption and pre-snapshot races,
            // not as the mechanism that covers it.
            // jOOQ DSL rendering (nexus-4okz4 increment 2): UPDATE...FROM,
            // identical predicate.
            counts.put("manifest_repointed", ctx.update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, CHASH_ALIAS.NEW_CHASH)
                .from(CHASH_ALIAS)
                .where(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(CHASH_ALIAS.OLD_BYTES))
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.isDistinctFrom(CHASH_ALIAS.NEW_CHASH))
                .execute());
            // (chash_index two-phase repoint RETIRED — RDR-187/nexus-piwya.9:
            // the router table is dropped at boot, before any rung runs.
            // The two-collapse-direction idiom it pioneered — the
            // RekeyOpsIntegrationTest 3c catch — lives on in the
            // topic_assignments block below.)
            // topic_assignments: TEXT doc_id matches old_ref; PK
            // (tenant, doc_id, topic_id) — two-phase in both collapse
            // directions (the RekeyOpsIntegrationTest 3c idiom).
            // jOOQ DSL rendering (nexus-4okz4 increment 2): DELETE...USING
            // over aliased self-join copies (tb/aa/ab), identical predicate
            // including the ctid tie-break. ctid is a Postgres SYSTEM
            // column, absent from jOOQ's generated column metadata for
            // TOPIC_ASSIGNMENTS — Table.field(String,Class) resolves against
            // that metadata and returns null for it (caught by this
            // increment's own test run, not assumed); DSL.field(DSL.name(...))
            // constructs the reference directly by name instead, the same
            // idiom this file's EXCLUDED.* / house pattern already uses for
            // columns with no generated-Field counterpart.
            var tb = TOPIC_ASSIGNMENTS.as("tb");
            var aa = CHASH_ALIAS.as("aa");
            var ab = CHASH_ALIAS.as("ab");
            Field<Object> taCtid = DSL.field(DSL.name(TOPIC_ASSIGNMENTS.getName(), "ctid"), Object.class);
            Field<Object> tbCtid = DSL.field(DSL.name("tb", "ctid"), Object.class);
            ctx.deleteFrom(TOPIC_ASSIGNMENTS)
                .using(tb, aa, ab)
                .where(aa.field(CHASH_ALIAS.OLD_REF).eq(TOPIC_ASSIGNMENTS.DOC_ID))
                .and(ab.field(CHASH_ALIAS.OLD_REF).eq(tb.field(TOPIC_ASSIGNMENTS.DOC_ID)))
                .and(aa.field(CHASH_ALIAS.NEW_CHASH).eq(ab.field(CHASH_ALIAS.NEW_CHASH)))
                .and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(tb.field(TOPIC_ASSIGNMENTS.TOPIC_ID)))
                .and(tbCtid.lt(taCtid))
                .execute();
            var taKeep = TOPIC_ASSIGNMENTS.as("k");
            ctx.deleteFrom(TOPIC_ASSIGNMENTS)
                .using(CHASH_ALIAS)
                .where(TOPIC_ASSIGNMENTS.DOC_ID.eq(CHASH_ALIAS.OLD_REF))
                .and(DSL.exists(ctx.selectOne().from(taKeep)
                    .where(taKeep.field(TOPIC_ASSIGNMENTS.TOPIC_ID).eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
                    .and(taKeep.field(TOPIC_ASSIGNMENTS.DOC_ID).eq(hex(CHASH_ALIAS.NEW_CHASH)))))
                .execute();
            counts.put("topic_assignments_repointed", ctx.update(TOPIC_ASSIGNMENTS)
                .set(TOPIC_ASSIGNMENTS.DOC_ID, hex(CHASH_ALIAS.NEW_CHASH))
                .from(CHASH_ALIAS)
                .where(TOPIC_ASSIGNMENTS.DOC_ID.eq(CHASH_ALIAS.OLD_REF))
                .execute());
            // frecency: PK (tenant, chunk_id) — GREATEST-merge on collapse
            // (the RDR-185 _FRECENCY_MERGE_SQL semantics, PG port), covering
            // BOTH collapse directions via a per-target group aggregate over
            // every matching old row (3c catch: two olds, no target row).
            // keeper keyed by min(chunk_id), NOT ctid: an UPDATE rewrites
            // the row and changes its ctid, so ctid-based keeper selection
            // goes stale across statements (the 3c "expected 5 was 2" catch).
            // jOOQ DSL rendering (nexus-4okz4 increment 2):
            // frecencyAliasAggregateDsl replaces the deleted string form;
            // DSL.greatest() renders Postgres's native GREATEST(...).
            var g = ChashSqlIdioms.frecencyAliasAggregateDsl(ctx);
            Field<byte[]> gNewChash = g.field("new_chash", byte[].class);
            Field<String> gKeepId = g.field("keep_id", String.class);
            Field<Double> gFs = g.field("fs", Double.class);
            Field<Integer> gMc = g.field("mc", Integer.class);
            Field<OffsetDateTime> gLh = g.field("lh", OffsetDateTime.class);
            Field<OffsetDateTime> gEa = g.field("ea", OffsetDateTime.class);
            Field<Integer> gTd = g.field("td", Integer.class);
            // (i) an existing row AT the target absorbs the whole group.
            ctx.update(FRECENCY)
                .set(FRECENCY.FRECENCY_SCORE, DSL.greatest(FRECENCY.FRECENCY_SCORE, gFs))
                .set(FRECENCY.MISS_COUNT, DSL.greatest(FRECENCY.MISS_COUNT, gMc))
                .set(FRECENCY.LAST_HIT_AT, DSL.greatest(FRECENCY.LAST_HIT_AT, gLh))
                .set(FRECENCY.EMBEDDED_AT, DSL.greatest(FRECENCY.EMBEDDED_AT, gEa))
                .set(FRECENCY.TTL_DAYS, DSL.greatest(FRECENCY.TTL_DAYS, gTd))
                .from(g)
                .where(FRECENCY.CHUNK_ID.eq(hex(gNewChash)))
                .execute();
            var frecencyKeep = FRECENCY.as("k");
            ctx.deleteFrom(FRECENCY)
                .using(CHASH_ALIAS)
                .where(FRECENCY.CHUNK_ID.eq(CHASH_ALIAS.OLD_REF))
                .and(DSL.exists(ctx.selectOne().from(frecencyKeep)
                    .where(frecencyKeep.field(FRECENCY.CHUNK_ID).eq(hex(CHASH_ALIAS.NEW_CHASH)))))
                .execute();
            // (ii) no target row: the min-ctid keeper absorbs the group,
            // the other olds are deleted (the keeper is renamed below).
            var a2 = CHASH_ALIAS.as("a2");
            ctx.update(FRECENCY)
                .set(FRECENCY.FRECENCY_SCORE, gFs)
                .set(FRECENCY.MISS_COUNT, gMc)
                .set(FRECENCY.LAST_HIT_AT, gLh)
                .set(FRECENCY.EMBEDDED_AT, gEa)
                .set(FRECENCY.TTL_DAYS, gTd)
                .from(g, a2)
                .where(a2.field(CHASH_ALIAS.OLD_REF).eq(FRECENCY.CHUNK_ID))
                .and(a2.field(CHASH_ALIAS.NEW_CHASH).eq(gNewChash))
                .and(FRECENCY.CHUNK_ID.eq(gKeepId))
                .execute();
            ctx.deleteFrom(FRECENCY)
                .using(g, a2)
                .where(a2.field(CHASH_ALIAS.OLD_REF).eq(FRECENCY.CHUNK_ID))
                .and(a2.field(CHASH_ALIAS.NEW_CHASH).eq(gNewChash))
                .and(FRECENCY.CHUNK_ID.ne(gKeepId))
                .execute();
            counts.put("frecency_repointed", ctx.update(FRECENCY)
                .set(FRECENCY.CHUNK_ID, hex(CHASH_ALIAS.NEW_CHASH))
                .from(CHASH_ALIAS)
                .where(FRECENCY.CHUNK_ID.eq(CHASH_ALIAS.OLD_REF))
                .execute());
            counts.put("relevance_log_repointed", ctx.update(RELEVANCE_LOG)
                .set(RELEVANCE_LOG.CHUNK_ID, hex(CHASH_ALIAS.NEW_CHASH))
                .from(CHASH_ALIAS)
                .where(RELEVANCE_LOG.CHUNK_ID.eq(CHASH_ALIAS.OLD_REF))
                .execute());

            // (6) verification scans, same transaction: residual mismatched
            // content rows and dangling pointers — MUST all be zero.
            // jOOQ DSL rendering (nexus-4okz4 increment 2):
            // residualMismatchCountDsl replaces the deleted string form
            // (SHARED with StagingPromoteOps.finalizeTenant — see
            // ChashSqlIdioms.residualMismatchCountDsl's javadoc).
            int residual = 0;
            for (Dim d : DIMS) {
                residual += ChashSqlIdioms.residualMismatchCountDsl(
                    ctx, d.table(), d.chash(), d.chunkText());
            }
            counts.put("residual_mismatched", residual);
            Integer danglingManifest = ChashSqlIdioms.danglingManifestCountDsl(ctx);
            counts.put("dangling_manifest", danglingManifest);
            // nexus-t76bp REWORK, critic Option 2 (T2 nexus/critique-t76bp-
            // rekey-gate-2026-08-08 [21807]): both counts were COMPUTED here
            // but never ENFORCED — the class's own javadoc said "MUST all be
            // zero" while the code merely returned them in the envelope for
            // "the client rung" to check post-hoc. StagingPromoteOps.
            // finalizeTenant, the sibling chash mover, already throws loud
            // on the identical counts (see
            // preExistingDanglingManifestRow_abortsFinalizeLoud). Mirrored
            // here as the DETECTION half of the combined fix.
            //
            // ACTUAL COVERAGE (corrected per critic round 2, T2 same doc,
            // "residual-naming adjudication" — the prior wording here
            // overclaimed "catches ANY race in a window the gate does not
            // cover", which is false): this check runs INSIDE rekey's own
            // transaction, so it can only ever see state COMMITTED before
            // its own statement's snapshot. It catches pre-existing
            // corruption (a dangling/mismatched row that already existed
            // before this rekey began) and any race that committed before
            // this point in this same transaction's view. A sweep that
            // blocks on rekey's OWN row locks necessarily commits AFTER
            // rekey does — permanently invisible to this check, not caught
            // by it.
            //
            // What actually protects step (2b)'s named residual — the
            // orphan-synthesize sub-branch — is a DIFFERENT mechanism, not
            // this scan: the surrogate chash exists only on the physical
            // row rekey itself just UPDATEd, and that row stays
            // exclusively locked from the UPDATE through this
            // transaction's commit. A concurrent sweep's DELETE targeting
            // the same row blocks on that row lock; once rekey commits,
            // the sweep's statement re-evaluates its own quals against the
            // new row version (EvalPlanQual) and finds it no longer
            // matches (chash/metadata changed under it), so the DELETE
            // affects zero rows instead of removing a now-referenced
            // surrogate. This check remains a genuine backstop for every
            // OTHER gap, present or future ("no silent fallbacks for
            // data-correctness problems — FAIL LOUD") — it is just not the
            // mechanism protecting the synthesize path specifically.
            if (residual != 0) {
                throw new IllegalStateException(
                    "rekey left " + residual + " digest-mismatched content row(s) — aborting");
            }
            if (danglingManifest != null && danglingManifest != 0) {
                throw new IllegalStateException(
                    "rekey found " + danglingManifest + " dangling manifest row(s) "
                    + "(manifest chash with no content row in any dim) — aborting");
            }
            // Census (nexus-jxizy.10.5): REPORTED here, fatal only on the
            // staging-finalize path — the shipped in-store rekey keeps its
            // contract while the envelope gains the every-column visibility
            // (critic-C3: the old verify saw 2 of ~6 surfaces).
            Map<String, Integer> census = ChashCensus.scan(ctx);
            counts.put("census_residue_columns", census.size());
            if (!census.isEmpty()) {
                counts.put("census_residue", census.toString());
                log.warn("event=rekey_census_residue tenant-scope residue={}", census);
            }
            return counts;
        });
        log.info("event=rekey_complete tenant={} counts={}", tenant, out);
        return out;
    }

    /**
     * Mismatched content rows with recovered old_ref (nexus-4okz4 increment
     * 2 — typed rendering REPLACING the deleted string-returning {@code
     * unionAllContentRows()}; RekeyOps-exclusive, private to this class
     * both before and after this conversion). Columns: {@code old_ref}
     * (the reversibility-lemma rendering), {@code old_bytes} (the row's
     * CURRENT physical chash), {@code new_chash} (the digest), {@code
     * source} (the originating table's name, {@link DimTables#CHUNKS_TABLE_NAME}).
     *
     * <p>RDR-191 repoint (nexus-o8dil.17): the former three-branch UNION
     * ALL (one branch per dim's own physical table, unioned together)
     * collapses to a SINGLE select — nexus.chunks now physically IS the
     * union those three branches used to compute, so unioning it with
     * itself would double/triple-count every mismatched row (the exact
     * DimTables D1 hazard). {@code DIMS.get(0)} is the sole entry (see
     * {@link #DIMS}'s javadoc); still routed through it rather than
     * {@code CHUNKS} directly so {@code source} keeps tracking {@link
     * DimTables#CHUNKS_TABLE_NAME} through one authority, not a second
     * hand-rolled literal. Table alias kept as {@code "u"} — downstream
     * callers ({@code conflictUnion}/{@code aliasUnion}) resolve columns by
     * string name, unaffected by the union-to-single-select change.
     */
    private static Table<?> unionAllContentRowsDsl(DSLContext ctx) {
        Dim d = DIMS.get(0);
        return ctx.select(
                ChashSqlIdioms.oldRefField(d.chash()).as("old_ref"),
                d.chash().as("old_bytes"),
                ChashSqlIdioms.digestField(d.chunkText()).as("new_chash"),
                DSL.val(d.name()).as("source"))
            .from(d.table())
            .where(d.chunkText().ne(""))
            .and(d.chash().isDistinctFrom(ChashSqlIdioms.digestField(d.chunkText())))
            .asTable("u");
    }
}
