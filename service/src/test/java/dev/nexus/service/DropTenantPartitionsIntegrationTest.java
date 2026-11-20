// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.SQLWarning;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.jooq.Condition;
import org.jooq.impl.SQLDataType;

import static dev.nexus.service.PartitionScratch.CODE_3;
import static dev.nexus.service.PartitionScratch.MINILM_384;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static dev.nexus.service.jooq.nexus.Tables.SESSION_TOKENS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Phase 3 Step 2 (nexus-3wh8d.21 runbook): {@code nexus.drop_tenant_partitions(tenant)} removes a
 * tenant's chunk and centroid leaves, and the rows that reference its chunks. It is what the tenant-removal
 * runbook calls and what the Python test substrate calls when a test's minted tenant is done.
 *
 * <p>Run as the schema owner of a store migrated by a NOSUPERUSER NOBYPASSRLS role (production's nexus_admin
 * shape): the function is not SECURITY DEFINER, so the deletes are subject to FORCE ROW LEVEL SECURITY and the
 * function has to set the tenant scope itself. A superuser connection would bypass RLS and prove nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DropTenantPartitionsIntegrationTest {

    private static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    private static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;

    /** The bystander whose every row and leaf must survive every drop; each test seeds the tenant it drops. */
    private static final String TB = "p225-drop-b";

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startMigratedByOwner();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        adminDs = new HikariDataSource(cfg);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seed(ctx, TB, 2000L);
            // 'default' holds a token-less leaf set from the walk and one row of each kind
            PgContainerHelper.insertCollection(ctx, "default", coll1("dflt"));
            PgContainerHelper.insertChunk1024(ctx, "default", coll1("dflt"), hash(90), vec(1024));
        }
    }

    @AfterAll
    void stopAll() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    private static String coll1(String tag) {
        return "code__" + tag + "-o__voyage-code-3__v1";
    }

    private static String coll2(String tag) {
        return "knowledge__" + tag + "-o__minilm-l6-v2-384__v1";
    }

    private static byte[] hash(int n) {
        byte[] b = new byte[32];
        b[0] = (byte) n;
        b[31] = (byte) (n + 1);
        return b;
    }

    private static String hex(byte[] b) {
        return HexFormat.of().formatHex(b);
    }

    private static Vector vec(int dim) {
        float[] f = new float[dim];
        Arrays.fill(f, 0.1f);
        f[0] = 0.5f;
        return Vector.of(f);
    }

    /**
     * A tenant with a token (so the trigger gives it leaves), two collections on two models, two chunks in each,
     * manifest rows for the 1024-wide pair, an assignment, an orphaned-at row and a centroid per collection.
     */
    private static void seed(DSLContext su, String tenant, long topicId) {
        PgContainerHelper.seedServiceToken(su, "tok-" + tenant, tenant, "p225drop");
        String c1 = coll1(tenant);
        String c2 = coll2(tenant);
        PgContainerHelper.insertCollection(su, tenant, c1);
        PgContainerHelper.insertCollection(su, tenant, c2);
        byte[] a = hash(1), b = hash(2), x = hash(3), y = hash(4);
        PgContainerHelper.insertChunk1024(su, tenant, c1, a, vec(1024));
        PgContainerHelper.insertChunk1024(su, tenant, c1, b, vec(1024));
        PgContainerHelper.insertChunk384(su, tenant, c2, x, vec(384));
        PgContainerHelper.insertChunk384(su, tenant, c2, y, vec(384));
        PgContainerHelper.ownChunks(su, tenant, c1, hex(a), hex(b));
        su.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(topicId, tenant, "t", c1, 0, OffsetDateTime.now(), "pending").execute();
        su.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID,
                TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
            .values(tenant, a, topicId, "projection", c1, OffsetDateTime.now(), CODE_3).execute();
        su.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH,
                CHUNK_ORPHANED_AT.ORPHANED_AT, CHUNK_ORPHANED_AT.EMBEDDING_MODEL)
            .values(tenant, c2, x, OffsetDateTime.now().minusDays(2), MINILM_384).execute();
        su.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL,
                TAXONOMY_CENTROIDS.EMBEDDING_1024)
            .values(tenant, c1, topicId, CODE_3, "c1", vec(1024)).execute();
        su.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL,
                TAXONOMY_CENTROIDS.EMBEDDING_384)
            .values(tenant, c2, topicId + 1, MINILM_384, "c2", vec(384)).execute();
    }

    private Connection admin() throws SQLException {
        return DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
    }

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private static String sqlState(Throwable t) {
        for (Throwable x = t; x != null; x = x.getCause()) {
            if (x instanceof DataAccessException d && d.sqlState() != null) return d.sqlState();
            if (x instanceof SQLException s && s.getSQLState() != null) return s.getSQLState();
        }
        return null;
    }

    /** The tenant's leaves under both live parents, found by bound the way the function finds them. */
    private static List<String> leaves(DSLContext ctx, String tenant) {
        var out = new ArrayList<String>();
        String bound = "FOR VALUES IN ('" + tenant + "')";
        for (String parent : List.of("chunks", "taxonomy_centroids")) {
            for (var mp : PartitionScratch.children(ctx, parent)) {
                for (var leaf : PartitionScratch.children(ctx, mp.name())) {
                    if (leaf.bound().equals(bound)) out.add(leaf.name());
                }
            }
        }
        return out;
    }

    private static int modelPartitions(DSLContext ctx) {
        return PartitionScratch.children(ctx, "chunks").size() + PartitionScratch.children(ctx, "taxonomy_centroids").size();
    }

    /** Row counts of every table the function clears, for one tenant (read as superuser: RLS does not apply). */
    private Map<String, Integer> rowCounts(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            var m = new TreeMap<String, Integer>();
            m.put("chunks", ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant)));
            m.put("taxonomy_centroids", ctx.fetchCount(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID.eq(tenant)));
            m.put("catalog_document_chunks", ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)));
            m.put("topic_assignments", ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)));
            m.put("chunk_orphaned_at", ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant)));
            return m;
        }
    }

    // ── the tests ────────────────────────────────────────────────────────────

    @Test
    void dropsTheTenantsLeavesAndItsReferencingRows_andLeavesAnotherTenantUntouched() throws Exception {
        final String TA = "p225-drop-a";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 1000L);
        }
        Map<String, Integer> bBefore = rowCounts(TB);
        Map<String, Integer> aBefore = rowCounts(TA);
        assertThat(aBefore).containsEntry("chunks", 4).containsEntry("taxonomy_centroids", 2)
            .containsEntry("catalog_document_chunks", 2).containsEntry("topic_assignments", 1)
            .containsEntry("chunk_orphaned_at", 1);
        List<String> bLeaves;
        int expectedLeaves;
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            assertThat(leaves(ctx, TA)).hasSize(modelPartitions(ctx));
            bLeaves = leaves(ctx, TB);
            expectedLeaves = modelPartitions(ctx);
        }

        int dropped;
        try (Connection a = admin()) {
            dropped = PartitionScratch.dropTenantPartitions(dsl(a), TA);
        }

        assertThat(dropped).isEqualTo(expectedLeaves);
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            assertThat(leaves(ctx, TA)).isEmpty();
            assertThat(leaves(ctx, TB)).containsExactlyInAnyOrderElementsOf(bLeaves);
        }
        assertThat(rowCounts(TA)).allSatisfy((t, n) -> assertThat(n).as("rows left in %s", t).isZero());
        assertThat(rowCounts(TB)).isEqualTo(bBefore);
    }

    @Test
    void aSecondCall_andACallForATenantWithNoLeaves_dropNothing() throws Exception {
        final String TA = "p225-drop-twice";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 5000L);
        }
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            assertThat(PartitionScratch.dropTenantPartitions(ctx, TA)).isPositive();
            assertThat(PartitionScratch.dropTenantPartitions(ctx, TA)).isZero();
            assertThat(PartitionScratch.dropTenantPartitions(ctx, "p225-never-existed")).isZero();
            assertThat(leaves(ctx, TB)).isNotEmpty();
        }
    }

    @Test
    void theDefaultTenantAndANullTenantAreRefused_andNothingIsDropped() throws Exception {
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            int before = leaves(ctx, "default").size();
            assertThat(before).isEqualTo(modelPartitions(ctx));
            assertThatThrownBy(() -> PartitionScratch.dropTenantPartitions(ctx, "default"))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22023"));
            assertThatThrownBy(() -> PartitionScratch.dropTenantPartitions(ctx, null))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22004"));
            assertThat(leaves(ctx, "default")).hasSize(before);
        }
        assertThat(rowCounts("default").get("chunks")).isEqualTo(1);
    }

    @Test
    void theCallersTenantScopeIsRestored_andTheFunctionIsNotDefinerAndReachesNoEngineRole() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            seed(ctx, "p225-drop-scope", 3000L);
        }
        try (Connection a = admin()) {
            a.setAutoCommit(false);
            DSLContext ctx = dsl(a);
            PgContainerHelper.setTenant(a, "nexus.tenant", "caller-scope", true);
            PartitionScratch.dropTenantPartitions(ctx, "p225-drop-scope");
            assertThat(PartitionScratch.setting(ctx, "nexus.tenant")).isEqualTo("caller-scope");
            a.rollback();
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            // rolled back: the tenant is whole again, which also shows the drop is one transaction
            assertThat(leaves(ctx, "p225-drop-scope")).hasSize(modelPartitions(ctx));
            assertThat(PartitionScratch.functionFacts(ctx, "drop_tenant_partitions"))
                .as("not SECURITY DEFINER, fixed search_path, lock_timeout set, no PUBLIC or engine-role execute")
                .isEqualTo("definer=false;config=search_path=pg_catalog, pg_temp,lock_timeout=10s;public=false;nexus_svc=false");
        }
        try (Connection svc = DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME,
                PgContainerHelper.SVC_PASSWORD)) {
            assertThatThrownBy(() -> PartitionScratch.dropTenantPartitions(dsl(svc), "p225-drop-scope"))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("42501"));
        }
    }

    /**
     * The runbook end to end (docs/runbooks/rdr-225-tenant-removal.md), as the schema owner: the function, then the
     * tenant's session and service tokens. Nothing of the tenant remains and the bystander is untouched.
     */
    @Test
    void theRunbook_removesTheTenantsLeavesReferencingRowsAndTokens_andLeavesAnotherTenantUntouched() throws Exception {
        final String TA = "p225-drop-runbook";
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            seed(ctx, TA, 6000L);
            ctx.insertInto(SESSION_TOKENS)
                .set(SESSION_TOKENS.SESSION_TOKEN_HASH, "sess-hash-" + TA)
                .set(SESSION_TOKENS.TENANT_ID, TA)
                .set(SESSION_TOKENS.SESSION_ID, "sess-" + TA)
                .set(SESSION_TOKENS.EXPIRES_AT, OffsetDateTime.now().plusHours(1))
                .execute();
        }
        Map<String, Integer> bBefore = rowCounts(TB);
        int bTokens = tokenCount(TB);
        assertThat(tokenCount(TA)).isEqualTo(2);
        try (Connection a = admin()) {
            a.setAutoCommit(false);
            DSLContext ctx = dsl(a);
            assertThat(PartitionScratch.dropTenantPartitions(ctx, TA)).isEqualTo(modelPartitions(ctx));
            a.commit();
            ctx.deleteFrom(SESSION_TOKENS).where(SESSION_TOKENS.TENANT_ID.eq(TA)).execute();
            ctx.deleteFrom(SERVICE_TOKENS).where(SERVICE_TOKENS.TENANT_ID.eq(TA)).execute();
            a.commit();
            assertThat(leaves(ctx, TA)).isEmpty();
            assertThat(leaves(ctx, TB)).hasSize(modelPartitions(ctx));
        }
        assertThat(rowCounts(TA)).allSatisfy((t, n) -> assertThat(n).as("rows left in %s", t).isZero());
        assertThat(tokenCount(TA)).isZero();
        assertThat(rowCounts(TB)).isEqualTo(bBefore);
        assertThat(tokenCount(TB)).isEqualTo(bTokens);
    }

    /**
     * Why the runbook calls the function rather than dropping leaves by hand: a leaf whose chunks the manifest
     * references cannot be dropped directly (its partition-level FK clone depends on it). The refusal changes
     * nothing; DETACH then DROP, which the function does, is the path that works.
     */
    @Test
    void aDirectDropOfAReferencedChunksLeaf_isRefused_andChangesNothing() throws Exception {
        final String TA = "p225-drop-direct";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 7000L);
        }
        Map<String, Integer> before = rowCounts(TA);
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            // The tenant's leaf under the voyage-code-3 partition of chunks: it holds the manifest-referenced pair.
            String leaf = PartitionScratch.children(ctx, "chunks").stream()
                .filter(mp -> mp.bound().equals("FOR VALUES IN ('" + CODE_3 + "')"))
                .flatMap(mp -> PartitionScratch.children(ctx, mp.name()).stream())
                .filter(l -> l.bound().equals("FOR VALUES IN ('" + TA + "')"))
                .map(PartitionScratch.Child::name).findFirst().orElseThrow();
            assertThatThrownBy(() -> ctx.dropTable(DSL.name("nexus", leaf)).execute())
                .satisfies(t -> assertThat(sqlState(t)).as("dependent objects still exist").isEqualTo("2BP01"));
            assertThat(leaves(ctx, TA)).contains(leaf).hasSize(modelPartitions(ctx));
        }
        assertThat(rowCounts(TA)).isEqualTo(before);
    }

    private int tokenCount(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            return ctx.fetchCount(SERVICE_TOKENS, SERVICE_TOKENS.TENANT_ID.eq(tenant))
                + ctx.fetchCount(SESSION_TOKENS, SESSION_TOKENS.TENANT_ID.eq(tenant));
        }
    }

    @Test
    void aDroppedTenantCanBeGivenItsLeavesAgain() throws Exception {
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), "p225-drop-redo", 4000L);
        }
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            PartitionScratch.dropTenantPartitions(ctx, "p225-drop-redo");
            assertThat(leaves(ctx, "p225-drop-redo")).isEmpty();
            assertThat(PartitionScratch.createTenantPartitions(ctx, "chunks", "p225-drop-redo", true)).isPositive();
            assertThat(PartitionScratch.createTenantPartitions(ctx, "taxonomy_centroids", "p225-drop-redo", true)).isPositive();
            assertThat(leaves(ctx, "p225-drop-redo")).hasSize(modelPartitions(ctx));
        }
    }

    // ── lock order: the roots first (the 2026-11-20 production deadlock) ─────

    /** A model no partition carries: a search for it prunes every partition, so only the root is locked. */
    private static final String NO_SUCH_MODEL = "p225-no-such-model";

    /** The function's bounds (vectors-063): attempts, per-attempt lock_timeout, and the backoff step (0.2 s x attempt). */
    private static final int ATTEMPTS = 5;
    private static final long ATTEMPT_TIMEOUT_MS = 500;
    private static final long BACKOFF_STEP_MS = 200;
    /** Five full waits plus the four sleeps between them: 5 x 500 + 200 x (1 + 2 + 3 + 4). */
    private static final long MIN_GIVE_UP_MS = ATTEMPTS * ATTEMPT_TIMEOUT_MS + BACKOFF_STEP_MS * (ATTEMPTS - 1) * ATTEMPTS / 2;

    private static final Condition IS_RELATION_LOCK = DSL.field(DSL.name("locktype"), String.class).eq("relation");

    /**
     * A reader that is mid-search: AccessShare on the root of nexus.chunks and, so far, on nothing below it (the
     * planner takes the partitions after the root, and a search whose model no partition carries stops there).
     */
    private static void holdOnlyTheRoot(Connection s) throws SQLException {
        s.setAutoCommit(false);
        dsl(s).fetchCount(CHUNKS, CHUNKS.EMBEDDING_MODEL.eq(NO_SUCH_MODEL));
    }

    private static int backendPid(Connection c) {
        return dsl(c).select(DSL.function("pg_backend_pid", Integer.class)).fetchOne(0, Integer.class);
    }

    private static final org.jooq.Table<?> PG_LOCKS = DSL.table(DSL.name("pg_catalog", "pg_locks"));

    /** One relation lock of a backend, the relation as regclass text (schema-qualified when not on the search path). */
    private record RelLock(String relation, String mode, boolean granted) {}

    private static List<RelLock> relationLocks(Connection obs, int pid) {
        var regclass = org.jooq.impl.DefaultDataType.getDefaultDataType("regclass");
        return dsl(obs).select(DSL.field(DSL.name("relation")).cast(regclass).cast(SQLDataType.VARCHAR),
                DSL.field(DSL.name("mode"), String.class), DSL.field(DSL.name("granted"), Boolean.class))
            .from(PG_LOCKS)
            .where(DSL.field(DSL.name("pid"), Integer.class).eq(pid).and(IS_RELATION_LOCK))
            .fetch(r -> new RelLock(r.value1(), r.value2(), Boolean.TRUE.equals(r.value3())));
    }

    /** Every model partition and leaf under the two roots, as regclass text. */
    private static java.util.Set<String> belowTheRoots(Connection obs) {
        var out = new java.util.HashSet<String>();
        for (String parent : List.of("chunks", "taxonomy_centroids")) {
            for (var mp : PartitionScratch.children(dsl(obs), parent)) {
                out.add("nexus." + mp.name());
                for (var leaf : PartitionScratch.children(dsl(obs), mp.name())) out.add("nexus." + leaf.name());
            }
        }
        return out;
    }

    /** Whether backend {@code pid} is queued for a lock on the root of nexus.chunks. */
    private static boolean waitingForTheRoot(Connection obs, int pid) {
        return relationLocks(obs, pid).stream().anyMatch(l -> !l.granted() && l.relation().equals("nexus.chunks"));
    }

    /** Whether backend {@code pid} is queued for any relation lock. */
    private static boolean waitingForAnyLock(Connection obs, int pid) {
        return relationLocks(obs, pid).stream().anyMatch(l -> !l.granted());
    }

    /** The locks of {@code mode} backend {@code pid} holds on anything below the roots of chunks or taxonomy_centroids. */
    private static List<RelLock> locksBelowTheRoots(Connection obs, int pid, java.util.Set<String> below, String mode) {
        return relationLocks(obs, pid).stream()
            .filter(l -> l.granted() && l.mode().equals(mode) && below.contains(l.relation())).toList();
    }

    /** Polls (bounded) until {@code cond} holds; fails at once if the drop already finished, which is the case it excludes. */
    private static void awaitWhileRunning(String what, Future<?> drop, java.util.function.BooleanSupplier cond) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(8);
        while (!cond.getAsBoolean()) {
            if (drop.isDone()) {
                Object outcome;
                try {
                    outcome = drop.get();
                } catch (ExecutionException e) {
                    outcome = e.getCause();
                }
                throw new AssertionError("the drop finished (" + outcome + ") before " + what);
            }
            if (System.nanoTime() > deadline) throw new AssertionError("timed out waiting for " + what);
            Thread.sleep(10);
        }
    }

    /**
     * Searches enter nexus.chunks through the root and lock the partitions after it; the drop used to lock leaf by
     * leaf, so one of each could wait on the other (the cloud deadlock, engine 0.1.199). It now takes both roots
     * first. A reader that holds the root and nothing below it makes the drop queue at the root, holding no
     * exclusive lock below it, and the reader can then lock the partitions the drop is about to detach.
     */
    @Test
    void theDropQueuesAtTheRootBeforeLockingAnyPartition_soAReaderMidSearchCannotDeadlockWithIt() throws Exception {
        final String TA = "p225-drop-lockorder";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 8000L);
        }
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try (Connection s = admin(); Connection d = admin(); Connection obs = pg.createConnection("")) {
            int expectedLeaves = modelPartitions(dsl(obs));
            holdOnlyTheRoot(s);
            int sPid = backendPid(s);
            java.util.Set<String> below = belowTheRoots(obs);
            assertThat(locksBelowTheRoots(obs, sPid, below, "AccessShareLock")).as("the reader holds nothing below the root").isEmpty();
            int dPid = backendPid(d);

            StringBuilder notices = new StringBuilder();   // jOOQ drains the connection's warnings, so a listener reads them
            var cfg = new org.jooq.impl.DefaultConfiguration().set(d).set(SQLDialect.POSTGRES)
                .set(new org.jooq.impl.DefaultExecuteListenerProvider(new org.jooq.impl.DefaultExecuteListener() {
                    @Override
                    public void warning(org.jooq.ExecuteContext c) {
                        for (SQLWarning w = c.sqlWarning(); w != null; w = w.getNextWarning()) notices.append(w.getMessage()).append('\n');
                    }
                }));
            Future<Integer> drop = pool.submit(() -> PartitionScratch.dropTenantPartitions(DSL.using(cfg), TA));
            awaitWhileRunning("the drop queued for the root of nexus.chunks", drop, () -> waitingForTheRoot(obs, dPid));
            assertThat(locksBelowTheRoots(obs, dPid, below, "AccessExclusiveLock"))
                .as("while it waits for the root the drop holds no exclusive lock on a model partition or leaf").isEmpty();

            // The reader goes on into the partitions the drop will detach: it must get them at once.
            dsl(s).select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("lock_timeout"), DSL.val("3s"), DSL.val(true))).fetch();
            dsl(s).fetchCount(CHUNKS, CHUNKS.EMBEDDING_MODEL.eq(CODE_3));   // the call returning (inside lock_timeout) is the assertion
            assertThat(locksBelowTheRoots(obs, sPid, below, "AccessShareLock"))
                .as("the reader now holds partitions below the root (and the lock matching sees them)").isNotEmpty();
            s.commit();

            assertThat(drop.get(30, TimeUnit.SECONDS)).isEqualTo(expectedLeaves);
            assertThat(notices.toString()).contains(TA).contains("held").contains("attempt");
        } finally {
            pool.shutdownNow();
        }
        try (Connection a = admin()) {
            assertThat(leaves(dsl(a), TA)).isEmpty();
        }
        assertThat(rowCounts(TA)).allSatisfy((t, n) -> assertThat(n).as("rows left in %s", t).isZero());
    }

    /**
     * A reader that outlasts every attempt makes the drop give up with a named, retryable error and change nothing
     * (the deletes and the lock attempts are one transaction); once the reader is gone a rerun drops the lot.
     */
    @Test
    void aReaderThatHoldsTheRootThroughEveryAttempt_makesTheDropGiveUpWithTheNamedError_andNothingIsDetached() throws Exception {
        final String TA = "p225-drop-giveup";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 9000L);
        }
        Map<String, Integer> before = rowCounts(TA);
        List<String> leavesBefore;
        int expectedLeaves;
        try (Connection obs = pg.createConnection("")) {
            leavesBefore = leaves(dsl(obs), TA);
            expectedLeaves = modelPartitions(dsl(obs));
        }
        try (Connection s = admin(); Connection d = admin()) {
            holdOnlyTheRoot(s);
            long t0 = System.nanoTime();
            assertThatThrownBy(() -> PartitionScratch.dropTenantPartitions(dsl(d), TA))
                .satisfies(t -> assertThat(sqlState(t)).as("lock_not_available").isEqualTo("55P03"))
                .hasMessageContaining("held a lock it needs; rerun, after index builds finish if one is running")
                .hasMessageContaining(TA);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);
            assertThat(elapsedMs).as("every attempt waits its full lock_timeout and sleeps its backoff before the next")
                .isGreaterThanOrEqualTo(MIN_GIVE_UP_MS);
            s.rollback();
        }
        try (Connection obs = pg.createConnection("")) {
            assertThat(leaves(dsl(obs), TA)).containsExactlyInAnyOrderElementsOf(leavesBefore);
        }
        assertThat(rowCounts(TA)).as("the deletes rolled back with the failed call").isEqualTo(before);

        try (Connection a = admin()) {
            assertThat(PartitionScratch.dropTenantPartitions(dsl(a), TA)).isEqualTo(expectedLeaves);
            assertThat(leaves(dsl(a), TA)).isEmpty();
        }
        assertThat(rowCounts(TA)).allSatisfy((t, n) -> assertThat(n).as("rows left in %s", t).isZero());
    }

    /**
     * The cycle a root-first lock alone leaves open: a manifest writer holds RowExclusive on a table that references
     * chunks and then needs the root for its foreign-key check, while the drop holds the roots and wants that table
     * to detach. The drop now takes the referencing tables first, so it queues behind the writer holding nothing the
     * writer needs; the writer reaches the root, commits, and the drop completes. No 40P01 on either side.
     */
    @Test
    void aManifestWriterHoldingTheReferencingTableThenNeedingTheRoot_neverDeadlocksWithTheDrop() throws Exception {
        final String TA = "p225-drop-writer";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 10000L);
        }
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try (Connection w = admin(); Connection d = admin(); Connection obs = pg.createConnection("")) {
            int expectedLeaves = modelPartitions(dsl(obs));
            w.setAutoCommit(false);
            PgContainerHelper.lockTable(w, CATALOG_DOCUMENT_CHUNKS, PgContainerHelper.LockMode.ROW_EXCLUSIVE);
            int dPid = backendPid(d);
            Future<Integer> drop = pool.submit(() -> PartitionScratch.dropTenantPartitions(dsl(d), TA));
            awaitWhileRunning("the drop queued behind the writer", drop, () -> waitingForAnyLock(obs, dPid));

            String writerState = null;
            try {
                dsl(w).fetchCount(CHUNKS, CHUNKS.EMBEDDING_MODEL.eq(NO_SUCH_MODEL));   // the foreign-key check's root access
                w.commit();
            } catch (RuntimeException e) {
                writerState = sqlState(e);
                w.rollback();
            }
            assertThat(writerState).as("the writer reaches the root and commits; a deadlock victim would be 40P01").isNull();
            assertThat(drop.get(30, TimeUnit.SECONDS)).as("the drop completes once the writer is done").isEqualTo(expectedLeaves);
        } finally {
            pool.shutdownNow();
        }
        try (Connection a = admin()) {
            assertThat(leaves(dsl(a), TA)).isEmpty();
        }
    }

    /**
     * Index builds (CREATE INDEX CONCURRENTLY), REINDEX CONCURRENTLY and vacuum hold SHARE UPDATE EXCLUSIVE on a
     * leaf for minutes. The drop locks only its own tenant's partitions, so such a lock on ANOTHER tenant's leaf must
     * not make it give up.
     */
    @Test
    void aShareUpdateExclusiveLockOnAnotherTenantsLeaf_doesNotBlockTheDrop() throws Exception {
        final String TA = "p225-drop-othersue";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 11000L);
        }
        int expectedLeaves;
        String bystanderLeaf;
        try (Connection obs = pg.createConnection("")) {
            expectedLeaves = modelPartitions(dsl(obs));
            bystanderLeaf = leaves(dsl(obs), TB).getFirst();
        }
        try (Connection s = admin(); Connection d = admin()) {
            s.setAutoCommit(false);
            PgContainerHelper.lockTable(s, DSL.table(DSL.name("nexus", bystanderLeaf)),
                PgContainerHelper.LockMode.SHARE_UPDATE_EXCLUSIVE);
            assertThat(PartitionScratch.dropTenantPartitions(dsl(d), TA)).isEqualTo(expectedLeaves);
            s.rollback();
        }
        try (Connection a = admin()) {
            assertThat(leaves(dsl(a), TA)).isEmpty();
            assertThat(leaves(dsl(a), TB)).contains(bystanderLeaf);
        }
    }

    /** A tenant with no leaves is answered from the catalog: 0, and no lock on either root (nothing to wait for). */
    @Test
    void aTenantWithNoLeaves_returnsZeroWithoutLockingTheRoots() throws Exception {
        try (Connection a = admin(); Connection obs = pg.createConnection("")) {
            // read the partition names first: once the call holds locks, a catalog read of a partition bound waits for it
            java.util.Set<String> roots = new java.util.HashSet<>(belowTheRoots(obs));
            roots.add("nexus.chunks");
            roots.add("nexus.taxonomy_centroids");
            a.setAutoCommit(false);
            assertThat(PartitionScratch.dropTenantPartitions(dsl(a), "p225-no-leaves-at-all")).isZero();
            assertThat(relationLocks(obs, backendPid(a)).stream().filter(l -> roots.contains(l.relation())).toList())
                .as("no lock of any mode on either root or anything below it").isEmpty();
            a.rollback();
        }
    }
}
