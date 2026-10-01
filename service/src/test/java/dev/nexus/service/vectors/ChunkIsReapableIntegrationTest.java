// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgActivityProbe;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.types.DayToSecond;
import org.jooq.types.YearToMonth;
import org.jooq.types.YearToSecond;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7 (bead nexus-wbfpw.15): {@code nexus.chunk_is_reapable}, the ONE reapable(c)
 * predicate, as an inlinable set-returning SQL function, and the orphaning record
 * ({@code nexus.chunk_orphaned_at}, written by triggers on the manifest) that gives its grace window
 * its meaning.
 *
 * <p>Semantic units of the predicate:
 * <ul>
 *   <li>no own-collection manifest row in ANY owner state, plus {@code last_written_at < now() - grace};</li>
 *   <li>the grace window is injectable, and NULL means the default (30 days);</li>
 *   <li>a quarantine sibling is never reapable, by {@code lifecycle_state};</li>
 *   <li>it inlines (no function scan in the plan) under the RLS-subject role;</li>
 *   <li>used as the only WHERE of a DELETE it loses to a racing client write (READ COMMITTED lock-wait
 *       and recheck, nexus-wbfpw.43 review).</li>
 * </ul>
 * The orphaning record (vectors-021-1 and -3) is pinned here at the table level, with direct manifest DML;
 * the same property through the real writers is in {@code ReapableMidRunJourneyTest}. The S1a row table is in
 * {@code Rdr192EngineLivenessMatrixIntegrationTest}.
 *
 * <p>There is no in-flight-index pin to test: the metadata key it read is stamped by no writer, so the
 * record-at-orphaning rule replaced it (review T2 nexus/review-wbfpw15-17-code, C1). The record is a side
 * table and the triggers never UPDATE {@code nexus.chunks} (Sam, 2026-10-01): a chunk-row UPDATE
 * re-inserts the row into the 1024-d HNSW index, 11 s for 5000 chunks against 50 ms here.
 *
 * <p>Fixture chunks are inserted through direct substrate SQL, never through the write routes, which
 * from RDR-223 Phase 3 refuse an ownerless write.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkIsReapableIntegrationTest {

    private static final String SVC_ROLE = "svc_reapable_test";
    private static final String SVC_PASS = "svc_reapable_test_pass";

    private static final String KNOWLEDGE = "knowledge__reap-k__minilm-l6-v2-384__v1";
    private static final String DOCS = "docs__reap-d__minilm-l6-v2-384__v1";
    private static final String CODE = "code__reap-c__minilm-l6-v2-384__v1";
    private static final String RDR = "rdr__reap-r__minilm-l6-v2-384__v1";
    private static final String OTHER = "knowledge__reap-o__minilm-l6-v2-384__v1";

    private static final Duration DAYS_40 = Duration.ofDays(40);

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private static byte[] bytes(String chashHex) {
        return Chash.fromHex(chashHex).toBytes();
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private void register(String tenant, String collection) throws Exception {
        su(ctx -> PgContainerHelper.insertCollection(ctx, tenant, collection));
    }

    /** Inserts one chunk through substrate SQL, last written {@code age} ago. No manifest row. */
    private String orphan(String tenant, String collection, String seed, Duration age) throws Exception {
        String hex = ch(tenant + "/" + collection + "/" + seed);
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(Map.of()));
            ageTo(ctx, tenant, collection, hex, age);
        });
        return hex;
    }

    /** Makes the chunk old in every respect: its write time, and its orphaning record if it has one. */
    private static void ageTo(DSLContext ctx, String tenant, String collection, String hex, Duration age) {
        OffsetDateTime then = OffsetDateTime.now().minus(age);
        ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
           .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(bytes(hex))))
           .execute();
        ctx.update(CHUNK_ORPHANED_AT).set(CHUNK_ORPHANED_AT.ORPHANED_AT, then)
           .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection))
                  .and(CHUNK_ORPHANED_AT.CHASH.eq(bytes(hex))))
           .execute();
    }

    /** Writes (or overwrites) the chunk's orphaning record, {@code age} ago. A fixture for the predicate's tests. */
    private void recordOrphaning(String tenant, String collection, String hex, Duration age) throws Exception {
        OffsetDateTime then = OffsetDateTime.now().minus(age);
        su(ctx -> ctx.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION,
                CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.ORPHANED_AT)
            .values(tenant, collection, bytes(hex), then)
            .onConflict(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH)
            .doUpdate().set(CHUNK_ORPHANED_AT.ORPHANED_AT, then).execute());
    }

    private static YearToSecond interval(Duration d) {
        if (d == null) return null;
        return new YearToSecond(new YearToMonth(0, 0), DayToSecond.valueOf(d));
    }

    /** The one call shape every consumer uses: EXISTS over the function, correlated on the chunk row. */
    private static org.jooq.Condition reapable(Duration grace) {
        return DSL.exists(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
            CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.LAST_WRITTEN_AT,
            DSL.val(interval(grace), SQLDataType.INTERVAL))));
    }

    /** True when {@code reapable(c)} holds for the chunk, evaluated as the RLS-subject service role. */
    private boolean isReapable(String tenant, String collection, String hex, Duration grace) {
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                      .and(CHUNKS.CHASH.eq(bytes(hex))))
               .and(reapable(grace))));
    }

    private boolean isReapable(String tenant, String collection, String hex) {
        return isReapable(tenant, collection, hex, null);
    }

    private void document(String tenant, String tumbler, String collection, boolean tombstoned) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.DELETED_AT)
            .values(tenant, tumbler, "doc " + tumbler, collection, tombstoned ? OffsetDateTime.now().minusDays(1) : null)
            .onConflictDoNothing().execute());
    }

    private void manifestRow(String tenant, String docId, String collection, String hex, int position)
            throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .values(tenant, docId, position, bytes(hex), collection).execute());
    }

    private OffsetDateTime lastWrittenAt(String tenant, String collection, String hex) throws Exception {
        OffsetDateTime[] out = new OffsetDateTime[1];
        su(ctx -> out[0] = ctx.select(CHUNKS.LAST_WRITTEN_AT).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(bytes(hex))))
            .fetchOne(0, OffsetDateTime.class));
        return out[0];
    }

    /** The chunk's orphaning record, or null when it has none. */
    private OffsetDateTime orphanedAt(String tenant, String collection, String hex) throws Exception {
        OffsetDateTime[] out = new OffsetDateTime[1];
        su(ctx -> out[0] = ctx.select(CHUNK_ORPHANED_AT.ORPHANED_AT).from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection))
                   .and(CHUNK_ORPHANED_AT.CHASH.eq(bytes(hex))))
            .fetchOne(0, OffsetDateTime.class));
        return out[0];
    }

    /** True when the chunk has an orphaning record written within the last ten minutes. */
    private boolean orphanRecordedRecently(String tenant, String collection, String hex) throws Exception {
        OffsetDateTime at = orphanedAt(tenant, collection, hex);
        return at != null && at.isAfter(OffsetDateTime.now().minusMinutes(10));
    }

    private long recordCount(String tenant) throws Exception {
        long[] n = new long[1];
        su(ctx -> n[0] = ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant)));
        return n[0];
    }

    /** The physical tuple id and creating transaction of the chunk row: both change on any UPDATE of it. */
    private String rowVersion(String tenant, String collection, String hex) throws Exception {
        String[] out = new String[1];
        var ctid = DSL.field(DSL.name("ctid")).cast(SQLDataType.VARCHAR);
        var xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
        su(ctx -> {
            var r = ctx.select(ctid, xmin).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(bytes(hex))))
                .fetchOne();
            out[0] = r.get(ctid) + "/" + r.get(xmin);
        });
        return out[0];
    }

    // ── what the predicate means ─────────────────────────────────────────────

    @Test
    void anAgedChunkWithNoManifestRowIsReapable_andAFreshOneIsNot() throws Exception {
        String t = "reap-basic";
        register(t, KNOWLEDGE);
        String aged = orphan(t, KNOWLEDGE, "aged", DAYS_40);
        String fresh = orphan(t, KNOWLEDGE, "fresh", Duration.ZERO);

        assertThat(isReapable(t, KNOWLEDGE, aged)).as("aged past the 30 day default, no owner").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, fresh)).as("created now: inside the grace window").isFalse();
    }

    @Test
    void theCollectionPrefixDoesNotMatter_everyContentTypeIsCovered() throws Exception {
        String t = "reap-prefixes";
        for (String col : List.of(KNOWLEDGE, DOCS, CODE, RDR)) {
            register(t, col);
            String hex = orphan(t, col, "p", DAYS_40);
            assertThat(isReapable(t, col, hex)).as("reapable in %s", col).isTrue();
        }
    }

    @Test
    void anyOwnCollectionManifestRowMakesTheChunkNotReapable_inAnyOwnerState() throws Exception {
        String t = "reap-owner-states";
        register(t, KNOWLEDGE);
        String live = orphan(t, KNOWLEDGE, "live", DAYS_40);
        String tombstoned = orphan(t, KNOWLEDGE, "tomb", DAYS_40);
        document(t, "reap-live-doc", KNOWLEDGE, false);
        document(t, "reap-dead-doc", KNOWLEDGE, true);
        manifestRow(t, "reap-live-doc", KNOWLEDGE, live, 0);
        manifestRow(t, "reap-dead-doc", KNOWLEDGE, tombstoned, 0);
        // Adding a manifest row is not an orphaning; age them again to be certain of the premise.
        su(ctx -> {
            ageTo(ctx, t, KNOWLEDGE, live, DAYS_40);
            ageTo(ctx, t, KNOWLEDGE, tombstoned, DAYS_40);
        });

        assertThat(isReapable(t, KNOWLEDGE, live)).as("live owner").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, tombstoned))
            .as("tombstoned owner only: still has a manifest row, so purge_trash's arm, not this one").isFalse();
    }

    @Test
    void aManifestRowInAnotherCollectionDoesNotProtect() throws Exception {
        String t = "reap-other-collection";
        register(t, KNOWLEDGE);
        register(t, OTHER);
        String hex = orphan(t, KNOWLEDGE, "shared", DAYS_40);
        su(ctx -> PgContainerHelper.insertChunks(ctx, t, OTHER, List.of(hex), List.of("shared text"),
            List.of(new float[384]), List.of(Map.of())));
        document(t, "reap-other-doc", OTHER, false);
        manifestRow(t, "reap-other-doc", OTHER, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        assertThat(isReapable(t, KNOWLEDGE, hex)).as("GH #1546 shape: own collection has no row").isTrue();
        assertThat(isReapable(t, OTHER, hex)).as("owned in OTHER").isFalse();
    }

    // ── the grace window ─────────────────────────────────────────────────────

    @Test
    void theGraceWindowIsInjectable_andNullMeansTheThirtyDayDefault() throws Exception {
        String t = "reap-grace";
        register(t, KNOWLEDGE);
        String twoHours = orphan(t, KNOWLEDGE, "two-hours", Duration.ofHours(2));
        String thirtyOneDays = orphan(t, KNOWLEDGE, "thirty-one-days", Duration.ofDays(31));

        assertThat(isReapable(t, KNOWLEDGE, twoHours, Duration.ofHours(1))).as("1h grace, 2h old").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, twoHours, Duration.ofHours(3))).as("3h grace, 2h old").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, twoHours, Duration.ZERO)).as("zero grace").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, twoHours, null)).as("default 30d, 2h old").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, thirtyOneDays, null)).as("default 30d, 31d old").isTrue();
    }

    @Test
    void graceKeysOnLastWrittenAt_notCreatedAt() throws Exception {
        String t = "reap-grace-key";
        register(t, KNOWLEDGE);
        // created_at is old, last_written_at is fresh: a re-write refreshed it (nexus-wbfpw.43).
        String hex = orphan(t, KNOWLEDGE, "rewritten", DAYS_40);
        su(ctx -> ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
            .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex))))
            .execute());

        assertThat(isReapable(t, KNOWLEDGE, hex)).as("old created_at must not defeat a fresh last_written_at")
            .isFalse();
    }

    // ── quarantine siblings are never reapable ────────────────────────────────

    @Test
    void aQuarantineSiblingChunkIsNeverReapable_byLifecycleState() throws Exception {
        String t = "reap-quarantine";
        String origin = KNOWLEDGE;
        String sibling = "quarantine-" + KNOWLEDGE;
        register(t, origin);
        register(t, sibling);
        String inSibling = orphan(t, sibling, "quarantined", DAYS_40);
        String inOrigin = orphan(t, origin, "ordinary", DAYS_40);

        assertThat(isReapable(t, origin, inOrigin)).as("the same shape in an ordinary collection").isTrue();
        assertThat(isReapable(t, sibling, inSibling))
            .as("no manifest row and 40 days old, but the collection is a quarantine sibling").isFalse();
    }

    @Test
    void theQuarantineExclusionReadsLifecycleState_notTheName() throws Exception {
        String t = "reap-quarantine-state";
        String odd = "knowledge__reap-q-oddname__minilm-l6-v2-384__v1";   // no quarantine- prefix
        register(t, odd);
        su(ctx -> ctx.update(org.jooq.impl.DSL.table(DSL.name("nexus", "catalog_collections")))
            .set(DSL.field(DSL.name("lifecycle_state"), String.class), "quarantine")
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(t)
                   .and(DSL.field(DSL.name("name"), String.class).eq(odd))).execute());
        String hex = orphan(t, odd, "x", DAYS_40);

        assertThat(isReapable(t, odd, hex)).as("state, not name, decides").isFalse();
    }

    // ── the orphaning record (vectors-021-1 and -3) ───────────────────────────

    @Test
    void droppingTheLastManifestRowStartsTheClock_andAnAgedChunkIsNotInstantlyReapable() throws Exception {
        String t = "reap-stamp-delete";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "owned", DAYS_40);
        document(t, "stamp-d1", KNOWLEDGE, false);
        manifestRow(t, "stamp-d1", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));
        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("precondition: no record yet").isNull();
        OffsetDateTime written = lastWrittenAt(t, KNOWLEDGE, hex);
        String version = rowVersion(t, KNOWLEDGE, hex);

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-d1"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("the orphaning was recorded").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("ownerless for seconds, not 30 days").isFalse();
        assertThat(lastWrittenAt(t, KNOWLEDGE, hex)).as("last_written_at is a client-write clock; the record does not move it")
            .isEqualTo(written);
        assertThat(rowVersion(t, KNOWLEDGE, hex))
            .as("the chunk row was not rewritten (an UPDATE re-inserts it into every vector index)").isEqualTo(version);

        // and the record is what holds it: age the record alone and the chunk is reapable
        recordOrphaning(t, KNOWLEDGE, hex, DAYS_40);
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("record 40 days old, write 40 days old").isTrue();
    }

    @Test
    void thePredicateTakesTheLaterOfTheWriteAndTheRecord() throws Exception {
        String t = "reap-later-of";
        register(t, KNOWLEDGE);
        String recordedRecently = orphan(t, KNOWLEDGE, "recorded-recently", DAYS_40);
        String writtenRecently = orphan(t, KNOWLEDGE, "written-recently", DAYS_40);
        String bothOld = orphan(t, KNOWLEDGE, "both-old", DAYS_40);
        recordOrphaning(t, KNOWLEDGE, recordedRecently, Duration.ofMinutes(5));
        recordOrphaning(t, KNOWLEDGE, writtenRecently, DAYS_40);
        su(ctx -> ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
            .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(writtenRecently))))
            .execute());
        recordOrphaning(t, KNOWLEDGE, bothOld, DAYS_40);

        assertThat(isReapable(t, KNOWLEDGE, recordedRecently)).as("old write, orphaned 5 minutes ago").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, writtenRecently)).as("orphaned 40 days ago, written just now").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, bothOld)).as("old write, old record").isTrue();
    }

    /**
     * The race the first version of the stamp lost. Two documents own the same chunk (identical text in one
     * collection collapses to one chunk row by design); two writers drop the last two owners concurrently. A
     * trigger that asked "does a manifest row still exist?" saw the OTHER transaction's uncommitted row in each
     * transaction and recorded nothing, so both committed and the chunk was ownerless with its old clock and
     * reapable at once. The triggers now record every dropped key, after locking the chunk row, so the second
     * writer waits for the first and the chunk ends up recorded whichever order they commit in.
     */
    @Test
    void aConcurrentDropOfASharedChunkLeavesItStamped() throws Exception {
        String t = "reap-stamp-race";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "shared", DAYS_40);
        document(t, "stamp-r1", KNOWLEDGE, false);
        document(t, "stamp-r2", KNOWLEDGE, false);
        manifestRow(t, "stamp-r1", KNOWLEDGE, hex, 0);
        manifestRow(t, "stamp-r2", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));
        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("precondition: no record yet").isNull();

        try (Connection c1 = svcDs.getConnection(); Connection c2 = svcDs.getConnection()) {
            c1.setAutoCommit(false);
            c2.setAutoCommit(false);
            PgContainerHelper.setTenant(c1, TenantScope.DEFAULT_TENANT_GUC, t, true);
            PgContainerHelper.setTenant(c2, TenantScope.DEFAULT_TENANT_GUC, t, true);
            DSLContext w1 = DSL.using(c1, SQLDialect.POSTGRES);
            DSLContext w2 = DSL.using(c2, SQLDialect.POSTGRES);

            w1.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
              .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-r1"))).execute();
            CompletableFuture<Integer> second = CompletableFuture.supplyAsync(() ->
                w2.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                  .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-r2")))
                  .execute());
            // The second delete waits on the chunk row the first one's trigger locked; give it a moment to
            // reach the wait (it is not required to: a trigger that never waits is the broken one, and the
            // assertion below is what catches it).
            try {
                second.get(1500, TimeUnit.MILLISECONDS);
            } catch (java.util.concurrent.TimeoutException expected) {
                // blocked on the chunk row, as designed
            }
            c1.commit();
            second.get(30, TimeUnit.SECONDS);
            c2.commit();
        }

        assertThat(su1(t, "stamp-r1", "stamp-r2")).as("both owners are gone").isZero();
        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("the last owner left, so the clock started").isTrue();
        assertThat(recordCount(t)).as("one record for the one chunk").isEqualTo(1);
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("not reapable at once").isFalse();
    }

    private long su1(String tenant, String... docIds) throws Exception {
        long[] n = new long[1];
        su(ctx -> n[0] = ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.in(docIds))));
        return n[0];
    }

    /**
     * Recording a chunk another document still owns is harmless: it is not reapable (it has an owner), and
     * the record is what lets the clock start the moment the last owner goes. The record must exist: it is
     * the over-recording the design chose, and the assertion on it is what a trigger reduced to RETURN NULL
     * fails.
     */
    @Test
    void aDropOfOneOfTwoOwnersRecordsTheChunkAnyway_andItIsStillNotReapable() throws Exception {
        String t = "reap-stamp-shared";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "shared", DAYS_40);
        document(t, "stamp-s1", KNOWLEDGE, false);
        document(t, "stamp-s2", KNOWLEDGE, false);
        manifestRow(t, "stamp-s1", KNOWLEDGE, hex, 0);
        manifestRow(t, "stamp-s2", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-s1"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("recorded although stamp-s2 still owns it").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("another document still owns it").isFalse();

        // and when the second owner goes too, the record is refreshed, so the clock runs from THAT drop
        recordOrphaning(t, KNOWLEDGE, hex, DAYS_40);
        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-s2"))).execute());
        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("a stale record is refreshed by the last drop").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("ownerless for seconds").isFalse();
    }

    @Test
    void replacingARowWithADifferentChashRecordsTheOldChash_andOnlyThat() throws Exception {
        String t = "reap-stamp-update";
        register(t, KNOWLEDGE);
        String oldHex = orphan(t, KNOWLEDGE, "old", DAYS_40);
        String newHex = orphan(t, KNOWLEDGE, "new", DAYS_40);
        document(t, "stamp-u1", KNOWLEDGE, false);
        manifestRow(t, "stamp-u1", KNOWLEDGE, oldHex, 0);
        su(ctx -> {
            ageTo(ctx, t, KNOWLEDGE, oldHex, DAYS_40);
            ageTo(ctx, t, KNOWLEDGE, newHex, DAYS_40);
        });

        // The shape of append's ON CONFLICT (tenant, doc, position) DO UPDATE: the position now names another chash.
        su(ctx -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(newHex))
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-u1"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, oldHex)).as("lost its owner").isTrue();
        assertThat(orphanedAt(t, KNOWLEDGE, newHex)).as("gained one; untouched").isNull();
    }

    @Test
    void anUpdateThatDoesNotChangeTheChashRecordsNothing() throws Exception {
        String t = "reap-stamp-noop";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "kept", DAYS_40);
        document(t, "stamp-n1", KNOWLEDGE, false);
        manifestRow(t, "stamp-n1", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        su(ctx -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, 7)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-n1"))).execute());

        assertThat(orphanedAt(t, KNOWLEDGE, hex)).isNull();
    }

    @Test
    void tombstoningADocumentIsNotAnOrphaning_itsManifestRowsStay() throws Exception {
        String t = "reap-stamp-tombstone";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "tomb", DAYS_40);
        document(t, "stamp-t1", KNOWLEDGE, false);
        manifestRow(t, "stamp-t1", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq("stamp-t1"))).execute());

        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("the manifest row is still there").isNull();
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("dead, but purge_trash's, not reapable").isFalse();
    }

    @Test
    void aHardDocumentDeleteCascadesIntoTheRecord() throws Exception {
        String t = "reap-stamp-cascade";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "cascaded", DAYS_40);
        document(t, "stamp-c1", KNOWLEDGE, false);
        manifestRow(t, "stamp-c1", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq("stamp-c1"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("the FK cascade deleted the manifest row").isTrue();
    }

    @Test
    void aStatementThatMovesThePositionAndTheChashTogetherRecordsTheOldChash() throws Exception {
        String t = "reap-stamp-move";
        register(t, KNOWLEDGE);
        String oldHex = orphan(t, KNOWLEDGE, "old", DAYS_40);
        String newHex = orphan(t, KNOWLEDGE, "new", DAYS_40);
        document(t, "stamp-m1", KNOWLEDGE, false);
        manifestRow(t, "stamp-m1", KNOWLEDGE, oldHex, 0);
        su(ctx -> {
            ageTo(ctx, t, KNOWLEDGE, oldHex, DAYS_40);
            ageTo(ctx, t, KNOWLEDGE, newHex, DAYS_40);
        });

        su(ctx -> ctx.update(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 5).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(newHex))
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-m1"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, oldHex)).as("the old chash was dropped").isTrue();
        assertThat(orphanedAt(t, KNOWLEDGE, newHex)).as("the new chash was gained").isNull();
    }

    /** Append's upsert of a position (and the import's) is INSERT ... ON CONFLICT (tenant, doc, position) DO UPDATE. */
    @Test
    void anInsertOnConflictDoUpdateOverAnOccupiedPositionRecordsTheDisplacedChunk() throws Exception {
        String t = "reap-stamp-upsert";
        register(t, KNOWLEDGE);
        String displaced = orphan(t, KNOWLEDGE, "displaced", DAYS_40);
        String incoming = orphan(t, KNOWLEDGE, "incoming", DAYS_40);
        document(t, "stamp-up1", KNOWLEDGE, false);
        manifestRow(t, "stamp-up1", KNOWLEDGE, displaced, 3);
        su(ctx -> {
            ageTo(ctx, t, KNOWLEDGE, displaced, DAYS_40);
            ageTo(ctx, t, KNOWLEDGE, incoming, DAYS_40);
        });

        su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .values(t, "stamp-up1", 3, bytes(incoming), KNOWLEDGE)
            .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
            .doUpdate().set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(incoming)).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, displaced)).as("displaced from position 3").isTrue();
        assertThat(orphanedAt(t, KNOWLEDGE, incoming)).isNull();
    }

    /** The one-hour margin: a chunk written moments ago is not recorded (it is inside its grace). */
    @Test
    void aChunkWrittenWithinTheLastHourIsNotRecorded() throws Exception {
        String t = "reap-stamp-guard";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "recent", Duration.ofMinutes(5));
        document(t, "stamp-g1", KNOWLEDGE, false);
        manifestRow(t, "stamp-g1", KNOWLEDGE, hex, 0);

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-g1"))).execute());

        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("written 5 minutes ago: no record").isNull();
        assertThat(isReapable(t, KNOWLEDGE, hex)).isFalse();
    }

    /** The same margin on the record itself: a drop within the hour of the previous one does not move it. */
    @Test
    void aChunkRecordedWithinTheLastHourIsNotRecordedAgain() throws Exception {
        String t = "reap-stamp-guard-record";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "rerecorded", DAYS_40);
        recordOrphaning(t, KNOWLEDGE, hex, Duration.ofMinutes(20));
        OffsetDateTime before = orphanedAt(t, KNOWLEDGE, hex);
        document(t, "stamp-gr1", KNOWLEDGE, false);
        manifestRow(t, "stamp-gr1", KNOWLEDGE, hex, 0);
        su(ctx -> ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minus(DAYS_40))
            .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex)))).execute());

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-gr1"))).execute());

        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("recorded 20 minutes ago: left as it was").isEqualTo(before);
    }

    // ── a record that is no longer true is harmless, and cleaned ───────────────

    @Test
    void aRecordIsRemovedWithItsChunk() throws Exception {
        String t = "reap-record-cascade-delete";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "doomed", DAYS_40);
        recordOrphaning(t, KNOWLEDGE, hex, DAYS_40);
        assertThat(recordCount(t)).isEqualTo(1);

        su(ctx -> ctx.deleteFrom(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex)))).execute());

        assertThat(recordCount(t)).as("ON DELETE CASCADE: no record for a chunk that is gone").isZero();
    }

    @Test
    void aRecordFollowsItsChunkWhenTheCollectionIsRenamed() throws Exception {
        String t = "reap-record-cascade-update";
        String renamed = "knowledge__reap-renamed__minilm-l6-v2-384__v1";
        register(t, KNOWLEDGE);
        register(t, renamed);
        String hex = orphan(t, KNOWLEDGE, "moving", DAYS_40);
        recordOrphaning(t, KNOWLEDGE, hex, DAYS_40);

        su(ctx -> ctx.update(CHUNKS).set(CHUNKS.COLLECTION, renamed)
            .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex)))).execute());

        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("nothing left under the old name").isNull();
        assertThat(orphanedAt(t, renamed, hex)).as("ON UPDATE CASCADE: the record moved with the chunk").isNotNull();
        assertThat(isReapable(t, renamed, hex)).isTrue();
    }

    /** A stale record of a chunk that is owned again can never make it reapable: condition 2 fails. */
    @Test
    void aStaleRecordOfAReOwnedChunkIsHarmless() throws Exception {
        String t = "reap-record-reowned";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "reowned", DAYS_40);
        recordOrphaning(t, KNOWLEDGE, hex, DAYS_40);
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("precondition: orphaned 40 days ago, reapable").isTrue();

        document(t, "record-reowned-doc", KNOWLEDGE, false);
        manifestRow(t, "record-reowned-doc", KNOWLEDGE, hex, 0);

        assertThat(isReapable(t, KNOWLEDGE, hex)).as("owned again: the old record means nothing").isFalse();
    }

    // ── the paths the triggers do not cover, and what is true of the roles ─────

    /** The header says nexus_svc cannot TRUNCATE the manifest, which is why the triggers need not cover it. */
    @Test
    void theServiceRoleCanDeleteButNotTruncateTheManifest() throws Exception {
        boolean[] priv = new boolean[2];
        su(ctx -> {
            priv[0] = Boolean.TRUE.equals(ctx.select(DSL.function("has_table_privilege", Boolean.class,
                DSL.val(SVC_ROLE), DSL.val("nexus.catalog_document_chunks"), DSL.val("DELETE"))).fetchOne(0, Boolean.class));
            priv[1] = Boolean.TRUE.equals(ctx.select(DSL.function("has_table_privilege", Boolean.class,
                DSL.val(SVC_ROLE), DSL.val("nexus.catalog_document_chunks"), DSL.val("TRUNCATE"))).fetchOne(0, Boolean.class));
        });
        assertThat(priv[0]).as("non-vacuity: the probe sees the role's DELETE privilege on the manifest").isTrue();
        assertThat(priv[1]).as("a TRUNCATE by the service role would orphan every chunk with no record").isFalse();
    }

    /**
     * Manifest DML by a role that is subject to RLS and has no tenant GUC deletes NO rows, so there is nothing
     * to record: the RLS policy on the manifest closes that path, it does not leak a delete the triggers miss.
     */
    @Test
    void aServiceSessionWithNoTenantGucDeletesNoManifestRows_andRecordsNothing() throws Exception {
        String t = "reap-no-guc";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "noguc", DAYS_40);
        document(t, "noguc-doc", KNOWLEDGE, false);
        manifestRow(t, "noguc-doc", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        int deleted;
        try (Connection c = svcDs.getConnection()) {
            deleted = DSL.using(c, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("noguc-doc"))).execute();
        }

        assertThat(deleted).as("no nexus.tenant GUC: the policy hides every manifest row").isZero();
        assertThat(su1(t, "noguc-doc")).as("the manifest row is still there").isEqualTo(1);
        assertThat(orphanedAt(t, KNOWLEDGE, hex)).isNull();
        // non-vacuity: the same statement with the GUC deletes it and records the chunk
        int withGuc = tenantScope.withTenant(t, ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("noguc-doc"))).execute());
        assertThat(withGuc).isEqualTo(1);
        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).isTrue();
    }

    /**
     * Run as the RLS-subject role with the tenant GUC, as the engine runs: a tenant-A drop records tenant A's
     * chunk and not tenant B's chunk with the same (collection, chash). The triggers are SECURITY INVOKER, so
     * FORCE RLS binds them. This test does NOT prove the explicit tenant equality in the trigger bodies (RLS
     * alone hides tenant B's rows from this role); the owner-run test below does.
     */
    @Test
    void theRecordIsTenantScoped_underTheServiceRoleAndItsGuc() throws Exception {
        String a = "reap-stamp-tenant-a";
        String b = "reap-stamp-tenant-b";
        register(a, KNOWLEDGE);
        register(b, KNOWLEDGE);
        String chash = ch("tenant-shared-text");
        for (String t : List.of(a, b)) {
            su(ctx -> {
                PgContainerHelper.insertChunks(ctx, t, KNOWLEDGE, List.of(chash), List.of("same text"),
                    List.of(new float[384]), List.of(Map.of()));
                ageTo(ctx, t, KNOWLEDGE, chash, DAYS_40);
            });
        }
        document(a, "stamp-ta1", KNOWLEDGE, false);
        manifestRow(a, "stamp-ta1", KNOWLEDGE, chash, 0);
        su(ctx -> ageTo(ctx, a, KNOWLEDGE, chash, DAYS_40));

        tenantScope.withTenant(a, ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(a).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-ta1"))).execute());

        assertThat(orphanRecordedRecently(a, KNOWLEDGE, chash)).as("tenant A's chunk lost its owner").isTrue();
        assertThat(orphanedAt(b, KNOWLEDGE, chash)).as("tenant B's chunk, same key, is not A's to record").isNull();
    }

    /**
     * The explicit tenant equality in the trigger bodies, which only the table owner exercises (a role that
     * bypasses RLS sees both tenants' chunk rows, so the join's tenant_id is the only thing between tenant A's
     * statement and tenant B's chunk). Removing {@code o.tenant_id = c.tenant_id} from either trigger turns this
     * red, and a positive control shows the owner-run statement does record tenant A.
     */
    @Test
    void theRecordIsTenantScoped_whenTheWriterBypassesRls_byTheJoinAlone() throws Exception {
        String a = "reap-owner-tenant-a";
        String b = "reap-owner-tenant-b";
        register(a, KNOWLEDGE);
        register(b, KNOWLEDGE);
        String chash = ch("owner-shared-text");
        for (String t : List.of(a, b)) {
            su(ctx -> {
                PgContainerHelper.insertChunks(ctx, t, KNOWLEDGE, List.of(chash), List.of("same text"),
                    List.of(new float[384]), List.of(Map.of()));
                ageTo(ctx, t, KNOWLEDGE, chash, DAYS_40);
            });
        }
        document(a, "owner-ta1", KNOWLEDGE, false);
        manifestRow(a, "owner-ta1", KNOWLEDGE, chash, 0);
        document(a, "owner-ta2", KNOWLEDGE, false);
        manifestRow(a, "owner-ta2", KNOWLEDGE, chash, 1);

        // DELETE trigger
        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(a).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("owner-ta1"))).execute());
        assertThat(orphanRecordedRecently(a, KNOWLEDGE, chash)).as("positive control: the owner-run delete records A").isTrue();
        assertThat(orphanedAt(b, KNOWLEDGE, chash)).as("DELETE trigger: tenant B's chunk is not tenant A's to record").isNull();

        // UPDATE trigger: move the second row to another chash so the old one is dropped
        String other = ch("owner-other-text");
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, a, KNOWLEDGE, List.of(other), List.of("other text"),
                List.of(new float[384]), List.of(Map.of()));
            ageTo(ctx, a, KNOWLEDGE, other, DAYS_40);
            ctx.deleteFrom(CHUNK_ORPHANED_AT).where(CHUNK_ORPHANED_AT.TENANT_ID.in(a, b)).execute();
            ageTo(ctx, a, KNOWLEDGE, chash, DAYS_40);
        });
        su(ctx -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(other))
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(a).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("owner-ta2"))).execute());
        assertThat(orphanRecordedRecently(a, KNOWLEDGE, chash)).as("positive control: the owner-run update records A").isTrue();
        assertThat(orphanedAt(b, KNOWLEDGE, chash)).as("UPDATE trigger: tenant B's chunk is not tenant A's to record").isNull();
    }

    // ── what the trigger functions are, pinned in the live definition ──────────

    private String functionDefinition(String signature) throws Exception {
        String[] out = new String[1];
        su(ctx -> out[0] = PgCatalogProbes.routineDefinition(ctx, signature));
        assertThat(out[0]).as("%s must resolve", signature).isNotNull();
        return out[0];
    }

    private static int occurrences(String haystack, String needle) {
        int n = 0;
        for (int i = haystack.indexOf(needle); i >= 0; i = haystack.indexOf(needle, i + 1)) n++;
        return n;
    }

    /**
     * The ordered pre-lock, pinned in the live function bodies (a definition pin, the style of
     * PgVectorRepositoryGcQuarantineTest's sweep-gate pins): the chunk rows are locked in key order BEFORE the
     * record is upserted, and the upsert itself runs in key order. A trigger whose lock or ordering is deleted
     * still passes every behavioural test (the concurrent drop only needs the lock to serialize, and a lone
     * writer needs nothing), which is why this is read from the definition. It also pins that the triggers never
     * write nexus.chunks: an UPDATE of a chunk row re-inserts it into the 1024-d HNSW index.
     */
    @Test
    void theTriggersLockTheChunkRowsInKeyOrderBeforeTheyRecord_andNeverWriteNexusChunks() throws Exception {
        for (String signature : List.of("nexus.stamp_chunks_on_manifest_delete()", "nexus.stamp_chunks_on_manifest_update()")) {
            String def = functionDefinition(signature);
            int lock = def.indexOf("FOR NO KEY UPDATE OF c");
            int record = def.indexOf("INSERT INTO nexus.chunk_orphaned_at");
            assertThat(lock).as("%s locks the chunk rows", signature).isPositive();
            assertThat(record).as("%s upserts the record", signature).isPositive();
            assertThat(lock).as("%s: the lock comes BEFORE the upsert", signature).isLessThan(record);
            assertThat(occurrences(def, "ORDER BY c.tenant_id, c.collection, c.chash"))
                .as("%s: both the lock pass and the upsert are ordered by the key", signature).isEqualTo(2);
            assertThat(def).as("%s: one-hour guard on the existing record", signature)
                .contains("WHERE q.orphaned_at < now() - interval '1 hour'");
            assertThat(def).as("%s: never an UPDATE of nexus.chunks", signature)
                .doesNotContain("UPDATE nexus.chunks").doesNotContain("last_written_at =");
            assertThat(def).as("%s: tenant equality in the join", signature)
                .contains("o.tenant_id = c.tenant_id");
        }
    }

    /**
     * The stamp triggers are SECURITY INVOKER (the changelog convention) and not callable by PUBLIC. A DEFINER
     * function would run with its owner's privileges; an INVOKER one runs under the writer's FORCE RLS.
     */
    @Test
    void theStampTriggerFunctionsAreInvokerAndNotExecutableByPublic() throws Exception {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc"));
        var name = DSL.field(DSL.name("proname"), String.class);
        var definer = DSL.field(DSL.name("prosecdef"), Boolean.class);
        var acl = DSL.cast(DSL.field(DSL.name("proacl")), SQLDataType.VARCHAR);
        java.util.Map<String, Boolean> definers = new java.util.TreeMap<>();
        java.util.Map<String, String> acls = new java.util.TreeMap<>();
        su(ctx -> ctx.select(name, definer, acl).from(proc)
            .where(name.in("stamp_chunks_on_manifest_delete", "stamp_chunks_on_manifest_update"))
            .fetch().forEach(r -> {
                definers.put(r.get(name), r.get(definer));
                acls.put(r.get(name), r.get(acl));
            }));

        assertThat(definers.keySet()).containsExactly("stamp_chunks_on_manifest_delete", "stamp_chunks_on_manifest_update");
        definers.forEach((fn, isDefiner) -> {
            assertThat(isDefiner).as("%s must be SECURITY INVOKER", fn).isFalse();
            assertThat(acls.get(fn)).as("%s has an ACL (EXECUTE revoked from PUBLIC)", fn).isNotNull();
            assertThat(acls.get(fn)).as("%s: a bare '=X/' item (no role name) would mean PUBLIC has EXECUTE", fn)
                .doesNotContainPattern("(^\\{|,)=X/");
        });
    }

    /** The side table: keyed by the chunk, tenant-scoped, and cascading from it. */
    @Test
    void theSideTableIsTenantScopedAndKeyedByItsChunk() throws Exception {
        su(ctx -> {
            assertThat(PgCatalogProbes.columnNames(ctx, "nexus", "chunk_orphaned_at"))
                .containsExactlyInAnyOrder("tenant_id", "collection", "chash", "orphaned_at");
            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "chunk_orphaned_at");
            assertThat(rls).as("nexus.chunk_orphaned_at must exist").isNotNull();
            assertThat(rls.enabled()).as("RLS ENABLE").isTrue();
            assertThat(rls.forced()).as("RLS FORCE").isTrue();
            List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "chunk_orphaned_at");
            assertThat(policies).hasSize(1);
            assertThat(policies.get(0).policyname()).isEqualTo("tenant_isolation");
            PgCatalogProbes.Constraint fk = PgCatalogProbes.foreignKey(ctx, "nexus", "chunk_orphaned_at_chunk_fk");
            assertThat(fk).as("the record must not outlive or misname its chunk").isNotNull();
            assertThat(fk.confdeltype()).as("ON DELETE CASCADE").isEqualTo("c");
            assertThat(fk.confupdtype()).as("ON UPDATE CASCADE").isEqualTo("c");
            assertThat(fk.convalidated()).isTrue();
            String[] opts = PgCatalogProbes.relOptions(ctx, "nexus", "chunk_orphaned_at");
            assertThat(opts).as("storage parameters").isNotNull();
            assertThat(List.of(opts)).contains("autovacuum_analyze_scale_factor=0.02");
            assertThat(PgCatalogProbes.indexDefs(ctx, "nexus", "chunk_orphaned_at"))
                .as("the primary key is the probe the predicate uses, and the only index")
                .hasSize(1).allMatch(d -> d.contains("(tenant_id, collection, chash)"));
        });
    }

    // ── inlining and plan, under the RLS-subject role ────────────────────────

    @Test
    void thePredicateInlines_noFunctionScanAndNoOpaqueCallInThePlan() throws Exception {
        String t = "reap-plan";
        register(t, KNOWLEDGE);
        orphan(t, KNOWLEDGE, "x", DAYS_40);

        String plan = tenantScope.withTenant(t, ctx -> ctx.explain(
            ctx.selectOne().from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)))
               .and(reapable(null))).toString());

        assertThat(plan).as("an opaque per-row call would name the function:%n%s", plan)
            .doesNotContain("chunk_is_reapable");
        assertThat(plan).as("plan:%n%s", plan).doesNotContain("Function Scan");
        assertThat(plan).as("the body is planned in place: it reads the catalog tables directly:%n%s", plan)
            .contains("catalog_document_chunks");
    }

    // ── a racing client write wins: the predicate as a DELETE's own WHERE ────

    /**
     * nexus-wbfpw.43 review requirement (3): the grace check must sit in the reaper's DELETE statement
     * itself, so READ COMMITTED blocks on an uncommitted refresh and rechecks the qual on the new row
     * version. Writer A holds an uncommitted {@code last_written_at} refresh; the reaper-shaped DELETE (the
     * predicate and nothing else) blocks; A commits; the row survives and the DELETE skips it.
     */
    @Test
    void aRacingRefreshWins_theDeleteBlocksThenRechecksAndKeepsTheRow() throws Exception {
        String t = "reap-race";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "raced", DAYS_40);
        String control = orphan(t, KNOWLEDGE, "control", DAYS_40);

        try (Connection writer = svcDs.getConnection()) {
            writer.setAutoCommit(false);
            PgContainerHelper.setTenant(writer, TenantScope.DEFAULT_TENANT_GUC, t, true);
            DSL.using(writer, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex))))
               .execute();

            CompletableFuture<Integer> reaper = CompletableFuture.supplyAsync(() ->
                tenantScope.withTenant(t, ctx -> ctx.deleteFrom(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)))
                    .and(reapable(null))
                    .execute()));

            assertThat(PgActivityProbe.waitsOnALock(pg, "delete from%chunks%"))
                .as("the reaper DELETE blocks on the uncommitted refresh").isTrue();
            assertThat(reaper.isDone()).isFalse();

            writer.commit();
            int deleted = reaper.get(30, TimeUnit.SECONDS);

            assertThat(deleted).as("only the un-refreshed control chunk is reaped").isEqualTo(1);
        }
        assertThat(exists(t, KNOWLEDGE, hex)).as("the refreshed chunk survived the racing DELETE").isTrue();
        assertThat(exists(t, KNOWLEDGE, control)).as("the control was reaped").isFalse();
    }

    private boolean exists(String tenant, String collection, String hex) {
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(ctx.selectOne().from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                   .and(CHUNKS.CHASH.eq(bytes(hex))))));
    }
}
