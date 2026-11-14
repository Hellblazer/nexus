// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExportSessions.Config;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Tenant export sessions and their reads against a real PostgreSQL and the real {@code nexus.chunks} partition
 * tree (RDR-248, bead nexus-csayy.2): what the held transaction is and holds, the per-tenant and per-engine
 * caps, the idle and total bounds on an injected clock, the header, the keyset walk of the chunks composite key
 * with its plan (gate N1), a text key full of quotes and backslashes, snapshot consistency, and a page that
 * waits behind a table lock. The routes, scopes and tokens are {@code TenantExportRoutesTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantExportSessionsTest {

    private static final String A = "t-export-a";
    private static final String B = "t-export-b";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINILM = "minilm-l6-v2-384";
    private static final String CA1 = "knowledge__exporta1__bge-base-en-v15-768__v1";
    private static final String CA2 = "knowledge__exporta2__bge-base-en-v15-768__v1";
    private static final String CB1 = "knowledge__exportb1__minilm-l6-v2-384__v1";
    private static final int A_CHUNKS = 40;
    private static final String EXPLAIN_ROLE = "svc_export_explain";
    private static final String EXPLAIN_PASS = "svc_export_explain_pass";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    /** A pool for a role that may call the test schema's explain function (the engine role may not). */
    private HikariDataSource explainDs;
    private final List<TenantExportSessions> opened = new ArrayList<>();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.ensureTenantPartitions(dsl, A);
            PgContainerHelper.ensureTenantPartitions(dsl, B);
            PgContainerHelper.insertCollection(dsl, A, CA1, BGE);
            PgContainerHelper.insertCollection(dsl, A, CA2, BGE);
            PgContainerHelper.insertCollection(dsl, B, CB1, MINILM);
            for (int i = 0; i < A_CHUNKS; i++) {
                insertChunk(dsl, A, i % 4 == 0 ? CA2 : CA1, "a chunk " + i);
            }
            for (int i = 0; i < 5; i++) {
                insertMemory(dsl, A, "title-" + i);
            }
            for (int i = 0; i < 3; i++) {
                insertMemory(dsl, B, "title-" + i);
            }
            for (int i = 0; i < 7; i++) {   // a timestamptz key with microseconds, two rows sharing one instant
                dsl.insertInto(SEARCH_TELEMETRY).set(SEARCH_TELEMETRY.TENANT_ID, A)
                    .set(SEARCH_TELEMETRY.TS, OffsetDateTime.parse("2026-11-12T10:15:30.123456Z").plusSeconds(i / 2))
                    .set(SEARCH_TELEMETRY.QUERY_HASH, "q" + i).set(SEARCH_TELEMETRY.COLLECTION, "c")
                    .set(SEARCH_TELEMETRY.RAW_COUNT, 1).set(SEARCH_TELEMETRY.KEPT_COUNT, 1).execute();
            }
            for (String key : List.of("it's", "back\\slash", "éclair", "z", "quote\"d", "semi;colon", "100%")) {
                dsl.insertInto(CATALOG_META).set(CATALOG_META.TENANT_ID, A).set(CATALOG_META.KEY, key)
                    .set(CATALOG_META.VALUE, "v").execute();
            }
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, EXPLAIN_ROLE, EXPLAIN_PASS);
        }
        var explainCfg = new HikariConfig();
        explainCfg.setJdbcUrl(pg.getJdbcUrl());
        explainCfg.setUsername(EXPLAIN_ROLE);
        explainCfg.setPassword(EXPLAIN_PASS);
        explainCfg.setMaximumPoolSize(2);
        explainCfg.setAutoCommit(true);
        explainDs = new HikariDataSource(explainCfg);
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
    }

    @AfterEach
    void closeSessions() {
        opened.forEach(TenantExportSessions::close);
        opened.clear();
    }

    @AfterAll
    void stop() {
        if (explainDs != null) explainDs.close();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ─────────────────────────────────────────────────────────────────────────────────

    private static byte[] sha256(String text) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    private static void insertChunk(DSLContext ctx, String tenant, String collection, String text) throws Exception {
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, collection)
            .set(CHUNKS.CHASH, sha256(tenant + collection + text))
            .set(CHUNKS.EMBEDDING_MODEL, BGE)
            .set(CHUNKS.CHUNK_TEXT, text)
            .set(CHUNKS.EMBEDDING_768, Vector.of(new float[768]))
            .execute();
    }

    private static void insertMemory(DSLContext ctx, String tenant, String title) {
        ctx.insertInto(MEMORY)
            .columns(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT, MEMORY.TIMESTAMP)
            .values(tenant, "p", title, "content of " + title, OffsetDateTime.now())
            .execute();
    }

    private Config config(int maxSessions) {
        return Config.defaults().withMaxSessions(maxSessions).withLockTimeoutMs(300).withSweepInterval(Duration.ofHours(1));
    }

    private TenantExportSessions sessions(Config config, ExportTestClock clock) {
        var s = new TenantExportSessions(ds, config, clock);
        opened.add(s);
        return s;
    }

    private static ExportTestClock clock() {
        return new ExportTestClock(Instant.parse("2026-11-12T00:00:00Z"));
    }

    private static ExportTable table(String name) {
        return TenantExport.exportable(name).orElseThrow();
    }

    private static List<Object> after(ExportTable t, String cursor) {
        return TenantExportKeys.binds(t.primaryKey(), TenantExportKeys.decode(cursor, t.primaryKey()));
    }

    // ── the held transaction ─────────────────────────────────────────────────────────────────────

    @Test
    void heldTransactionIsRepeatableReadReadOnlyAndHoldsNoTableLock() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        Connection held = session.heldConnectionForTests();
        DSLContext inHeld = DSL.using(held, SQLDialect.POSTGRES);
        assertThat(inHeld.select(DSL.function("current_setting", String.class, DSL.inline("transaction_isolation")))
            .fetchOne(0, String.class)).isEqualTo("repeatable read");
        assertThat(inHeld.select(DSL.function("current_setting", String.class, DSL.inline("transaction_read_only")))
            .fetchOne(0, String.class)).isEqualTo("on");
        Integer pid = inHeld.select(DSL.function("pg_backend_pid", Integer.class)).fetchOne(0, Integer.class);

        try (Connection su = pg.createConnection("")) {
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);
            Field<Integer> lockPid = DSL.field(DSL.name("pid"), Integer.class);
            Field<String> type = DSL.field(DSL.name("locktype"), String.class);
            var locks = sctx.select(type).from(DSL.table(DSL.name("pg_locks"))).where(lockPid.eq(pid)).fetch(type);
            assertThat(locks).as("the held backend's locks: its own virtual transaction id and nothing else")
                .doesNotContain("relation").contains("virtualxid");
            Record activity = sctx.select(DSL.field(DSL.name("state"), String.class),
                    DSL.field(DSL.field(DSL.name("backend_xmin")).isNotNull()))
                .from(DSL.table(DSL.name("pg_stat_activity"))).where(lockPid.eq(pid)).fetchOne();
            assertThat(activity.get(0, String.class)).isEqualTo("idle in transaction");
            assertThat(activity.get(1, Boolean.class)).as("the snapshot pins the vacuum horizon").isTrue();
        }
    }

    @Test
    void aTenantCreateTakingAnAccessExclusiveLockOnTheModelPartitionIsNotBlockedByAnOpenSession() throws Exception {
        var s = sessions(config(2), clock());
        s.open(A);
        // the DDL tenant create runs: it needs AccessExclusive on the model-level chunks partition, which a held
        // transaction that had read chunks would block (spike round 2: 503 after 3.45 s with 467 locks held)
        CompletableFuture<Void> create = CompletableFuture.runAsync(() -> {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                PgContainerHelper.ensureTenantPartitions(DSL.using(su, SQLDialect.POSTGRES), "t-export-newcomer");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        create.get(10, TimeUnit.SECONDS);
        assertThat(create).isCompleted();
    }

    // ── caps ─────────────────────────────────────────────────────────────────────────────────────

    @Test
    void oneSessionPerTenantAndACapOnTheEngine() {
        var s = sessions(config(2), clock());
        var a = s.open(A).session();
        assertThatThrownBy(() -> s.open(A)).isInstanceOfSatisfying(TenantExportSessions.SessionExistsException.class,
            e -> assertThat(e.sessionId()).isEqualTo(a.id()));
        s.open(B);
        assertThat(s.openCount()).isEqualTo(2);
        assertThatThrownBy(() -> s.open("t-export-c")).isInstanceOfSatisfying(
            TenantExportSessions.CapacityFullException.class, e -> assertThat(e.retryAfterSeconds()).isEqualTo(30));

        s.close(A, a.id());
        s.open("t-export-c");   // the freed slot is usable at once
        assertThat(s.openCount()).isEqualTo(2);
    }

    @Test
    void aSessionIsInvisibleToEveryOtherTenantAndToAWrongId() {
        var s = sessions(config(2), clock());
        var a = s.open(A).session();
        assertThat(s.lookup(A, a.id())).isSameAs(a);
        assertThatThrownBy(() -> s.lookup(B, a.id())).isInstanceOf(TenantExportSessions.NoSuchSessionException.class);
        assertThatThrownBy(() -> s.lookup(A, "00000000-0000-4000-8000-000000000000"))
            .isInstanceOf(TenantExportSessions.NoSuchSessionException.class);
        assertThatThrownBy(() -> s.close(B, a.id())).isInstanceOf(TenantExportSessions.NoSuchSessionException.class);
        assertThat(s.openCount()).as("a stranger's close left the session open").isEqualTo(1);
    }

    @Test
    void closeAndEvictReleaseTheHeldConnection() {
        var s = sessions(config(2), clock());
        int before = ds.getHikariPoolMXBean().getActiveConnections();
        var a = s.open(A).session();
        assertThat(ds.getHikariPoolMXBean().getActiveConnections()).isEqualTo(before + 1);
        s.close(A, a.id());
        assertThat(ds.getHikariPoolMXBean().getActiveConnections()).isEqualTo(before);
        assertThatThrownBy(() -> s.lookup(A, a.id())).isInstanceOf(TenantExportSessions.NoSuchSessionException.class);

        s.open(A);
        assertThat(s.evictTenant(A)).isEqualTo(1);
        assertThat(s.evictTenant(A)).isZero();
        assertThat(ds.getHikariPoolMXBean().getActiveConnections()).isEqualTo(before);
    }

    // ── bounds, on an injected clock ─────────────────────────────────────────────────────────────

    @Test
    void anIdleSessionIsClosedAtTheIdleBoundAndItsConnectionReturned() {
        var clock = clock();
        var s = sessions(config(2), clock);
        int before = ds.getHikariPoolMXBean().getActiveConnections();
        var a = s.open(A).session();

        clock.advance(Duration.ofMinutes(9).plusSeconds(59));
        assertThat(s.sweep()).as("inside the idle bound").isZero();
        assertThat(s.lookup(A, a.id())).as("a request restarts the idle clock").isSameAs(a);

        clock.advance(Duration.ofMinutes(9).plusSeconds(59));
        assertThat(s.sweep()).isZero();
        s.lookup(A, a.id());

        clock.advance(Duration.ofMinutes(10));
        assertThat(s.sweep()).as("10 minutes since the last request").isEqualTo(1);
        assertThat(s.openCount()).isZero();
        assertThat(ds.getHikariPoolMXBean().getActiveConnections()).isEqualTo(before);
        assertThatThrownBy(() -> s.lookup(A, a.id())).isInstanceOf(TenantExportSessions.NoSuchSessionException.class);
    }

    @Test
    void aSessionKeptBusyIsStillClosedAtTheTotalBound() {
        var clock = clock();
        var s = sessions(config(2), clock);
        var a = s.open(A).session();
        for (int i = 0; i < 6; i++) {          // 6 x 9 min = 54 min of requests, never idle for 10
            clock.advance(Duration.ofMinutes(9));
            assertThat(s.lookup(A, a.id())).isSameAs(a);
        }
        clock.advance(Duration.ofMinutes(5).plusSeconds(59));   // 59 min 59 s since open
        assertThat(s.lookup(A, a.id())).isSameAs(a);
        clock.advance(Duration.ofSeconds(1));                   // 60 min
        assertThatThrownBy(() -> s.lookup(A, a.id())).isInstanceOf(TenantExportSessions.NoSuchSessionException.class);
        assertThat(s.openCount()).isZero();
        s.open(A);   // and the tenant may start again
    }

    @Test
    void theBoundsAreConfigurableFromTheEnvironment() {
        Config c = Config.fromEnv(Map.of(TenantExportSessions.ENV_TOTAL_SECONDS, "120",
            TenantExportSessions.ENV_IDLE_SECONDS, "30", TenantExportSessions.ENV_MAX_SESSIONS, "4")::get);
        assertThat(c.totalBound()).isEqualTo(Duration.ofSeconds(120));
        assertThat(c.idleBound()).isEqualTo(Duration.ofSeconds(30));
        assertThat(c.maxSessions()).isEqualTo(4);
        Config d = Config.fromEnv(k -> null);
        assertThat(d.totalBound()).as("Sam, 2026-11-11: 60 minutes").isEqualTo(Duration.ofMinutes(60));
        assertThat(d.idleBound()).isEqualTo(Duration.ofMinutes(10));
        assertThat(d.pageBytes()).isEqualTo(16 * 1024 * 1024);
        assertThatThrownBy(() -> Config.fromEnv(Map.of(TenantExportSessions.ENV_IDLE_SECONDS, "soon")::get))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> Config.fromEnv(Map.of(TenantExportSessions.ENV_TOTAL_SECONDS, "0")::get))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── the header ───────────────────────────────────────────────────────────────────────────────

    @Test
    void headerNamesEveryExportableTableWithItsLiveNonGeneratedColumnsAndTheTenantsModels() throws Exception {
        var s = sessions(config(2), clock());
        var headerA = s.open(A).header();
        var headerB = s.open(B).header();

        Map<String, TenantExportRegistryParityTest.LiveTable> live;
        try (Connection su = pg.createConnection("")) {
            live = TenantExportRegistryParityTest.readLiveRlsTables(DSL.using(su, SQLDialect.POSTGRES));
        }
        List<String> expectedTables = TenantExport.TABLES.stream().filter(ExportTable::exported)
            .map(ExportTable::name).toList();
        assertThat(headerA.tables().stream().map(TenantExportReads.TableHeader::name)).containsExactlyElementsOf(expectedTables);
        int generatedSkipped = 0;
        for (var t : headerA.tables()) {
            var liveTable = live.get(t.name());
            List<Column> expected = liveTable.columns().values().stream()
                .filter(c -> c.generated() == null).map(c -> new Column(c.name(), c.type())).toList();
            assertThat(t.columns()).as(t.name()).isEqualTo(expected);
            assertThat(t.primaryKey()).as(t.name() + " key").isEqualTo(liveTable.primaryKey());
            generatedSkipped += liveTable.columns().size() - expected.size();
        }
        assertThat(generatedSkipped).as("generated columns left out of the rows (fts_vector)").isGreaterThanOrEqualTo(3);

        assertThat(headerA.embeddingModels()).extracting(m -> m.get("embedding_model")).containsExactly(BGE);
        assertThat(headerB.embeddingModels()).extracting(m -> m.get("embedding_model")).containsExactly(MINILM);
        assertThat(headerA.embeddingModels().get(0)).containsKeys("embedding_model", "dimension", "provider");
    }

    // ── pages ────────────────────────────────────────────────────────────────────────────────────

    /** Every chunk key of {@code tenant} in primary-key order, as the cursor's texts. */
    private List<List<String>> chunkKeysInOrder(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<List<String>> keys = new ArrayList<>();
            for (Record r : ctx.select(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL)
                    .from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
                    .orderBy(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).fetch()) {
                keys.add(List.of(r.get(CHUNKS.TENANT_ID), r.get(CHUNKS.COLLECTION),
                    HexFormat.of().formatHex(r.get(CHUNKS.CHASH)), r.get(CHUNKS.EMBEDDING_MODEL)));
            }
            return keys;
        }
    }

    @Test
    void pagesWalkTheChunksCompositeKeyInOrderWithoutGapOrRepeat() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable chunks = table("chunks");
        List<List<String>> expected = chunkKeysInOrder(A);
        assertThat(expected).hasSize(A_CHUNKS);

        int target = 20_000;          // rows are about 3.2 KB, so about six to a page
        List<Object> after = null;
        int total = 0;
        int pages = 0;
        Long count = null;
        while (pages++ < 100) {
            TenantExportReads.Page page;
            try {
                page = s.reads().page(session.snapshotId(), A, chunks, after, target, 100_000);
            } catch (RuntimeException e) {
                throw new AssertionError("page " + pages + " after " + total + " rows failed", e);
            }
            if (after == null) {
                count = page.tableRows();
            } else {
                assertThat(page.tableRows()).as("only a table's first page counts it").isNull();
            }
            total += page.rows();
            assertThat(page.body().length).as("a page is cut near the byte target").isLessThan(target + 8_000);
            if (page.rows() > 0) {
                assertThat(TenantExportKeys.decode(page.lastKey(), chunks.primaryKey()))
                    .as("page %d ends at the key in position %d", pages, total).isEqualTo(expected.get(total - 1));
            }
            if (page.done()) break;
            assertThat(page.rows()).isPositive();
            after = after(chunks, page.lastKey());
        }
        assertThat(total).isEqualTo(A_CHUNKS);
        assertThat(count).isEqualTo((long) A_CHUNKS);
        assertThat(pages).as("a 20 KB target needs several pages for about 130 KB").isGreaterThan(4);
    }

    @Test
    void aTextKeyWithQuotesBackslashesAndUnicodeIsBoundExactly() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable meta = table("catalog_meta");
        List<String> expected;
        try (Connection su = pg.createConnection("")) {
            expected = DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_META.KEY).from(CATALOG_META)
                .where(CATALOG_META.TENANT_ID.eq(A)).orderBy(CATALOG_META.TENANT_ID, CATALOG_META.KEY)
                .fetch(CATALOG_META.KEY);
        }
        assertThat(expected).contains("it's", "back\\slash", "\u00e9clair", "z", "quote\"d", "semi;colon", "100%");
        List<Object> after = null;
        List<String> seen = new ArrayList<>();
        for (int i = 0; i < 20; i++) {
            var page = s.reads().page(session.snapshotId(), A, meta, after, 1 << 20, 1);   // one row a page
            if (page.rows() == 0) {
                assertThat(page.done()).isTrue();
                break;
            }
            assertThat(page.rows()).isEqualTo(1);
            seen.add(TenantExportKeys.decode(page.lastKey(), meta.primaryKey()).get(1));
            after = after(meta, page.lastKey());
        }
        assertThat(seen).isEqualTo(expected);
    }

    @Test
    void laterWritesAreInvisibleToLaterPagesAndTheStampedTenantDecidesWhatIsRead() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable memory = table("memory");
        var first = s.reads().page(session.snapshotId(), A, memory, null, 1 << 20, 100_000);
        assertThat(first.tableRows()).isEqualTo(5L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);
            insertMemory(sctx, A, "written-after-the-session-opened");
            sctx.deleteFrom(MEMORY).where(MEMORY.TENANT_ID.eq(A)).and(MEMORY.TITLE.eq("title-0")).execute();
            var later = s.reads().page(session.snapshotId(), A, memory, null, 1 << 20, 100_000);
            assertThat(later.tableRows()).as("the session's snapshot, not the table's present state").isEqualTo(5L);
            assertThat(later.rows()).isEqualTo(5);
            assertThat(later.body()).isEqualTo(first.body());
            // the snapshot carries no tenant: the same snapshot under B's stamp reads B's rows
            assertThat(s.reads().page(session.snapshotId(), B, memory, null, 1 << 20, 100_000).tableRows()).isEqualTo(3L);
            // restore the fixture for the other tests
            sctx.deleteFrom(MEMORY).where(MEMORY.TITLE.eq("written-after-the-session-opened")).execute();
            insertMemory(sctx, A, "title-0");
        }
    }

    @Test
    void aClosedSessionsSnapshotAnswersGone() {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable memory = table("memory");
        s.reads().page(session.snapshotId(), A, memory, null, 1 << 20, 100);
        s.close(A, session.id());
        assertThatThrownBy(() -> s.reads().page(session.snapshotId(), A, memory, null, 1 << 20, 100))
            .isInstanceOf(TenantExportReads.SnapshotGoneException.class);
        assertThatThrownBy(() -> s.reads().header(session.snapshotId(), A))
            .isInstanceOf(TenantExportReads.SnapshotGoneException.class);
    }

    @Test
    void aSnapshotIdThatIsNotOneIsRefusedBeforeAnyStatement() {
        var s = sessions(config(2), clock());
        for (String bad : List.of("'; DROP TABLE nexus.memory; --", "", "00000003-0000001B-1' OR 'a'='a", "xyz")) {
            assertThatThrownBy(() -> s.reads().page(bad, A, table("memory"), null, 1 << 20, 100))
                .as(bad).isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    void aPageWaitingBehindATableLockIsBusyAndNotHung() throws Exception {
        var s = sessions(config(2), clock());   // lock_timeout 300 ms
        var session = s.open(A).session();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, MEMORY, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            assertThatThrownBy(() -> s.reads().page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100))
                .isInstanceOf(TenantExportReads.PageBusyException.class);
            assertThat(Duration.ofNanos(System.nanoTime() - t0)).isLessThan(Duration.ofSeconds(10));
            holder.rollback();
        }
        // and once the lock is gone the same page reads
        assertThat(s.reads().page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100).rows()).isEqualTo(5);
    }

    // ── the sweeper, on the real clock ───────────────────────────────────────────────────────────

    @Test
    void theBackgroundSweeperClosesAnAbandonedSessionOnTheRealClock() throws Exception {
        Config quick = Config.defaults().withBounds(Duration.ofMinutes(1), Duration.ofMillis(300))
            .withSweepInterval(Duration.ofMillis(100));
        var s = new TenantExportSessions(ds, quick, java.time.Clock.systemUTC());
        opened.add(s);
        int activeBefore = ds.getHikariPoolMXBean().getActiveConnections();
        var session = s.open(A).session();
        Integer pid = DSL.using(session.heldConnectionForTests(), SQLDialect.POSTGRES)
            .select(DSL.function("pg_backend_pid", Integer.class)).fetchOne(0, Integer.class);
        assertThat(s.openCount()).isEqualTo(1);

        // nothing calls the session again: only the sweeper can end it
        long deadline = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        while (s.openCount() > 0 && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(s.openCount()).as("the sweeper expired the idle session").isZero();
        // the connection is returned after the session leaves the map: poll, with a deadline
        long returned = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        while (ds.getHikariPoolMXBean().getActiveConnections() != activeBefore && System.nanoTime() < returned) {
            Thread.sleep(25);
        }
        assertThat(ds.getHikariPoolMXBean().getActiveConnections()).as("its connection is back in the pool")
            .isEqualTo(activeBefore);
        try (Connection su = pg.createConnection("")) {
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);
            Field<String> state = DSL.field(DSL.name("state"), String.class);
            var states = sctx.select(state).from(DSL.table(DSL.name("pg_stat_activity")))
                .where(DSL.field(DSL.name("pid"), Integer.class).eq(pid)).fetch(state);
            assertThat(states).as("the held transaction is gone (the backend may live on, idle, in the pool)")
                .doesNotContain("idle in transaction");
            assertThat(sctx.fetchCount(DSL.table(DSL.name("pg_stat_activity")),
                DSL.field(DSL.name("query"), String.class).likeIgnoreCase("%pg_export_snapshot%")
                    .and(state.eq("idle in transaction"))))
                .as("no export snapshot is held by any backend").isZero();
        }
    }

    @Test
    void aSessionWithAPageInFlightIsNeverExpiredAsIdleAndIdleRestartsWhenThePageEnds() throws Exception {
        var clock = clock();
        var s = sessions(config(2), clock);
        var session = s.open(A).session();
        CountDownLatch inPage = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        CompletableFuture<String> page = CompletableFuture.supplyAsync(() -> s.withPage(session, held -> {
            inPage.countDown();
            try {
                release.await(30, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return "read";
        }));
        assertThat(inPage.await(10, TimeUnit.SECONDS)).isTrue();

        clock.advance(Duration.ofMinutes(11));
        assertThat(s.sweep()).as("a page is being read: that is not idle time").isZero();
        assertThat(s.openCount()).isEqualTo(1);
        assertThatThrownBy(() -> s.withPage(session, held -> "second"))
            .isInstanceOf(TenantExportSessions.PageInFlightException.class);

        release.countDown();
        assertThat(page.get(10, TimeUnit.SECONDS)).isEqualTo("read");
        assertThat(s.sweep()).as("the idle clock restarted when the page ended").isZero();
        clock.advance(Duration.ofMinutes(10));
        assertThat(s.sweep()).isEqualTo(1);
    }

    @Test
    void theStatementsOfOnePageShareOneTimeBudget() throws Exception {
        // the count and the COPY are two statements; the work between them (here a sleep) spends the budget, so the
        // COPY is refused. A bound restarted in full per statement would let this page through.
        var s = sessions(config(2).withPageBudgetMs(500), clock());
        var session = s.open(A).session();
        s.reads().beforeCopyForTests = ctx -> {
            try {
                Thread.sleep(700);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        };
        assertThatThrownBy(() -> s.reads().page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100))
            .isInstanceOf(TenantExportReads.PageOverBudgetException.class).hasMessageContaining("whole time budget");
        s.reads().beforeCopyForTests = null;
        assertThat(s.reads().page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100).rows()).isEqualTo(5);
    }

    @Test
    void closeFinishesTheExpiriesQueuedOnTheSweeperInsteadOfDroppingThem() throws Exception {
        var clock = clock();
        var s = sessions(config(2), clock);
        int before = ds.getHikariPoolMXBean().getActiveConnections();
        var a = s.open(A).session();
        var sweeper = s.sweeperForTests();
        CountDownLatch busy = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        sweeper.execute(() -> {   // the sweep thread is occupied, so the hand-off below queues behind this
            busy.countDown();
            try {
                release.await(30, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        });
        assertThat(busy.await(10, TimeUnit.SECONDS)).isTrue();
        clock.advance(Duration.ofMinutes(11));
        assertThatThrownBy(() -> s.lookup(A, a.id())).isInstanceOf(TenantExportSessions.NoSuchSessionException.class);
        assertThat(s.openCount()).isZero();
        assertThat(ds.getHikariPoolMXBean().getActiveConnections()).as("its rollback is queued, not done").isEqualTo(before + 1);

        CompletableFuture<Void> closing = CompletableFuture.runAsync(s::close);
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (!sweeper.isShutdown() && System.nanoTime() < deadline) {
            Thread.sleep(10);
        }
        assertThat(sweeper.isShutdown()).isTrue();
        release.countDown();
        closing.get(15, TimeUnit.SECONDS);
        assertThat(ds.getHikariPoolMXBean().getActiveConnections()).as("close ran the queued expiry").isEqualTo(before);
    }

    @Test
    void aPoolTooSmallForTheDefaultCapClampsItAndAnExplicitCapIsRefused() {
        Function<String, String> unset = k -> null;
        assertThat(Config.forPool(unset, 10).maxSessions()).isEqualTo(2);
        assertThat(Config.forPool(unset, 4).maxSessions()).isEqualTo(2);
        assertThat(Config.forPool(unset, 3).maxSessions()).as("pool 3 carries one").isEqualTo(1);
        assertThat(Config.forPool(unset, 2).maxSessions()).as("pool 2 carries none: export is off").isZero();
        assertThat(Config.forPool(unset, 1).maxSessions()).isZero();
        for (int pool = 1; pool <= 12; pool++) {   // the clamp is exactly the largest cap the headroom rule accepts
            int carried = Config.carriedSessions(pool);
            if (carried > 0) Config.defaults().withMaxSessions(carried).requirePoolHeadroom(pool);
            if (carried + 1 <= 64) {
                final int p = pool;
                assertThatThrownBy(() -> Config.defaults().withMaxSessions(carried + 1).requirePoolHeadroom(p))
                    .as("pool %d does not carry %d", pool, carried + 1).isInstanceOf(IllegalArgumentException.class);
            }
        }
        Function<String, String> two = Map.of(TenantExportSessions.ENV_MAX_SESSIONS, "2")::get;
        assertThat(Config.forPool(two, 4).maxSessions()).isEqualTo(2);
        assertThatThrownBy(() -> Config.forPool(two, 3)).isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("NX_EXPORT_MAX_SESSIONS=2").hasMessageContaining("at least 4").hasMessageContaining("it is 3");
        assertThat(Config.forPool(Map.of(TenantExportSessions.ENV_MAX_SESSIONS, "1")::get, 3).maxSessions()).isEqualTo(1);
        assertThatThrownBy(() -> Config.forPool(Map.of(TenantExportSessions.ENV_MAX_SESSIONS, "0")::get, 10))
            .as("zero is not a way to set the cap; the pool's size is").isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void anEngineWhosePoolCarriesNoSessionAnswersCapacityFullToEveryOpen() {
        var s = sessions(config(0), clock());
        assertThatThrownBy(() -> s.open(A)).isInstanceOf(TenantExportSessions.CapacityFullException.class);
        assertThat(s.openCount()).isZero();
    }

    // ── time budget, lock bound, progress and the cut ────────────────────────────────────────────

    @Test
    void aPageRunningPastItsTimeBudgetIsCancelledAsOverBudgetNotBusyAndNotHung() throws Exception {
        // a lock wait far longer than the budget: it is the statement timeout that ends the wait
        var s = sessions(config(2).withLockTimeoutMs(60_000).withPageBudgetMs(400), clock());
        var session = s.open(A).session();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, MEMORY, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            assertThatThrownBy(() -> s.reads().page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100))
                .isInstanceOf(TenantExportReads.PageOverBudgetException.class);
            assertThat(Duration.ofNanos(System.nanoTime() - t0)).as("cut at the 400 ms budget, not the 60 s lock bound")
                .isLessThan(Duration.ofSeconds(10));
            holder.rollback();
        }
        assertThat(s.reads().page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100).rows()).isEqualTo(5);
    }

    @Test
    void aTargetSmallerThanOneRowStillMakesProgressSoNoPageIsEmptyAndNotDone() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable chunks = table("chunks");
        List<Object> after = null;
        int total = 0;
        for (int pages = 0; pages < 60; pages++) {
            var page = s.reads().page(session.snapshotId(), A, chunks, after, 1, 100_000);   // one byte: below any row
            if (page.done()) break;
            assertThat(page.rows()).as("a page that is not the last holds a row").isEqualTo(1);
            total += page.rows();
            after = after(chunks, page.lastKey());
        }
        assertThat(total).isEqualTo(A_CHUNKS);
    }

    @Test
    void aSinkThatFailsWithARuntimeExceptionLeavesNoPoisonedConnectionInThePool() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable chunks = table("chunks");
        s.reads().sinkWrapperForTests = sink -> new java.io.OutputStream() {
            @Override
            public void write(int b) {
                throw new IllegalStateException("boom");
            }

            @Override
            public void write(byte[] b, int off, int len) {
                throw new IllegalStateException("boom");
            }
        };
        assertThatThrownBy(() -> s.reads().page(session.snapshotId(), A, chunks, null, 1 << 20, 100_000))
            .isInstanceOf(IllegalStateException.class).hasMessageContaining("boom");
        s.reads().sinkWrapperForTests = null;
        for (int i = 0; i < 12; i++) {   // every connection of a pool of 8 is borrowed more than once
            assertThat(s.reads().page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100).rows()).isEqualTo(5);
            assertThat(s.reads().page(session.snapshotId(), A, chunks, null, 20_000, 100_000).rows()).isPositive();
        }
    }

    // ── keys of other types, and a text key under standard_conforming_strings off ────────────────

    @Test
    void aTimestamptzKeyPagesAcrossItsBoundaryWithMicrosecondsIntact() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable telemetry = table("search_telemetry");
        List<String> expected;
        try (Connection su = pg.createConnection("")) {
            expected = DSL.using(su, SQLDialect.POSTGRES).select(SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH)
                .from(SEARCH_TELEMETRY).where(SEARCH_TELEMETRY.TENANT_ID.eq(A))
                .orderBy(SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                    SEARCH_TELEMETRY.COLLECTION)
                .fetch(r -> r.get(SEARCH_TELEMETRY.TS).toInstant() + "|" + r.get(SEARCH_TELEMETRY.QUERY_HASH));
        }
        assertThat(expected).hasSize(7);
        assertThat(expected.get(0)).startsWith("2026-11-12T10:15:30.123456Z");
        List<Object> after = null;
        List<String> seen = new ArrayList<>();
        for (int i = 0; i < 20; i++) {
            var page = s.reads().page(session.snapshotId(), A, telemetry, after, 1 << 20, 2);   // two rows a page: the
            if (page.rows() == 0) {                                                             // boundary splits a shared instant
                assertThat(page.done()).isTrue();
                break;
            }
            // the page's last row is read back through the cursor's own text form
            var key = TenantExportKeys.decode(page.lastKey(), telemetry.primaryKey());
            seen.add(Instant.parse(key.get(1)) + "|" + key.get(2));
            after = after(telemetry, page.lastKey());
        }
        // each page's last key is the key of position 2, 4, 6 and 7
        assertThat(seen).containsExactly(expected.get(1), expected.get(3), expected.get(5), expected.get(6));
    }

    @Test
    void aTextKeyWithABackslashAndAQuoteIsExactEvenWhenStandardConformingStringsIsOff() throws Exception {
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        ExportTable meta = table("catalog_meta");
        List<String> expected;
        try (Connection su = pg.createConnection("")) {
            expected = DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_META.KEY).from(CATALOG_META)
                .where(CATALOG_META.TENANT_ID.eq(A)).orderBy(CATALOG_META.TENANT_ID, CATALOG_META.KEY)
                .fetch(CATALOG_META.KEY);
        }
        boolean[] wasOff = {false};
        s.reads().beforeCopyForTests = ctx -> {
            ctx.select(DSL.function("set_config", String.class, DSL.inline("standard_conforming_strings"),
                DSL.inline("off"), DSL.inline(true))).fetch();
            wasOff[0] = "off".equals(ctx.select(DSL.function("current_setting", String.class,
                DSL.inline("standard_conforming_strings"))).fetchOne(0, String.class));
        };
        List<Object> after = null;
        List<String> seen = new ArrayList<>();
        for (int i = 0; i < 30; i++) {
            var page = s.reads().page(session.snapshotId(), A, meta, after, 1 << 20, 1);
            if (page.rows() == 0) break;
            seen.add(TenantExportKeys.decode(page.lastKey(), meta.primaryKey()).get(1));
            after = after(meta, page.lastKey());
        }
        assertThat(wasOff[0]).as("the setting really was off while the statement ran").isTrue();
        assertThat(seen).as("a backslash key and a quote key neither skip nor repeat a row").isEqualTo(expected);
        assertThat(expected).contains("back\\slash", "it's");
    }

    // ── config ───────────────────────────────────────────────────────────────────────────────────

    @Test
    void theTunablesAreRangedSoNothingWrapsOverflowsOrYieldsAnEmptyPage() {
        for (var bad : List.of(Map.entry(TenantExportSessions.ENV_PAGE_BYTES, "0"),
            Map.entry(TenantExportSessions.ENV_PAGE_BYTES, "1000"),
            Map.entry(TenantExportSessions.ENV_PAGE_BYTES, "19000001"),
            Map.entry(TenantExportSessions.ENV_PAGE_BYTES, "4294967297"),
            Map.entry(TenantExportSessions.ENV_MAX_SESSIONS, "4294967298"),
            Map.entry(TenantExportSessions.ENV_MAX_SESSIONS, "65"),
            Map.entry(TenantExportSessions.ENV_TOTAL_SECONDS, "9223372036854775807"),
            Map.entry(TenantExportSessions.ENV_TOTAL_SECONDS, "604801"),
            Map.entry(TenantExportSessions.ENV_IDLE_SECONDS, "-5"),
            Map.entry(TenantExportSessions.ENV_PAGE_BUDGET_SECONDS, "301"))) {
            assertThatThrownBy(() -> Config.fromEnv(Map.of(bad.getKey(), bad.getValue())::get))
                .as(bad.getKey() + "=" + bad.getValue()).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(bad.getKey());
        }
        Config top = Config.fromEnv(Map.of(TenantExportSessions.ENV_PAGE_BYTES, "19000000",
            TenantExportSessions.ENV_TOTAL_SECONDS, "604800")::get);
        assertThat(top.pageBytes()).isEqualTo(19_000_000);
        assertThat(Instant.parse("2026-11-12T00:00:00Z").plus(top.totalBound())).isAfter(Instant.parse("2026-11-12T00:00:00Z"));
        assertThat(Config.defaults().pageBudgetMs()).as("20 s, inside the stricter 30 s response-start limit").isEqualTo(20_000);
    }

    @Test
    void aSessionCapThePoolCannotCarryIsRefusedWithAMessageNamingBothSettings() {
        Config.defaults().requirePoolHeadroom(10);                   // 2 sessions: needs 4
        Config.defaults().withMaxSessions(5).requirePoolHeadroom(10);   // 5 sessions: needs 10
        assertThatThrownBy(() -> Config.defaults().withMaxSessions(6).requirePoolHeadroom(10))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("NX_EXPORT_MAX_SESSIONS=6")
            .hasMessageContaining("NX_POOL_SIZE of at least 12").hasMessageContaining("it is 10");
        assertThatThrownBy(() -> Config.defaults().withMaxSessions(1).requirePoolHeadroom(2))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("at least 3");
    }

    // ── gate N1: the plan of a deep chunks page ──────────────────────────────────────────────────

    @Test
    void aDeepChunksPageIsAPerLeafIndexScanUnderMergeAppendWithNoSort() throws Exception {
        ExportTable chunks = table("chunks");
        List<List<String>> keys = chunkKeysInOrder(A);
        List<Object> deep = TenantExportKeys.binds(chunks.primaryKey(), keys.get(keys.size() / 2));
        String plan = new TenantScope(explainDs).withTenant(A, ctx -> {
            // the planner pins other tests use for a plan at fixture scale: it must be able to choose this plan
            for (String guc : List.of("enable_seqscan", "enable_bitmapscan", "enable_sort")) {
                PgSession.setLocal(ctx, guc, "off");
            }
            var columns = TenantExportReads.liveColumns(ctx, chunks);
            return PgContainerHelper.explainPlanWith(ctx,
                TenantExportReads.pageSelect(ctx, chunks, columns, A, deep, 1000),
                new PgContainerHelper.ExplainOptions(false, false, false, false));
        });

        List<String> nodes = plan.lines().map(l -> l.replaceFirst("^\\s*(->\\s*)?", "").trim()).toList();
        assertThat(nodes).as("the plan:%n%s", plan).contains("Merge Append");
        assertThat(nodes.stream().filter(n -> n.startsWith("Index Scan using") || n.startsWith("Index Only Scan using")))
            .as("one index scan per tenant leaf of each model partition:%n%s", plan).hasSizeGreaterThanOrEqualTo(2);
        assertThat(nodes).as("no sort of the page:%n%s", plan).doesNotContain("Sort");
        assertThat(nodes).noneMatch(n -> n.startsWith("Incremental Sort") || n.startsWith("Seq Scan"));
        assertThat(nodes).anyMatch(n -> n.equals("Limit"));
        assertThat(plan).as("the keyset is a row-value comparison on the key, not an OR chain").contains("> ROW(");
    }

    /**
     * The plan the shipped page code runs, on analyzed tables, with the test setting NO planner GUC. Gate N1 scale run
     * (RDR-248, nexus-csayy.4): on analyzed tables the planner costs the keyset's row-value comparison at one row, so it
     * chose Limit -> Sort -> Append -> Index Scan and the sort read every remaining row of the tenant before the first
     * went out: a page cost time linear in the tenant's size. The sibling test above pins the planner itself and so only
     * proves the sort-free plan is possible; this one reads the plan from inside the page's own transaction, through the
     * page's own seam, so whatever the page sets for itself is what is explained.
     */
    @Test
    void aDeepChunksPageOnAnalyzedTablesRunsNoSortUnderThePlannerSettingsThePageSetsItself() throws Exception {
        ExportTable chunks = table("chunks");
        List<List<String>> keys = chunkKeysInOrder(A);
        List<Object> deep = TenantExportKeys.binds(chunks.primaryKey(), keys.get(keys.size() / 2));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        // a reads object on the role that may call the test schema's explain function; the snapshot is the engine pool's
        var reads = new TenantExportReads(explainDs, 300, 20_000);
        String[] plan = new String[1];
        String[] enableSort = new String[1];
        reads.beforeCopyForTests = ctx -> {
            enableSort[0] = ctx.select(DSL.function("current_setting", String.class, DSL.inline("enable_sort")))
                .fetchOne(0, String.class);
            var columns = TenantExportReads.liveColumns(ctx, chunks);
            plan[0] = PgContainerHelper.explainPlanWith(ctx,
                TenantExportReads.pageSelect(ctx, chunks, columns, A, deep, 5),
                new PgContainerHelper.ExplainOptions(false, false, false, false));
        };
        var page = reads.page(session.snapshotId(), A, chunks, deep, 1 << 20, 5);
        assertThat(page.rows()).as("the page still reads its rows").isEqualTo(5);

        List<String> nodes = plan[0].lines().map(l -> l.replaceFirst("^\\s*(->\\s*)?", "").trim()).toList();
        assertThat(nodes).as("no sort of the page (the planner settings were the page's own):%n%s", plan[0])
            .noneMatch(n -> n.equals("Sort") || n.startsWith("Incremental Sort") || n.startsWith("Seq Scan"));
        assertThat(nodes).as("the leaves merge in key order:%n%s", plan[0]).contains("Merge Append");
        assertThat(nodes.stream().filter(n -> n.startsWith("Index Scan using") || n.startsWith("Index Only Scan using")))
            .as("an index scan per leaf, in key order:%n%s", plan[0]).hasSizeGreaterThanOrEqualTo(2);
        assertThat(plan[0]).as("the key is an index condition, not a filter:%n%s", plan[0]).contains("Index Cond");
        assertThat(enableSort[0]).as("the page's own setting, seen from inside its transaction").isEqualTo("off");

        // a table keyed by id alone is left to the planner: its small-tenant plan can be a sort of the tenant's few rows
        String[] memoryEnableSort = new String[1];
        reads.beforeCopyForTests = ctx -> memoryEnableSort[0] = ctx.select(
            DSL.function("current_setting", String.class, DSL.inline("enable_sort"))).fetchOne(0, String.class);
        reads.page(session.snapshotId(), A, table("memory"), null, 1 << 20, 100);
        assertThat(memoryEnableSort[0]).as("memory's key is id alone: not pinned").isEqualTo("on");

        // transaction-local: the pooled connection the page used is back to the server default
        try (Connection c = explainDs.getConnection()) {
            assertThat(DSL.using(c, SQLDialect.POSTGRES)
                .select(DSL.function("current_setting", String.class, DSL.inline("enable_sort")))
                .fetchOne(0, String.class)).as("the setting did not leak to the pool").isEqualTo("on");
        }
    }

    /**
     * Every table the pin covers (an exported table whose primary key leads with {@code tenant_id}), ANALYZEd, no planner
     * setting of the test's own, a deep keyset page read through the page's own transaction: no Sort anywhere in the plan.
     * The tables the fixture leaves empty are covered too (the key is made up from the key's types), because the plan of a
     * page does not depend on the rows it will find.
     */
    @Test
    void aDeepPageOfEveryTableThePinCoversRunsNoSortOnAnalyzedTables() throws Exception {
        List<ExportTable> pinned = TenantExport.TABLES.stream().filter(ExportTable::exported)
            .filter(t -> "tenant_id".equals(t.primaryKey().get(0).name())).toList();
        List<ExportTable> unpinned = TenantExport.TABLES.stream().filter(ExportTable::exported)
            .filter(t -> !"tenant_id".equals(t.primaryKey().get(0).name())).toList();
        // not vacuous: the registry's tenant-led tables are many, and the id-keyed ones are the minority left to the planner
        assertThat(pinned.size()).as("tenant-led exported tables").isGreaterThanOrEqualTo(15);
        assertThat(unpinned.stream().map(ExportTable::name)).contains("memory");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (ExportTable t : pinned) {
                PgContainerHelper.analyzeTable(su, DSL.table(DSL.name(t.schema(), t.table())));
            }
        }
        var s = sessions(config(2), clock());
        var session = s.open(A).session();
        var reads = new TenantExportReads(explainDs, 300, 20_000);
        List<String> withSort = new ArrayList<>();
        int explained = 0;
        for (ExportTable t : pinned) {
            List<String> parts = new ArrayList<>();
            for (Column k : t.primaryKey()) {
                parts.add(switch (k.name().equals("tenant_id") ? "tenant" : k.type()) {
                    case "tenant" -> A;
                    case "text" -> "m";
                    case "bigint", "integer" -> "1";
                    case "bytea" -> "00";
                    case "uuid" -> "00000000-0000-0000-0000-000000000001";
                    case "timestamp with time zone" -> "2026-01-01T00:00:00Z";
                    default -> throw new IllegalStateException(t.name() + " key type " + k.type());
                });
            }
            List<Object> deep = TenantExportKeys.binds(t.primaryKey(), parts);
            String[] plan = new String[1];
            reads.beforeCopyForTests = ctx -> plan[0] = PgContainerHelper.explainPlanWith(ctx,
                TenantExportReads.pageSelect(ctx, t, TenantExportReads.liveColumns(ctx, t), A, deep, 5),
                new PgContainerHelper.ExplainOptions(false, false, false, false));
            reads.page(session.snapshotId(), A, t, deep, 1 << 20, 5);
            explained++;
            List<String> nodes = plan[0].lines().map(l -> l.replaceFirst("^\\s*(->\\s*)?", "").trim()).toList();
            if (nodes.stream().anyMatch(n -> n.equals("Sort") || n.startsWith("Incremental Sort"))) {
                withSort.add(t.name() + ":\n" + plan[0]);
            }
        }
        assertThat(explained).as("every pinned table was explained").isEqualTo(pinned.size());
        assertThat(withSort).as("tables whose deep page plan sorts").isEmpty();
    }
}
