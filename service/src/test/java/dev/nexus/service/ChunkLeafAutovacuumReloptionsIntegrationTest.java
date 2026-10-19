// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-s9wav (4lnn1 R1): every leaf of {@code nexus.chunks} carries the four autovacuum reloptions Sam
 * decided on 2026-10-18, in both postures a leaf is born in (the walk's, repaired by the runAlways self-heal
 * {@code vectors-034-2}; the trigger's and {@code create_model_partition}'s, set at creation by
 * {@code partition_ensure_leaf}, redefined in {@code vectors-034-1}).
 *
 * <p>PostgreSQL 17 refuses storage parameters on a partitioned table and autovacuum visits leaves only, so the
 * parent and the model partitions carry none; {@link #theParentRefusesStorageParametersItself} pins that premise
 * rather than trusting the docs. {@code taxonomy_centroids} is deliberately untouched.
 *
 * <p>A dedicated container, migrated by a non-superuser owner (production's nexus_admin shape): the leaves are
 * owned by that role, and a superuser-migrated cluster would prove nothing about who may alter them. The
 * "touches only what needs it" claim is measured with an event trigger that logs every {@code ALTER TABLE} on a
 * chunks leaf, with a control arm (a stripped leaf IS logged) so a trigger that never fires cannot pass.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkLeafAutovacuumReloptionsIntegrationTest {

    private static final String ADMIN = "nexus_admin_s9wav";
    private static final String ADMIN_PASS = "nexus_admin_s9wav_pass";

    /** Sam's ruling, 2026-10-18 (T2 nexus/4lnn1-decisions-2026-10-18). Written out again here on purpose. */
    private static final Set<String> FOUR = new TreeSet<>(List.of(
        "autovacuum_vacuum_scale_factor=0.05",
        "autovacuum_vacuum_threshold=1000",
        "autovacuum_vacuum_insert_scale_factor=0.05",
        "autovacuum_analyze_scale_factor=0.02"));

    private static final Table<?> PG_CLASS = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
    private static final Table<?> PG_NAMESPACE = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
    private static final Field<String[]> RELOPTIONS = DSL.field(DSL.name("c", "reloptions"), String[].class);
    private static final Table<?> ALTER_LOG = DSL.table(DSL.name("public", "s9wav_alters"));
    private static final Field<String> LOGGED = DSL.field(DSL.name("public", "s9wav_alters", "obj"), String.class);

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(3);
        adminDs = new HikariDataSource(cfg);
        SchemaMigrator.migrate(adminDs);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // SECURITY DEFINER: the migrating role may not insert into this table of the superuser's.
            PgContainerHelper.runSuperuserDdl(su, """
                CREATE TABLE public.s9wav_alters (tag text, obj text);
                CREATE FUNCTION public.s9wav_log_alter() RETURNS event_trigger
                    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
                BEGIN
                    INSERT INTO public.s9wav_alters
                    SELECT d.command_tag, d.object_identity FROM pg_event_trigger_ddl_commands() d
                     WHERE d.command_tag = 'ALTER TABLE' AND d.object_identity LIKE 'nexus.chunks\\_m%';
                END
                $f$;
                CREATE EVENT TRIGGER s9wav_log_alter ON ddl_command_end EXECUTE FUNCTION public.s9wav_log_alter();
                """);
        }
    }

    @AfterAll
    void stopAll() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private Connection admin() throws Exception {
        return DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASS);
    }

    /** pg_class.reloptions of a nexus relation as a set; empty when the relation has none. */
    private static Set<String> reloptions(DSLContext ctx, String rel) {
        Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
        String[] opts = ctx.select(RELOPTIONS).from(PG_CLASS)
            .join(PG_NAMESPACE).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(relname.eq(rel))
            .fetchOne(RELOPTIONS);
        return opts == null ? new TreeSet<>() : new TreeSet<>(Arrays.asList(opts));
    }

    /** The model partitions of a parent, then every leaf under them: {@code [modelPartition, leaf]} pairs. */
    private static List<String[]> leavesOf(DSLContext ctx, String parent) {
        List<String[]> out = new ArrayList<>();
        for (var mp : PartitionScratch.children(ctx, parent)) {
            for (var leaf : PartitionScratch.children(ctx, mp.name())) {
                out.add(new String[] {mp.name(), leaf.name()});
            }
        }
        return out;
    }

    private static List<String> loggedAlters(DSLContext ctx) {
        return ctx.select(LOGGED).from(ALTER_LOG).orderBy(LOGGED).fetch(LOGGED);
    }

    private void clearAlterLog() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.runSuperuserDdl(su, "TRUNCATE public.s9wav_alters");
        }
    }

    private void suDdl(String ddl) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.runSuperuserDdl(su, ddl);
        }
    }

    @SuppressWarnings("deprecation")
    private static Boolean setLeaf(DSLContext ctx, String leaf) {
        return dev.nexus.service.jooq.nexus.Routines.partitionSetLeafAutovacuum(ctx.configuration(), "nexus." + leaf);
    }

    // ── a full migrate ───────────────────────────────────────────────────────

    @Test
    void afterAFullMigrate_everyChunksLeafCarriesExactlyTheFour_andNoPartitionedRelationCarriesAny() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            var leaves = leavesOf(ctx, "chunks");
            // Non-vacuity: at least the four real models, each with at least the default tenant's leaf.
            assertThat(PartitionScratch.children(ctx, "chunks")).as("model partitions").hasSizeGreaterThanOrEqualTo(4);
            assertThat(leaves).as("chunks leaves examined").hasSizeGreaterThanOrEqualTo(4);
            for (String[] l : leaves) {
                assertThat(reloptions(ctx, l[1])).as("reloptions of %s", l[1]).isEqualTo(FOUR);
            }
            assertThat(reloptions(ctx, "chunks")).as("the partitioned parent").isEmpty();
            for (var mp : PartitionScratch.children(ctx, "chunks")) {
                assertThat(reloptions(ctx, mp.name())).as("model partition %s", mp.name()).isEmpty();
            }
            // taxonomy_centroids is out of scope: its leaves keep PostgreSQL's defaults.
            var centroidLeaves = leavesOf(ctx, "taxonomy_centroids");
            assertThat(centroidLeaves).as("taxonomy_centroids leaves examined").hasSizeGreaterThanOrEqualTo(4);
            for (String[] l : centroidLeaves) {
                assertThat(reloptions(ctx, l[1])).as("taxonomy_centroids leaf %s", l[1]).isEmpty();
            }
            System.out.println("S9WAV evidence: chunks leaves examined " + leaves.size() + ", all carry the four; model partitions "
                + PartitionScratch.children(ctx, "chunks").size() + " and the parent carry none; taxonomy_centroids leaves examined "
                + centroidLeaves.size() + ", none touched");
        }
    }

    @Test
    void theParentRefusesStorageParametersItself() throws Exception {
        assertThatThrownBy(() -> suDdl("ALTER TABLE nexus.chunks SET (autovacuum_vacuum_scale_factor = 0.05)"))
            .hasStackTraceContaining("partitioned table");
        try (Connection su = pg.createConnection("")) {
            assertThat(reloptions(dsl(su), "chunks")).isEmpty();
        }
    }

    // ── a leaf born after the migrate ────────────────────────────────────────

    @Test
    void aNewTenantsLeavesCarryTheFourTheMomentTheyExist_andTheCentroidLeavesDoNot() throws Exception {
        String tenant = "s9wav-new-tenant";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(dsl(su), "tok-s9wav-new", tenant, "s9wav-new");   // the trigger makes the leaves
            DSLContext ctx = dsl(su);
            int seen = 0;
            for (var mp : PartitionScratch.children(ctx, "chunks")) {
                String leaf = PartitionScratch.expectedName("chunks", modelOf(mp.bound()), tenant);
                assertThat(PartitionScratch.children(ctx, mp.name()).stream().map(PartitionScratch.Child::name))
                    .as("the trigger made the tenant's leaf under %s", mp.name()).contains(leaf);
                assertThat(reloptions(ctx, leaf)).as("new leaf %s", leaf).isEqualTo(FOUR);
                assertThat(PartitionScratch.owner(ctx, leaf)).as("owner of %s", leaf).isEqualTo(ADMIN);
                seen++;
            }
            assertThat(seen).as("new leaves examined: one per model partition").isEqualTo(PartitionScratch.children(ctx, "chunks").size())
                .isGreaterThanOrEqualTo(4);
            for (var mp : PartitionScratch.children(ctx, "taxonomy_centroids")) {
                String leaf = PartitionScratch.expectedName("taxonomy_centroids", modelOf(mp.bound()), tenant);
                assertThat(reloptions(ctx, leaf)).as("new centroid leaf %s", leaf).isEmpty();
            }
        }
    }

    @Test
    void aNewModelPartitionsLeavesCarryTheFour_forTheDefaultTenantAndEveryTokenTenant() throws Exception {
        String model = "s9wav-added-1024";
        String tenant = "s9wav-model-tenant";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(dsl(su), "tok-s9wav-model", tenant, "s9wav-model");
            dsl(su).insertInto(EMBEDDING_MODELS, EMBEDDING_MODELS.EMBEDDING_MODEL, EMBEDDING_MODELS.DIMENSION, EMBEDDING_MODELS.PROVIDER)
                .values(model, 1024, "voyage").execute();
        }
        // create_model_partition runs as its caller, the schema owner, as the changesets do.
        String partition;
        try (Connection a = admin()) {
            a.setAutoCommit(true);
            partition = PartitionScratch.createModelPartition(dsl(a), "chunks", model, true);
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            var leaves = PartitionScratch.children(ctx, PartitionScratch.partitionName(ctx, "chunks", model, null));
            assertThat(partition).contains(PartitionScratch.partitionName(ctx, "chunks", model, null));
            assertThat(leaves.stream().map(PartitionScratch.Child::name))
                .as("leaves of the new model: default and both token tenants of this class")
                .contains(PartitionScratch.expectedName("chunks", model, "default"),
                          PartitionScratch.expectedName("chunks", model, tenant));
            for (var leaf : leaves) {
                assertThat(reloptions(ctx, leaf.name())).as("leaf %s of the new model", leaf.name()).isEqualTo(FOUR);
            }
            assertThat(reloptions(ctx, PartitionScratch.partitionName(ctx, "chunks", model, null)))
                .as("the new model partition itself").isEmpty();
            System.out.println("S9WAV evidence: new model " + model + " got " + leaves.size() + " leaves, all carry the four");
        }
    }

    // ── the self-heal ────────────────────────────────────────────────────────

    @Test
    void theSelfHealRestoresAStrippedAndAWrongValuedLeaf_touchesNoOther_andASecondMigrateIsANoOp() throws Exception {
        String stripped;
        String wrong;
        Set<String> before = new TreeSet<>();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            var leaves = leavesOf(ctx, "chunks");
            stripped = leaves.get(0)[1];
            wrong = leaves.get(leaves.size() - 1)[1];
            assertThat(wrong).as("two different leaves").isNotEqualTo(stripped);
            for (String[] l : leaves) before.add(l[1] + " " + reloptions(ctx, l[1]));
        }
        suDdl("ALTER TABLE nexus." + stripped + " RESET (autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold, "
            + "autovacuum_vacuum_insert_scale_factor, autovacuum_analyze_scale_factor);"
            + "ALTER TABLE nexus." + wrong + " SET (autovacuum_vacuum_scale_factor = 0.2)");
        try (Connection su = pg.createConnection("")) {
            assertThat(reloptions(dsl(su), stripped)).as("stripped, as set up").isEmpty();
            assertThat(reloptions(dsl(su), wrong)).as("wrong, as set up").contains("autovacuum_vacuum_scale_factor=0.2");
        }
        clearAlterLog();

        SchemaMigrator.migrate(adminDs);

        Set<String> after = new TreeSet<>();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            assertThat(reloptions(ctx, stripped)).as("stripped leaf healed").isEqualTo(FOUR);
            assertThat(reloptions(ctx, wrong)).as("wrong-valued leaf healed").isEqualTo(FOUR);
            // control arm: the event trigger sees an ALTER TABLE, and sees exactly the two leaves that needed one.
            assertThat(loggedAlters(ctx)).as("ALTER TABLE on chunks leaves during the heal")
                .containsExactlyInAnyOrder("nexus." + stripped, "nexus." + wrong);
            for (String[] l : leavesOf(ctx, "chunks")) after.add(l[1] + " " + reloptions(ctx, l[1]));
            assertThat(after).as("every other leaf unchanged, the two healed now equal to the rest").hasSize(before.size());
        }
        clearAlterLog();

        SchemaMigrator.migrate(adminDs);

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            assertThat(loggedAlters(ctx)).as("a second migrate alters no chunks leaf").isEmpty();
            int examined = 0;
            for (String[] l : leavesOf(ctx, "chunks")) {
                assertThat(setLeaf(ctx, l[1])).as("the helper finds nothing to do on %s", l[1]).isFalse();
                examined++;
            }
            assertThat(loggedAlters(ctx)).as("the helper alters nothing either").isEmpty();
            System.out.println("S9WAV evidence: heal altered exactly 2 of " + before.size() + " leaves; second migrate altered 0; helper no-op on "
                + examined + " leaves");
        }
    }

    @Test
    void theHelperRefusesAnythingThatIsNotAnOrdinaryLeaf() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            assertThatThrownBy(() -> setLeaf(ctx, "chunks")).hasStackTraceContaining("not an ordinary table");
            String modelPartition = PartitionScratch.children(ctx, "chunks").get(0).name();
            assertThatThrownBy(() -> setLeaf(ctx, modelPartition)).hasStackTraceContaining("not an ordinary table");
        }
    }

    @Test
    void theServiceRoleCannotUseTheHelperToAlterALeafItDoesNotOwn() throws Exception {
        String leaf;
        try (Connection su = pg.createConnection("")) {
            leaf = leavesOf(dsl(su), "chunks").get(0)[1];
        }
        suDdl("ALTER TABLE nexus." + leaf + " RESET (autovacuum_vacuum_threshold)");
        try (Connection svc = DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD)) {
            svc.setAutoCommit(true);
            assertThatThrownBy(() -> setLeaf(dsl(svc), leaf)).hasStackTraceContaining("must be owner");
        }
        try (Connection a = admin()) {
            a.setAutoCommit(true);
            assertThat(setLeaf(dsl(a), leaf)).as("the owner repairs it").isTrue();
            assertThat(reloptions(dsl(a), leaf)).isEqualTo(FOUR);
        }
    }

    private static String modelOf(String bound) {
        // "FOR VALUES IN ('voyage-code-3')"
        int a = bound.indexOf('\'');
        int b = bound.lastIndexOf('\'');
        return bound.substring(a + 1, b);
    }
}
