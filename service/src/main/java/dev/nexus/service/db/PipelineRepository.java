// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.PDF_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.PDF_PAGES;
import static dev.nexus.service.jooq.nexus.Tables.PDF_PIPELINE;

/**
 * RDR-186 bead nexus-146xx.16 (engine half) — jOOQ streaming-PDF buffer repository.
 *
 * <p>The engine-hosted twin of the client's {@code PipelineDB}
 * ({@code src/nexus/pipeline_buffer.py}): crash-resumable working state for
 * the RDR-048 three-stage streaming ingest. Semantics mirrored 1:1 —
 * created/resuming/skip with the 5-minute stale heartbeat, INSERT-OR-REPLACE
 * pages, INSERT-OR-IGNORE chunks (idempotent resume: an existing row that may
 * already carry an embedding is never overwritten), per-index mark-uploaded.
 *
 * <p>The orphan scan SPLITS across the boundary: staleness is judged here
 * (server clock vs updated_at), but {@code pdf_path} existence can only be
 * checked by the CLIENT (the file lives on its disk) — {@link #listPipelines}
 * serves the rows for that client-side half.
 *
 * <p>RDR-164 CA-4 / Open Q3 cross-reference (critic-146xx-16e): the
 * service-mode collection-delete cascade's ONE remaining non-atomic step was
 * the client-local pipeline purge — {@code collection_purge._purge_pipeline_db}
 * unconditionally opens local sqlite in both modes. The .16 CLIENT half MUST
 * re-point it to {@code POST /v1/pipeline/delete_collection} in service mode,
 * or the cascade silently orphans the engine-side rows forever (a worse bug
 * than the accepted debt it replaces).
 *
 * <p>Embedding sentinel semantics (nexus-9n1u3, carried verbatim): SQL NULL =
 * not embedded; non-empty BYTEA = client-embedded packed floats; EMPTY BYTEA
 * = service-mode sentinel (the JVM embeds at upload). The HTTP layer maps
 * these to JSON null / base64 / "" respectively.
 */
public final class PipelineRepository {

    private static final Logger log = LoggerFactory.getLogger(PipelineRepository.class);

    /** Stale heartbeat threshold — mirrors pipeline_buffer.STALE_THRESHOLD. */
    public static final Duration STALE_THRESHOLD = Duration.ofMinutes(5);

    private static final Set<String> PROGRESS_FIELDS = Set.of(
        "total_pages", "pages_extracted", "chunks_created",
        "chunks_embedded", "chunks_uploaded"
    );

    private final TenantScope tenantScope;

    public PipelineRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── pipeline lifecycle ───────────────────────────────────────────────────

    /** Mirrors {@code PipelineDB.create_pipeline}: created / resuming / skip. */
    public String create(String tenant, String contentHash, String pdfPath, String collection) {
        requireNonBlank(contentHash, "content_hash");
        requireNonBlank(pdfPath, "pdf_path");
        requireNonBlank(collection, "collection");
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.select(PDF_PIPELINE.STATUS, PDF_PIPELINE.UPDATED_AT)
                         .from(PDF_PIPELINE)
                         .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                                 .and(PDF_PIPELINE.CONTENT_HASH.eq(contentHash)))
                         .fetchOne();
            if (row == null) {
                int inserted = ctx.insertInto(PDF_PIPELINE,
                        PDF_PIPELINE.TENANT_ID, PDF_PIPELINE.CONTENT_HASH,
                        PDF_PIPELINE.PDF_PATH, PDF_PIPELINE.COLLECTION,
                        PDF_PIPELINE.STATUS, PDF_PIPELINE.STARTED_AT, PDF_PIPELINE.UPDATED_AT)
                   .values(tenant, contentHash, pdfPath, collection, "running", now, now)
                   .onConflict(PDF_PIPELINE.TENANT_ID, PDF_PIPELINE.CONTENT_HASH)
                   .doNothing()
                   .execute();
                return inserted == 1 ? "created" : "skip";  // concurrent insert won
            }
            String status = row.value1();
            if ("completed".equals(status)) {
                return "skip";
            }
            boolean stale = row.value2().isBefore(now.minus(STALE_THRESHOLD));
            if ("failed".equals(status) || stale) {
                ctx.update(PDF_PIPELINE)
                   .set(PDF_PIPELINE.STATUS, "resuming")
                   .set(PDF_PIPELINE.UPDATED_AT, now)
                   .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                           .and(PDF_PIPELINE.CONTENT_HASH.eq(contentHash)))
                   .execute();
                return "resuming";
            }
            return "skip";  // running with a fresh heartbeat
        });
    }

    /** Full pipeline row as a map, or null. */
    public Map<String, Object> get(String tenant, String contentHash) {
        return tenantScope.withTenant(tenant, ctx -> {
            var record = ctx.selectFrom(PDF_PIPELINE)
                            .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                                    .and(PDF_PIPELINE.CONTENT_HASH.eq(contentHash)))
                            .fetchOne();
            return record == null ? null : pipelineRowToMap(record.intoMap());
        });
    }

    /** Every pipeline row for the tenant — the client-side orphan scan's input. */
    public List<Map<String, Object>> listPipelines(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectFrom(PDF_PIPELINE)
               .where(PDF_PIPELINE.TENANT_ID.eq(tenant))
               .orderBy(PDF_PIPELINE.CONTENT_HASH)
               .fetch(r -> pipelineRowToMap(r.intoMap())));
    }

    /** Allowlisted numeric progress counters + heartbeat refresh. */
    public void updateProgress(String tenant, String contentHash, Map<String, Integer> fields) {
        requireNonBlank(contentHash, "content_hash");
        var bad = new ArrayList<String>();
        for (String key : fields.keySet()) {
            if (!PROGRESS_FIELDS.contains(key)) bad.add(key);
        }
        if (!bad.isEmpty()) {
            throw new IllegalArgumentException("unknown progress fields: " + bad);
        }
        if (fields.isEmpty()) return;
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            var update = ctx.update(PDF_PIPELINE).set(PDF_PIPELINE.UPDATED_AT, now);
            for (var entry : fields.entrySet()) {
                update = update.set(
                    DSL.field(DSL.name("nexus", "pdf_pipeline", entry.getKey()), Integer.class),
                    entry.getValue());
            }
            update.where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                    .and(PDF_PIPELINE.CONTENT_HASH.eq(contentHash)))
                  .execute();
            return null;
        });
    }

    public void storeExtractionMeta(String tenant, String contentHash, String metadataJson) {
        setPipelineField(tenant, contentHash,
            ctx -> ctx.update(PDF_PIPELINE)
                      .set(PDF_PIPELINE.EXTRACTION_META, metadataJson == null ? "" : metadataJson));
    }

    public void markCompleted(String tenant, String contentHash) {
        setPipelineField(tenant, contentHash,
            ctx -> ctx.update(PDF_PIPELINE).set(PDF_PIPELINE.STATUS, "completed"));
    }

    public void markFailed(String tenant, String contentHash, String error) {
        setPipelineField(tenant, contentHash,
            ctx -> ctx.update(PDF_PIPELINE)
                      .set(PDF_PIPELINE.STATUS, "failed")
                      .set(PDF_PIPELINE.ERROR, error == null ? "" : error));
    }

    private interface UpdateStart {
        org.jooq.UpdateSetMoreStep<?> begin(org.jooq.DSLContext ctx);
    }

    private void setPipelineField(String tenant, String contentHash, UpdateStart start) {
        requireNonBlank(contentHash, "content_hash");
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            start.begin(ctx)
                 .set(PDF_PIPELINE.UPDATED_AT, now)
                 .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                         .and(PDF_PIPELINE.CONTENT_HASH.eq(contentHash)))
                 .execute();
            return null;
        });
    }

    // ── pages ────────────────────────────────────────────────────────────────

    /** Batch upsert (INSERT-OR-REPLACE parity); one call = one transaction. */
    public int writePages(String tenant, String contentHash, List<Map<String, Object>> pages) {
        requireNonBlank(contentHash, "content_hash");
        if (pages.isEmpty()) return 0;
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            for (Map<String, Object> page : pages) {
                ctx.insertInto(PDF_PAGES,
                        PDF_PAGES.TENANT_ID, PDF_PAGES.CONTENT_HASH, PDF_PAGES.PAGE_INDEX,
                        PDF_PAGES.PAGE_TEXT, PDF_PAGES.METADATA_JSON, PDF_PAGES.CREATED_AT)
                   .values(tenant, contentHash,
                           ((Number) page.get("page_index")).intValue(),
                           (String) page.get("page_text"),
                           page.get("metadata_json") instanceof String s ? s : "{}",
                           now)
                   .onConflict(PDF_PAGES.TENANT_ID, PDF_PAGES.CONTENT_HASH, PDF_PAGES.PAGE_INDEX)
                   .doUpdate()
                   .set(PDF_PAGES.PAGE_TEXT, DSL.field("EXCLUDED.page_text", String.class))
                   .set(PDF_PAGES.METADATA_JSON, DSL.field("EXCLUDED.metadata_json", String.class))
                   .set(PDF_PAGES.CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
                   .execute();
            }
            return null;
        });
        return pages.size();
    }

    /** Pages with page_index >= start, ordered. */
    public List<Map<String, Object>> readPagesFrom(String tenant, String contentHash, int startIndex) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectFrom(PDF_PAGES)
               .where(PDF_PAGES.TENANT_ID.eq(tenant)
                       .and(PDF_PAGES.CONTENT_HASH.eq(contentHash))
                       .and(PDF_PAGES.PAGE_INDEX.ge(startIndex)))
               .orderBy(PDF_PAGES.PAGE_INDEX)
               .fetch(r -> timeToString(r.intoMap())));
    }

    // ── chunks ───────────────────────────────────────────────────────────────

    /** Batch INSERT-OR-IGNORE (idempotent resume; existing rows keep their
     *  embeddings); one call = one transaction. Returns rows actually inserted. */
    public int writeChunks(String tenant, String contentHash, List<Map<String, Object>> chunks) {
        requireNonBlank(contentHash, "content_hash");
        if (chunks.isEmpty()) return 0;
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        return tenantScope.withTenant(tenant, ctx -> {
            int inserted = 0;
            for (Map<String, Object> chunk : chunks) {
                inserted += ctx.insertInto(PDF_CHUNKS,
                        PDF_CHUNKS.TENANT_ID, PDF_CHUNKS.CONTENT_HASH, PDF_CHUNKS.CHUNK_INDEX,
                        PDF_CHUNKS.CHUNK_TEXT, PDF_CHUNKS.CHUNK_ID, PDF_CHUNKS.METADATA_JSON,
                        PDF_CHUNKS.EMBEDDING, PDF_CHUNKS.UPLOADED, PDF_CHUNKS.CREATED_AT)
                   .values(tenant, contentHash,
                           ((Number) chunk.get("chunk_index")).intValue(),
                           (String) chunk.get("chunk_text"),
                           (String) chunk.get("chunk_id"),
                           chunk.get("metadata_json") instanceof String s ? s : "{}",
                           (byte[]) chunk.get("embedding"),
                           Boolean.FALSE,
                           now)
                   .onConflict(PDF_CHUNKS.TENANT_ID, PDF_CHUNKS.CONTENT_HASH, PDF_CHUNKS.CHUNK_INDEX)
                   .doNothing()
                   .execute();
            }
            return inserted;
        });
    }

    public List<Map<String, Object>> readReadyChunks(String tenant, String contentHash) {
        return readChunks(tenant, contentHash, PDF_CHUNKS.UPLOADED.isFalse(), 0);
    }

    public List<Map<String, Object>> readUploadableChunks(String tenant, String contentHash, int limit) {
        return readChunks(tenant, contentHash,
            PDF_CHUNKS.UPLOADED.isFalse().and(PDF_CHUNKS.EMBEDDING.isNotNull()), limit);
    }

    private List<Map<String, Object>> readChunks(
            String tenant, String contentHash, Condition extra, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var query = ctx.selectFrom(PDF_CHUNKS)
                           .where(PDF_CHUNKS.TENANT_ID.eq(tenant)
                                   .and(PDF_CHUNKS.CONTENT_HASH.eq(contentHash))
                                   .and(extra))
                           .orderBy(PDF_CHUNKS.CHUNK_INDEX);
            var rows = limit > 0 ? query.limit(limit).fetch() : query.fetch();
            return rows.map(r -> timeToString(r.intoMap()));
        });
    }

    public int markUploaded(String tenant, String contentHash, List<Integer> chunkIndices) {
        requireNonBlank(contentHash, "content_hash");
        if (chunkIndices.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(PDF_CHUNKS)
               .set(PDF_CHUNKS.UPLOADED, Boolean.TRUE)
               .where(PDF_CHUNKS.TENANT_ID.eq(tenant)
                       .and(PDF_CHUNKS.CONTENT_HASH.eq(contentHash))
                       .and(PDF_CHUNKS.CHUNK_INDEX.in(chunkIndices)))
               .execute());
    }

    public int countEmbeddedChunks(String tenant, String contentHash) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetchCount(PDF_CHUNKS,
                PDF_CHUNKS.TENANT_ID.eq(tenant)
                    .and(PDF_CHUNKS.CONTENT_HASH.eq(contentHash))
                    .and(PDF_CHUNKS.EMBEDDING.isNotNull())));
    }

    public int countPipelines(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetchCount(PDF_PIPELINE, PDF_PIPELINE.TENANT_ID.eq(tenant)));
    }

    // ── cleanup ──────────────────────────────────────────────────────────────

    /** Delete WAL page/chunk rows, preserving the pipeline row's audit trail
     *  (the nexus-2fyb orphan-page replay fix, mirrored). */
    public void clearOrphanWal(String tenant, String contentHash) {
        requireNonBlank(contentHash, "content_hash");
        tenantScope.withTenant(tenant, ctx -> {
            ctx.deleteFrom(PDF_PAGES)
               .where(PDF_PAGES.TENANT_ID.eq(tenant).and(PDF_PAGES.CONTENT_HASH.eq(contentHash)))
               .execute();
            ctx.deleteFrom(PDF_CHUNKS)
               .where(PDF_CHUNKS.TENANT_ID.eq(tenant).and(PDF_CHUNKS.CONTENT_HASH.eq(contentHash)))
               .execute();
            return null;
        });
    }

    /** Remove all three tables' rows for one pipeline. */
    public void deletePipeline(String tenant, String contentHash) {
        requireNonBlank(contentHash, "content_hash");
        tenantScope.withTenant(tenant, ctx -> {
            ctx.deleteFrom(PDF_PAGES)
               .where(PDF_PAGES.TENANT_ID.eq(tenant).and(PDF_PAGES.CONTENT_HASH.eq(contentHash)))
               .execute();
            ctx.deleteFrom(PDF_CHUNKS)
               .where(PDF_CHUNKS.TENANT_ID.eq(tenant).and(PDF_CHUNKS.CONTENT_HASH.eq(contentHash)))
               .execute();
            ctx.deleteFrom(PDF_PIPELINE)
               .where(PDF_PIPELINE.TENANT_ID.eq(tenant).and(PDF_PIPELINE.CONTENT_HASH.eq(contentHash)))
               .execute();
            return null;
        });
    }

    /** Remove every pipeline (+ pages + chunks) targeting a collection
     *  (the nexus-8a8e `nx collection delete` hook, mirrored). Returns the
     *  number of pipeline rows removed. */
    public int deleteForCollection(String tenant, String collection) {
        requireNonBlank(collection, "collection");
        return tenantScope.withTenant(tenant, ctx -> {
            var hashes = ctx.select(PDF_PIPELINE.CONTENT_HASH)
                            .from(PDF_PIPELINE)
                            .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                                    .and(PDF_PIPELINE.COLLECTION.eq(collection)))
                            .fetch(PDF_PIPELINE.CONTENT_HASH);
            if (hashes.isEmpty()) return 0;
            ctx.deleteFrom(PDF_PAGES)
               .where(PDF_PAGES.TENANT_ID.eq(tenant).and(PDF_PAGES.CONTENT_HASH.in(hashes)))
               .execute();
            ctx.deleteFrom(PDF_CHUNKS)
               .where(PDF_CHUNKS.TENANT_ID.eq(tenant).and(PDF_CHUNKS.CONTENT_HASH.in(hashes)))
               .execute();
            ctx.deleteFrom(PDF_PIPELINE)
               .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                       .and(PDF_PIPELINE.COLLECTION.eq(collection)))
               .execute();
            return hashes.size();
        });
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Stringify temporal values so the HTTP layer serializes stably. */
    private static Map<String, Object> timeToString(Map<String, Object> row) {
        Map<String, Object> out = new HashMap<>(row);
        out.computeIfPresent("created_at", (k, v) -> v.toString());
        return out;
    }

    private static Map<String, Object> pipelineRowToMap(Map<String, Object> row) {
        Map<String, Object> out = new HashMap<>(row);
        out.computeIfPresent("started_at", (k, v) -> v.toString());
        out.computeIfPresent("updated_at", (k, v) -> v.toString());
        return out;
    }

    private static void requireNonBlank(String value, String field) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("'" + field + "' is required");
        }
    }
}
