// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.Clock;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21): what the swap leaves behind and how it fails, beside {@link Rdr229SwapMovesIntegrationTest} (the fixtures are
 * {@link Rdr229SwapMovesFixtures}): the manifest id key is deferred so purge_trash and the collection delete still commit; the boot isolation
 * check asks about the registry the verification reads; the verification's warm-up runs in the READ ONLY pass and not in the re-check;
 * the refusal, the idempotence and the 55P03 rollback each hold a falsifier.
 */
class Rdr229SwapMovesAfterTheSwapIntegrationTest extends Rdr229SwapMovesFixtures {

    // ── 8: the manifest id key is deferred (linda 2026-11-22, option b) ─────────────────────────────────────

    /**
     * The functions that defer the manifest's NAME key by name ({@code SET CONSTRAINTS fk_catalog_chunks_chunk DEFERRED}) are
     * the ones the id key must not break (found by the tail of the statement, {@code <name key> DEFERRED}, which the name key's id twin never matches): purge_trash deletes chunks while their manifest rows still exist, and the Java kin
     * ({@code CatalogRepository#deferManifestChunkFk}, from {@code deleteCollectionTxn}) does the same. Found live, not from the
     * changelog: every function in schema nexus whose body names the key.
     */
    @Test
    void purgeTrashIsTheOnlyLiveFunctionThatDefersTheManifestNameKeyByName() {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<String> src = DSL.field(DSL.name("p", "prosrc"), String.class);
        Field<String> name = DSL.field(DSL.name("p", "proname"), String.class);
        var names = ctx().selectDistinct(name).from(proc)
            .join(ns).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(src.contains("fk_catalog_chunks_chunk DEFERRED"))
            .fetch(name);
        assertThat(names).as("the live functions that defer the manifest name key by name").containsExactly("purge_trash");
    }

    /**
     * After the swap purge_trash, which defers only the name key, deletes a chunk whose manifest row (carrying its id) is still there
     * and commits: the id key is DEFERRABLE INITIALLY DEFERRED, so its check waits for the commit, by which point step 4 has cascaded
     * the manifest row away. An immediate id key raises 23503 at the DELETE.
     */
    @Test
    void afterTheSwapPurgeTrashDeletesChunksWithManifestRowsPresentAndCommitsCleanly() throws Exception {
        var t = seed("purge");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the swap completed").isNotNull();
        String tenant = t.catalogDocumentChunks();
        assertThat(ctx().fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant))).as("non-vacuity: a chunk").isEqualTo(1);
        assertThat(ctx().fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
            .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.isNotNull()))).as("non-vacuity: a manifest row with its id").isEqualTo(1);
        ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(60))
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute();

        long purged;
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            var tx = DSL.using(c, SQLDialect.POSTGRES);
            tx.select(DSL.function("set_config", String.class, DSL.inline("nexus.tenant"), DSL.inline(tenant), DSL.inline(true))).fetch();
            purged = tx.select(DSL.function(DSL.name("nexus", "purge_trash"), Long.class,
                DSL.val(org.jooq.types.DayToSecond.valueOf(java.time.Duration.ofDays(30))))).fetchOne(0, Long.class);
            c.commit();
        }

        assertThat(purged).as("the tombstoned document was purged").isEqualTo(1L);
        assertThat(ctx().fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant))).as("the chunk was swept").isZero();
        assertThat(ctx().fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant))).as("and its manifest row").isZero();
    }

    /** The Java kin of purge_trash: the collection delete defers the name key the same way, deletes chunks first, then the manifest rows. */
    @Test
    void afterTheSwapTheJavaDeferOfTheNameKeyAlsoLeavesTheChunkDeleteFirstOrderingValid() throws Exception {
        var t = seed("kin");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the swap completed").isNotNull();
        String tenant = t.catalogDocumentChunks();
        assertThat(ctx().fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant))).as("non-vacuity").isEqualTo(1);

        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            var tx = DSL.using(c, SQLDialect.POSTGRES);
            CatalogRepository.deferManifestChunkFk(tx);
            tx.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).execute();
            tx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)).execute();
            c.commit();
        }

        assertThat(ctx().fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant))).isZero();
        assertThat(ctx().fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant))).isZero();
    }

    /**
     * The manifest's id key is DEFERRABLE INITIALLY DEFERRED and the other two child keys and the composite are not deferrable; a real
     * orphan against the id key (a manifest row whose name finds its chunk but whose id names another collection) is accepted by the
     * statement and refused at COMMIT with 23503. The name key still checks at the statement, so this is the only thing the deferral
     * moves: the orphan check of the id key.
     */
    @Test
    void theManifestIdKeyIsDeferredAndRefusesARealOrphanAtCommitNotAtTheStatement() throws Exception {
        var t = seed("deferred");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the swap completed").isNotNull();

        Fk manifestKey = idTwins("catalog_document_chunks").get(0);
        assertThat(manifestKey.deferrable()).as("condeferrable").isTrue();
        assertThat(manifestKey.deferred()).as("condeferred: INITIALLY DEFERRED").isTrue();
        for (String child : List.of("topic_assignments", "chunk_orphaned_at")) {
            assertThat(idTwins(child).get(0).deferrable()).as(child + " id key is unchanged: not deferrable").isFalse();
        }
        assertThat(compositeOnId().get(0).deferrable()).as("the composite is unchanged: not deferrable").isFalse();

        String tenant = t.catalogDocumentChunks();
        String name = StampMoveSeeds.collection(tenant, 0);
        String otherName = name.replace("__v1", "r__v1");
        PgContainerHelper.insertCollection(ctx(), tenant, otherName, BGE);
        long otherId = StampMoveSeeds.id(ctx(), tenant, otherName);
        byte[] chash = ctx().select(CHUNKS.CHASH).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).fetchOne(CHUNKS.CHASH);
        int before = ctx().fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant));

        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSL.using(c, SQLDialect.POSTGRES).insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "1.9.0")
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 1).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, 1).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, name)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, otherId).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, BGE)
                .execute();   // not refused here: the name key is satisfied and the id key waits for the commit
            assertThatThrownBy(c::commit).as("the id key refuses the orphan at COMMIT")
                .isInstanceOfSatisfying(SQLException.class, e -> assertThat(e.getSQLState()).isEqualTo("23503"));
            c.rollback();
        }
        assertThat(ctx().fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant))).as("nothing landed").isEqualTo(before);
    }

    // ── 9: the boot isolation check asks about what the swap's verification reads ───────────────────────────

    /**
     * The chunks swap's verification joins nexus.catalog_collections, which carries an owner-read policy (vectors-065), so the boot
     * isolation check asks about it while a swap move is registered, in the pattern of the residue moves' test
     * ({@code NoteUriResidueRepairMovesIntegrationTest}): it is listed beside the driving tables (nexus.chunks is the chunks check's
     * own), and the policy engine's question finds the policy for the admin role.
     */
    @Test
    void theIsolationCheckAsksAboutTheRegistryTheSwapVerificationReads() throws Exception {
        seed("isolation");
        registerSwaps();
        var tables = DrivingTableIsolationCheck.registeredDrivingTables(ctx());
        assertThat(tables).extracting(DrivingTableIsolationCheck.Table::name)
            .containsExactlyInAnyOrder("taxonomy_centroids", "catalog_collections");
        try (var c = adminPool.getConnection()) {
            var found = DrivingTableIsolationCheck.violations(DSL.using(c, SQLDialect.POSTGRES), tables);
            assertThat(found).as("non-vacuity: the admin role is the one the owner-read policies are for")
                .extracting(v -> v.table().name() + "." + v.policy())
                .contains("catalog_collections.catalog_collections_owner_read");
        }
    }

    // ── 10: the verification's warm-up runs in the READ ONLY pass, not in the completion's re-check ─────────

    /** What one run of {@code verification} scanned, read from the transaction's own statistics before it is rolled back. */
    private record Scans(long chunksTree, long registry, long rows) { }

    private Scans scansOf(String verification, boolean readOnly) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            c.setReadOnly(readOnly);
            long rows = MoveSql.countRows(c, verification, null, 10);
            var x = DSL.table(DSL.name("pg_catalog", "pg_stat_xact_user_tables"));
            Field<String> relname = DSL.field(DSL.name("relname"), String.class);
            Field<Long> seq = DSL.field(DSL.name("seq_scan"), Long.class);
            Field<Long> idx = DSL.field(DSL.name("idx_scan"), Long.class);
            long chunksTree = 0;
            long registry = 0;
            for (var r : DSL.using(c, SQLDialect.POSTGRES).select(relname, seq, idx).from(x)
                    .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus")).fetch()) {
                long n = (r.value2() == null ? 0 : r.value2()) + (r.value3() == null ? 0 : r.value3());
                if (r.value1().startsWith("chunks")) {
                    chunksTree += n;
                } else if (r.value1().equals("catalog_collections")) {
                    registry += n;
                }
            }
            c.rollback();
            return new Scans(chunksTree, registry, rows);
        }
    }

    /**
     * The warm-up arms run in the finisher's first pass, which is READ ONLY, and not in the completion transaction's re-check, which is
     * read-write and runs the same text under what is left of 1400 ms: there they would scan the whole heap a second time for nothing.
     * Measured by the transaction's own scan counters over {@code nexus.chunks} and its leaves and over the registry: a READ ONLY run
     * scans both, a read-write run scans neither (the leaf-key arm reads the catalogs only), and both return no row.
     */
    @Test
    void theWarmUpArmsScanInTheReadOnlyPassAndNotInTheReadWriteRecheck() throws Exception {
        seed("warm");
        registerSwaps();
        String verification = (String) moves(LABEL_F.eq(CHUNKS_SWAP)).get(0).get("verification");

        Scans readOnly = scansOf(verification, true);
        Scans readWrite = scansOf(verification, false);

        assertThat(readOnly.rows()).as("the verification finds no row on a good store").isZero();
        assertThat(readWrite.rows()).isZero();
        assertThat(readOnly.chunksTree()).as("READ ONLY pass: the warm-up scanned nexus.chunks (the heap arm and the anti-join)").isGreaterThanOrEqualTo(2);
        assertThat(readOnly.registry()).as("READ ONLY pass: the warm-up scanned the registry (the anti-join)").isPositive();
        assertThat(readWrite.chunksTree()).as("read-write re-check: nexus.chunks is not scanned").isZero();
        assertThat(readWrite.registry()).as("read-write re-check: the registry is not scanned").isZero();
    }

    /** A leaf as {@code regclass} prints it ({@code nexus.name} or {@code nexus."odd-name"}) without the schema and the quotes. */
    private static String unquoted(String regclass) {
        String name = regclass.startsWith("nexus.") ? regclass.substring("nexus.".length()) : regclass;
        return name.startsWith("\"") && name.endsWith("\"") ? name.substring(1, name.length() - 1).replace("\"\"", "\"") : name;
    }

    // ── 11: the refusal of the missing id key ───────────────────────────────────────────────────────────────

    /**
     * {@code on_complete}'s own refusal, past the verification: every leaf has a valid unique index on the id key (so the verification,
     * which reads the leaves, finds nothing) but the parent has no constraint on it. The completion raises 55000 with the swap's message,
     * which is permanent (failed, not blocked), and nothing it adds exists.
     */
    @Test
    void theSwapRefusesWithItsOwnErrorWhenEveryLeafHasTheKeyButTheParentHasNot() throws Exception {
        seed("parentless");
        registerSwaps();
        IdKeyIndexProbe.reset(ctx());
        int n = 0;
        for (String leaf : IdKeyIndexProbe.leaves(ctx(), IdKeyIndexProbe.CHUNKS)) {
            ctx().alterTable(DSL.table(DSL.name("nexus", unquoted(leaf))))
                .add(DSL.constraint(DSL.name("p33c_refusal_" + n++)).unique(DSL.name("tenant_id"), DSL.name("collection_id"),
                    DSL.name("chash"), DSL.name("embedding_model")))
                .execute();
        }
        assertThat(IdKeyIndexProbe.leavesWithoutValidIndex(ctx(), IdKeyIndexProbe.CHUNKS)).as("precondition: every leaf has a valid key").isEmpty();
        assertThat(IdKeyIndexProbe.indexes(ctx(), IdKeyIndexProbe.CHUNKS)).as("precondition: and the parent has none")
            .noneMatch(i -> i.level() == 0);

        runSwaps(new VerifyingFinisher());

        var row = moves(LABEL_F.eq(CHUNKS_SWAP)).get(0);
        assertThat(row.get("completed_at")).as("not completed").isNull();
        assertThat(row.get("failed_at")).as("a permanent failure: the verification passed, so the refusal is on_complete's").isNotNull();
        assertThat((String) row.get("failure")).as("the swap's own error: its SQLSTATE and its message")
            .contains("sqlstate=55000")
            .contains("rdr229 swap of nexus.chunks refused: it has no valid unique key on (tenant_id, collection_id, chash, embedding_model)");
        for (String child : CHILD_ID_COLUMN.keySet()) {
            assertThat(idTwins(child)).as("no id twin on " + child).isEmpty();
        }
        assertThat(compositeOnId()).as("no composite on the id").isEmpty();
        assertThat(registeredBySwap()).as("no validation move").isEmpty();
    }

    // ── 12: a second pass adds nothing ──────────────────────────────────────────────────────────────────────

    private List<String> constraintNames(String table) {
        var names = new ArrayList<String>();
        for (String type : List.of("f", "u", "p")) {
            constraints(table, type).forEach(fk -> names.add(type + ":" + fk.name()));
        }
        java.util.Collections.sort(names);
        return names;
    }

    /**
     * The on_complete executed again on a store it has already swapped adds no constraint and no move row, and raises nothing: each
     * statement is guarded by what the catalog or the registry holds. Both swaps, and the validation moves it registered stay three.
     */
    @Test
    void executingTheOnCompleteAgainOnASwappedStoreAddsNothingAndRaisesNothing() throws Exception {
        seed("again");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: swapped").isNotNull();
        assertThat(completedAt(CENTROIDS_SWAP)).isNotNull();
        var tables = List.of("chunks", "taxonomy_centroids", "catalog_document_chunks", "chunk_orphaned_at", "topic_assignments");
        var before = new LinkedHashMap<String, List<String>>();
        tables.forEach(t -> before.put(t, constraintNames(t)));
        var labelsBefore = moves(DSL.trueCondition()).stream().map(m -> (String) m.get("label")).sorted().toList();
        assertThat(registeredBySwap()).as("non-vacuity: the three validation moves are there").hasSize(3);

        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            for (int pass = 0; pass < 2; pass++) {
                for (String table : List.of("chunks", "taxonomy_centroids")) {
                    MoveSql.runOnComplete(c, Rdr229SwapMove.spec(table).onComplete());
                }
            }
        }

        tables.forEach(t -> assertThat(constraintNames(t)).as("constraints of " + t).isEqualTo(before.get(t)));
        assertThat(moves(DSL.trueCondition()).stream().map(m -> (String) m.get("label")).sorted().toList())
            .as("the same move rows, no duplicate").isEqualTo(labelsBefore);
    }

    // ── 13: a lock on a LATER object rolls back what came before it ────────────────────────────────────────

    /**
     * The completion is one transaction: a lock it cannot get on an object it reaches LATE (here the last child table, and here the
     * registry the composite references, which is the last statement) is a transient 55P03 after earlier statements applied, and they roll
     * back with it: no id twin on any child, no composite, no validation move registered. The move stays blocked and retryable.
     */
    private void assertRolledBackWhileHeld(java.util.function.Consumer<DSLContext> hold) throws Exception {
        registerSwaps();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            hold.accept(DSL.using(holder, SQLDialect.POSTGRES));

            runSwaps(new VerifyingFinisher());

            holder.rollback();
        }
        var chunks = moves(LABEL_F.eq(CHUNKS_SWAP)).get(0);
        assertThat(chunks.get("completed_at")).as("not completed while the lock is held").isNull();
        assertThat(chunks.get("failed_at")).as("blocked, never failed, on 55P03").isNull();
        assertThat(chunks.get("last_transient")).as("the transient blocked state is recorded").isNotNull();
        assertThat((String) chunks.get("last_transient")).contains("55P03");
        for (String child : CHILD_ID_COLUMN.keySet()) {
            assertThat(idTwins(child)).as("rolled back: no id twin on " + child).isEmpty();
        }
        assertThat(compositeOnId()).as("rolled back: no composite").isEmpty();
        assertThat(registeredBySwap()).as("rolled back: no validation move").isEmpty();
    }

    @Test
    void aLockOnTheLastChildTableRollsBackTheEarlierChildKeysAndTheMoves() throws Exception {
        var t = seed("latechild");
        assertRolledBackWhileHeld(h -> h.update(DSL.table(DSL.name("nexus", "topic_assignments")))
            .set(DSL.field(DSL.name("topic_id"), Long.class), DSL.field(DSL.name("topic_id"), Long.class))
            .where(TENANT_F.eq(t.topicAssignments())).execute());
    }

    @Test
    void aLockOnTheRegistryTheCompositeReferencesRollsBackEveryChildKeyAndEveryMove() throws Exception {
        var t = seed("lateregistry");
        assertRolledBackWhileHeld(h -> h.update(DSL.table(DSL.name("nexus", "catalog_collections")))
            .set(DSL.field(DSL.name("name"), String.class), DSL.field(DSL.name("name"), String.class))
            .where(TENANT_F.eq(t.centroids())).execute());
    }

    // ── 14: the child-side lookups of the chunk's id key use an index each ──────────────────────────────────

    private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger(Rdr229SwapMovesAfterTheSwapIntegrationTest.class);

    private String planOf(java.util.function.Function<DSLContext, org.jooq.Query> query) {
        return ctx().transactionResult(cfg -> {
            var tx = cfg.dsl();
            dev.nexus.service.db.PgSession.setLocal(tx, "enable_seqscan", "off");   // a small table seq-scans whatever index it has: ask for the index the lookup can use
            return PgContainerHelper.explainPlanInlined(tx, query.apply(tx));
        });
    }

    /**
     * Every delete or update of a chunk row looks up the child rows by the child's id key, and the child needs an index for that lookup or the
     * chunk delete reads the whole child table. EXPLAIN shows no trigger plan, so this EXPLAINs the lookup the foreign key's trigger issues
     * (ON DELETE CASCADE: a DELETE on the child by the four key columns; NO ACTION: a SELECT ... FOR KEY SHARE by them), with enable_seqscan off so a
     * table this small names the index it would use. chunk_orphaned_at has only its name primary key (tenant_id, collection, chash) and gets
     * vectors-074's (tenant_id, chash); the manifest uses idx_catalog_chunks_chash and topic_assignments idx_ta_tenant_doc_id.
     */
    @Test
    void theChildLookupsOfTheChunkIdKeyEachUseAnIndexOnceTheSwapIsDone() throws Exception {
        var t = seed("plans");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the swap completed").isNotNull();
        // enough distinct chashes under one collection that (tenant_id, chash) is far more selective than the manifest's (tenant_id, collection)
        // index, which the planner would otherwise take in a tie. The rows are real chunks with their manifest rows, written after the swap so the id keys check each.
        String tenantOfRows = t.catalogDocumentChunks();
        String collectionOfRows = StampMoveSeeds.collection(tenantOfRows, 0);
        long idOfRows = StampMoveSeeds.id(ctx(), tenantOfRows, collectionOfRows);
        var hashes = new ArrayList<String>();
        var texts = new ArrayList<String>();
        var vectors = new ArrayList<float[]>();
        var metas = new ArrayList<Map<String, Object>>();
        for (int i = 0; i < 300; i++) {
            hashes.add(String.format("%04x", i) + "0".repeat(60));
            texts.add("plan row " + i);
            vectors.add(new float[768]);
            metas.add(Map.of());
        }
        PgContainerHelper.insertChunks(ctx(), tenantOfRows, collectionOfRows, hashes, texts, vectors, metas);
        for (int i = 0; i < 300; i++) {
            ctx().insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenantOfRows).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "1.9.0")
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 1000 + i).set(CATALOG_DOCUMENT_CHUNKS.CHASH, java.util.HexFormat.of().parseHex(hashes.get(i)))
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, 1000 + i).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collectionOfRows)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, idOfRows).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, BGE).execute();
        }
        new dev.nexus.service.db.TenantScope(adminPool).vacuumAnalyze(List.of("nexus.catalog_document_chunks"));

        // the lookup of a real key of the seeded tenant, so the planner's estimate for tenant_id alone is the 300 rows it holds
        byte[] chash = java.util.HexFormat.of().parseHex(hashes.get(7));
        Field<String> tenant = TENANT_F;
        Field<Long> collectionId = DSL.field(DSL.name("collection_id"), Long.class);
        Field<Long> sourceCollectionId = DSL.field(DSL.name("source_collection_id"), Long.class);
        Field<byte[]> chashF = DSL.field(DSL.name("chash"), byte[].class);
        Field<byte[]> docId = DSL.field(DSL.name("doc_id"), byte[].class);
        Field<String> model = DSL.field(DSL.name("embedding_model"), String.class);

        String orphaned = planOf(tx -> tx.deleteFrom(DSL.table(DSL.name("nexus", "chunk_orphaned_at")))
            .where(tenant.eq(tenantOfRows)).and(collectionId.eq(idOfRows)).and(chashF.eq(chash)).and(model.eq(BGE)));
        var x = DSL.table(DSL.name("nexus", "catalog_document_chunks")).as("x");
        String manifest = planOf(tx -> tx.select(DSL.inline(1)).from(x)
            .where(tenant.eq(tenantOfRows)).and(collectionId.eq(idOfRows)).and(chashF.eq(chash)).and(model.eq(BGE)).forKeyShare().of(x));
        String assignments = planOf(tx -> tx.deleteFrom(DSL.table(DSL.name("nexus", "topic_assignments")))
            .where(tenant.eq(tenantOfRows)).and(sourceCollectionId.eq(idOfRows)).and(docId.eq(chash)).and(model.eq(BGE)));
        log.info("PLAN chunk_orphaned_at (ON DELETE CASCADE lookup):\n{}", orphaned);
        log.info("PLAN catalog_document_chunks (NO ACTION lookup, FOR KEY SHARE):\n{}", manifest);
        log.info("PLAN topic_assignments (ON DELETE CASCADE lookup):\n{}", assignments);

        assertThat(orphaned).as("chunk_orphaned_at: the new (tenant_id, chash) index").contains("idx_chunk_orphaned_at_tenant_chash")
            .doesNotContain("Seq Scan");
        assertThat(manifest).as("catalog_document_chunks").contains("idx_catalog_chunks_chash").doesNotContain("Seq Scan");
        assertThat(assignments).as("topic_assignments").contains("idx_ta_tenant_doc_id").doesNotContain("Seq Scan");
    }
}
