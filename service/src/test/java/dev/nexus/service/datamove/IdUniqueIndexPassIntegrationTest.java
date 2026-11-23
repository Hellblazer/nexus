// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.LoggerFactory;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Function;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): {@link IdUniqueIndexPass}, its id-keyed UNIQUE arbiters and the id twins' plain supporting indexes, built at run time
 * after gate 2. Real PostgreSQL, the production pass on the admin connection.
 */
class IdUniqueIndexPassIntegrationTest extends DataMoveIntegrationBase {

    private static final String TENANT = "p34-iduq";

    private static final Table<?> TAXONOMY_META = table("nexus", "taxonomy_meta");
    private static final Table<?> PDF_PIPELINE = table("nexus", "pdf_pipeline");
    private static final Table<?> REEMBED_JOBS = table("nexus", "reembed_jobs");
    private static final Field<String> COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final Field<String> SOURCE_COLLECTION = DSL.field(DSL.name("source_collection"), String.class);

    private static Field<Object> column(String name) {
        return DSL.field(DSL.name(name));
    }

    @org.junit.jupiter.api.BeforeEach
    void theWaitedChangesetsHaveRun() {
        IdIndexCatalogReads.recordWaitedChangesets(ctx());   // the state after P3.5b's form-A boot: every twin may be built
    }

    @AfterEach
    void dropTheIndexesAndRows() throws Exception {
        IdIndexCatalogReads.forgetWaitedChangesets(ctx());
        asReplica(d -> {
            // jOOQ renders DROP INDEX without the schema of its name (an index is dropped by its own name), so the schema goes on the path
            d.set(DSL.name("search_path"), DSL.inline("nexus")).execute();
            for (IdUniqueIndexPass.Spec s : IdUniqueIndexPass.SPECS) {
                d.dropIndexIfExists(DSL.name(s.index())).execute();
                assertThat(PgCatalogProbes.indexExists(d, "nexus", s.index())).as("%s is dropped", s.index()).isFalse();
            }
            for (Table<?> t : List.of(TAXONOMY_META, PDF_PIPELINE, REEMBED_JOBS)) {
                d.deleteFrom(t).where(TENANT_ID.eq(TENANT)).execute();
            }
            return 0;
        });
    }

    /** Runs {@code work} on a fresh superuser connection, which ends with the call. */
    private <T> T asSuperuser(Function<DSLContext, T> work) throws Exception {
        try (Connection conn = pg.createConnection("")) {
            conn.setAutoCommit(true);
            return work.apply(DSL.using(conn, SQLDialect.POSTGRES));
        }
    }

    /** As {@link #asSuperuser}, with triggers and foreign keys off (session_replication_role replica); unique indexes still hold. */
    private <T> T asReplica(Function<DSLContext, T> work) throws Exception {
        return asSuperuser(d -> {
            d.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            return work.apply(d);
        });
    }

    private static Field<Object> advisoryLock(String function) {
        return DSL.function(function, SQLDataType.OTHER, DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY));
    }

    /** While a schema migration holds its advisory lock the pass builds nothing and leaves its entry for the next tick. */
    @Test
    void whileAMigrationHoldsItsLockThePassStandsDown() throws Exception {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        try (Connection walk = pg.createConnection("")) {
            var walkCtx = DSL.using(walk, SQLDialect.POSTGRES);
            walkCtx.select(advisoryLock("pg_advisory_lock")).fetch();
            var r = pass(registry, true).runOnce();
            assertThat(r.built()).as("nothing built under a migration").isZero();
            assertThat(registry.snapshot()).as("the work is still reported, not finished")
                .anyMatch(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID));
            walkCtx.select(advisoryLock("pg_advisory_unlock")).fetch();
        }
        assertThat(pass(registry, true).runOnce().built()).as("after it, the next tick builds").isEqualTo(applicable());
        assertThat(registry.snapshot()).noneMatch(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID));
    }

    // ── the target-less ON CONFLICT DO NOTHING writers (linda's ruling 2026-11-22) ─────────────────────────────

    /**
     * The state after P3.7's one-row rename, made directly: the row written under the old label keeps it, and the same row arrives under the
     * new label with the same collection id (P3.1's dual write stamps the id from the registry, which now has the new label). The insert is
     * PipelineRepository#create's, ON CONFLICT DO NOTHING with no target, which arbitrates on every unique index. Without the id twin it makes a
     * second pipeline row for the same document (a re-ingest); with it, none.
     */
    @Test
    void aPipelineRowWrittenAgainUnderTheNewLabelIsNotDuplicatedOnceTheTwinExists() throws Exception {
        assertThat(pipelineRow("docs__p34-old")).isEqualTo(1);
        assertThat(pipelineRow("docs__p34-new")).as("control: without the twin the new label makes a second row").isEqualTo(1);
        asReplica(d -> d.deleteFrom(PDF_PIPELINE).where(TENANT_ID.eq(TENANT)).and(COLLECTION.eq("docs__p34-new")).execute());

        pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(pipelineRow("docs__p34-new")).as("with the twin: no second row").isZero();
        assertThat(ctx().fetchCount(DSL.selectOne().from(PDF_PIPELINE).where(TENANT_ID.eq(TENANT)))).isEqualTo(1);
    }

    /** PipelineRepository#create's insert: ON CONFLICT DO NOTHING with no target, so it arbitrates on every unique index. Rows written: 0 or 1. */
    private int pipelineRow(String label) throws Exception {
        return asReplica(d -> d.insertInto(PDF_PIPELINE)
            .columns(column("tenant_id"), column("content_hash"), column("pdf_path"), column("collection"), column("keyed_by"),
                column("status"), column("started_at"), column("updated_at"), column("collection_id"))
            .values(DSL.inline(TENANT), DSL.inline("h1"), DSL.inline("/a.pdf"), DSL.inline(label), DSL.inline("document"),
                DSL.inline("running"), DSL.currentTimestamp(), DSL.currentTimestamp(), DSL.inline(990101L))
            .onConflictDoNothing().execute());
    }

    /** The same for reembed_jobs: a second active job for one collection under its new label is refused by the twin's partial index. */
    @Test
    void aReembedJobStartedAgainUnderTheNewLabelIsNotDuplicatedOnceTheTwinExists() throws Exception {
        assertThat(reembedJob("code__p34-old")).isEqualTo(1);
        assertThat(reembedJob("code__p34-new")).as("control: without the twin a second active job starts").isEqualTo(1);
        asReplica(d -> d.deleteFrom(REEMBED_JOBS).where(TENANT_ID.eq(TENANT)).and(SOURCE_COLLECTION.eq("code__p34-new")).execute());

        pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(reembedJob("code__p34-new")).as("with the twin: no second active job").isZero();
        assertThat(ctx().fetchCount(DSL.selectOne().from(REEMBED_JOBS).where(TENANT_ID.eq(TENANT)))).isEqualTo(1);
    }

    /**
     * A second active job for one collection id, as a target-less ON CONFLICT DO NOTHING insert: it shows the twin refusing the row at the
     * index. ReembedJobRepository#start itself is a plain INSERT whose 23505 on either lock becomes the typed refusal
     * (ReembedJobRepositoryTest#aStartUnderARenamedLabel_collidingOnTheIdTwin_isRefusedNamingTheHolder). Rows written: 0 or 1.
     */
    private int reembedJob(String sourceLabel) throws Exception {
        return asReplica(d -> d.insertInto(REEMBED_JOBS)
            .columns(column("tenant_id"), column("job_id"), column("source_collection"), column("target_collection"), column("state"),
                column("started_at"), column("updated_at"), column("source_collection_id"), column("target_collection_id"))
            .values(DSL.inline(TENANT), DSL.function(DSL.name("gen_random_uuid"), SQLDataType.UUID), DSL.inline(sourceLabel),
                DSL.inline("code__p34-target"), DSL.inline("planned"), DSL.currentTimestamp(), DSL.currentTimestamp(),
                DSL.inline(990201L), DSL.inline(990202L))
            .onConflictDoNothing().execute());
    }

    /** How many of the pass's indexes apply here: the retired queue's does not where its table is gone (a fresh walk). */
    private int applicable() {
        return (int) IdUniqueIndexPass.SPECS.stream().filter(s -> !s.optional() || IdUniqueIndexPass.applicable(ctx(), s)).count();
    }

    private IdUniqueIndexPass pass(BootWorkRegistry registry, boolean gate2) {
        return new IdUniqueIndexPass(adminConnection, registry, () -> gate2);
    }

    /**
     * linda 2026-11-22: the canary twin waits for vectors-078-8, the form-A redefinition of canary_query_ring_upsert, to be recorded; built
     * before it, two writers racing on one digest under the old body (ON CONFLICT on the name key) can raise a plain 23505 on the twin.
     * Until it is recorded the twin is not built, every other index is, and gate 3's read stays false; once recorded, the next pass builds it.
     */
    @Test
    void theCanaryTwinWaitsForItsWritersChangeset_andGate3WithIt() {
        IdIndexCatalogReads.forgetWaitedChangesets(ctx());
        String canary = "idx_canary_query_ring_id_digest_uq";
        assertThat(ArbiterTwins.WAITS_FOR_CHANGESETS).as("the checked map names the canary twin").containsKey(canary);

        var registry = new BootWorkRegistry(Clock.systemUTC());
        var first = pass(registry, true).runOnce();
        assertThat(PgCatalogProbes.indexExists(ctx(), "nexus", canary)).as("not built before vectors-078-8 is recorded").isFalse();
        assertThat(first.built()).as("every other applicable index is").isEqualTo(applicable() - 1);
        assertThat(IdUniqueIndexPass.allValid(ctx())).as("gate 3 waits with it").isFalse();
        assertThat(IdUniqueIndexPass.waitingChangesets(ctx())).as("the wait is named, for gate3_why").containsExactly("vectors-078-8");
        assertThat(registry.snapshot()).as("and reported in boot_work, not finished")
            .filteredOn(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID)).singleElement()
            .satisfies(e -> assertThat(e.what()).contains(canary).contains("vectors-078-8"));

        IdIndexCatalogReads.recordWaitedChangesets(ctx());
        var second = pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(second.built()).as("once recorded, the next pass builds the canary twin").isEqualTo(1);
        assertThat(IdUniqueIndexPass.allValid(ctx())).isTrue();
        assertThat(IdUniqueIndexPass.waitingChangesets(ctx())).as("nothing waits once built").isEmpty();
    }

    /**
     * An index of the pass's name that stands VALID with another definition (an operator's, or an older engine's) is a named failure: the
     * pass reports it in boot_work, never drops it, never tries CREATE over it (no 42P07 every tick), and gate 3's read stays false.
     */
    @Test
    void anIndexOfOurNameWithAnotherDefinition_isReportedAndNeverRebuilt() throws Exception {
        String index = "idx_taxonomy_meta_id_uq";
        asSuperuser(d -> d.createIndex(index).on(TAXONOMY_META, DSL.field(DSL.name("tenant_id"))).execute());
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var pass = pass(registry, true);
        List<String> sent = new ArrayList<>();
        pass.beforeStatement(sent::add);

        pass.runOnce();
        assertThat(sent).as("no CREATE over it, no DROP of it").noneMatch(q -> q.contains(index));
        assertThat(PgCatalogProbes.indexExists(ctx(), "nexus", index)).as("left standing").isTrue();
        assertThat(IdUniqueIndexPass.allValid(ctx())).as("gate 3 stays false").isFalse();
        assertThat(registry.snapshot()).filteredOn(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID)).singleElement()
            .satisfies(e -> assertThat(e.what()).contains(index).contains("different definition"));
        sent.clear();
        pass.runOnce();
        assertThat(sent).as("and not on the next tick either").noneMatch(q -> q.contains(index));
    }

    /**
     * A build that hits its statement bound (57014) backs off instead of rebuilding on every tick. The bound is made short and a writer holds an
     * open transaction on the table, which CREATE INDEX CONCURRENTLY waits for, so the build times out; the next tick does not send it again.
     */
    @Test
    void aBuildThatTimesOut_backsOff_andIsNotRebuiltOnTheNextTick() throws Exception {
        String index = "idx_taxonomy_meta_id_uq";
        var pass = pass(new BootWorkRegistry(Clock.systemUTC()), true);
        pass.buildTimeout(java.time.Duration.ofSeconds(2));
        List<String> sent = new ArrayList<>();
        pass.beforeStatement(sent::add);
        try (java.sql.Connection writer = pg.createConnection("")) {
            writer.setAutoCommit(false);
            DSL.using(writer, SQLDialect.POSTGRES).update(TAXONOMY_META).set(TENANT_ID, TENANT_ID).where(DSL.falseCondition()).execute();
            pass.runOnce();
            writer.rollback();
        }
        assertThat(sent).as("the build was tried once").anyMatch(q -> q.contains("INDEX CONCURRENTLY " + index));
        assertThat(PgCatalogProbes.indexExists(ctx(), "nexus", index) && IdUniqueIndexPass.valid(ctx(), spec(index)))
            .as("and did not finish").isFalse();
        sent.clear();
        pass.runOnce();
        assertThat(sent).as("backed off: not sent again on the next tick").noneMatch(q -> q.contains("INDEX CONCURRENTLY " + index));
        assertThat(IdUniqueIndexPass.backoffAfter(1)).isEqualTo(java.time.Duration.ofHours(1));
        assertThat(IdUniqueIndexPass.backoffAfter(3)).isEqualTo(java.time.Duration.ofHours(4));
        assertThat(IdUniqueIndexPass.backoffAfter(10)).isEqualTo(java.time.Duration.ofHours(24));
    }

    /** A finished pass looks again after its recheck period, so an index dropped or invalidated later is built again. */
    @Test
    void aFinishedPassLooksAgain_andRebuildsAnIndexLostLater() throws Exception {
        var pass = pass(new BootWorkRegistry(Clock.systemUTC()), true);
        pass.recheckEvery(java.time.Duration.ZERO);
        pass.runOnce();
        assertThat(IdUniqueIndexPass.allValid(ctx())).isTrue();
        asSuperuser(d -> {
            d.set(DSL.name("search_path"), DSL.inline("nexus")).execute();
            return d.dropIndex(DSL.name("idx_pdf_pipeline_id_uq")).execute();
        });
        assertThat(IdUniqueIndexPass.allValid(ctx())).isFalse();
        assertThat(pass.runOnce().built()).as("the same pass, finished, rebuilds it on its next look").isEqualTo(1);
        assertThat(IdUniqueIndexPass.allValid(ctx())).isTrue();
    }

    private static IdUniqueIndexPass.Spec spec(String index) {
        return IdUniqueIndexPass.SPECS.stream().filter(s -> s.index().equals(index)).findFirst().orElseThrow();
    }

    @Test
    void beforeGate2NothingIsBuilt() {
        var r = pass(new BootWorkRegistry(Clock.systemUTC()), false).runOnce();
        assertThat(r.ran()).isFalse();
        for (IdUniqueIndexPass.Spec s : IdUniqueIndexPass.SPECS) {
            assertThat(PgCatalogProbes.indexDefs(ctx(), "nexus", s.table())).as(s.table()).noneMatch(d -> d.contains(s.index()));
        }
        assertThat(IdUniqueIndexPass.allValid(ctx())).isFalse();
    }

    @Test
    void afterGate2EveryApplicableIndexIsBuiltValidWithItsExactDefinition() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var r = pass(registry, true).runOnce();

        assertThat(r.ran()).isTrue();
        assertThat(r.built()).isEqualTo(applicable());
        assertThat(r.corrupt()).isEmpty();
        assertThat(IdUniqueIndexPass.allValid(ctx())).as("gate 3's read").isTrue();
        assertThat(PgCatalogProbes.indexDef(ctx(), "nexus", "idx_topics_root_label_id_uq"))
            .as("the upsert's predicate exactly, or ON CONFLICT cannot infer it")
            .startsWith("CREATE ")
            .endsWith("UNIQUE INDEX idx_topics_root_label_id_uq ON nexus.topics USING btree (tenant_id, collection_id, label) WHERE (parent_id IS NULL)");
        assertThat(PgCatalogProbes.indexDef(ctx(), "nexus", "idx_catalog_chunks_collection_id"))
            .as("the id twin of idx_catalog_chunks_collection, a full index, which gate 3 requires")
            .startsWith("CREATE ")
            .endsWith("INDEX idx_catalog_chunks_collection_id ON nexus.catalog_document_chunks USING btree (tenant_id, collection_id)")
            .doesNotContain("UNIQUE").doesNotContain("WHERE");
        assertThat(registry.snapshot()).as("the entry is gone once the work is done").noneMatch(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID));
        assertThat(pass(registry, true).runOnce().built()).as("a second pass builds nothing").isZero();
    }

    /** The state a killed build leaves (an INVALID index of the name) is dropped and built again. */
    @Test
    void anInvalidLeftoverIsDroppedAndRebuilt() throws Exception {
        pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        asSuperuser(d -> d.update(table("pg_catalog", "pg_index")).set(DSL.field(DSL.name("indisvalid"), Boolean.class), false)
            .where(column("indexrelid").eq(DSL.function("to_regclass", SQLDataType.OTHER, DSL.val("nexus.idx_taxonomy_meta_id_uq"))))
            .execute());
        assertThat(IdUniqueIndexPass.allValid(ctx())).isFalse();

        var r = pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(r.dropped()).isEqualTo(1);
        assertThat(r.built()).isEqualTo(1);
        assertThat(IdUniqueIndexPass.allValid(ctx())).isTrue();
    }

    /**
     * The planted corruption: two taxonomy_meta rows under different names with one collection id (the name-keyed primary key does not see
     * it). The build hits 23505; the pass drops the INVALID leftover, builds the other three, keeps a boot_work entry that says it is
     * corruption and that the rename stays on its name path, and does not try that build again in this engine's life. Gate 3's read stays
     * false. Once the rows are repaired, a new engine's pass builds it.
     */
    @Test
    void aDuplicateOnTheIdIsReportedAsCorruptionOnceAndNeverLooped() throws Exception {
        // replica: no trigger rewrites the id, no foreign key checks the plant
        asReplica(d -> d.insertInto(TAXONOMY_META).columns(column("tenant_id"), column("collection"), column("collection_id"))
            .values(DSL.inline(TENANT), DSL.inline("p34-a"), DSL.inline(990001L))
            .values(DSL.inline(TENANT), DSL.inline("p34-b"), DSL.inline(990001L)).execute());
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var pass = pass(registry, true);
        List<String> sent = new ArrayList<>();
        pass.beforeStatement(sent::add);

        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(IdUniqueIndexPass.class);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        IdUniqueIndexPass.Result first;
        try {
            first = pass.runOnce();
        } finally {
            logger.detachAppender(appender);
        }
        assertThat(appender.list).map(ILoggingEvent::getFormattedMessage).as("the duplicate keys are in the log, read by the report query")
            .anyMatch(m -> m.contains("event=id_unique_index_duplicate") && m.contains("table=taxonomy_meta")
                && m.contains(TENANT + "/990001 x2"));

        assertThat(first.corrupt()).containsExactly("taxonomy_meta");
        assertThat(first.built()).as("every other applicable one").isEqualTo(applicable() - 1);
        assertThat(PgCatalogProbes.indexDefs(ctx(), "nexus", "taxonomy_meta")).as("no INVALID leftover stays")
            .noneMatch(d -> d.contains("idx_taxonomy_meta_id_uq"));
        assertThat(IdUniqueIndexPass.allValid(ctx())).as("gate 3 stays false").isFalse();
        assertThat(registry.snapshot()).filteredOn(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID)).singleElement()
            .satisfies(e -> assertThat(e.what()).contains("taxonomy_meta").contains("corrupt").contains("fourteen-table name path")
                .doesNotContain(TENANT).doesNotContain("990001"));

        sent.clear();
        var second = pass.runOnce();
        assertThat(second.corrupt()).containsExactly("taxonomy_meta");
        assertThat(sent).as("not built again in this engine's life").noneMatch(s -> s.contains("idx_taxonomy_meta_id_uq"));

        asReplica(d -> d.deleteFrom(TAXONOMY_META).where(TENANT_ID.eq(TENANT)).and(COLLECTION.eq("p34-b")).execute());
        var nextEngine = pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(nextEngine.built()).as("repaired, the next engine's pass builds it").isEqualTo(1);
        assertThat(IdUniqueIndexPass.allValid(ctx())).isTrue();
    }
}
