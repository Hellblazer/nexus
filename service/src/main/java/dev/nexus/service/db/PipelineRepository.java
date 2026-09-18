// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
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
 * <p><strong>Row identity (nexus-edjmu, pipeline-002-per-row-identity.xml).</strong>
 * A {@code pdf_pipeline} row is one RUN of one document: identity
 * {@code pipeline_id}, unique on {@code (tenant_id, content_hash, collection,
 * pdf_path)}. Both WAL tables key on {@code (tenant_id, pipeline_id, idx)} and
 * carry no {@code content_hash} at all, so every page/chunk read and write in
 * this class is scoped to one run by construction — a second document sharing
 * the bytes starts with an empty WAL of its own (no sibling-seeded counters,
 * no cross-row cleanup). Every operation after {@link #create} takes the
 * {@code pipeline_id} the caller resolved through {@link #resolve}; a caller
 * that only knows a content hash (a client older than this changeset)
 * resolves it there too, and lands on ITS OWN row: the one row whose
 * {@code keyed_by} is {@code content_hash}, never a document row. See
 * {@link PipelineRef}.
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

    /**
     * Stale heartbeat threshold — nexus-lcmbp fix-list #6: the client analog is
     * {@code STALE_THRESHOLD} in {@code src/nexus/db/http_pipeline_client.py}
     * (Python, {@code timedelta(minutes=5)}). The two MUST stay numerically
     * identical: this value drives {@link #create}'s running-vs-stale decision
     * and, via {@link PipelineConflictException}, the {@code
     * stale_threshold_seconds} field on the 409 {@code conflict_running} wire
     * body; the Python constant drives the client's own orphan-scan staleness
     * judgment ({@code scan_orphaned_pipelines}) against the SAME
     * server-stamped {@code updated_at}, and is what a caller compares
     * {@code PipelineConflictRunning.stale_threshold_seconds} against. A drift
     * here would not fail loudly — it would silently make one side's
     * staleness judgment disagree with the other's. Pinned by
     * {@code tests/db/test_pipeline_fake_engine_parity.py::
     * test_stale_threshold_agrees_across_client_and_wire}.
     */
    public static final Duration STALE_THRESHOLD = Duration.ofMinutes(5);

    /** {@code pdf_pipeline.keyed_by} values (pipeline-002-1's CHECK). */
    public static final String KEYED_BY_CONTENT_HASH = "content_hash";
    public static final String KEYED_BY_DOCUMENT = "document";

    private static final Set<String> PROGRESS_FIELDS = Set.of(
        "total_pages", "pages_extracted", "chunks_created",
        "chunks_embedded", "chunks_uploaded"
    );

    /**
     * How a caller names a pipeline row. Exactly one of the two forms is used:
     * {@code pipelineId} when the caller holds the id {@link #create} returned;
     * otherwise {@code contentHash}, optionally narrowed by {@code collection}
     * and {@code pdfPath} (the pre-create {@code --force} delete knows all
     * three; a client older than pipeline-002 sends the hash alone).
     */
    public record PipelineRef(Long pipelineId, String contentHash, String collection, String pdfPath) {
        public static PipelineRef byId(long pipelineId) {
            return new PipelineRef(pipelineId, null, null, null);
        }

        public static PipelineRef byHash(String contentHash) {
            return new PipelineRef(null, contentHash, null, null);
        }

        public static PipelineRef byDocument(String contentHash, String collection, String pdfPath) {
            return new PipelineRef(null, contentHash, collection, pdfPath);
        }

        /** Human-readable form for error messages. */
        public String describe() {
            if (pipelineId != null) return "pipeline_id=" + pipelineId;
            StringBuilder sb = new StringBuilder("content_hash=").append(contentHash);
            if (collection != null && !collection.isBlank()) sb.append(" collection=").append(collection);
            if (pdfPath != null && !pdfPath.isBlank()) sb.append(" pdf_path=").append(pdfPath);
            return sb.toString();
        }
    }

    /** {@link #create}'s answer: the wire {@code status} and the row it names. */
    public record CreateResult(String status, long pipelineId) {}

    private final TenantScope tenantScope;

    public PipelineRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── resolution ───────────────────────────────────────────────────────────

    /**
     * The one row a {@link PipelineRef} names, or {@code null} when none does.
     *
     * <p>By id: that row, tenant-scoped. By hash narrowed by {@code
     * collection} / {@code pdfPath}: the rows matching (a full narrowing is
     * at most one row, the UNIQUE), newest first. By hash ALONE: only rows
     * with {@code keyed_by = 'content_hash'}, at most one per hash (the
     * partial unique index), because the only caller that names a row by
     * bare hash is a client older than pipeline-002, and such a client can
     * only ever have created a legacy row. A document row a newer client
     * inserts for the same bytes, before or after, is therefore invisible
     * to an old client's progress/complete/fail/clear_wal/delete sequence
     * (the substantive-critic's implementation finding: preferring legacy
     * rows was not enough, since with none present the old client adopted
     * a stranger's document row). A narrowing field that matches no row is
     * a miss, never a widen to the bare hash.
     */
    public Long resolve(String tenant, PipelineRef ref) {
        return tenantScope.withTenant(tenant, ctx -> resolveIn(ctx, tenant, ref));
    }

    private static Long resolveIn(DSLContext ctx, String tenant, PipelineRef ref) {
        if (ref.pipelineId() != null) {
            return ctx.select(PDF_PIPELINE.PIPELINE_ID)
                      .from(PDF_PIPELINE)
                      .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                              .and(PDF_PIPELINE.PIPELINE_ID.eq(ref.pipelineId())))
                      .fetchOne(PDF_PIPELINE.PIPELINE_ID);
        }
        requireNonBlank(ref.contentHash(), "content_hash");
        return ctx.select(PDF_PIPELINE.PIPELINE_ID)
                  .from(PDF_PIPELINE)
                  .where(hashCondition(tenant, ref))
                  .orderBy(PDF_PIPELINE.STARTED_AT.desc(), PDF_PIPELINE.PIPELINE_ID.desc())
                  .limit(1)
                  .fetchOne(PDF_PIPELINE.PIPELINE_ID);
    }

    private static boolean narrowed(PipelineRef ref) {
        return (ref.collection() != null && !ref.collection().isBlank())
            || (ref.pdfPath() != null && !ref.pdfPath().isBlank());
    }

    private static Condition hashCondition(String tenant, PipelineRef ref) {
        Condition c = PDF_PIPELINE.TENANT_ID.eq(tenant)
                .and(PDF_PIPELINE.CONTENT_HASH.eq(ref.contentHash()));
        if (!narrowed(ref)) {
            return c.and(PDF_PIPELINE.KEYED_BY.eq(KEYED_BY_CONTENT_HASH));
        }
        if (ref.collection() != null && !ref.collection().isBlank()) {
            c = c.and(PDF_PIPELINE.COLLECTION.eq(ref.collection()));
        }
        if (ref.pdfPath() != null && !ref.pdfPath().isBlank()) {
            c = c.and(PDF_PIPELINE.PDF_PATH.eq(ref.pdfPath()));
        }
        return c;
    }

    // ── pipeline lifecycle ───────────────────────────────────────────────────

    /** Mirrors {@code PipelineDB.create_pipeline}: created / resuming / skip.
     *
     * <p>nexus-lcmbp: a {@code running} row with a FRESH heartbeat is a LOUD
     * refusal ({@link PipelineConflictException}, mapped by the HTTP layer to a
     * 409), never a {@code "skip"}. The prior "skip" here was indistinguishable
     * on the wire from every other short-circuit (e.g. {@code completed}), so a
     * caller checking only the return code observed silent success — zero
     * chunks written, {@code rc=0}. The SAME retry against the SAME row returns
     * a loud {@code "resuming"} once the heartbeat ages past
     * {@link #STALE_THRESHOLD}; making the fresh-heartbeat branch loud too
     * removes the time-dependent success/failure split.
     *
     * <p>nexus-edjmu, two algorithms selected by {@code documentIdentity}:
     * <ul>
     *   <li>{@code false} (a client that sends no {@code identity} on
     *   {@code /create}, every release before pipeline-002): the pipeline-001
     *   algorithm verbatim, one row per hash. The existing-row lookup is the
     *   hash-only {@link #resolve} (legacy rows only), then, when there is
     *   none, the document row of the SAME document (same collection and
     *   path: the one row an old client may adopt, since it is its own
     *   document); a document row of ANOTHER document sharing the bytes is
     *   never adopted, the old client gets its own legacy row beside it.
     *   An adopted row becomes a legacy row on {@code "resuming"} so the
     *   client's later hash-only calls find it. A row it inserts is stamped
     *   {@code keyed_by='content_hash'}. {@code completed -> "skip"} stays,
     *   exactly as that client expects.</li>
     *   <li>{@code true} ({@code identity="document"}): one row per
     *   {@code (content_hash, collection, pdf_path)}. No row -> insert,
     *   {@code "created"}. Running with a fresh heartbeat -> 409. Failed or
     *   stale -> {@code "resuming"} (and the row becomes a document row, so a
     *   legacy row at the same path is taken over). Completed -> the row is a
     *   leftover of a client that died between {@code mark_completed} and
     *   {@code delete}; its WAL is wiped, its counters reset, and the answer
     *   is {@code "created"}: a fresh run whose T3 upserts are idempotent and
     *   whose manifest is written for the CURRENT catalog document. Resuming
     *   it instead cannot write that manifest (nothing is left to upload) and
     *   the completion fence refuses; skipping it is the bead's own symptom.
     *   {@code "skip"} is never returned on this path.</li>
     * </ul>
     * A concurrent insert that wins a unique race is re-read and judged by
     * the same branches rather than reported as {@code "skip"}. Two indexes
     * make the races atomic at the database, as pipeline-001's primary key
     * did: {@code pdf_pipeline_document_uq} for document creates, and the
     * partial {@code pdf_pipeline_legacy_uq} ({@code (tenant_id,
     * content_hash) WHERE keyed_by = 'content_hash'}) for legacy creates,
     * so two old clients racing the same bytes at two paths still collide
     * on the insert instead of both landing and then addressing each
     * other's row by hash. {@code ON CONFLICT DO NOTHING} without a target
     * covers whichever index fires. */
    public CreateResult create(String tenant, String contentHash, String pdfPath,
                               String collection, boolean documentIdentity) {
        requireNonBlank(contentHash, "content_hash");
        requireNonBlank(pdfPath, "pdf_path");
        requireNonBlank(collection, "collection");
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        PipelineRef ref = documentIdentity
                ? PipelineRef.byDocument(contentHash, collection, pdfPath)
                : PipelineRef.byHash(contentHash);
        String keyedBy = documentIdentity ? KEYED_BY_DOCUMENT : KEYED_BY_CONTENT_HASH;
        return tenantScope.withTenant(tenant, ctx -> {
            for (int attempt = 0; attempt < 2; attempt++) {
                Long existing = resolveIn(ctx, tenant, ref);
                if (existing == null && !documentIdentity) {
                    // No legacy row: the same document's document row, if any.
                    existing = resolveIn(ctx, tenant, PipelineRef.byDocument(contentHash, collection, pdfPath));
                }
                if (existing == null) {
                    Long inserted = ctx.insertInto(PDF_PIPELINE,
                            PDF_PIPELINE.TENANT_ID, PDF_PIPELINE.CONTENT_HASH,
                            PDF_PIPELINE.PDF_PATH, PDF_PIPELINE.COLLECTION,
                            PDF_PIPELINE.KEYED_BY,
                            PDF_PIPELINE.STATUS, PDF_PIPELINE.STARTED_AT, PDF_PIPELINE.UPDATED_AT)
                       .values(tenant, contentHash, pdfPath, collection, keyedBy, "running", now, now)
                       .onConflictDoNothing()
                       .returning(PDF_PIPELINE.PIPELINE_ID)
                       .fetchOne(PDF_PIPELINE.PIPELINE_ID);
                    if (inserted != null) {
                        return new CreateResult("created", inserted);
                    }
                    continue;  // a concurrent insert won the UNIQUE race: re-read it
                }
                var row = ctx.select(PDF_PIPELINE.STATUS, PDF_PIPELINE.UPDATED_AT, PDF_PIPELINE.STARTED_AT)
                             .from(PDF_PIPELINE)
                             .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                                     .and(PDF_PIPELINE.PIPELINE_ID.eq(existing)))
                             .fetchOne();
                if (row == null) {
                    continue;  // deleted between the resolve and the read
                }
                String status = row.value1();
                if ("completed".equals(status)) {
                    if (!documentIdentity) {
                        return new CreateResult("skip", existing);
                    }
                    resetLeftoverRow(ctx, tenant, existing, now);
                    return new CreateResult("created", existing);
                }
                OffsetDateTime updatedAt = row.value2();
                boolean stale = updatedAt.isBefore(now.minus(STALE_THRESHOLD));
                if ("failed".equals(status) || stale) {
                    // The resumer's algorithm owns the row from here: a
                    // document client addresses it by id; a legacy client
                    // by bare hash, which finds legacy rows only.
                    ctx.update(PDF_PIPELINE)
                       .set(PDF_PIPELINE.STATUS, "resuming")
                       .set(PDF_PIPELINE.KEYED_BY, keyedBy)
                       .set(PDF_PIPELINE.UPDATED_AT, now)
                       .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                               .and(PDF_PIPELINE.PIPELINE_ID.eq(existing)))
                       .execute();
                    return new CreateResult("resuming", existing);
                }
                // running with a fresh heartbeat — LOUD conflict, never silent success.
                throw new PipelineConflictException(contentHash, row.value3(),
                    Duration.between(updatedAt, now), STALE_THRESHOLD);
            }
            throw new IllegalStateException(
                "pipeline create for " + ref.describe() + " lost the insert race twice");
        });
    }

    /** A leftover 'completed' row becomes a fresh run: WAL wiped, counters
     *  and audit fields reset, {@code started_at} restarted, and the row
     *  marked a document row. */
    private static void resetLeftoverRow(DSLContext ctx, String tenant, long pipelineId, OffsetDateTime now) {
        deleteWal(ctx, tenant, pipelineId);
        ctx.update(PDF_PIPELINE)
           .set(PDF_PIPELINE.STATUS, "running")
           .set(PDF_PIPELINE.KEYED_BY, KEYED_BY_DOCUMENT)
           .setNull(PDF_PIPELINE.TOTAL_PAGES)
           .set(PDF_PIPELINE.PAGES_EXTRACTED, 0)
           .setNull(PDF_PIPELINE.CHUNKS_CREATED)
           .setNull(PDF_PIPELINE.CHUNKS_EMBEDDED)
           .set(PDF_PIPELINE.CHUNKS_UPLOADED, 0)
           .set(PDF_PIPELINE.ERROR, "")
           .set(PDF_PIPELINE.EXTRACTION_META, "")
           .set(PDF_PIPELINE.STARTED_AT, now)
           .set(PDF_PIPELINE.UPDATED_AT, now)
           .where(PDF_PIPELINE.TENANT_ID.eq(tenant).and(PDF_PIPELINE.PIPELINE_ID.eq(pipelineId)))
           .execute();
    }

    /** Full pipeline row as a map, or null. */
    public Map<String, Object> get(String tenant, PipelineRef ref) {
        return tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return null;
            var record = ctx.selectFrom(PDF_PIPELINE)
                            .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                                    .and(PDF_PIPELINE.PIPELINE_ID.eq(id)))
                            .fetchOne();
            return record == null ? null : pipelineRowToMap(record.intoMap());
        });
    }

    /** Every pipeline row for the tenant — the client-side orphan scan's input. */
    public List<Map<String, Object>> listPipelines(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectFrom(PDF_PIPELINE)
               .where(PDF_PIPELINE.TENANT_ID.eq(tenant))
               .orderBy(PDF_PIPELINE.PIPELINE_ID)
               .fetch(r -> pipelineRowToMap(r.intoMap())));
    }

    /** Allowlisted numeric progress counters + heartbeat refresh. A ref that
     *  names no row is a no-op (the pipeline-001 contract for an unknown hash). */
    public void updateProgress(String tenant, PipelineRef ref, Map<String, Integer> fields) {
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
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return null;
            var update = ctx.update(PDF_PIPELINE).set(PDF_PIPELINE.UPDATED_AT, now);
            for (var entry : fields.entrySet()) {
                update = update.set(
                    DSL.field(DSL.name("nexus", "pdf_pipeline", entry.getKey()), Integer.class),
                    entry.getValue());
            }
            update.where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                    .and(PDF_PIPELINE.PIPELINE_ID.eq(id)))
                  .execute();
            return null;
        });
    }

    public void storeExtractionMeta(String tenant, PipelineRef ref, String metadataJson) {
        setPipelineField(tenant, ref,
            ctx -> ctx.update(PDF_PIPELINE)
                      .set(PDF_PIPELINE.EXTRACTION_META, metadataJson == null ? "" : metadataJson));
    }

    public void markCompleted(String tenant, PipelineRef ref) {
        setPipelineField(tenant, ref,
            ctx -> ctx.update(PDF_PIPELINE).set(PDF_PIPELINE.STATUS, "completed"));
    }

    public void markFailed(String tenant, PipelineRef ref, String error) {
        setPipelineField(tenant, ref,
            ctx -> ctx.update(PDF_PIPELINE)
                      .set(PDF_PIPELINE.STATUS, "failed")
                      .set(PDF_PIPELINE.ERROR, error == null ? "" : error));
    }

    private interface UpdateStart {
        org.jooq.UpdateSetMoreStep<?> begin(DSLContext ctx);
    }

    private void setPipelineField(String tenant, PipelineRef ref, UpdateStart start) {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return null;
            start.begin(ctx)
                 .set(PDF_PIPELINE.UPDATED_AT, now)
                 .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                         .and(PDF_PIPELINE.PIPELINE_ID.eq(id)))
                 .execute();
            return null;
        });
    }

    /** The row a WAL write belongs to; a ref that names no row is refused
     *  (the FK would refuse the INSERT anyway; this names the reason). */
    private static long requireRun(DSLContext ctx, String tenant, PipelineRef ref) {
        Long id = resolveIn(ctx, tenant, ref);
        if (id == null) {
            throw new IllegalArgumentException("no pipeline row for " + ref.describe());
        }
        return id;
    }

    // ── pages ────────────────────────────────────────────────────────────────

    /** Batch upsert (INSERT-OR-REPLACE parity); one call = one transaction.
     *
     * <p>nexus-yvzhz: {@code page_text} and {@code metadata_json} are sanitized
     * (NUL stripped, mirroring {@code PgVectorRepository.stripNul}) before the
     * bind — a broken PDF ToUnicode CMap can carry raw NUL bytes in the
     * PyMuPDF text layer, and Postgres {@code text} cannot store {@code 0x00}
     * (SQLSTATE 22021). Safe here because neither is an identity source: no
     * chash derives from page text. Contrast {@link #writeChunks}, where
     * {@code chunk_text} is NOT sanitized because it IS caller-computed
     * identity (the chash is over the exact bytes). */
    public int writePages(String tenant, PipelineRef ref, List<Map<String, Object>> pages) {
        if (pages.isEmpty()) return 0;
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            long id = requireRun(ctx, tenant, ref);
            for (Map<String, Object> page : pages) {
                ctx.insertInto(PDF_PAGES,
                        PDF_PAGES.TENANT_ID, PDF_PAGES.PIPELINE_ID, PDF_PAGES.PAGE_INDEX,
                        PDF_PAGES.PAGE_TEXT, PDF_PAGES.METADATA_JSON, PDF_PAGES.CREATED_AT)
                   .values(tenant, id,
                           ((Number) page.get("page_index")).intValue(),
                           stripNul((String) page.get("page_text")),
                           stripNul(page.get("metadata_json") instanceof String s ? s : "{}"),
                           now)
                   .onConflict(PDF_PAGES.TENANT_ID, PDF_PAGES.PIPELINE_ID, PDF_PAGES.PAGE_INDEX)
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

    /** Pages with page_index >= start, ordered; empty when the ref names no row. */
    public List<Map<String, Object>> readPagesFrom(String tenant, PipelineRef ref, int startIndex) {
        return tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return List.of();
            return ctx.selectFrom(PDF_PAGES)
                      .where(PDF_PAGES.TENANT_ID.eq(tenant)
                              .and(PDF_PAGES.PIPELINE_ID.eq(id))
                              .and(PDF_PAGES.PAGE_INDEX.ge(startIndex)))
                      .orderBy(PDF_PAGES.PAGE_INDEX)
                      .fetch(r -> timeToString(r.intoMap()));
        });
    }

    // ── chunks ───────────────────────────────────────────────────────────────

    /** Batch INSERT-OR-IGNORE (idempotent resume; existing rows keep their
     *  embeddings); one call = one transaction. Returns rows actually inserted.
     *
     * <p>nexus-yvzhz: unlike {@link #writePages}, {@code chunk_text} is bound
     * RAW, deliberately NOT NUL-stripped — {@code chunk_id}/chash is
     * caller-computed identity over the exact bytes, and silently mutating the
     * text would desync content addressing. A NUL byte in {@code chunk_text}
     * therefore still reaches Postgres unsanitized and SQLSTATE 22021 fires;
     * nexus-dmrkm maps that to a typed 422 (not the previous opaque 500) so
     * the rejection is legible instead of silent-mutation-or-crash.
     * {@code metadata_json} is NOT an identity source (only {@code chunk_id}
     * and {@code chunk_text} are), so it IS sanitized like the pages path. */
    public int writeChunks(String tenant, PipelineRef ref, List<Map<String, Object>> chunks) {
        if (chunks.isEmpty()) return 0;
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        return tenantScope.withTenant(tenant, ctx -> {
            long id = requireRun(ctx, tenant, ref);
            int inserted = 0;
            for (Map<String, Object> chunk : chunks) {
                inserted += ctx.insertInto(PDF_CHUNKS,
                        PDF_CHUNKS.TENANT_ID, PDF_CHUNKS.PIPELINE_ID, PDF_CHUNKS.CHUNK_INDEX,
                        PDF_CHUNKS.CHUNK_TEXT, PDF_CHUNKS.CHUNK_ID, PDF_CHUNKS.METADATA_JSON,
                        PDF_CHUNKS.EMBEDDING, PDF_CHUNKS.UPLOADED, PDF_CHUNKS.CREATED_AT)
                   .values(tenant, id,
                           ((Number) chunk.get("chunk_index")).intValue(),
                           (String) chunk.get("chunk_text"),
                           (String) chunk.get("chunk_id"),
                           stripNul(chunk.get("metadata_json") instanceof String s ? s : "{}"),
                           (byte[]) chunk.get("embedding"),
                           Boolean.FALSE,
                           now)
                   .onConflict(PDF_CHUNKS.TENANT_ID, PDF_CHUNKS.PIPELINE_ID, PDF_CHUNKS.CHUNK_INDEX)
                   .doNothing()
                   .execute();
            }
            return inserted;
        });
    }

    public List<Map<String, Object>> readReadyChunks(String tenant, PipelineRef ref) {
        return readChunks(tenant, ref, PDF_CHUNKS.UPLOADED.isFalse(), 0);
    }

    public List<Map<String, Object>> readUploadableChunks(String tenant, PipelineRef ref, int limit) {
        return readChunks(tenant, ref,
            PDF_CHUNKS.UPLOADED.isFalse().and(PDF_CHUNKS.EMBEDDING.isNotNull()), limit);
    }

    private List<Map<String, Object>> readChunks(
            String tenant, PipelineRef ref, Condition extra, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return List.of();
            var query = ctx.selectFrom(PDF_CHUNKS)
                           .where(PDF_CHUNKS.TENANT_ID.eq(tenant)
                                   .and(PDF_CHUNKS.PIPELINE_ID.eq(id))
                                   .and(extra))
                           .orderBy(PDF_CHUNKS.CHUNK_INDEX);
            var rows = limit > 0 ? query.limit(limit).fetch() : query.fetch();
            return rows.map(r -> timeToString(r.intoMap()));
        });
    }

    public int markUploaded(String tenant, PipelineRef ref, List<Integer> chunkIndices) {
        if (chunkIndices.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return 0;
            return ctx.update(PDF_CHUNKS)
                      .set(PDF_CHUNKS.UPLOADED, Boolean.TRUE)
                      .where(PDF_CHUNKS.TENANT_ID.eq(tenant)
                              .and(PDF_CHUNKS.PIPELINE_ID.eq(id))
                              .and(PDF_CHUNKS.CHUNK_INDEX.in(chunkIndices)))
                      .execute();
        });
    }

    /** Embedded chunks of ONE run: a second document sharing the bytes starts
     *  at zero (the deadlock the key-widen attempt had: a sibling's count
     *  seeded the chunker past chunks the new row never uploaded). */
    public int countEmbeddedChunks(String tenant, PipelineRef ref) {
        return tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return 0;
            return ctx.fetchCount(PDF_CHUNKS,
                PDF_CHUNKS.TENANT_ID.eq(tenant)
                    .and(PDF_CHUNKS.PIPELINE_ID.eq(id))
                    .and(PDF_CHUNKS.EMBEDDING.isNotNull()));
        });
    }

    public int countPipelines(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetchCount(PDF_PIPELINE, PDF_PIPELINE.TENANT_ID.eq(tenant)));
    }

    // ── cleanup ──────────────────────────────────────────────────────────────

    /** Delete ONE run's WAL page/chunk rows, preserving the pipeline row's
     *  audit trail (the nexus-2fyb orphan-page replay fix, mirrored). A
     *  sibling run sharing the bytes is untouched: the WAL is keyed on the
     *  run, not the hash (nexus-edjmu).
     *
     * <p>nexus-33q80: zeroes {@code chunks_uploaded} and {@code
     * pages_extracted} on the pipeline row in the SAME transaction as the
     * WAL wipe. Before this, the client made a SECOND call
     * ({@code update_progress(chunks_uploaded=0)}) right after
     * {@code clear_orphan_wal} to undo exactly this staleness; if the wipe
     * landed and that second call independently failed, the counter
     * survived a wiped WAL and a resume seeded the uploader from it,
     * eventually refusing completion with {@code IndexRunVerifyRefused}.
     * Two client calls can never be atomic; doing it here removes the
     * failure window entirely (the client's own reset call, and its
     * {@code pipeline_chunks_uploaded_reset_failed_after_wal_wipe} log
     * event, are now dead code and removed). */
    public void clearOrphanWal(String tenant, PipelineRef ref) {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return null;
            deleteWal(ctx, tenant, id);
            ctx.update(PDF_PIPELINE)
               .set(PDF_PIPELINE.CHUNKS_UPLOADED, 0)
               .set(PDF_PIPELINE.PAGES_EXTRACTED, 0)
               .set(PDF_PIPELINE.UPDATED_AT, now)
               .where(PDF_PIPELINE.TENANT_ID.eq(tenant).and(PDF_PIPELINE.PIPELINE_ID.eq(id)))
               .execute();
            return null;
        });
    }

    private static void deleteWal(DSLContext ctx, String tenant, long pipelineId) {
        ctx.deleteFrom(PDF_PAGES)
           .where(PDF_PAGES.TENANT_ID.eq(tenant).and(PDF_PAGES.PIPELINE_ID.eq(pipelineId)))
           .execute();
        ctx.deleteFrom(PDF_CHUNKS)
           .where(PDF_CHUNKS.TENANT_ID.eq(tenant).and(PDF_CHUNKS.PIPELINE_ID.eq(pipelineId)))
           .execute();
    }

    /** Remove ONE run: its pipeline row, and its pages and chunks through the
     *  {@code ON DELETE CASCADE} FK (pipeline-002-2). Returns whether a row
     *  was removed. */
    public boolean deletePipeline(String tenant, PipelineRef ref) {
        return tenantScope.withTenant(tenant, ctx -> {
            Long id = resolveIn(ctx, tenant, ref);
            if (id == null) return false;
            int deleted = ctx.deleteFrom(PDF_PIPELINE)
                             .where(PDF_PIPELINE.TENANT_ID.eq(tenant).and(PDF_PIPELINE.PIPELINE_ID.eq(id)))
                             .execute();
            return deleted == 1;
        });
    }

    /** Remove every pipeline (+ pages + chunks, by FK cascade) targeting a
     *  collection (the nexus-8a8e `nx collection delete` hook, mirrored).
     *  A run of the same bytes in ANOTHER collection keeps its row and WAL.
     *  Returns the number of pipeline rows removed. */
    public int deleteForCollection(String tenant, String collection) {
        requireNonBlank(collection, "collection");
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(PDF_PIPELINE)
               .where(PDF_PIPELINE.TENANT_ID.eq(tenant)
                       .and(PDF_PIPELINE.COLLECTION.eq(collection)))
               .execute());
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Strip NUL (0x00) — unstorable in Postgres {@code text}/{@code jsonb}
     *  (nexus-rvfwj / nexus-yvzhz). Mirrors {@code PgVectorRepository.stripNul};
     *  duplicated rather than shared because the two repositories live in
     *  different packages with no existing common base. */
    private static String stripNul(String s) {
        return (s != null && s.indexOf('\u0000') >= 0) ? s.replace("\u0000", "") : s;
    }

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
