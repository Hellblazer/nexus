// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciReconciler.PassReport;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the reconciler builds each collection's HNSW index on {@code collection_id} beside the
 * name-predicate one: the deparsed predicate, the build before and after gate 2, the null-id and killed-build holds, the
 * backoff, the anchor and the REINDEX of an id index, the null-id scan, and which query shapes the index serves. The fixtures
 * are {@link PciIdIndexSupport}'s; gate 3 and its latch are {@link PciIdGate3IntegrationTest}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciIdIndexIntegrationTest extends PciIdIndexSupport {

    // -- the deparsed predicate --------------------------------------------------------------------------

    @Test
    void theIdPredicateDeparsesAsThePatternExpects_andTheIndexIsReadBackByItsIdNotItsName() throws Exception {
        String tenant = newTenant("deparse");
        long id = collection(tenant, name("deparse"), 300);
        PciSettings s = settings(16);
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
        PciCatalog.Leaf leaf = leaf(tenant);

        try (var pass = builder.open()) {
            assertThat(pass.buildById(leaf, 384, id)).isEqualTo(PciBuilderSession.DdlOutcome.DONE);
        }

        String predicate = asSuperuser(su -> su.select(DSL.function(DSL.name("pg_catalog", "pg_get_expr"), SQLDataType.CLOB,
                    DSL.field(DSL.name("i", "indpred")), DSL.field(DSL.name("i", "indrelid"))))
            .from(DSL.table(DSL.name("pg_catalog", "pg_index")).as("i"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
            .where(DSL.field(DSL.name("c", "relname"), String.class).eq(PciCatalog.indexNameById(M384, tenant, id)))
            .fetchOne(0, String.class));
        assertThat(predicate).isEqualTo("(collection_id = '" + id + "'::bigint)");
        var read = leaf(tenant).indexes().stream().filter(PciCatalog.Index::idParsed).toList();
        assertThat(read).hasSize(1);
        assertThat(read.get(0).collectionId()).isEqualTo(id);
        assertThat(read.get(0).valid()).isTrue();
        assertThat(read.get(0).name()).isEqualTo(PciCatalog.indexNameById(M384, tenant, id));
        assertThat(PciBuilderSession.Pass.ownsForComment(leaf(tenant), read.get(0)))
            .as("the ownership check accepts the id-hash name").isTrue();
        assertThat(PciBuilderSession.Pass.ownsForComment(leaf(tenant),
            new PciCatalog.Index(read.get(0).name(), true, null, null, null, null, id + 1)))
            .as("and refuses an id whose hash is not the name").isFalse();
    }

    // -- before gate 2 ---------------------------------------------------------------------------------------

    @Test
    void beforeGate2_noIdBuildIsPlannedOrStarted_andTheNameIndexIsBuiltAsToday() throws Exception {
        String tenant = newTenant("pre");
        collection(tenant, name("pre"), 300);
        PciReconciler r = reconciler(settings(16));
        gate2 = false;

        PassReport pass = r.reconcileOnce();

        assertThat(pass.builds()).as("the name index only").isEqualTo(1);
        assertThat(validNameIndexed(tenant)).containsExactly(name("pre"));
        assertThat(validIdIndexed(tenant)).isEmpty();
        assertThat(hasInvalidId(tenant)).isFalse();
        assertThat(uncovered(r, tenant)).as("planned, and no VALID id index: gate 3 cannot hold").hasSize(1);
    }

    // -- the transition --------------------------------------------------------------------------------------

    @Test
    void afterGate2_theIdIndexIsBuiltBesideTheNameIndex_andGate3ReadsItAsCovered() throws Exception {
        String tenant = newTenant("go");
        long id = collection(tenant, name("go"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();                                   // gate 2 not held: the name index
        gate2 = true;

        PassReport pass = r.reconcileOnce();

        assertThat(pass.builds()).isEqualTo(1);
        assertThat(validNameIndexed(tenant)).as("the name index is not dropped").containsExactly(name("go"));
        assertThat(validIdIndexed(tenant)).containsExactly(id);
        assertThat(uncovered(r, tenant)).as("covered").isEmpty();
        var coverage = coverage(r).orElseThrow();
        assertThat(coverage.validIdIndexed()).contains(PciCatalog.idKey(M384, tenant, id));
        // A further pass changes nothing.
        PassReport again = r.reconcileOnce();
        assertThat(again.builds()).isZero();
        assertThat(again.drops()).isZero();
    }

    @Test
    void aCollectionWithANullIdRow_getsNoIdIndex_keepsItsNameIndex_andGate3DoesNotCountItComplete() throws Exception {
        String tenant = newTenant("null");
        collection(tenant, name("null"), 300);
        long okId = collection(tenant, name("ok"), 300);
        unstampOneRow(tenant, name("null"));
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;

        PassReport pass = r.reconcileOnce();

        assertThat(validNameIndexed(tenant)).containsExactlyInAnyOrder(name("null"), name("ok"));
        assertThat(validIdIndexed(tenant)).as("only the stamped collection").containsExactly(okId);
        assertThat(pass.builds()).isEqualTo(1);
        assertThat(uncovered(r, tenant)).as("the null-id collection is planned and uncovered").hasSize(1);

        // Once its last null row is stamped the next pass builds it and gate 3 reads complete.
        long nullId = stamp(tenant, name("null"));
        r.reconcileOnce();
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(okId, nullId);
        assertThat(uncovered(r, tenant)).isEmpty();
    }

    @Test
    void aKilledIdBuild_isLeftInvalid_gate3DoesNotHold_theNameIndexStays_andTheNextPassDropsAndRebuildsIt() throws Exception {
        String tenant = newTenant("kill");
        long id = collection(tenant, name("kill"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;

        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(() -> hasInvalidId(tenant), "the concurrent id build's invalid index");
            terminateBuilder();
            held.close();
            PassReport ended = pass.get(60, TimeUnit.SECONDS);
            assertThat(ended.ranToEnd()).as("the pass is over").isFalse();
        }

        assertThat(hasInvalidId(tenant)).as("the killed CIC left its invalid id index").isTrue();
        assertThat(validNameIndexed(tenant)).as("the name index is not dropped").containsExactly(name("kill"));
        assertThat(validIdIndexed(tenant)).isEmpty();
        assertThat(uncovered(r, tenant)).as("gate 3 is false between the kill and the next pass").hasSize(1);
        assertThat(coverage(r).orElseThrow().invalidIdIndexed()).as("the INVALID id index is reported").hasSize(1);

        PassReport next = r.reconcileOnce();
        assertThat(next.drops()).as("the invalid index is dropped").isEqualTo(1);
        assertThat(next.builds()).as("and built again from the start in the same pass").isEqualTo(1);
        assertThat(validIdIndexed(tenant)).containsExactly(id);
        assertThat(hasInvalidId(tenant)).isFalse();
        assertThat(uncovered(r, tenant)).isEmpty();
    }

    // -- S2: backoff, the hysteresis band and an INVALID index keep gate 3 shut ---------------------------------

    void cancelBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .fetch());
    }

    boolean builderRunningCreateIndex() {
        try {
            return asSuperuser(su -> su.fetchExists(DSL.selectOne()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class)
                    .eq(PciBuilderSession.builderApplicationName(NONCE)))
                .and(DSL.field(DSL.name("state"), String.class).eq("active"))
                .and(DSL.field(DSL.name("query"), String.class).likeIgnoreCase("CREATE INDEX%"))));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void aFailedIdBuildThatIsBackedOff_keepsGate3Shut_andIsNotBuiltAgainWhileBackedOff() throws Exception {
        String tenant = newTenant("backoff");
        long id = collection(tenant, name("backoff"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        PassReport failed;
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(this::builderRunningCreateIndex, "the builder's CREATE INDEX CONCURRENTLY");
            cancelBuilder();                     // the statement fails (57014), the session carries on: charged to backoff
            held.close();
            failed = pass.get(60, TimeUnit.SECONDS);
        }
        assertThat(failed.failedBuilds()).isEqualTo(1);
        assertThat(r.trackedFailures()).as("backed off").isEqualTo(1);
        assertThat(uncovered(r, tenant)).as("gate 3 stays false").hasSize(1);

        PassReport next = r.reconcileOnce();     // the backoff has not lapsed: the invalid index is dropped, nothing is built

        assertThat(next.builds()).as("backed off: not built again").isZero();
        assertThat(validIdIndexed(tenant)).isEmpty();
        assertThat(validNameIndexed(tenant)).containsExactly(name("backoff"));
        assertThat(uncovered(r, tenant)).as("a backed-off collection with a name index still holds gate 3 shut").hasSize(1);
        assertThat(id).isPositive();
    }

    @Test
    void aCollectionInTheHysteresisBand_keepsGate3ShutUntilItsIdIndexIsBuilt_andThenGetsOne() throws Exception {
        String tenant = newTenant("band");
        long id = collection(tenant, name("band"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();                                       // the name index, at 300 rows
        assertThat(validNameIndexed(tenant)).containsExactly(name("band"));
        // Rows fall into B/2 .. B-1 (B is 200): the name index is kept.
        deleteRows(tenant, name("band"), 150);
        gate2 = true;

        assertThat(uncovered(r, tenant)).as("a name index with no id index").hasSize(1);
        r.reconcileOnce();

        assertThat(validNameIndexed(tenant)).as("kept in the band").containsExactly(name("band"));
        assertThat(validIdIndexed(tenant)).as("and the id index is built so gate 3 is reachable").containsExactly(id);
        assertThat(uncovered(r, tenant)).isEmpty();
    }

    // -- S6: the id index has the name index's lifecycle: anchor, REINDEX ----------------------------------------

    @Test
    void anIdBuildGetsItsAnchorComment() throws Exception {
        String tenant = newTenant("anchor");
        long id = collection(tenant, name("anchor"), 300);
        PciReconciler r = reconciler(reindexSettings());
        r.reconcileOnce();
        gate2 = true;

        r.reconcileOnce();

        PciCatalog.Index index = idIndex(tenant);
        assertThat(index.collectionId()).isEqualTo(id);
        assertThat(index.anchor()).as("the id index is anchored like a name index").isPresent();
        var anchor = index.anchor().orElseThrow();
        assertThat(anchor.origin()).isEqualTo(PciCatalog.ORIGIN_BUILT);
        assertThat(anchor.rows()).as("the rows its predicate selects").isEqualTo(300);
        assertThat(anchor.bytes()).isPositive();
        assertThat(anchor.relfilenode()).as("stamped with the storage it was written against").isNotNull();
    }

    @Test
    void aReindexOfAnIdIndex_runs_keepsItValid_andWritesAFreshAnchor() throws Exception {
        String tenant = newTenant("reindex");
        long id = collection(tenant, name("reindex"), 300);
        PciReconciler r = reconciler(reindexSettings());
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        r.reconcileOnce();
        var before = idIndex(tenant).anchor().orElseThrow();
        assertThat(before.rows()).isEqualTo(300);
        // Over a quarter of the rows go (80 of 300): deletes since build. A restarted engine sees it through the anchor.
        deleteRows(tenant, name("reindex"), 80);
        PciReconciler restarted = reconciler(reindexSettings());

        for (int pass = 0; pass < 4 && idIndex(tenant).anchor().orElseThrow().rows() != 220; pass++) {
            restarted.reconcileOnce();        // at most one REINDEX a pass: the name index and the id index take turns
        }

        PciCatalog.Index after = idIndex(tenant);
        assertThat(after.valid()).as("the REINDEX leaves it valid").isTrue();
        assertThat(after.collectionId()).isEqualTo(id);
        assertThat(after.anchor().orElseThrow().rows()).as("a fresh anchor counts the rows now").isEqualTo(220);
        assertThat(after.anchor().orElseThrow().builtAt()).isAfter(before.builtAt());
        assertThat(after.anchor().orElseThrow().relfilenode()).as("new storage").isNotEqualTo(before.relfilenode());
    }

    // -- S4: the null-id scan -------------------------------------------------------------------------------------

    @Test
    void theNullIdScanIsSkippedWhileTheChecksAreValidated_andRunsWhenOneIsMissingOrNotValid() throws Exception {
        String tenant = newTenant("checks");
        collection(tenant, name("checks"), 300);
        var candidate = List.of(new PciReconcilePlanner.RegistryRow(name("checks"), "live", "", idOf(tenant, name("checks"))));
        unstampOneRow(tenant, name("checks"));
        try (Connection admin = adminDs.getConnection()) {
            DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
            assertThat(PciIdBuilds.checksValidated(ctx)).as("both CHECKs are absent in this class").isFalse();
            assertThat(PciIdBuilds.nullIdCollections(ctx, leaf(tenant), candidate)).as("scanned, and found").containsExactly(name("checks"));

            stamp(tenant, name("checks"));
            asSuperuser(su -> { P31CheckReplay.restore(su, CHUNKS_CHECKS); return null; });   // the changeset's own text: both NOT VALID
            try {
                assertThat(PciIdBuilds.checksValidated(ctx)).as("present but NOT VALID").isFalse();
                asSuperuser(su -> {
                    P31CheckReplay.lift(su, CHUNKS_CHECKS);
                    // added validated (every row is stamped), as the stamp move's on_complete leaves them
                    su.alterTable(CHUNKS).add(DSL.constraint(DSL.name("chunks_collection_id_check"))
                        .check(DSL.field(DSL.nullif(CHUNKS.COLLECTION, "").isNull()).eq(DSL.field(CHUNKS.COLLECTION_ID.isNull())))).execute();
                    su.alterTable(CHUNKS).add(DSL.constraint(DSL.name("chunks_collection_id_not_null"))
                        .check(CHUNKS.COLLECTION_ID.isNotNull())).execute();
                    return null;
                });
                assertThat(PciIdBuilds.checksValidated(ctx)).as("both validated: no row can have a null id").isTrue();
                assertThat(PciIdBuilds.nullIdCollections(ctx, leaf(tenant), candidate)).as("no scan").isEmpty();
            } finally {
                asSuperuser(su -> { P31CheckReplay.lift(su, CHUNKS_CHECKS); return null; });
            }
        }
    }

    @Test
    void aNullIdProbeThatFails_holdsBackOnlyTheIdBuild_andTheCountingTransactionSurvives() throws Exception {
        String tenant = newTenant("probe");
        long id = collection(tenant, name("probe"), 300);
        PciCatalog.Leaf leaf = leaf(tenant);
        var candidate = List.of(new PciReconcilePlanner.RegistryRow(name("probe"), "live", "", id));
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            // An uncommitted rename of the leaf holds ACCESS EXCLUSIVE on it until the rollback below.
            DSL.using(holder, SQLDialect.POSTGRES).alterTable(DSL.table(DSL.name(leaf.schema(), leaf.name())))
                .renameTo(DSL.name(leaf.name() + "_held")).execute();
            try (Connection c = adminDs.getConnection()) {
                c.setAutoCommit(false);
                DSLContext tx = DSL.using(c, SQLDialect.POSTGRES);
                tx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("lock_timeout"), DSL.val("200ms"), DSL.inline(true)))
                    .fetch();

                var held = PciIdBuilds.nullIdCollections(tx, leaf, candidate);

                assertThat(held).as("a probe that fails answers: held back (fail closed)").containsExactly(name("probe"));
                assertThat(tx.selectOne().fetchOne(0, Integer.class)).as("the transaction was not aborted").isEqualTo(1);
                c.rollback();
            }
            holder.rollback();
        }
    }

    // -- elsinore's _by_id search functions: which query shapes use the id-predicate partial index ----------------

    /** The ANN query vector as pgvector text, 384 wide. */
    static String vectorText() {
        StringBuilder b = new StringBuilder("[");
        for (int i = 0; i < 384; i++) {
            b.append(i == 0 ? "" : ",").append(i == 5 ? "1" : "0");
        }
        return b.append(']').toString();
    }

    long idxScan(String index) throws Exception {
        return asSuperuser(su -> su.select(DSL.field(DSL.name("idx_scan"), Long.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_indexes")))
            .where(DSL.field(DSL.name("indexrelname"), String.class).eq(index)).fetchOne(0, Long.class));
    }

    @Test
    void whichByIdQueryShapesUseTheIdPredicatePartialIndex() throws Exception {
        String tenant = newTenant("shapes");
        long id = collection(tenant, name("shapes"), 400);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        String index = PciCatalog.indexNameById(M384, tenant, id);
        assertThat(validIdIndexed(tenant)).as("non-vacuity: the id index exists and is valid").containsExactly(id);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-id-index-plan-shapes.xml",
                "databasechangelog_test_id_shapes", java.util.Map.of("svcRole", PgContainerHelper.SVC_USERNAME));
        }
        java.util.Map<String, String> plans = new java.util.LinkedHashMap<>();
        try (Connection svc = svcDs.getConnection()) {
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            for (String shape : List.of("a", "b", "c", "d")) {
                plans.put(shape, ctx.select(DSL.function(DSL.name("nexus", "test_id_shape_plan"), SQLDataType.CLOB, DSL.val(shape),
                    DSL.val(tenant), DSL.val(M384), DSL.val(id), DSL.val(vectorText()))).fetchOne(0, String.class));
            }
        }
        // plpgsql: eight calls on one session, the index's idx_scan read after each. The plan cache keeps a custom plan for the first five calls and
        // may then adopt a generic one, which cannot prove the statement implies the partial index's predicate.
        java.util.Map<String, String> perCall = new java.util.LinkedHashMap<>();
        for (String mode : List.of("direct", "execute")) {
            try (Connection svc = svcDs.getConnection()) {
                svc.setAutoCommit(true);
                DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
                StringBuilder used = new StringBuilder();
                for (int call = 1; call <= 8; call++) {
                    long before = idxScan(index);
                    ctx.select(DSL.function(DSL.name("nexus", "test_id_shape_call"), SQLDataType.BIGINT, DSL.val(mode), DSL.val(tenant),
                        DSL.val(M384), DSL.val(id), DSL.val(vectorText()))).fetchOne(0, Long.class);
                    used.append(idxScan(index) > before ? 'I' : '-');
                }
                perCall.put(mode, used.toString());
            }
        }
        String own = "Index Scan using " + leaf(tenant).name() + "_embedding_384_idx";
        // Measured on PG 17 with enable_seqscan, enable_bitmapscan and enable_sort off (see the changelog), as nexus_svc with the tenant qual, 400 rows.
        assertThat(plans.get("a")).as("(a) collection_id = 42, an int4 literal: the partial index serves it").contains("Index Scan using " + index);
        assertThat(plans.get("b")).as("(b) collection_id = 42::bigint").contains("Index Scan using " + index);
        assertThat(plans.get("c")).as("(c) PREPAREd $1 bigint, force_generic_plan: the planner cannot prove the predicate, so the leaf's own HNSW index serves it")
            .doesNotContain(index).contains(own);
        assertThat(plans.get("d")).as("(d) the same, force_custom_plan: the partial index again").contains("Index Scan using " + index);
        assertThat(perCall.get("direct")).as("(e) a plpgsql variable used directly: every call, the sixth to the eighth included (I = the partial index served the call)")
            .isEqualTo("IIIIIIII");
        assertThat(perCall.get("execute")).as("(f) EXECUTE format('... = %L', v): every call").isEqualTo("IIIIIIII");
    }
}
