// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.ExecuteContext;
import org.jooq.ExecuteListener;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultExecuteListenerProvider;
import org.jooq.types.DayToSecond;
import org.jooq.types.YearToMonth;
import org.jooq.types.YearToSecond;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-qyvz5, nexus-5jvny: the clear's statement splitting, against a real database. No other test sends
 * more than a handful of chashes through it, so {@link DepartedWriterSpans#BATCH} (chashes per statement) and
 * the departing-tuple cap were unpinned, and the trash purge's clear was one statement over every aged row.
 * The statements are counted with an {@code ExecuteListener} on the transaction's own context, so a change
 * that stops splitting fails here though every row would still come out right.
 *
 * <p>Every scenario owns its chunks (a per-scenario tag in the text), so the tests need no order.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DepartedWriterSpansBatchTest {

    private static final String SVC_ROLE = "svc_span_batch";
    private static final String SVC_PASS = "svc_span_batch_pass";
    private static final String TENANT = "span-batch";
    private static final String COLL = "knowledge__span-batch-c__minilm-l6-v2-384__v1";
    private static final String COLL2 = "knowledge__span-batch-d__minilm-l6-v2-384__v1";
    private static final List<String> SPAN_KEYS = List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalog;
    private final AtomicInteger seq = new AtomicInteger();

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
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalog = new CatalogRepository(tenantScope);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL2);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture helpers ─────────────────────────────────────────────────────

    /** Records the SQL of every statement the context runs. */
    private static final class Statements implements ExecuteListener {
        final List<String> sql = new ArrayList<>();

        @Override
        public void executeStart(ExecuteContext ctx) {
            sql.add(ctx.sql());
        }

        long updatesOfChunks() {
            // an UPDATE of nexus.chunks, whether or not the statement opens with a WITH
            return sql.stream().filter(s -> s.toLowerCase().contains("update \"nexus\".\"chunks\"")).count();
        }
    }

    /** {@code n} chunk rows in {@link #COLL}, each carrying the span 500, 510, 5000, 5100; returns their hex chashes. */
    private List<String> chunks(String tag, int n) {
        return chunks(COLL, tag, n);
    }

    private List<String> chunks(String coll, String tag, int n) {
        List<String> hex = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            String text = "span-batch " + tag + " " + i;
            hex.add(Chash.ofText(text).toHex());
            texts.add(text);
            vecs.add(new float[384]);
            metas.add(Map.of("line_start", 500, "line_end", 510, "chunk_start_char", 5000, "chunk_end_char", 5100));
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, coll, hex, texts, vecs, metas);
        } catch (java.sql.SQLException e) {
            throw new AssertionError(e);
        }
        return hex;
    }

    /** A live document holding every chunk with NO span (the holder the clear works for). */
    private void liveHolder(List<String> hex) {
        liveHolder(COLL, hex);
    }

    private void liveHolder(String coll, List<String> hex) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.ownChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, coll, hex.toArray(String[]::new));
        } catch (java.sql.SQLException e) {
            throw new AssertionError(e);
        }
    }

    private static byte[] bytes(String hex) {
        return HexFormat.of().parseHex(hex);
    }

    /** The span keys each chunk row holds, by hex chash. */
    private Map<String, List<String>> rowSpanKeys(List<String> hex) {
        return rowSpanKeys(COLL, hex);
    }

    private Map<String, List<String>> rowSpanKeys(String coll, List<String> hex) {
        Map<String, List<String>> out = new java.util.LinkedHashMap<>();
        for (String h : hex) {
            var meta = tenantScope.withTenant(TENANT, ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(coll)).and(CHUNKS.CHASH.eq(bytes(h))))
                .fetchOne(CHUNKS.METADATA));
            List<String> keys = new ArrayList<>();
            for (String k : SPAN_KEYS) {
                if (meta.data().contains("\"" + k + "\"")) keys.add(k);
            }
            out.put(h, keys);
        }
        return out;
    }

    // ── clear: BATCH and the tuple cap ──────────────────────────────────────

    @Test
    void aDepartureOfMoreThanBatchChashes_runsInStatementsOfAtMostBatch_andClearsEveryRow() {
        String tag = "b" + seq.incrementAndGet();
        int n = 2 * DepartedWriterSpans.BATCH + 50;
        List<String> hex = chunks(tag, n);
        liveHolder(hex);
        List<DepartedWriterSpans.Gone> gone = new ArrayList<>();
        for (String h : hex) gone.add(new DepartedWriterSpans.Gone(COLL, bytes(h), null, null, null, null));
        var statements = new Statements();

        int cleared = tenantScope.withTenant(TENANT, ctx ->
            DepartedWriterSpans.clear(ctx.configuration().derive(new DefaultExecuteListenerProvider(statements)).dsl(),
                TENANT, gone));

        assertThat(cleared).as("chunk rows updated").isEqualTo(n);
        assertThat(statements.updatesOfChunks()).as("one UPDATE per batch of %d chashes", DepartedWriterSpans.BATCH)
            .isEqualTo(3);
        assertThat(rowSpanKeys(hex).values()).as("every row lost its span").allSatisfy(k -> assertThat(k).isEmpty());
    }

    @Test
    void aDepartureOfMoreThanMaxTuples_runsInMoreThanOneStatement_andEachFieldIsDecided() {
        String tag = "t" + seq.incrementAndGet();
        int chashes = 100;
        int perChash = 12;
        List<String> hex = chunks(tag, chashes);
        liveHolder(hex);
        // 1200 departing tuples, more than the 1000 one statement takes. Every chash has tuples whose line_start
        // (1..12) is not the row's 500, so none of them wrote it and line_start stays; their other fields are
        // absent, so they could have, and those are cleared. Even-numbered chashes have a tuple at 500 too.
        List<DepartedWriterSpans.Gone> gone = new ArrayList<>();
        for (int c = 0; c < chashes; c++) {
            for (int t = 1; t <= perChash; t++) {
                int ls = (c % 2 == 0 && t == perChash) ? 500 : t;
                gone.add(new DepartedWriterSpans.Gone(COLL, bytes(hex.get(c)), ls, null, null, null));
            }
        }
        var statements = new Statements();

        int cleared = tenantScope.withTenant(TENANT, ctx ->
            DepartedWriterSpans.clear(ctx.configuration().derive(new DefaultExecuteListenerProvider(statements)).dsl(),
                TENANT, gone));

        assertThat(cleared).isEqualTo(chashes);
        assertThat(statements.updatesOfChunks()).as("1200 tuples do not fit one statement").isEqualTo(2);
        var keys = rowSpanKeys(hex);
        for (int c = 0; c < chashes; c++) {
            assertThat(keys.get(hex.get(c))).as("chash %d", c)
                .isEqualTo(c % 2 == 0 ? List.of() : List.of("line_start"));
        }
    }

    @Test
    void clearOnAnAutocommitConnection_failsLoudAndChangesNothing() {
        String tag = "a" + seq.incrementAndGet();
        List<String> hex = chunks(tag, 1);
        liveHolder(hex);
        var gone = List.of(new DepartedWriterSpans.Gone(COLL, bytes(hex.get(0)), null, null, null, null));

        // a pool connection outside TenantScope's transaction: autocommit on
        assertThatThrownBy(() -> {
            try (Connection conn = svcDs.getConnection()) {
                assertThat(conn.getAutoCommit()).isTrue();
                DepartedWriterSpans.clear(DSL.using(conn, SQLDialect.POSTGRES), TENANT, gone);
            }
        }).isInstanceOf(IllegalStateException.class).hasMessageContaining("open transaction");

        assertThat(rowSpanKeys(hex).get(hex.get(0))).as("nothing was cleared").isEqualTo(SPAN_KEYS);
    }

    // ── trash purge: one candidate read, then statements of at most BATCH chashes ─────────────────────

    /** An aged tombstone P holding every chunk with the row's own span in its manifest. */
    private void agedWriter(String doc, List<String> hex) {
        agedWriter(COLL, doc, hex);
    }

    private void agedWriter(String coll, String doc, List<String> hex) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            d.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, doc)
                .set(CATALOG_DOCUMENTS.TITLE, doc)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, coll)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(60))
                .execute();
            String model = PgContainerHelper.collectionModel(d, TENANT, coll);
            var insert = d.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END,
                CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END);
            for (int i = 0; i < hex.size(); i++) {
                insert = insert.values(TENANT, doc, i, bytes(hex.get(i)), coll, model, 500, 510, 5000, 5100);
            }
            insert.execute();
        } catch (java.sql.SQLException e) {
            throw new AssertionError(e);
        }
    }

    @Test
    void trashPurgeClear_overMoreThanBatchChashes_runsOneReadThenStatementsOfAtMostBatch() {
        String tag = "p" + seq.incrementAndGet();
        int n = 2 * DepartedWriterSpans.BATCH + 50;
        List<String> hex = chunks(tag, n);
        // a chunk the aged writer holds alone (no holder to clear for) rides along and must stay untouched
        List<String> alone = chunks(tag + "-alone", 3);
        agedWriter("aged-" + tag, new ArrayList<>(concat(hex, alone)));
        liveHolder(hex);
        var statements = new Statements();
        var threshold = DSL.currentOffsetDateTime().sub(new YearToSecond(new YearToMonth(0, 0), new DayToSecond(30)));

        int cleared = tenantScope.withTenant(TENANT, ctx ->
            DepartedWriterSpans.clearBeforeTrashPurge(
                ctx.configuration().derive(new DefaultExecuteListenerProvider(statements)).dsl(), TENANT, threshold));

        assertThat(cleared).isEqualTo(n);
        assertThat(statements.updatesOfChunks()).as("one UPDATE per batch of %d chashes", DepartedWriterSpans.BATCH)
            .isEqualTo(3);
        assertThat(rowSpanKeys(hex).values()).allSatisfy(k -> assertThat(k).isEmpty());
        assertThat(rowSpanKeys(alone).values()).as("no other holder: not the clear's")
            .allSatisfy(k -> assertThat(k).isEqualTo(SPAN_KEYS));
    }

    /**
     * Pins that the purge, run through {@code purgeTrash}, still clears every row when its chashes span more
     * than one batch. It does not prove the split: it stays green with the splitting disabled, because one
     * oversized statement clears the same rows. The statement-count tests above pin the split.
     */
    @Test
    void purgeTrash_overMoreThanBatchChashes_stillClearsEveryRow_correctnessAcrossBatchesNotTheSplit() {
        String tag = "q" + seq.incrementAndGet();
        int n = DepartedWriterSpans.BATCH + 20;
        List<String> hex = chunks(tag, n);
        agedWriter("aged-" + tag, hex);
        liveHolder(hex);

        Map<String, Object> purged = catalog.purgeTrash(TENANT, 30);

        assertThat(((Number) purged.get("documents_purged")).intValue()).isGreaterThanOrEqualTo(1);
        assertThat(rowSpanKeys(hex).values()).allSatisfy(k -> assertThat(k).isEmpty());
    }

    /**
     * The candidates are read ordered by (collection, chash) and a statement is built per collection: a batch
     * flushes when the collection changes, not only when it reaches BATCH. Two collections of a few chashes
     * each, far under BATCH, so only the collection change can separate them; without the flush the second
     * collection's chashes ride a batch named for the first and are never matched.
     */
    @Test
    void trashPurgeClear_overTwoCollections_runsOneStatementPerCollection_andClearsBoth() {
        // the statement count is exact, so no other scenario's aged tombstone may be a candidate: purge them first
        // (and ours at the end), because the classes' scenarios share one database
        catalog.purgeTrash(TENANT, 30);
        String tag = "c" + seq.incrementAndGet();
        List<String> inFirst = chunks(COLL, tag + "-1", 3);
        List<String> inSecond = chunks(COLL2, tag + "-2", 3);
        agedWriter(COLL, "aged-" + tag + "-1", inFirst);
        agedWriter(COLL2, "aged-" + tag + "-2", inSecond);
        liveHolder(COLL, inFirst);
        liveHolder(COLL2, inSecond);
        var statements = new Statements();
        var threshold = DSL.currentOffsetDateTime().sub(new YearToSecond(new YearToMonth(0, 0), new DayToSecond(30)));

        int cleared = tenantScope.withTenant(TENANT, ctx ->
            DepartedWriterSpans.clearBeforeTrashPurge(
                ctx.configuration().derive(new DefaultExecuteListenerProvider(statements)).dsl(), TENANT, threshold));

        assertThat(cleared).as("chunk rows updated in both collections").isEqualTo(6);
        assertThat(statements.updatesOfChunks()).as("one UPDATE per collection").isEqualTo(2);
        assertThat(rowSpanKeys(COLL, inFirst).values()).as("first collection").allSatisfy(k -> assertThat(k).isEmpty());
        assertThat(rowSpanKeys(COLL2, inSecond).values()).as("second collection")
            .allSatisfy(k -> assertThat(k).isEmpty());
        catalog.purgeTrash(TENANT, 30);
    }

    private static List<String> concat(List<String> a, List<String> b) {
        List<String> out = new ArrayList<>(a);
        out.addAll(b);
        return out;
    }
}
