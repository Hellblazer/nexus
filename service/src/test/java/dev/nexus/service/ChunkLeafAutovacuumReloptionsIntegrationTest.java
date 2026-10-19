// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.ChunksLeafAutovacuumCheck;
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
import java.sql.SQLWarning;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import javax.xml.parsers.DocumentBuilderFactory;

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
 * <p>A clone of the owner-migrated template (nexus-0ty8z; the event trigger is database-local), migrated by a non-superuser owner (production's nexus_admin shape): the leaves are
 * owned by that role, and a superuser-migrated cluster would prove nothing about who may alter them. The walk is the
 * template's; this class tests the state it leaves and the later boots, which {@code SchemaMigrator.migrate} below runs. The
 * "touches only what needs it" claim is measured with an event trigger that logs every {@code ALTER TABLE} on a
 * chunks leaf, with a control arm (a stripped leaf IS logged) so a trigger that never fires cannot pass.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkLeafAutovacuumReloptionsIntegrationTest {

    private static final String ADMIN = PgContainerHelper.TEMPLATE_OWNER;
    private static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;

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
        pg = PgContainerHelper.startMigratedByOwner();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(3);
        adminDs = new HikariDataSource(cfg);
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

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            assertThat(reloptions(ctx, stripped)).as("stripped leaf healed").isEqualTo(FOUR);
            assertThat(reloptions(ctx, wrong)).as("wrong-valued leaf healed").isEqualTo(FOUR);
            // control arm: the event trigger sees an ALTER TABLE, and sees exactly the two leaves that needed one.
            assertThat(loggedAlters(ctx)).as("ALTER TABLE on chunks leaves during the heal")
                .containsExactlyInAnyOrder("nexus." + stripped, "nexus." + wrong);
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

    // ── contention: the self-heal never wedges a boot ─────────────────────────

    private record LockRow(String rel, int pid) {}

    private static final String SUE = "ShareUpdateExclusiveLock";
    private static final String STRIP = "RESET (autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold, "
        + "autovacuum_vacuum_insert_scale_factor, autovacuum_analyze_scale_factor)";

    /** Every {@code nexus.chunks} leaf name, sorted. */
    private List<String> allLeafNames() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return leavesOf(dsl(su), "chunks").stream().map(l -> l[1]).sorted().toList();
        }
    }

    private void stripLeaves(List<String> leaves) throws Exception {
        StringBuilder ddl = new StringBuilder();
        for (String l : leaves) ddl.append("ALTER TABLE nexus.").append(l).append(' ').append(STRIP).append(';');
        suDdl(ddl.toString());
    }

    /** ShareUpdateExclusiveLock rows on relations, granted or waiting, from pg_locks. */
    private static List<LockRow> sueLocks(DSLContext ctx, boolean granted) {
        var l = DSL.table(DSL.name("pg_catalog", "pg_locks")).as("l");
        return ctx.select(DSL.field(DSL.name("c", "relname"), String.class), DSL.field(DSL.name("l", "pid"), Integer.class))
            .from(l)
            .join(PG_CLASS).on(DSL.field(DSL.name("l", "relation")).eq(DSL.field(DSL.name("c", "oid"))))
            .where(DSL.field(DSL.name("l", "locktype"), String.class).eq("relation"))
            .and(DSL.field(DSL.name("l", "mode"), String.class).eq(SUE))
            .and(DSL.field(DSL.name("l", "granted"), Boolean.class).eq(granted))
            .fetch(r -> new LockRow(r.value1(), r.value2()));
    }

    /**
     * A superuser session in an open transaction whose own deadlock_timeout is 60 s. PostgreSQL aborts the session
     * whose deadlock check runs first; the migration's is 1 s, so with this holder the migration is always the
     * victim, whatever the scheduling, and the test never races two 1 s timers.
     */
    private Connection deadlockHolder() throws Exception {
        Connection c = pg.createConnection("");
        c.setAutoCommit(false);
        dsl(c).set(DSL.name("deadlock_timeout"), DSL.inline("60s")).execute();
        return c;
    }

    private static String outcomeOf(Future<List<String>> f) {
        try {
            return "returned " + f.get();
        } catch (Exception e) {
            return "failed with " + e;
        }
    }

    /** Every lock request that is waiting right now, for a failure message. */
    private static List<String> waitingLocks(DSLContext ctx) {
        var l = DSL.table(DSL.name("pg_catalog", "pg_locks")).as("l");
        return ctx.select(DSL.field(DSL.name("l", "locktype"), String.class), DSL.field(DSL.name("l", "mode"), String.class),
                DSL.field(DSL.name("l", "pid"), Integer.class), DSL.field(DSL.name("l", "relation")).cast(String.class))
            .from(l)
            .where(DSL.field(DSL.name("l", "granted"), Boolean.class).eq(false))
            .fetch(r -> r.value1() + "/" + r.value2() + "/pid" + r.value3() + "/rel" + r.value4());
    }

    private static int backendPid(Connection c) throws Exception {
        return c.unwrap(org.postgresql.PGConnection.class).getBackendPID();
    }

    private static void awaitTrue(String what, BooleanSupplier probe, long timeoutMs) throws Exception {
        long end = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs);
        while (System.nanoTime() < end) {
            if (probe.getAsBoolean()) return;
            Thread.sleep(50);
        }
        throw new AssertionError("timed out waiting for " + what);
    }

    /** Holds SHARE UPDATE EXCLUSIVE on a leaf in the open transaction of {@code c} (COMMENT ON TABLE takes it). */
    private static void holdLeaf(Connection c, String leaf) {
        dsl(c).commentOnTable(DSL.name("nexus", leaf)).is("s9wav lock holder").execute();
    }

    private static List<String> captureLogs(org.junit.jupiter.api.function.Executable body) throws Throwable {
        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.execute();
            return logs.list.stream().map(e -> e.getLevel() + " " + e.getFormattedMessage()).toList();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    /** The body of changeset vectors-034-2, read from the changelog the service ships. */
    private static String selfHealSql() throws Exception {
        try (var in = ChunkLeafAutovacuumReloptionsIntegrationTest.class.getClassLoader()
                .getResourceAsStream("db/changelog/vectors-034-chunks-leaf-autovacuum.xml")) {
            assertThat(in).as("the vectors-034 changelog on the classpath").isNotNull();
            var sets = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(in).getElementsByTagName("changeSet");
            for (int i = 0; i < sets.getLength(); i++) {
                var cs = (org.w3c.dom.Element) sets.item(i);
                if ("vectors-034-2".equals(cs.getAttribute("id"))) {
                    return cs.getElementsByTagName("sql").item(0).getTextContent();
                }
            }
        }
        throw new AssertionError("changeset vectors-034-2 not found");
    }

    /** Runs the self-heal DO block alone on {@code c} and returns every NOTICE/WARNING it raised. */
    private static List<String> runSelfHeal(Connection c) throws Exception {
        c.setAutoCommit(true);
        try (Statement st = c.createStatement()) {
            st.execute(selfHealSql());
            List<String> out = new ArrayList<>();
            for (SQLWarning w = st.getWarnings(); w != null; w = w.getNextWarning()) out.add(w.getMessage());
            return out;
        }
    }

    @Test
    void aLeafLockedByAnotherSession_isSkippedWithoutWedgingTheBoot_andTheNextBootHealsIt() throws Throwable {
        List<String> leaves = allLeafNames();
        assertThat(leaves).as("leaves examined").hasSizeGreaterThanOrEqualTo(4);
        String locked = leaves.get(1);
        List<String> stripped = List.of(leaves.get(0), locked, leaves.get(leaves.size() - 1));
        stripLeaves(stripped);
        clearAlterLog();

        List<String> logs;
        long millis;
        try (Connection holder = admin()) {
            holder.setAutoCommit(false);
            holdLeaf(holder, locked);
            long t0 = System.nanoTime();
            logs = captureLogs(() -> SchemaMigrator.migrate(adminDs));
            millis = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = dsl(su);
                assertThat(reloptions(ctx, locked)).as("the locked leaf is unaltered").isEmpty();
                assertThat(reloptions(ctx, leaves.get(0))).as("an unlocked stripped leaf is healed").isEqualTo(FOUR);
                assertThat(reloptions(ctx, leaves.get(leaves.size() - 1))).as("the other unlocked stripped leaf is healed").isEqualTo(FOUR);
                assertThat(loggedAlters(ctx)).as("the migrate altered exactly the two unlocked leaves")
                    .containsExactlyInAnyOrder("nexus." + leaves.get(0), "nexus." + leaves.get(leaves.size() - 1));
            }
            holder.rollback();
        }
        // Non-vacuity: the lock was really contended (the heal waited out lock_timeout = 5 s) and the post-migrate
        // check reported the skipped leaf where an operator reads it.
        assertThat(millis).as("the migrate waited out the 5 s lock_timeout on the held leaf").isGreaterThanOrEqualTo(4_500);
        assertThat(logs).as("the post-migrate check names the skipped leaf at WARN")
            .anyMatch(m -> m.startsWith("WARN ") && m.contains("event=chunks_leaf_autovacuum_checked")
                && m.contains("policy_unset=1") && m.contains(locked));

        clearAlterLog();
        List<String> second = captureLogs(() -> SchemaMigrator.migrate(adminDs));
        try (Connection su = pg.createConnection("")) {
            assertThat(reloptions(dsl(su), locked)).as("released, the next boot heals the leaf").isEqualTo(FOUR);
            assertThat(loggedAlters(dsl(su))).as("and only that leaf").containsExactly("nexus." + locked);
        }
        assertThat(second).anyMatch(m -> m.startsWith("INFO ") && m.contains("event=chunks_leaf_autovacuum_checked")
            && m.contains("policy_unset=0 autovacuum_disabled=0"));
        System.out.println("S9WAV evidence: locked-leaf migrate took " + millis + " ms, completed, locked leaf unaltered, "
            + (stripped.size() - 1) + " other stripped leaves healed; next boot healed the released leaf");
    }

    /**
     * A CREATE INDEX CONCURRENTLY holds SHARE UPDATE EXCLUSIVE on a leaf and waits for older transactions, the
     * migration's among them, while the migration waits for that leaf: a deadlock, detected after 1 s
     * (deadlock_timeout), before the 5 s lock_timeout. Built here without an index build: a session holds every
     * leaf except the one the migration alters first, and once the migration holds that one and waits for the
     * next, the session asks for it too. Two phases, because which leaf the heal alters first is the database's
     * ordering: phase 1 finds it by holding every leaf and watching which one the migration waits for.
     */
    @Test
    void aDeadlockWithASessionThatWaitsOnTheMigration_isSkippedNotFatal_andTheNextBootHeals() throws Throwable {
        List<String> leaves = allLeafNames();
        assertThat(leaves).as("leaves examined").hasSizeGreaterThanOrEqualTo(4);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            // Phase 1: which leaf does the heal alter first?
            stripLeaves(leaves);
            String first;
            try (Connection holder = deadlockHolder(); Connection su = pg.createConnection("")) {
                for (String l : leaves) holdLeaf(holder, l);
                int holderPid = backendPid(holder);
                Future<?> migrate = pool.submit(() -> SchemaMigrator.migrate(adminDs));
                List<String> waiting = new ArrayList<>();
                awaitTrue("the heal to wait for the first leaf", () -> {
                    waiting.clear();
                    sueLocks(dsl(su), false).stream().filter(r -> r.pid() != holderPid && leaves.contains(r.rel()))
                        .forEach(r -> waiting.add(r.rel()));
                    return !waiting.isEmpty();
                }, 90_000);
                first = waiting.get(0);
                holder.rollback();
                migrate.get(90, TimeUnit.SECONDS);
            }
            assertThat(leaves).contains(first);

            // Phase 2: hold every leaf but `first`; the heal alters `first`, then waits on the next; ask for `first`.
            stripLeaves(leaves);
            clearAlterLog();
            try (Connection holder = deadlockHolder(); Connection su = pg.createConnection("")) {
                for (String l : leaves) if (!l.equals(first)) holdLeaf(holder, l);
                int holderPid = backendPid(holder);
                Future<?> migrate = pool.submit(() -> SchemaMigrator.migrate(adminDs));
                awaitTrue("the heal to hold " + first + " and wait for another leaf", () -> {
                    boolean holds = sueLocks(dsl(su), true).stream().anyMatch(r -> r.pid() != holderPid && r.rel().equals(first));
                    boolean waits = sueLocks(dsl(su), false).stream().anyMatch(r -> r.pid() != holderPid && leaves.contains(r.rel()));
                    return holds && waits;
                }, 90_000);
                Future<?> holderAsks = pool.submit(() -> holdLeaf(holder, first));
                migrate.get(120, TimeUnit.SECONDS);       // fatal 40P01 here if deadlock_detected is not caught
                holderAsks.get(30, TimeUnit.SECONDS);     // the holder was granted `first`, not made the victim
                holder.rollback();
                try (Connection check = pg.createConnection("")) {
                    DSLContext ctx = dsl(check);
                    assertThat(reloptions(ctx, first)).as("the leaf the migration held is healed").isEqualTo(FOUR);
                    for (String l : leaves) {
                        if (!l.equals(first)) assertThat(reloptions(ctx, l)).as("leaf %s was held by the other session", l).isEmpty();
                    }
                    assertThat(loggedAlters(ctx)).as("only the first leaf was altered").containsExactly("nexus." + first);
                }
            }
            clearAlterLog();
            SchemaMigrator.migrate(adminDs);
            try (Connection su = pg.createConnection("")) {
                for (String l : leaves) assertThat(reloptions(dsl(su), l)).as("next boot heals %s", l).isEqualTo(FOUR);
            }
            System.out.println("S9WAV evidence: deadlock-shaped migrate completed; first leaf " + first + " healed, "
                + (leaves.size() - 1) + " held leaves skipped, all " + leaves.size() + " healed by the next boot");
        } finally {
            pool.shutdownNow();
        }
    }

    // ── ownership ────────────────────────────────────────────────────────────

    private void makeRole(String role) throws Exception {
        suDdl("DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + role + "') THEN CREATE ROLE "
            + role + " NOLOGIN; END IF; END $r$");
    }

    @Test
    void aLeafOwnedByAnotherRoleThatAlreadyCarriesTheFour_isAlreadyCorrect_notForeign_andNothingWarns() throws Exception {
        List<String> leaves = allLeafNames();
        String leaf = leaves.get(0);
        makeRole("s9wav_other");
        suDdl("ALTER TABLE nexus." + leaf + " OWNER TO s9wav_other");
        try {
            clearAlterLog();
            List<String> messages;
            try (Connection a = admin()) {
                messages = runSelfHeal(a);
            }
            assertThat(messages).as("no foreign-owner warning for a leaf that needs nothing")
                .noneMatch(m -> m.contains("owned by a role"));
            assertThat(messages).as("the notice carries the counts").anyMatch(m -> m.contains(
                "leaves examined " + leaves.size() + ", altered 0, already correct " + leaves.size()
                + ", locked 0, vanished 0, foreign-owned 0"));
            try (Connection su = pg.createConnection("")) {
                assertThat(loggedAlters(dsl(su))).as("nothing altered").isEmpty();
            }
            System.out.println("S9WAV evidence: foreign-owned correct leaf: examined " + leaves.size() + ", already correct "
                + leaves.size() + ", foreign 0, warnings about ownership 0");
        } finally {
            suDdl("ALTER TABLE nexus." + leaf + " OWNER TO " + ADMIN);
        }
    }

    @Test
    void aLeafOwnedByAnotherRoleThatLacksTheFour_isReportedForeign_leftAsItIs_andNamedByThePostMigrateCheck() throws Exception {
        List<String> leaves = allLeafNames();
        String leaf = leaves.get(0);
        makeRole("s9wav_other");
        stripLeaves(List.of(leaf));
        suDdl("ALTER TABLE nexus." + leaf + " OWNER TO s9wav_other");
        try {
            List<String> messages;
            try (Connection a = admin()) {
                messages = runSelfHeal(a);
            }
            assertThat(messages).filteredOn(m -> m.contains("owned by a role")).as("one foreign-owner warning").hasSize(1);
            assertThat(messages).anyMatch(m -> m.contains(
                "leaves examined " + leaves.size() + ", altered 0, already correct " + (leaves.size() - 1)
                + ", locked 0, vanished 0, foreign-owned 1"));
            try (Connection su = pg.createConnection("")) {
                assertThat(reloptions(dsl(su), leaf)).as("left as it is").isEmpty();
                var report = ChunksLeafAutovacuumCheck.inspect(dsl(su));
                assertThat(report.leaves()).isEqualTo(leaves.size());
                assertThat(report.policyUnset()).as("the post-migrate check names it").containsExactly(leaf);
                assertThat(report.autovacuumDisabled()).isEmpty();
            }
        } finally {
            suDdl("ALTER TABLE nexus." + leaf + " OWNER TO " + ADMIN);
            try (Connection a = admin()) {
                a.setAutoCommit(true);
                setLeaf(dsl(a), leaf);
            }
        }
    }

    // ── a leaf dropped under the heal ────────────────────────────────────────

    /**
     * The heal cannot see a leaf vanish between its scan and its ALTER: {@code pg_partition_tree} takes an
     * AccessShareLock on every leaf and the heal's transaction keeps it, so a concurrent DROP of a leaf queues behind
     * the heal instead. The DROP does collide with the heal's wait for that leaf, though: a session that holds the
     * leaf (as a vacuum would) and then drops it waits for the heal's AccessShareLock while the heal waits for the
     * session's lock, a deadlock the heal must absorb like any other. (The vanished-leaf handler in vectors-034-2 is
     * a guard for the case the locks do not cover; PostgreSQL gives no way to reach it from here.)
     */
    @Test
    void aConcurrentDropOfALeaf_deadlocksWithTheHeal_andIsSkippedNotFatal() throws Exception {
        String tenant = "s9wav-vanish-tenant";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(dsl(su), "tok-s9wav-vanish", tenant, "s9wav-vanish");
        }
        String model;
        try (Connection su = pg.createConnection("")) {
            model = modelOf(PartitionScratch.children(dsl(su), "chunks").get(0).bound());
        }
        String victim = PartitionScratch.expectedName("chunks", model, tenant);
        stripLeaves(List.of(victim));
        ExecutorService pool = Executors.newFixedThreadPool(2);
        List<String> messages;
        int total = allLeafNames().size();
        try (Connection dropper = deadlockHolder(); Connection su = pg.createConnection(""); Connection healer = admin()) {
            holdLeaf(dropper, victim);
            int dropperPid = backendPid(dropper);
            Future<List<String>> heal = pool.submit(() -> runSelfHeal(healer));
            try {
                awaitTrue("the heal to wait for the held leaf", () ->
                    sueLocks(dsl(su), false).stream().anyMatch(r -> r.pid() != dropperPid && r.rel().equals(victim)), 30_000);
            } catch (AssertionError e) {
                throw new AssertionError(e.getMessage() + "; waiting lock requests: " + waitingLocks(dsl(su))
                    + "; heal: " + (heal.isDone() ? outcomeOf(heal) : "still running"), e);
            }
            Future<?> drop = pool.submit(() -> dsl(dropper).dropTable(DSL.name("nexus", victim)).cascade().execute());
            messages = heal.get(60, TimeUnit.SECONDS);   // fatal 40P01 here if deadlock_detected is not caught
            drop.get(60, TimeUnit.SECONDS);              // the drop proceeds once the heal's transaction ends
            dropper.commit();
        } finally {
            pool.shutdownNow();
            try (Connection a = admin()) {
                a.setAutoCommit(true);
                PartitionScratch.createTenantPartitions(dsl(a), "chunks", tenant, true);   // put the tree back
            }
        }
        // Normally 40P01. If this thread stalls past the heal's 1 s deadlock_timeout before the DROP is issued,
        // the deadlock check finds no cycle yet and is not re-armed, so the heal ends at the 5 s lock_timeout
        // (55P03) instead; both are the busy handler, which is what this test pins.
        assertThat(messages).as("the leaf is counted locked, not fatal")
            .anyMatch(m -> m.contains("40P01: deadlock detected") || m.contains("55P03"));
        assertThat(messages).anyMatch(m -> m.contains("leaves examined " + total + ", altered 0, already correct " + (total - 1)
            + ", locked 1, vanished 0, foreign-owned 0"));
        System.out.println("S9WAV evidence: concurrent leaf drop: heal completed, " + total + " leaves examined, 1 skipped on 40P01");
    }

    // ── the post-migrate check an operator reads ─────────────────────────────

    @Test
    void autovacuumDisabledOnALeaf_isKept_byTheHeal_andNamedByThePostMigrateCheck() throws Throwable {
        List<String> leaves = allLeafNames();
        String stripped = leaves.get(0);
        String disabled = leaves.get(leaves.size() - 1);
        stripLeaves(List.of(stripped));
        suDdl("ALTER TABLE nexus." + disabled + " SET (autovacuum_enabled = false, fillfactor = 90)");
        try {
            try (Connection su = pg.createConnection("")) {
                var before = ChunksLeafAutovacuumCheck.inspect(dsl(su));
                assertThat(before.leaves()).isEqualTo(leaves.size());
                assertThat(before.policyUnset()).containsExactly(stripped);
                assertThat(before.autovacuumDisabled()).containsExactly(disabled);
                assertThat(before.healthy()).isFalse();
            }
            List<String> logs = captureLogs(() -> SchemaMigrator.migrate(adminDs));
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = dsl(su);
                assertThat(reloptions(ctx, stripped)).isEqualTo(FOUR);
                // The heal sets the four and keeps every other reloption.
                assertThat(reloptions(ctx, disabled)).containsAll(FOUR).contains("autovacuum_enabled=false", "fillfactor=90");
                var after = ChunksLeafAutovacuumCheck.inspect(ctx);
                assertThat(after.policyUnset()).isEmpty();
                assertThat(after.autovacuumDisabled()).containsExactly(disabled);
            }
            assertThat(logs).as("the boot log names the leaf with autovacuum off")
                .anyMatch(m -> m.startsWith("WARN ") && m.contains("event=chunks_leaf_autovacuum_checked")
                    && m.contains("policy_unset=0 autovacuum_disabled=1") && m.contains(disabled));
        } finally {
            suDdl("ALTER TABLE nexus." + disabled + " RESET (autovacuum_enabled, fillfactor)");
        }
        try (Connection su = pg.createConnection("")) {
            var healthy = ChunksLeafAutovacuumCheck.inspect(dsl(su));
            assertThat(healthy.healthy()).isTrue();
            System.out.println("S9WAV evidence: post-migrate check leaves=" + healthy.leaves()
                + " policy_unset=0 autovacuum_disabled=0 after the reset; autovacuum_enabled=false and fillfactor kept by the heal");
        }
    }


    private static String modelOf(String bound) {
        // "FOR VALUES IN ('voyage-code-3')"
        int a = bound.indexOf('\'');
        int b = bound.lastIndexOf('\'');
        return bound.substring(a + 1, b);
    }
}
