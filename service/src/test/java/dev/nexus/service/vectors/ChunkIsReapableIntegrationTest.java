// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.JSONB;
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
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7 (bead nexus-wbfpw.15): {@code nexus.chunk_is_reapable}, the ONE
 * reapable(c) predicate, as an inlinable set-returning SQL function.
 *
 * <p>Every test here is a semantic unit of the predicate:
 * <ul>
 *   <li>no own-collection manifest row in ANY owner state, plus
 *       {@code last_written_at < now() - grace};</li>
 *   <li>the grace window is injectable, and NULL means the default (30 days);</li>
 *   <li>a chunk naming a document in {@code index_state = 'indexing'} is excluded
 *       until that run's {@code index_started_at} ages past the pin TTL (RDR-223
 *       Phase 2 gate critique, S2);</li>
 *   <li>it inlines (no function scan in the plan) under the RLS-subject role;</li>
 *   <li>used as the only WHERE of a DELETE it loses to a racing client write
 *       (READ COMMITTED lock-wait and recheck, nexus-wbfpw.43 review).</li>
 * </ul>
 * The S1a row table (R1 to R9 plus the fresh-R1 control) is in {@code
 * Rdr192EngineLivenessMatrixIntegrationTest}; this class carries what a row table
 * cannot: parameters, the pin, inlining and the race.
 *
 * <p>Fixture chunks are inserted through direct substrate SQL, never through the
 * write routes, which from RDR-223 Phase 3 refuse an ownerless write.
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

    /** Registers {@code collection} for {@code tenant}; idempotent per (tenant, collection). */
    private void register(String tenant, String collection) throws Exception {
        su(ctx -> PgContainerHelper.insertCollection(ctx, tenant, collection));
    }

    /**
     * Inserts one chunk through substrate SQL and gives it the age {@code age}
     * (both {@code created_at} and {@code last_written_at}). No manifest row.
     */
    private String orphan(String tenant, String collection, String seed, Duration age,
                          Map<String, Object> metadata) throws Exception {
        String hex = ch(tenant + "/" + collection + "/" + seed);
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(metadata));
            age(ctx, tenant, collection, hex, age);
        });
        return hex;
    }

    private static void age(DSLContext ctx, String tenant, String collection, String hex, Duration age) {
        OffsetDateTime then = OffsetDateTime.now().minus(age);
        ctx.update(CHUNKS)
           .set(CHUNKS.CREATED_AT, then)
           .set(CHUNKS.LAST_WRITTEN_AT, then)
           .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                  .and(CHUNKS.CHASH.eq(bytes(hex))))
           .execute();
    }

    private static YearToSecond interval(Duration d) {
        if (d == null) return null;
        return new YearToSecond(new YearToMonth(0, 0), DayToSecond.valueOf(d));
    }

    /** The one call shape every consumer uses: EXISTS over the function, correlated on the chunk row. */
    private static org.jooq.Condition reapable(Duration grace, Duration pinTtl) {
        return DSL.exists(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
            CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.LAST_WRITTEN_AT, CHUNKS.METADATA,
            DSL.val(interval(grace), SQLDataType.INTERVAL), DSL.val(interval(pinTtl), SQLDataType.INTERVAL))));
    }

    /** True when {@code reapable(c)} holds for the chunk, evaluated as the RLS-subject service role. */
    private boolean isReapable(String tenant, String collection, String hex, Duration grace, Duration pinTtl) {
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                      .and(CHUNKS.CHASH.eq(bytes(hex))))
               .and(reapable(grace, pinTtl))));
    }

    private boolean isReapable(String tenant, String collection, String hex) {
        return isReapable(tenant, collection, hex, null, null);
    }

    private void document(String tenant, String tumbler, String collection, String indexState,
                          Duration startedAgo, boolean tombstoned) throws Exception {
        su(ctx -> {
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                    CATALOG_DOCUMENTS.INDEX_STATE, CATALOG_DOCUMENTS.INDEX_STARTED_AT,
                    CATALOG_DOCUMENTS.DELETED_AT)
               .values(tenant, tumbler, "doc " + tumbler, collection, indexState,
                       startedAgo == null ? null : OffsetDateTime.now().minus(startedAgo),
                       tombstoned ? OffsetDateTime.now().minusDays(1) : null)
               .execute();
        });
    }

    private void manifestRow(String tenant, String docId, String collection, String hex, int position)
            throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .values(tenant, docId, position, bytes(hex), collection)
            .execute());
    }

    // ── what the predicate means ─────────────────────────────────────────────

    @Test
    void anAgedChunkWithNoManifestRowIsReapable_andAFreshOneIsNot() throws Exception {
        String t = "reap-basic";
        register(t, KNOWLEDGE);
        String aged = orphan(t, KNOWLEDGE, "aged", DAYS_40, Map.of());
        String fresh = orphan(t, KNOWLEDGE, "fresh", Duration.ZERO, Map.of());

        assertThat(isReapable(t, KNOWLEDGE, aged)).as("aged past the 30 day default, no owner").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, fresh)).as("created now: inside the grace window").isFalse();
    }

    @Test
    void theCollectionPrefixDoesNotMatter_everyContentTypeIsCovered() throws Exception {
        String t = "reap-prefixes";
        for (String col : List.of(KNOWLEDGE, DOCS, CODE, RDR)) {
            register(t, col);
            String hex = orphan(t, col, "p", DAYS_40, Map.of());
            assertThat(isReapable(t, col, hex)).as("reapable in %s", col).isTrue();
        }
    }

    @Test
    void anyOwnCollectionManifestRowMakesTheChunkNotReapable_inAnyOwnerState() throws Exception {
        String t = "reap-owner-states";
        register(t, KNOWLEDGE);
        String live = orphan(t, KNOWLEDGE, "live", DAYS_40, Map.of());
        String tombstoned = orphan(t, KNOWLEDGE, "tomb", DAYS_40, Map.of());
        document(t, "reap-live-doc", KNOWLEDGE, null, null, false);
        document(t, "reap-dead-doc", KNOWLEDGE, null, null, true);
        manifestRow(t, "reap-live-doc", KNOWLEDGE, live, 0);
        manifestRow(t, "reap-dead-doc", KNOWLEDGE, tombstoned, 0);

        assertThat(isReapable(t, KNOWLEDGE, live)).as("live owner").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, tombstoned))
            .as("tombstoned owner only: still has a manifest row, so purge_trash's arm, not this one").isFalse();
    }

    @Test
    void aManifestRowInAnotherCollectionDoesNotProtect() throws Exception {
        String t = "reap-other-collection";
        register(t, KNOWLEDGE);
        register(t, OTHER);
        String hex = orphan(t, KNOWLEDGE, "shared", DAYS_40, Map.of());
        su(ctx -> PgContainerHelper.insertChunks(ctx, t, OTHER, List.of(hex), List.of("shared text"),
            List.of(new float[384]), List.of(Map.of())));
        document(t, "reap-other-doc", OTHER, null, null, false);
        manifestRow(t, "reap-other-doc", OTHER, hex, 0);

        assertThat(isReapable(t, KNOWLEDGE, hex)).as("GH #1546 shape: own collection has no row").isTrue();
        assertThat(isReapable(t, OTHER, hex)).as("owned in OTHER").isFalse();
    }

    // ── the grace window ─────────────────────────────────────────────────────

    @Test
    void theGraceWindowIsInjectable_andNullMeansTheThirtyDayDefault() throws Exception {
        String t = "reap-grace";
        register(t, KNOWLEDGE);
        String twoHours = orphan(t, KNOWLEDGE, "two-hours", Duration.ofHours(2), Map.of());
        String thirtyOneDays = orphan(t, KNOWLEDGE, "thirty-one-days", Duration.ofDays(31), Map.of());

        assertThat(isReapable(t, KNOWLEDGE, twoHours, Duration.ofHours(1), null)).as("1h grace, 2h old").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, twoHours, Duration.ofHours(3), null)).as("3h grace, 2h old").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, twoHours, Duration.ZERO, null)).as("zero grace").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, twoHours, null, null)).as("default 30d, 2h old").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, thirtyOneDays, null, null)).as("default 30d, 31d old").isTrue();
    }

    @Test
    void graceKeysOnLastWrittenAt_notCreatedAt() throws Exception {
        String t = "reap-grace-key";
        register(t, KNOWLEDGE);
        // created_at is old, last_written_at is fresh: a re-write refreshed it (nexus-wbfpw.43).
        String hex = orphan(t, KNOWLEDGE, "rewritten", DAYS_40, Map.of());
        su(ctx -> ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
            .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex))))
            .execute());

        assertThat(isReapable(t, KNOWLEDGE, hex)).as("old created_at must not defeat a fresh last_written_at")
            .isFalse();
    }

    // ── the in-flight index run pin ──────────────────────────────────────────

    @Test
    void aChunkNamingADocumentThatIsIndexingIsNotReapable_untilTheRunsTtlLapses() throws Exception {
        String t = "reap-pin";
        register(t, DOCS);
        String run = "reap-pin-doc";
        document(t, run, DOCS, "indexing", Duration.ofHours(2), false);
        String pinned = orphan(t, DOCS, "pinned", DAYS_40, Map.of("catalog_doc_id", run));

        assertThat(isReapable(t, DOCS, pinned)).as("old tail chunk of a run in flight").isFalse();
        assertThat(isReapable(t, DOCS, pinned, null, Duration.ofHours(1)))
            .as("same run, pin TTL 1h, started 2h ago: a dead run no longer pins").isTrue();
        assertThat(isReapable(t, DOCS, pinned, null, Duration.ofDays(1))).as("TTL 1d still pins").isFalse();
    }

    @Test
    void theDefaultPinTtlIsSevenDays() throws Exception {
        String t = "reap-pin-default";
        register(t, DOCS);
        document(t, "reap-run-6d", DOCS, "indexing", Duration.ofDays(6), false);
        document(t, "reap-run-8d", DOCS, "indexing", Duration.ofDays(8), false);
        String sixDays = orphan(t, DOCS, "six", DAYS_40, Map.of("catalog_doc_id", "reap-run-6d"));
        String eightDays = orphan(t, DOCS, "eight", DAYS_40, Map.of("catalog_doc_id", "reap-run-8d"));

        assertThat(isReapable(t, DOCS, sixDays)).isFalse();
        assertThat(isReapable(t, DOCS, eightDays)).isTrue();
    }

    @Test
    void onlyTheIndexingStatePins_completeFailedAndUnknownDoNot() throws Exception {
        String t = "reap-pin-states";
        register(t, DOCS);
        for (String state : new String[] {"complete", "failed"}) {
            document(t, "reap-doc-" + state, DOCS, state, Duration.ofMinutes(5), false);
        }
        document(t, "reap-doc-null", DOCS, null, null, false);
        for (String state : new String[] {"complete", "failed", "null"}) {
            String hex = orphan(t, DOCS, "s-" + state, DAYS_40, Map.of("catalog_doc_id", "reap-doc-" + state));
            assertThat(isReapable(t, DOCS, hex)).as("owner state %s does not pin", state).isTrue();
        }
    }

    @Test
    void anIndexingDocumentWithNoStartTimeCannotPin_becauseItsTtlCannotBeEvaluated() throws Exception {
        String t = "reap-pin-nostart";
        register(t, DOCS);
        document(t, "reap-nostart", DOCS, "indexing", null, false);
        String hex = orphan(t, DOCS, "nostart", DAYS_40, Map.of("catalog_doc_id", "reap-nostart"));

        assertThat(isReapable(t, DOCS, hex)).isTrue();
    }

    @Test
    void theLegacyDocIdKeyPinsToo_whenCatalogDocIdIsAbsent() throws Exception {
        String t = "reap-pin-legacy-key";
        register(t, DOCS);
        document(t, "reap-legacy-run", DOCS, "indexing", Duration.ofMinutes(10), false);
        String viaDocId = orphan(t, DOCS, "legacy", DAYS_40, Map.of("doc_id", "reap-legacy-run"));
        String emptyPreferred = orphan(t, DOCS, "empty-preferred", DAYS_40,
            Map.of("catalog_doc_id", "", "doc_id", "reap-legacy-run"));

        assertThat(isReapable(t, DOCS, viaDocId)).as("doc_id fallback").isFalse();
        assertThat(isReapable(t, DOCS, emptyPreferred)).as("empty catalog_doc_id falls back").isFalse();
    }

    @Test
    void thePinIsTenantScoped_anotherTenantsIndexingDocumentDoesNotPin() throws Exception {
        String a = "reap-pin-tenant-a";
        String b = "reap-pin-tenant-b";
        register(a, DOCS);
        register(b, DOCS);
        document(b, "reap-shared-tumbler", DOCS, "indexing", Duration.ofMinutes(1), false);
        String hex = orphan(a, DOCS, "x", DAYS_40, Map.of("catalog_doc_id", "reap-shared-tumbler"));

        assertThat(isReapable(a, DOCS, hex)).isTrue();
    }

    // ── inlining and plan, under the RLS-subject role ────────────────────────

    @Test
    void thePredicateInlines_noFunctionScanAndNoOpaqueCallInThePlan() throws Exception {
        String t = "reap-plan";
        register(t, KNOWLEDGE);
        orphan(t, KNOWLEDGE, "x", DAYS_40, Map.of());

        String plan = tenantScope.withTenant(t, ctx -> ctx.explain(
            ctx.selectOne().from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)))
               .and(reapable(null, null))).toString());

        assertThat(plan).as("an opaque per-row call would name the function:%n%s", plan).doesNotContain("chunk_is_reapable");
        assertThat(plan).as("plan:%n%s", plan).doesNotContain("Function Scan");
        assertThat(plan).as("the body is planned in place: it reads the catalog tables directly:%n%s", plan)
            .contains("catalog_document_chunks");
    }

    // ── a racing client write wins: the predicate as a DELETE's own WHERE ────

    /**
     * nexus-wbfpw.43 review requirement (3): the grace check must sit in the
     * reaper's DELETE statement itself, so READ COMMITTED blocks on an uncommitted
     * refresh and rechecks the qual on the new row version. Writer A holds an
     * uncommitted {@code last_written_at} refresh; the reaper-shaped DELETE (the
     * predicate and nothing else) blocks; A commits; the row survives and the
     * DELETE reports 0 rows.
     */
    @Test
    void aRacingRefreshWins_theDeleteBlocksThenRechecksAndKeepsTheRow() throws Exception {
        String t = "reap-race";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "raced", DAYS_40, Map.of());
        String control = orphan(t, KNOWLEDGE, "control", DAYS_40, Map.of());

        try (Connection writer = svcDs.getConnection()) {
            writer.setAutoCommit(false);
            PgContainerHelper.setTenant(writer, TenantScope.DEFAULT_TENANT_GUC, t, true);
            var w = DSL.using(writer, SQLDialect.POSTGRES);
            // The refresh a client re-write performs (never commits until we say so).
            w.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
             .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex))))
             .execute();

            CompletableFuture<Integer> reaper = CompletableFuture.supplyAsync(() ->
                tenantScope.withTenant(t, ctx -> ctx.deleteFrom(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)))
                    .and(reapable(null, null))
                    .execute()));

            // The DELETE must be waiting on the writer's row lock, not finished.
            assertThat(waitsOnALock(t)).as("the reaper DELETE blocks on the uncommitted refresh").isTrue();
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

    /** Polls pg_stat_activity (superuser) until some backend is waiting on a Lock for a DELETE on nexus.chunks. */
    private boolean waitsOnALock(String tenant) throws Exception {
        for (int i = 0; i < 100; i++) {
            try (Connection c = pg.createConnection("")) {
                try (var st = c.createStatement();
                     var rs = st.executeQuery("SELECT count(*) FROM pg_stat_activity"
                         + " WHERE wait_event_type = 'Lock' AND query ILIKE 'delete from%nexus%chunks%'")) {
                    rs.next();
                    if (rs.getInt(1) > 0) return true;
                }
            }
            Thread.sleep(100);
        }
        return false;
    }
}
