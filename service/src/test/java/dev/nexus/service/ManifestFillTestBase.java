// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2: the shared fixture of the manifest fill job tests ({@code POST /v1/catalog/manifest/fill}):
 * the fillable-route fixture ({@link ManifestFillableTestBase}: a real PostgreSQL, the real engine, one document + chunk
 * + manifest row builders) plus the job's start, poll and wait helpers, a read of the manifest columns a fill writes, a
 * snapshot of everything a fill must NOT write, and a row-lock holder that simulates a concurrent writer.
 */
abstract class ManifestFillTestBase extends ManifestFillableTestBase {

    /** The columns a fill writes, as the manifest row holds them. */
    record Row(boolean written, String title, String type, Integer page, Integer lineStart, Integer lineEnd,
               Integer charStart, Integer charEnd) { }

    /** A legacy row, as the fixture's manifest write leaves it: nothing written, every column NULL. */
    protected static final Row LEGACY = new Row(false, null, null, null, null, null, null, null);

    protected Row row(String docId, int position) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE,
                    CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER,
                    CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END,
                    CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END)
                .from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId).and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(position)))
                .fetchOne();
            assertThat(r).as("manifest row " + docId + "#" + position).isNotNull();
            return new Row(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7(), r.value8());
        }
    }

    /** One document, one chunk row with {@code metadata}, one manifest row at position 0 (legacy: NULL labels, NULL spans). */
    protected String oneRow(int chashSeed, String metadata) throws Exception {
        String d = doc("fill doc " + seq);
        chunk(TENANT, h(chashSeed), metadata);
        manifest(d, h(chashSeed));
        return d;
    }

    // ── the job ─────────────────────────────────────────────────────────────

    protected static String fillBody(String kind, boolean dryRun, int batchRows) {
        return "{\"kind\":\"" + kind + "\",\"dry_run\":" + dryRun + ",\"batch_rows\":" + batchRows + "}";
    }

    /** POST /manifest/fill as the default tenant, expecting the 202; returns the body. */
    protected Map<String, Object> start(String kind, boolean dryRun, int batchRows) throws Exception {
        var resp = post(TOKEN, TENANT, "/v1/catalog/manifest/fill", fillBody(kind, dryRun, batchRows));
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(202);
        return mapper.readValue(resp.body(), MAP_T);
    }

    protected Map<String, Object> poll(String token, String tenant, String jobId) throws Exception {
        var resp = get(token, tenant, "/v1/catalog/manifest/fill/" + jobId);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    protected Map<String, Object> poll(String jobId) throws Exception {
        return poll(TOKEN, TENANT, jobId);
    }

    /** Polls until the job is no longer running (60 s at most), and returns its last status. */
    protected Map<String, Object> awaitEnd(String token, String tenant, String jobId) throws Exception {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(60);
        while (true) {
            var status = poll(token, tenant, jobId);
            if (!"running".equals(status.get("state"))) return status;
            assertThat(System.nanoTime()).as("the fill job ends: " + status).isLessThan(deadline);
            Thread.sleep(20);
        }
    }

    protected Map<String, Object> awaitEnd(String jobId) throws Exception {
        return awaitEnd(TOKEN, TENANT, jobId);
    }

    /** Starts a fill of {@code kind} (batches of {@code batchRows}) and waits for it to end. */
    protected Map<String, Object> runFill(String kind, boolean dryRun, int batchRows) throws Exception {
        return awaitEnd((String) start(kind, dryRun, batchRows).get("job_id"));
    }

    protected Map<String, Object> runFill(String kind) throws Exception {
        return runFill(kind, false, 1000);
    }

    @SuppressWarnings("unchecked")
    protected static Map<String, Object> counts(Map<String, Object> status) {
        return (Map<String, Object>) status.get("counts");
    }

    // ── what a fill must not write ──────────────────────────────────────────

    /** Everything on the document and chunk side of the manifest a fill must leave alone, as comparable text. */
    protected record Untouched(List<String> documents, List<String> chunks, int orphanRows) { }

    protected Untouched untouched() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            var docs = new ArrayList<String>();
            dsl.select(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.INDEXED_AT,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.METADATA)
                .from(CATALOG_DOCUMENTS).orderBy(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
                .fetch().forEach(r -> docs.add(r.formatCSV(false)));
            var chunks = new ArrayList<String>();
            dsl.select(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.METADATA, CHUNKS.CHUNK_TEXT,
                    CHUNKS.LAST_WRITTEN_AT)
                .from(CHUNKS).orderBy(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH)
                .fetch().forEach(r -> chunks.add(r.formatCSV(false)));
            return new Untouched(docs, chunks, dsl.fetchCount(CHUNK_ORPHANED_AT));
        }
    }

    /** The {@code indexed_at} the document carries now. */
    protected OffsetDateTime indexedAt(String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_DOCUMENTS.INDEXED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(docId)).fetchOne().value1();
        }
    }

    // ── a concurrent writer ─────────────────────────────────────────────────

    /**
     * A second connection that holds the row lock of one manifest row in an open transaction: a concurrent writer that
     * has not committed. The fill's pick (a plain read) goes past it; the fill's UPDATE of that row waits for it.
     * {@link #commit()} lets the writer's change land; {@link #rollback()} lets the UPDATE go on unchanged.
     */
    protected final class RowLock implements AutoCloseable {
        private final Connection conn;
        private final DSLContext dsl;
        private final String docId;
        private final int position;

        RowLock(String docId, int position) throws Exception {
            this.conn = pg.createConnection("");
            this.conn.setAutoCommit(false);
            this.dsl = DSL.using(conn, SQLDialect.POSTGRES);
            this.docId = docId;
            this.position = position;
            int n = dsl.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId).and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(position)))
                .forUpdate().fetch().size();
            assertThat(n).as("the row to lock exists").isEqualTo(1);
        }

        /** The writer's change, inside its still-open transaction. */
        RowLock labelWritten(String title) {
            int n = dsl.update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true).set(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, title)
                .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId).and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(position)))
                .execute();
            assertThat(n).isEqualTo(1);
            return this;
        }

        RowLock lineStart(int value) {
            int n = dsl.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.LINE_START, value)
                .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId).and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(position)))
                .execute();
            assertThat(n).isEqualTo(1);
            return this;
        }

        void commit() throws Exception {
            conn.commit();
        }

        void rollback() throws Exception {
            conn.rollback();
        }

        @Override
        public void close() throws Exception {
            conn.close();
        }
    }

    /** True while some backend is waiting on a lock in a statement over the manifest table (the fill's UPDATE, held by a {@link RowLock}). */
    protected boolean aManifestStatementWaitsOnALock() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var activity = DSL.table(DSL.name("pg_stat_activity"));
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(activity,
                DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")
                    .and(DSL.field(DSL.name("query"), String.class).likeIgnoreCase("update%catalog_document_chunks%"))) > 0;
        }
    }

    /** Waits (30 s at most) until the fill's UPDATE is blocked on a lock. */
    protected void awaitBlockedUpdate() throws Exception {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(30);
        while (!aManifestStatementWaitsOnALock()) {
            assertThat(System.nanoTime()).as("the fill's UPDATE reaches the row lock").isLessThan(deadline);
            Thread.sleep(20);
        }
    }

    /**
     * A second connection that holds the retire-orphans sweep's gate EXCLUSIVE for one collection in an open transaction,
     * as the sweep does (the key is {@code CatalogRepository.acquireSweepGateShared}'s). Every manifest writer takes the
     * gate SHARED for the collections it writes, so while this is held a fill batch over that collection waits.
     */
    protected final class SweepGateHold implements AutoCloseable {
        private final Connection conn;

        SweepGateHold(String tenant, String collection) throws Exception {
            this.conn = pg.createConnection("");
            this.conn.setAutoCommit(false);
            DSL.using(conn, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + tenant + "/" + collection)))).fetch();
        }

        void release() throws Exception {
            conn.rollback();
        }

        @Override
        public void close() throws Exception {
            conn.close();
        }
    }

    /** Waits (30 s at most) until some backend waits on an advisory lock: a fill batch behind a {@link SweepGateHold}. */
    protected void awaitAdvisoryLockWait() throws Exception {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(30);
        while (true) {
            try (Connection su = pg.createConnection("")) {
                if (DSL.using(su, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("pg_stat_activity")),
                        DSL.field(DSL.name("wait_event"), String.class).eq("advisory")) > 0) {
                    return;
                }
            }
            assertThat(System.nanoTime()).as("the fill batch reaches the sweep gate").isLessThan(deadline);
            Thread.sleep(20);
        }
    }
}
