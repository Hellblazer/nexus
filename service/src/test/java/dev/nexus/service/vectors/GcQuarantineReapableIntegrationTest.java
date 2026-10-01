// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 8 (bead nexus-wbfpw.16): {@code gc_quarantine_orphans} and its bounded twin
 * select candidates with reapable(c). The S1a row table (R1 to R9, P7 equal to REAP, a fresh
 * orphan staying put) is in {@code Rdr192EngineLivenessMatrixIntegrationTest}; this class carries
 * the parts a row table cannot: the default window's edges, the in-flight index pin, and a client
 * write that races the move.
 *
 * <p>Fixture chunks are inserted through substrate SQL, not the write routes, which from RDR-223
 * Phase 3 refuse an ownerless write.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcQuarantineReapableIntegrationTest {

    private static final String SVC_ROLE = "svc_gcreap_test";
    private static final String SVC_PASS = "svc_gcreap_test_pass";
    private static final String TENANT = "gcreap-tenant";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private PgVectorRepository vectors;

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
        Embedder zero = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                return texts.stream().map(t -> new float[384]).toList();
            }
            @Override public void close() { }
        };
        vectors = new PgVectorRepository(tenantScope, zero, zero);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private static String col(String slug, String prefix) {
        return prefix + "__gcreap-" + slug + "__minilm-l6-v2-384__v1";
    }

    private static String quarantineOf(String collection) {
        return "quarantine-" + collection;
    }

    /** One orphan chunk (no manifest row) whose last_written_at is {@code age} ago. */
    private String orphan(String collection, String seed, Duration age, Map<String, Object> metadata)
            throws Exception {
        String hex = Chash.ofText(collection + "/" + seed).toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, collection);
            PgContainerHelper.insertChunks(ctx, TENANT, collection, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(metadata));
            ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minus(age))
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))
                      .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
               .execute();
        }
        return hex;
    }

    private boolean inCollection(String collection, String hex) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchExists(ctx.selectOne().from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))
                   .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))));
    }

    private void indexingDocument(String tumbler, String collection, Duration startedAgo) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                   CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                   CATALOG_DOCUMENTS.INDEX_STATE, CATALOG_DOCUMENTS.INDEX_STARTED_AT)
               .values(TENANT, tumbler, "run " + tumbler, collection, "indexing",
                   OffsetDateTime.now().minus(startedAgo))
               .execute();
        }
    }

    /** The two functions behind one shape: how many chunks moved. */
    private long moved(boolean bounded, String collection) {
        String q = quarantineOf(collection);
        return bounded
            ? vectors.quarantineOrphansBounded(TENANT, collection, q, "2026-09-30T00:00:00Z", 20, 100).moved()
            : vectors.quarantineOrphans(TENANT, collection, q, "2026-09-30T00:00:00Z", 20).moved();
    }

    // ── the default window's edges ───────────────────────────────────────────

    @Test
    void theDefaultGraceIsThirtyDays_unbounded() throws Exception {
        assertThirtyDayEdge(false, "edge-u");
    }

    @Test
    void theDefaultGraceIsThirtyDays_bounded() throws Exception {
        assertThirtyDayEdge(true, "edge-b");
    }

    private void assertThirtyDayEdge(boolean bounded, String slug) throws Exception {
        String c = col(slug, "knowledge");
        String twentyNine = orphan(c, "d29", Duration.ofDays(29), Map.of());
        String thirtyOne = orphan(c, "d31", Duration.ofDays(31), Map.of());

        assertThat(moved(bounded, c)).as("only the chunk older than 30 days moves").isEqualTo(1);
        assertThat(inCollection(c, twentyNine)).as("29 days old stays").isTrue();
        assertThat(inCollection(c, thirtyOne)).as("31 days old moved").isFalse();
        assertThat(inCollection(quarantineOf(c), thirtyOne)).isTrue();
    }

    @Test
    void everyContentTypePrefixIsCovered() throws Exception {
        for (String prefix : List.of("docs", "code", "rdr")) {
            String c = col("prefix-" + prefix, prefix);
            String hex = orphan(c, "x", Duration.ofDays(40), Map.of());
            assertThat(moved(false, c)).as("%s__ orphan moves", prefix).isEqualTo(1);
            assertThat(inCollection(c, hex)).isFalse();
        }
    }

    // ── an in-flight index run keeps its old tail ─────────────────────────────

    @Test
    void aChunkAnIndexingDocumentNamesIsLeftAlone_unbounded() throws Exception {
        assertIndexPin(false, "pin-u");
    }

    @Test
    void aChunkAnIndexingDocumentNamesIsLeftAlone_bounded() throws Exception {
        assertIndexPin(true, "pin-b");
    }

    private void assertIndexPin(boolean bounded, String slug) throws Exception {
        String c = col(slug, "docs");
        indexingDocument(slug + "-run", c, Duration.ofHours(2));
        String pinned = orphan(c, "pinned", Duration.ofDays(40), Map.of("catalog_doc_id", slug + "-run"));
        String unrelated = orphan(c, "unrelated", Duration.ofDays(40), Map.of());

        assertThat(moved(bounded, c)).as("the unrelated orphan moves, the pinned one does not").isEqualTo(1);
        assertThat(inCollection(c, pinned)).as("old tail chunk of a run in flight stays").isTrue();
        assertThat(inCollection(c, unrelated)).isFalse();
    }

    // ── a client write that races the move wins ───────────────────────────────

    @Test
    void aRacingRefreshWins_unbounded() throws Exception {
        assertRacingRefreshWins(false, "race-u");
    }

    @Test
    void aRacingRefreshWins_bounded() throws Exception {
        assertRacingRefreshWins(true, "race-b");
    }

    /**
     * A client re-write holds an uncommitted last_written_at refresh on one orphan while the
     * quarantine call runs. The call's victim selection reads the committed (aged) row and picks
     * it; the DELETE then blocks on the row lock and, once the writer commits, re-checks reapable(c)
     * on the new row version. The refreshed chunk must stay in the origin collection; its unrefreshed
     * neighbour moves.
     */
    private void assertRacingRefreshWins(boolean bounded, String slug) throws Exception {
        String c = col(slug, "knowledge");
        String raced = orphan(c, "raced", Duration.ofDays(40), Map.of());
        String neighbour = orphan(c, "neighbour", Duration.ofDays(40), Map.of());

        try (Connection writer = svcDs.getConnection()) {
            writer.setAutoCommit(false);
            PgContainerHelper.setTenant(writer, TenantScope.DEFAULT_TENANT_GUC, TENANT, true);
            DSL.using(writer, SQLDialect.POSTGRES).update(CHUNKS)
               .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(c))
                      .and(CHUNKS.CHASH.eq(Chash.fromHex(raced).toBytes())))
               .execute();

            Supplier<Long> call = () -> moved(bounded, c);
            CompletableFuture<Long> gc = CompletableFuture.supplyAsync(call);

            assertThat(waitsOnARowLock()).as("the quarantine DELETE blocks on the uncommitted refresh").isTrue();
            writer.commit();

            long movedCount = gc.get(30, TimeUnit.SECONDS);
            assertThat(movedCount).as("only the unrefreshed neighbour is moved").isEqualTo(1);
        }
        assertThat(inCollection(c, raced)).as("the refreshed chunk stays in its collection").isTrue();
        assertThat(inCollection(c, neighbour)).as("the neighbour moved").isFalse();
        assertThat(inCollection(quarantineOf(c), neighbour)).isTrue();
    }

    /** Polls pg_stat_activity (superuser) until a backend waits on a row lock inside the gc call. */
    private boolean waitsOnARowLock() throws Exception {
        for (int i = 0; i < 150; i++) {
            try (Connection c = pg.createConnection("");
                 var st = c.createStatement();
                 var rs = st.executeQuery("SELECT count(*) FROM pg_stat_activity"
                     + " WHERE wait_event_type = 'Lock' AND wait_event IN ('transactionid', 'tuple')"
                     + " AND state = 'active'")) {
                rs.next();
                if (rs.getInt(1) > 0) return true;
            }
            Thread.sleep(100);
        }
        return false;
    }
}
