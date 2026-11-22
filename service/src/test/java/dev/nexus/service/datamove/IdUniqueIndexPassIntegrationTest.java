// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.db.SchemaMigrator;
import org.slf4j.LoggerFactory;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.Statement;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): {@link IdUniqueIndexPass}, its id-keyed UNIQUE arbiters and the id twins' plain supporting indexes, built at run time
 * after gate 2. Real PostgreSQL, the production pass on the admin connection.
 */
class IdUniqueIndexPassIntegrationTest extends DataMoveIntegrationBase {

    private static final String TENANT = "p34-iduq";

    @AfterEach
    void dropTheIndexesAndRows() throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            su.setAutoCommit(true);
            for (IdUniqueIndexPass.Spec s : IdUniqueIndexPass.SPECS) {
                st.execute("DROP INDEX IF EXISTS nexus." + s.index());
            }
            st.execute("SET session_replication_role = replica");
            st.execute("DELETE FROM nexus.taxonomy_meta WHERE tenant_id = '" + TENANT + "'");
            st.execute("DELETE FROM nexus.pdf_pipeline WHERE tenant_id = '" + TENANT + "'");
            st.execute("DELETE FROM nexus.reembed_jobs WHERE tenant_id = '" + TENANT + "'");
        }
    }

    /** Runs {@code sql} as the superuser with triggers and foreign keys off (session_replication_role replica); unique indexes still hold. */
    private int plain(String sql) throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("SET session_replication_role = replica");
            return st.executeUpdate(sql);
        }
    }

    /** While a schema migration holds its advisory lock the pass builds nothing and leaves its entry for the next tick. */
    @Test
    void whileAMigrationHoldsItsLockThePassStandsDown() throws Exception {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        try (Connection walk = pg.createConnection(""); Statement st = walk.createStatement()) {
            st.execute("SELECT pg_advisory_lock(" + SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY + ")");
            var r = pass(registry, true).runOnce();
            assertThat(r.built()).as("nothing built under a migration").isZero();
            assertThat(registry.snapshot()).as("the work is still reported, not finished")
                .anyMatch(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID));
            st.execute("SELECT pg_advisory_unlock(" + SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY + ")");
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
        String first = "INSERT INTO nexus.pdf_pipeline (tenant_id, content_hash, pdf_path, collection, keyed_by, status, started_at, updated_at,"
            + " collection_id) VALUES ('" + TENANT + "', 'h1', '/a.pdf', '%s', 'document', 'running', now(), now(), 990101) ON CONFLICT DO NOTHING";
        assertThat(plain(first.formatted("docs__p34-old"))).isEqualTo(1);
        assertThat(plain(first.formatted("docs__p34-new"))).as("control: without the twin the new label makes a second row").isEqualTo(1);
        plain("DELETE FROM nexus.pdf_pipeline WHERE tenant_id = '" + TENANT + "' AND collection = 'docs__p34-new'");

        pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(plain(first.formatted("docs__p34-new"))).as("with the twin: no second row").isZero();
        assertThat(ctx().fetchCount(ctx().selectFrom("nexus.pdf_pipeline").where("tenant_id = ?", TENANT))).isEqualTo(1);
    }

    /** The same for reembed_jobs: a second active job for one collection under its new label is refused by the twin's partial index. */
    @Test
    void aReembedJobStartedAgainUnderTheNewLabelIsNotDuplicatedOnceTheTwinExists() throws Exception {
        String job = "INSERT INTO nexus.reembed_jobs (tenant_id, job_id, source_collection, target_collection, state, started_at, updated_at,"
            + " source_collection_id, target_collection_id) VALUES ('" + TENANT + "', gen_random_uuid(), '%s', 'code__p34-target', 'planned',"
            + " now(), now(), 990201, 990202) ON CONFLICT DO NOTHING";
        assertThat(plain(job.formatted("code__p34-old"))).isEqualTo(1);
        assertThat(plain(job.formatted("code__p34-new"))).as("control: without the twin a second active job starts").isEqualTo(1);
        plain("DELETE FROM nexus.reembed_jobs WHERE tenant_id = '" + TENANT + "' AND source_collection = 'code__p34-new'");

        pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(plain(job.formatted("code__p34-new"))).as("with the twin: no second active job").isZero();
        assertThat(ctx().fetchCount(ctx().selectFrom("nexus.reembed_jobs").where("tenant_id = ?", TENANT))).isEqualTo(1);
    }

    /** How many of the pass's indexes apply here: the retired queue's does not where its table is gone (a fresh walk). */
    private int applicable() {
        return (int) IdUniqueIndexPass.SPECS.stream().filter(s -> !s.optional() || IdUniqueIndexPass.applicable(ctx(), s)).count();
    }

    private IdUniqueIndexPass pass(BootWorkRegistry registry, boolean gate2) {
        return new IdUniqueIndexPass(adminConnection, registry, () -> gate2);
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
            .isEqualTo("CREATE UNIQUE INDEX idx_topics_root_label_id_uq ON nexus.topics USING btree (tenant_id, collection_id, label) WHERE (parent_id IS NULL)");
        assertThat(registry.snapshot()).as("the entry is gone once the work is done").noneMatch(e -> e.id().equals(IdUniqueIndexPass.BOOT_WORK_ID));
        assertThat(pass(registry, true).runOnce().built()).as("a second pass builds nothing").isZero();
    }

    /** The state a killed build leaves (an INVALID index of the name) is dropped and built again. */
    @Test
    void anInvalidLeftoverIsDroppedAndRebuilt() throws Exception {
        pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("UPDATE pg_catalog.pg_index SET indisvalid = false WHERE indexrelid = 'nexus.idx_taxonomy_meta_id_uq'::regclass");
        }
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
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("SET session_replication_role = replica");   // no trigger rewrites the id, no foreign key checks the plant
            st.execute("INSERT INTO nexus.taxonomy_meta (tenant_id, collection, collection_id) VALUES ('" + TENANT + "', 'p34-a', 990001), ('"
                + TENANT + "', 'p34-b', 990001)");
        }
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

        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("SET session_replication_role = replica");
            st.execute("DELETE FROM nexus.taxonomy_meta WHERE tenant_id = '" + TENANT + "' AND collection = 'p34-b'");
        }
        var nextEngine = pass(new BootWorkRegistry(Clock.systemUTC()), true).runOnce();
        assertThat(nextEngine.built()).as("repaired, the next engine's pass builds it").isEqualTo(1);
        assertThat(IdUniqueIndexPass.allValid(ctx())).isTrue();
    }
}
