// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgActivityProbe;
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
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7 (bead nexus-wbfpw.15): {@code nexus.chunk_is_reapable}, the ONE reapable(c)
 * predicate, as an inlinable set-returning SQL function, and the orphaning stamp that gives its
 * grace window its meaning.
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
 * The orphaning stamp (vectors-021-2) is pinned here at the table level, with direct manifest DML; the
 * same property through the real writers is in {@code ReapableMidRunJourneyTest}. The S1a row table is in
 * {@code Rdr192EngineLivenessMatrixIntegrationTest}.
 *
 * <p>There is no in-flight-index pin to test: the metadata key it read is stamped by no writer, so the
 * stamp-at-orphaning rule replaced it (review T2 nexus/review-wbfpw15-17-code, C1).
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

    private static void ageTo(DSLContext ctx, String tenant, String collection, String hex, Duration age) {
        OffsetDateTime then = OffsetDateTime.now().minus(age);
        ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
           .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(bytes(hex))))
           .execute();
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

    private boolean recentlyStamped(String tenant, String collection, String hex) throws Exception {
        return lastWrittenAt(tenant, collection, hex).isAfter(OffsetDateTime.now().minusMinutes(10));
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

    // ── the orphaning stamp (vectors-021-2) ───────────────────────────────────

    @Test
    void droppingTheLastManifestRowStartsTheClock_andAnAgedChunkIsNotInstantlyReapable() throws Exception {
        String t = "reap-stamp-delete";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "owned", DAYS_40);
        document(t, "stamp-d1", KNOWLEDGE, false);
        manifestRow(t, "stamp-d1", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));
        assertThat(recentlyStamped(t, KNOWLEDGE, hex)).as("precondition: aged").isFalse();

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-d1"))).execute());

        assertThat(recentlyStamped(t, KNOWLEDGE, hex)).as("last_written_at moved to the orphaning").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("ownerless for seconds, not 30 days").isFalse();
    }

    @Test
    void aChunkStillOwnedByAnotherDocumentIsNotStamped() throws Exception {
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

        assertThat(recentlyStamped(t, KNOWLEDGE, hex)).as("another document still owns it: no orphaning").isFalse();
    }

    @Test
    void replacingARowWithADifferentChashStampsTheOldChash_andOnlyThat() throws Exception {
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

        assertThat(recentlyStamped(t, KNOWLEDGE, oldHex)).as("lost its owner").isTrue();
        assertThat(recentlyStamped(t, KNOWLEDGE, newHex)).as("gained one; untouched").isFalse();
    }

    @Test
    void anUpdateThatDoesNotChangeTheChashStampsNothing() throws Exception {
        String t = "reap-stamp-noop";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "kept", DAYS_40);
        document(t, "stamp-n1", KNOWLEDGE, false);
        manifestRow(t, "stamp-n1", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        su(ctx -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, 7)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-n1"))).execute());

        assertThat(recentlyStamped(t, KNOWLEDGE, hex)).isFalse();
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

        assertThat(recentlyStamped(t, KNOWLEDGE, hex)).as("the manifest row is still there").isFalse();
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("dead, but purge_trash's, not reapable").isFalse();
    }

    @Test
    void aHardDocumentDeleteCascadesIntoTheStamp() throws Exception {
        String t = "reap-stamp-cascade";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "cascaded", DAYS_40);
        document(t, "stamp-c1", KNOWLEDGE, false);
        manifestRow(t, "stamp-c1", KNOWLEDGE, hex, 0);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, hex, DAYS_40));

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq("stamp-c1"))).execute());

        assertThat(recentlyStamped(t, KNOWLEDGE, hex)).as("the FK cascade deleted the manifest row").isTrue();
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
