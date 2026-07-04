package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.*;
import static org.jooq.impl.DSL.*;

/**
 * RDR-152 bead nexus-gmiaf.15 — repository for aspects, highlights, queue, and promotion-log.
 *
 * <p>Mirrors four SQLite stores in the Postgres service tier:
 * <ul>
 *   <li>{@code document_aspects} — per-document structured aspect records (RDR-089)</li>
 *   <li>{@code document_highlights} — DEVONthink highlight/mention notes (RDR-139 Layer E)</li>
 *   <li>{@code aspect_extraction_queue} — durable async extraction queue</li>
 *   <li>{@code aspect_promotion_log} — extras→column promotion audit log</li>
 * </ul>
 *
 * <p>All methods route through {@link TenantScope#withTenant} for RLS enforcement.
 *
 * <p>Queue claim strategy: {@code claimNext} uses
 * {@code SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1} — atomically claims one
 * pending row per caller with no double-claim risk across concurrent workers.
 * This is the key contention fix that motivated RDR-152 for this store.
 */
public final class AspectRepository {

    private static final Logger log = LoggerFactory.getLogger(AspectRepository.class);

    static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'")
                             .withZone(ZoneOffset.UTC);

    /** Fallback formatter for second-precision ISO strings without fractional seconds. */
    static final DateTimeFormatter UTC_SECOND_NO_FRAC =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

    /** Minimum confidence threshold (mirrors Python _MIN_CONFIDENCE = 0.3). */
    private static final double MIN_CONFIDENCE = 0.3;

    // EXCLUDED pseudo-table fields used in ON CONFLICT DO UPDATE SET, typed via
    // DSL.excluded(Field) (nexus-mzuj9: replaces the hand-built field("EXCLUDED.x", ...)
    // raw-string fragments — jOOQ codegen now type-checks every column reference here
    // against the generated DOCUMENT_ASPECTS / DOCUMENT_HIGHLIGHTS / ASPECT_EXTRACTION_QUEUE
    // tables, so a Liquibase column rename fails at COMPILE time instead of at runtime).
    private static final Field<String>          EX_PROBLEM_FORMULATION    = excluded(DOCUMENT_ASPECTS.PROBLEM_FORMULATION);
    private static final Field<String>          EX_PROPOSED_METHOD        = excluded(DOCUMENT_ASPECTS.PROPOSED_METHOD);
    private static final Field<String>          EX_EXPERIMENTAL_DATASETS  = excluded(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS);
    private static final Field<String>          EX_EXPERIMENTAL_BASELINES = excluded(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES);
    private static final Field<String>          EX_EXPERIMENTAL_RESULTS   = excluded(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS);
    private static final Field<String>          EX_EXTRAS                 = excluded(DOCUMENT_ASPECTS.EXTRAS);
    private static final Field<Double>          EX_CONFIDENCE             = excluded(DOCUMENT_ASPECTS.CONFIDENCE);
    private static final Field<OffsetDateTime>  EX_EXTRACTED_AT           = excluded(DOCUMENT_ASPECTS.EXTRACTED_AT);
    private static final Field<String>          EX_MODEL_VERSION          = excluded(DOCUMENT_ASPECTS.MODEL_VERSION);
    private static final Field<String>          EX_EXTRACTOR_NAME         = excluded(DOCUMENT_ASPECTS.EXTRACTOR_NAME);
    private static final Field<String>          EX_SOURCE_URI             = excluded(DOCUMENT_ASPECTS.SOURCE_URI);
    private static final Field<String>          EX_SALIENT_SENTENCES      = excluded(DOCUMENT_ASPECTS.SALIENT_SENTENCES);
    private static final Field<String>          EX_DOC_ID                 = excluded(DOCUMENT_ASPECTS.DOC_ID);

    // COALESCE(EXCLUDED.x, table.x) fields for importAspect path
    private static final Field<String>          EX_SOURCE_URI_COALESCE =
        coalesce(excluded(DOCUMENT_ASPECTS.SOURCE_URI), DOCUMENT_ASPECTS.SOURCE_URI);
    private static final Field<String>          EX_SALIENT_COALESCE =
        coalesce(excluded(DOCUMENT_ASPECTS.SALIENT_SENTENCES), DOCUMENT_ASPECTS.SALIENT_SENTENCES);
    private static final Field<String>          EX_DOC_ID_COALESCE =
        coalesce(excluded(DOCUMENT_ASPECTS.DOC_ID), DOCUMENT_ASPECTS.DOC_ID);

    // document_highlights EXCLUDED fields
    private static final Field<String>          EX_HL_SOURCE_URI   = excluded(DOCUMENT_HIGHLIGHTS.SOURCE_URI);
    private static final Field<String>          EX_HL_COLLECTION   = excluded(DOCUMENT_HIGHLIGHTS.COLLECTION);
    private static final Field<String>          EX_HL_HIGHLIGHTS   = excluded(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD);
    private static final Field<String>          EX_HL_MENTIONS     = excluded(DOCUMENT_HIGHLIGHTS.MENTIONS_MD);
    private static final Field<OffsetDateTime>  EX_HL_INGESTED_AT  = excluded(DOCUMENT_HIGHLIGHTS.INGESTED_AT);

    // aspect_extraction_queue EXCLUDED fields
    private static final Field<String>          EX_Q_DOC_ID          = excluded(ASPECT_EXTRACTION_QUEUE.DOC_ID);
    private static final Field<String>          EX_Q_CONTENT_HASH    = excluded(ASPECT_EXTRACTION_QUEUE.CONTENT_HASH);
    private static final Field<String>          EX_Q_CONTENT         = excluded(ASPECT_EXTRACTION_QUEUE.CONTENT);
    private static final Field<OffsetDateTime>  EX_Q_ENQUEUED_AT     = excluded(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT);
    private static final Field<String>          EX_Q_LAST_ERROR      = excluded(ASPECT_EXTRACTION_QUEUE.LAST_ERROR);
    // Complex GREATEST/LEAST/CASE fields for importQueueRow, typed via DSL.when/.otherwise
    // and DSL.greatest/DSL.least against the EXCLUDED pseudo-table + the live column.
    private static final Field<String>          EX_Q_STATUS_CASE =
        when(ASPECT_EXTRACTION_QUEUE.STATUS.eq("in_progress"), ASPECT_EXTRACTION_QUEUE.STATUS)
            .otherwise(excluded(ASPECT_EXTRACTION_QUEUE.STATUS));
    private static final Field<Integer>         EX_Q_RETRY_GREATEST =
        greatest(excluded(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT), ASPECT_EXTRACTION_QUEUE.RETRY_COUNT);
    private static final Field<OffsetDateTime>  EX_Q_ENQUEUED_LEAST =
        least(excluded(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT), ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT);
    private static final Field<OffsetDateTime>  EX_Q_ATTEMPT_GREATEST =
        greatest(excluded(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT), ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT);
    private static final Field<String>          EX_Q_LAST_ERROR_CASE =
        when(excluded(ASPECT_EXTRACTION_QUEUE.STATUS).eq("failed"), excluded(ASPECT_EXTRACTION_QUEUE.LAST_ERROR))
            .otherwise(ASPECT_EXTRACTION_QUEUE.LAST_ERROR);

    private final TenantScope tenantScope;

    public AspectRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── Timestamp helpers ──────────────────────────────────────────────────────

    static OffsetDateTime parseTs(String iso) {
        if (iso == null || iso.isBlank()) return OffsetDateTime.now(ZoneOffset.UTC);
        // Try fractional-seconds form first; fall back to second-precision.
        try {
            return OffsetDateTime.parse(iso, DateTimeFormatter.ISO_OFFSET_DATE_TIME);
        } catch (DateTimeParseException e1) {
            try {
                return OffsetDateTime.parse(iso,
                    DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                                     .withZone(ZoneOffset.UTC));
            } catch (DateTimeParseException e2) {
                return OffsetDateTime.now(ZoneOffset.UTC);
            }
        }
    }

    static String formatTs(OffsetDateTime dt) {
        if (dt == null) return null;
        return dt.atZoneSameInstant(ZoneOffset.UTC).format(
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'"));
    }

    // ── document_aspects ───────────────────────────────────────────────────────

    /**
     * Upsert an aspect record.
     *
     * <p>Confidence gate: records with confidence &lt; MIN_CONFIDENCE are rejected
     * (mirrors Python _MIN_CONFIDENCE = 0.3 gate in DocumentAspects.upsert).
     *
     * <p>Conflict key: (tenant_id, collection, source_path). Returns the
     * surrogate id of the inserted/updated row, or -1 if rejected.
     */
    /**
     * RDR-164 P1a: ensure catalog_collections has a stub row for {@code collection}
     * before any document_aspects / document_highlights / aspect_extraction_queue write,
     * so the NOT VALID collection FKs (fk-003) cannot reject a live serving write whose
     * collection has not yet been registered by the catalog ETL or a chunk upsert.
     * Idempotent (ON CONFLICT DO NOTHING); a no-op for null/blank collection
     * (document_highlights.collection is nullable — MATCH SIMPLE lets null escape the FK).
     */
    private static void ensureCollectionRegistered(DSLContext ctx, String tenant, String collection) {
        if (collection == null || collection.isBlank()) return;
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, collection)
           .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .doNothing()
           .execute();
    }

    public long upsertAspect(String tenant, Map<String, Object> body) {
        double confidence = body.containsKey("confidence")
            ? ((Number) body.get("confidence")).doubleValue()
            : -1.0;
        if (confidence < MIN_CONFIDENCE) {
            log.warn("event=aspect_upsert_rejected_low_confidence collection={} source_path={} confidence={}",
                body.get("collection"), body.get("source_path"), confidence);
            return -1L;
        }

        String collection    = (String) body.get("collection");
        String sourcePath    = (String) body.get("source_path");
        String extractedAt   = (String) body.get("extracted_at");
        String modelVersion  = (String) body.get("model_version");
        String extractorName = (String) body.get("extractor_name");
        if (collection == null || sourcePath == null || extractedAt == null
                || modelVersion == null || extractorName == null) {
            throw new IllegalArgumentException("aspect upsert: required fields missing");
        }

        OffsetDateTime extractedAtTs = parseTs(extractedAt);

        return tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            var result = ctx.insertInto(DOCUMENT_ASPECTS,
                    DOCUMENT_ASPECTS.TENANT_ID,
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .values(
                    tenant,
                    collection,
                    sourcePath,
                    (String) body.get("problem_formulation"),
                    (String) body.get("proposed_method"),
                    (String) body.get("experimental_datasets"),
                    (String) body.get("experimental_baselines"),
                    (String) body.get("experimental_results"),
                    (String) body.get("extras"),
                    confidence,
                    extractedAtTs,
                    modelVersion,
                    extractorName,
                    (String) body.get("source_uri"),
                    (String) body.get("salient_sentences"),
                    nullIfBlank((String) body.get("doc_id")))
                .onConflict(
                    DOCUMENT_ASPECTS.TENANT_ID,
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH)
                .doUpdate()
                .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION,    EX_PROBLEM_FORMULATION)
                .set(DOCUMENT_ASPECTS.PROPOSED_METHOD,        EX_PROPOSED_METHOD)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,  EX_EXPERIMENTAL_DATASETS)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES, EX_EXPERIMENTAL_BASELINES)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,   EX_EXPERIMENTAL_RESULTS)
                .set(DOCUMENT_ASPECTS.EXTRAS,                 EX_EXTRAS)
                .set(DOCUMENT_ASPECTS.CONFIDENCE,             EX_CONFIDENCE)
                .set(DOCUMENT_ASPECTS.EXTRACTED_AT,           EX_EXTRACTED_AT)
                .set(DOCUMENT_ASPECTS.MODEL_VERSION,          EX_MODEL_VERSION)
                .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME,         EX_EXTRACTOR_NAME)
                .set(DOCUMENT_ASPECTS.SOURCE_URI,             EX_SOURCE_URI)
                .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES,      EX_SALIENT_SENTENCES)
                .set(DOCUMENT_ASPECTS.DOC_ID,                 EX_DOC_ID)
                .returning(DOCUMENT_ASPECTS.ID)
                .fetch();
            return result.isEmpty() ? -1L : result.get(0).get(DOCUMENT_ASPECTS.ID);
        });
    }

    /**
     * Get an aspect record by (collection, source_path).
     */
    public Optional<Map<String, Object>> getAspect(String tenant, String collection, String sourcePath) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection)
                    .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(sourcePath)))
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(recordToMap(rows.get(0)));
        });
    }

    /**
     * Get an aspect record by doc_id (catalog tumbler).
     */
    public Optional<Map<String, Object>> getAspectByDocId(String tenant, String docId) {
        if (docId == null || docId.isBlank()) return Optional.empty();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(recordToMap(rows.get(0)));
        });
    }

    /**
     * List aspect records for a collection, paginated.
     */
    public List<Map<String, Object>> listByCollection(String tenant, String collection, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            var q = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection))
                .orderBy(DOCUMENT_ASPECTS.SOURCE_PATH.asc());
            var rows = (limit > 0 ? q.limit(limit).offset(offset) : q).fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) out.add(recordToMap(r));
            return out;
        });
    }

    /**
     * List records by extractor/version for re-extraction triage.
     */
    public List<Map<String, Object>> listByExtractorVersion(String tenant, String extractorName, String maxVersion) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.EXTRACTOR_NAME.eq(extractorName)
                    .and(DOCUMENT_ASPECTS.MODEL_VERSION.lt(maxVersion)))
                .orderBy(DOCUMENT_ASPECTS.COLLECTION.asc(), DOCUMENT_ASPECTS.SOURCE_PATH.asc())
                .fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) out.add(recordToMap(r));
            return out;
        });
    }

    /**
     * Delete an aspect by (collection, source_path). Returns deleted count.
     */
    public int deleteAspect(String tenant, String collection, String sourcePath) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(DOCUMENT_ASPECTS)
               .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection)
                   .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(sourcePath)))
               .execute());
    }

    /**
     * Rename collection denorm cache (mirrors DocumentAspects.rename_collection).
     */
    public int renameAspectCollection(String tenant, String oldColl, String newColl) {
        return tenantScope.withTenant(tenant, ctx -> {
            // RDR-164 P1a: register the new collection before re-pointing the denorm column
            ensureCollectionRegistered(ctx, tenant, newColl);
            // Collision defense: delete conflicting new-side rows first
            ctx.deleteFrom(DOCUMENT_ASPECTS)
               .where(DOCUMENT_ASPECTS.COLLECTION.eq(newColl)
                   .and(DOCUMENT_ASPECTS.SOURCE_PATH.in(
                       select(DOCUMENT_ASPECTS.SOURCE_PATH)
                           .from(DOCUMENT_ASPECTS)
                           .where(DOCUMENT_ASPECTS.COLLECTION.eq(oldColl)))))
               .execute();
            return ctx.update(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.COLLECTION, newColl)
                .where(DOCUMENT_ASPECTS.COLLECTION.eq(oldColl))
                .execute();
        });
    }

    /**
     * Set salient_sentences for a doc_id. Returns rows updated.
     */
    public int setSalientSentences(String tenant, String docId, String sentencesJson) {
        if (docId == null || docId.isBlank()) return 0;
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(DOCUMENT_ASPECTS)
               .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES, sentencesJson)
               .where(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
               .execute());
    }

    /**
     * Set salient_sentences by (collection, source_path) — pre-migration fallback.
     */
    public int setSalientSentencesByKey(String tenant, String collection, String sourcePath, String sentencesJson) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(DOCUMENT_ASPECTS)
               .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES, sentencesJson)
               .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection)
                   .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(sourcePath)))
               .execute());
    }

    /**
     * Get salient_sentences for a doc_id. Returns null when not found.
     */
    public String getSalientSentences(String tenant, String docId) {
        if (docId == null || docId.isBlank()) return null;
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(DOCUMENT_ASPECTS.SALIENT_SENTENCES)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
                .fetch();
            if (rows.isEmpty()) return null;
            Object val = rows.get(0).get(0);
            return val == null ? null : val.toString();
        });
    }

    /**
     * Fidelity ETL import — idempotent, complete-overwrite on (tenant_id, collection, source_path).
     * Confidence gate: rows with confidence &lt; MIN_CONFIDENCE are skipped.
     * extracted_at is preserved verbatim (EXCLUDED.*). Returns count imported.
     */
    public int importAspect(String tenant, Map<String, Object> body) {
        return tenantScope.withTenant(tenant, ctx -> doImportAspect(ctx, tenant, body));
    }

    /** PG Int16 bind-count limit is 32767; keep a safety margin (nexus-1usso). */
    private static final int MAX_BATCH_PARAMS = 30_000;

    /**
     * nexus-1usso: GUC-once bulk aspect import — ONE multi-row {@code INSERT
     * ... ON CONFLICT} statement per chunk, mirroring {@code
     * ChashRepository.doImportBatch} (f0ab406f). The RDR-176 P3 endpoint
     * already existed but still looped the per-row {@link #doImportAspect}
     * (N round-trips) — the plan-audit finding on nexus-1usso ("has the
     * endpoint" != "batches at the DB") applies to every Aspect import
     * method. The confidence gate is applied BEFORE the multi-row insert
     * (sub-confidence rows never reach it, matching the single-row path's
     * skip semantics); kept rows are then deduped on {@code (collection,
     * source_path)} — the conflict key — within a chunk, last occurrence
     * wins. Returns the number of rows actually written (sub-confidence
     * rows count 0, matching the single-row path) — NOT the post-dedup
     * landed count, since each surviving row independently satisfied the
     * confidence gate.
     */
    public int importAspectsBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> kept = new ArrayList<>();
            for (var body : rows) {
                double confidence = body.containsKey("confidence") && body.get("confidence") != null
                    ? ((Number) body.get("confidence")).doubleValue()
                    : -1.0;
                if (confidence >= MIN_CONFIDENCE) kept.add(body);
            }
            if (kept.isEmpty()) return 0;

            var collections = new java.util.LinkedHashSet<String>();
            for (var body : kept) {
                String c = (String) body.get("collection");
                if (c != null) collections.add(c);
            }
            for (String c : collections) ensureCollectionRegistered(ctx, tenant, c);

            // Conflict key: (tenant_id, collection, source_path). tenant constant.
            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(kept.size());
            for (var body : kept) {
                unique.put(body.get("collection") + " " + body.get("source_path"), body);
            }
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int cols = 16;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(DOCUMENT_ASPECTS,
                        DOCUMENT_ASPECTS.TENANT_ID,
                        DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH,
                        DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                        DOCUMENT_ASPECTS.PROPOSED_METHOD,
                        DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                        DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                        DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                        DOCUMENT_ASPECTS.EXTRAS,
                        DOCUMENT_ASPECTS.CONFIDENCE,
                        DOCUMENT_ASPECTS.EXTRACTED_AT,
                        DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                        DOCUMENT_ASPECTS.SOURCE_URI,
                        DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                        DOCUMENT_ASPECTS.DOC_ID);
                for (var body : batch) {
                    double confidence = ((Number) body.get("confidence")).doubleValue();
                    OffsetDateTime extractedAtTs = parseTs((String) body.get("extracted_at"));
                    insert = insert.values(
                            tenant,
                            (String) body.get("collection"),
                            (String) body.get("source_path"),
                            (String) body.get("problem_formulation"),
                            (String) body.get("proposed_method"),
                            (String) body.get("experimental_datasets"),
                            (String) body.get("experimental_baselines"),
                            (String) body.get("experimental_results"),
                            (String) body.get("extras"),
                            confidence,
                            extractedAtTs,
                            (String) body.get("model_version"),
                            (String) body.get("extractor_name"),
                            (String) body.get("source_uri"),
                            (String) body.get("salient_sentences"),
                            nullIfBlank((String) body.get("doc_id")));
                }
                insert.onConflict(
                        DOCUMENT_ASPECTS.TENANT_ID,
                        DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH)
                      .doUpdate()
                      .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION,    EX_PROBLEM_FORMULATION)
                      .set(DOCUMENT_ASPECTS.PROPOSED_METHOD,        EX_PROPOSED_METHOD)
                      .set(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,  EX_EXPERIMENTAL_DATASETS)
                      .set(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES, EX_EXPERIMENTAL_BASELINES)
                      .set(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,   EX_EXPERIMENTAL_RESULTS)
                      .set(DOCUMENT_ASPECTS.EXTRAS,                 EX_EXTRAS)
                      .set(DOCUMENT_ASPECTS.CONFIDENCE,             EX_CONFIDENCE)
                      .set(DOCUMENT_ASPECTS.EXTRACTED_AT,           EX_EXTRACTED_AT)
                      .set(DOCUMENT_ASPECTS.MODEL_VERSION,          EX_MODEL_VERSION)
                      .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME,         EX_EXTRACTOR_NAME)
                      .set(DOCUMENT_ASPECTS.SOURCE_URI,             EX_SOURCE_URI_COALESCE)
                      .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES,      EX_SALIENT_COALESCE)
                      .set(DOCUMENT_ASPECTS.DOC_ID,                 EX_DOC_ID_COALESCE)
                      .execute();
            }
            return kept.size();
        });
    }

    private int doImportAspect(DSLContext ctx, String tenant, Map<String, Object> body) {
        double confidence = body.containsKey("confidence") && body.get("confidence") != null
            ? ((Number) body.get("confidence")).doubleValue()
            : -1.0;
        if (confidence < MIN_CONFIDENCE) return 0;

        String extractedAt = (String) body.get("extracted_at");
        OffsetDateTime extractedAtTs = parseTs(extractedAt);

        {
            ensureCollectionRegistered(ctx, tenant, (String) body.get("collection"));
            return ctx.insertInto(DOCUMENT_ASPECTS,
                    DOCUMENT_ASPECTS.TENANT_ID,
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .values(
                    tenant,
                    (String) body.get("collection"),
                    (String) body.get("source_path"),
                    (String) body.get("problem_formulation"),
                    (String) body.get("proposed_method"),
                    (String) body.get("experimental_datasets"),
                    (String) body.get("experimental_baselines"),
                    (String) body.get("experimental_results"),
                    (String) body.get("extras"),
                    confidence,
                    extractedAtTs,
                    (String) body.get("model_version"),
                    (String) body.get("extractor_name"),
                    (String) body.get("source_uri"),
                    (String) body.get("salient_sentences"),
                    nullIfBlank((String) body.get("doc_id")))
                .onConflict(
                    DOCUMENT_ASPECTS.TENANT_ID,
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH)
                .doUpdate()
                .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION,    EX_PROBLEM_FORMULATION)
                .set(DOCUMENT_ASPECTS.PROPOSED_METHOD,        EX_PROPOSED_METHOD)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,  EX_EXPERIMENTAL_DATASETS)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES, EX_EXPERIMENTAL_BASELINES)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,   EX_EXPERIMENTAL_RESULTS)
                .set(DOCUMENT_ASPECTS.EXTRAS,                 EX_EXTRAS)
                .set(DOCUMENT_ASPECTS.CONFIDENCE,             EX_CONFIDENCE)
                .set(DOCUMENT_ASPECTS.EXTRACTED_AT,           EX_EXTRACTED_AT)
                .set(DOCUMENT_ASPECTS.MODEL_VERSION,          EX_MODEL_VERSION)
                .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME,         EX_EXTRACTOR_NAME)
                .set(DOCUMENT_ASPECTS.SOURCE_URI,             EX_SOURCE_URI_COALESCE)
                .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES,      EX_SALIENT_COALESCE)
                .set(DOCUMENT_ASPECTS.DOC_ID,                 EX_DOC_ID_COALESCE)
                .execute();
        }
    }

    // ── document_highlights ────────────────────────────────────────────────────

    /**
     * Upsert a highlight record. Returns true on write, false when no content.
     */
    public boolean upsertHighlight(String tenant, Map<String, Object> body) {
        String docId  = (String) body.get("doc_id");
        String ingestedAt = (String) body.get("ingested_at");
        if (docId == null || docId.isBlank()) throw new IllegalArgumentException("doc_id must not be empty");
        if (ingestedAt == null || ingestedAt.isBlank()) throw new IllegalArgumentException("ingested_at must not be empty");
        String highlightsMd = (String) body.getOrDefault("highlights_md", "");
        String mentionsMd   = (String) body.getOrDefault("mentions_md", "");
        if ((highlightsMd == null || highlightsMd.isBlank())
                && (mentionsMd == null || mentionsMd.isBlank())) {
            return false;
        }

        OffsetDateTime ingestedAtTs = parseTs(ingestedAt);
        tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, (String) body.get("collection"));
            ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                    DOCUMENT_HIGHLIGHTS.TENANT_ID,
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(
                    tenant,
                    docId,
                    (String) body.get("source_uri"),
                    (String) body.get("collection"),
                    highlightsMd,
                    mentionsMd,
                    ingestedAtTs)
                .onConflict(DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID)
                .doUpdate()
                .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI,    EX_HL_SOURCE_URI)
                .set(DOCUMENT_HIGHLIGHTS.COLLECTION,    EX_HL_COLLECTION)
                .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, EX_HL_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.MENTIONS_MD,   EX_HL_MENTIONS)
                .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT,   EX_HL_INGESTED_AT)
                .execute();
            return null;
        });
        return true;
    }

    /**
     * Get a highlight record by doc_id.
     */
    public Optional<Map<String, Object>> getHighlight(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId))
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(highlightToMap(rows.get(0)));
        });
    }

    /**
     * Get a highlight record by source_uri (DEVONthink UUID URI).
     */
    public Optional<Map<String, Object>> getHighlightBySourceUri(String tenant, String sourceUri) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.SOURCE_URI.eq(sourceUri))
                .limit(1)
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(highlightToMap(rows.get(0)));
        });
    }

    /**
     * List highlight records, most recent first.
     */
    public List<Map<String, Object>> listHighlights(String tenant, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .from(DOCUMENT_HIGHLIGHTS)
                .orderBy(DOCUMENT_HIGHLIGHTS.INGESTED_AT.desc())
                .limit(limit)
                .offset(offset)
                .fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) out.add(highlightToMap(r));
            return out;
        });
    }

    /**
     * Delete a highlight by doc_id. Returns true if deleted.
     */
    public boolean deleteHighlight(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(DOCUMENT_HIGHLIGHTS)
               .where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId))
               .execute() > 0);
    }

    /**
     * ETL import — fidelity-preserving highlight upsert.
     */
    public int importHighlight(String tenant, Map<String, Object> body) {
        return tenantScope.withTenant(tenant, ctx -> doImportHighlight(ctx, tenant, body));
    }

    /**
     * nexus-1usso: GUC-once bulk highlight import — ONE multi-row {@code
     * INSERT ... ON CONFLICT} statement per chunk. Rows with a blank
     * {@code doc_id} are skipped BEFORE the insert (matching the single-row
     * path's skip semantics); kept rows are deduped on {@code doc_id} — the
     * conflict key — within a chunk, last occurrence wins. Returns the
     * number of rows actually written (blank-doc_id rows count 0).
     */
    public int importHighlightsBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> kept = new ArrayList<>();
            for (var body : rows) {
                String docId = (String) body.get("doc_id");
                if (docId != null && !docId.isBlank()) kept.add(body);
            }
            if (kept.isEmpty()) return 0;

            var collections = new java.util.LinkedHashSet<String>();
            for (var body : kept) {
                String c = (String) body.get("collection");
                if (c != null) collections.add(c);
            }
            for (String c : collections) ensureCollectionRegistered(ctx, tenant, c);

            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(kept.size());
            for (var body : kept) unique.put((String) body.get("doc_id"), body);
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int cols = 7;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                        DOCUMENT_HIGHLIGHTS.TENANT_ID,
                        DOCUMENT_HIGHLIGHTS.DOC_ID,
                        DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                        DOCUMENT_HIGHLIGHTS.COLLECTION,
                        DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                        DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                        DOCUMENT_HIGHLIGHTS.INGESTED_AT);
                for (var body : batch) {
                    String ingestedAt = (String) body.get("ingested_at");
                    OffsetDateTime ingestedAtTs = parseTs(ingestedAt);
                    insert = insert.values(
                            tenant,
                            (String) body.get("doc_id"),
                            (String) body.get("source_uri"),
                            (String) body.get("collection"),
                            (String) body.getOrDefault("highlights_md", ""),
                            (String) body.getOrDefault("mentions_md", ""),
                            ingestedAtTs);
                }
                insert.onConflict(DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID)
                      .doUpdate()
                      .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI,    EX_HL_SOURCE_URI)
                      .set(DOCUMENT_HIGHLIGHTS.COLLECTION,    EX_HL_COLLECTION)
                      .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, EX_HL_HIGHLIGHTS)
                      .set(DOCUMENT_HIGHLIGHTS.MENTIONS_MD,   EX_HL_MENTIONS)
                      .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT,   EX_HL_INGESTED_AT)
                      .execute();
            }
            return kept.size();
        });
    }

    private int doImportHighlight(DSLContext ctx, String tenant, Map<String, Object> body) {
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) return 0;
        String ingestedAt = (String) body.get("ingested_at");
        OffsetDateTime ingestedAtTs = parseTs(ingestedAt);

        {
            ensureCollectionRegistered(ctx, tenant, (String) body.get("collection"));
            return ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                    DOCUMENT_HIGHLIGHTS.TENANT_ID,
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(
                    tenant,
                    docId,
                    (String) body.get("source_uri"),
                    (String) body.get("collection"),
                    (String) body.getOrDefault("highlights_md", ""),
                    (String) body.getOrDefault("mentions_md", ""),
                    ingestedAtTs)
                .onConflict(DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID)
                .doUpdate()
                .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI,    EX_HL_SOURCE_URI)
                .set(DOCUMENT_HIGHLIGHTS.COLLECTION,    EX_HL_COLLECTION)
                .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, EX_HL_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.MENTIONS_MD,   EX_HL_MENTIONS)
                .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT,   EX_HL_INGESTED_AT)
                .execute();
        }
    }

    // ── aspect_extraction_queue ────────────────────────────────────────────────

    /**
     * Enqueue a document for extraction (INSERT OR REPLACE semantics).
     * Re-enqueue at the same (collection, source_path) resets to pending.
     */
    public void enqueue(String tenant, Map<String, Object> body) {
        String collection = (String) body.get("collection");
        String sourcePath = (String) body.get("source_path");
        if (collection == null || collection.isBlank()) throw new IllegalArgumentException("collection required");
        if (sourcePath == null || sourcePath.isBlank()) throw new IllegalArgumentException("source_path required");
        String enqueuedAt = (String) body.getOrDefault("enqueued_at",
            OffsetDateTime.now(ZoneOffset.UTC).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME));
        OffsetDateTime enqueuedAtTs = parseTs(enqueuedAt);

        tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            ctx.insertInto(ASPECT_EXTRACTION_QUEUE,
                    ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID,
                    ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT,
                    ASPECT_EXTRACTION_QUEUE.STATUS,
                    ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                    ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,
                    ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT,
                    ASPECT_EXTRACTION_QUEUE.LAST_ERROR)
                .values(
                    tenant, collection, sourcePath,
                    nullIfBlank((String) body.get("doc_id")),
                    (String) body.getOrDefault("content_hash", ""),
                    (String) body.getOrDefault("content", ""),
                    "pending", 0, enqueuedAtTs, null, null)
                .onConflict(
                    ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH)
                .doUpdate()
                // nexus-nyout: a doc_id-less re-enqueue (blank -> NULL via
                // nullIfBlank; e.g. collection re-embed) must not erase an
                // existing correct tumbler — COALESCE keeps the old linkage;
                // a real incoming tumbler still overwrites. The queue IMPORT
                // path stays verbatim EXCLUDED.doc_id: fidelity import
                // reproduces exported rows exactly, including blanks.
                .set(ASPECT_EXTRACTION_QUEUE.DOC_ID,
                     coalesce(EX_Q_DOC_ID, ASPECT_EXTRACTION_QUEUE.DOC_ID))
                .set(ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,    EX_Q_CONTENT_HASH)
                .set(ASPECT_EXTRACTION_QUEUE.CONTENT,         EX_Q_CONTENT)
                .set(ASPECT_EXTRACTION_QUEUE.STATUS,          "pending")
                .set(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,     0)
                .set(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,     EX_Q_ENQUEUED_AT)
                .set(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT, (OffsetDateTime) null)
                .set(ASPECT_EXTRACTION_QUEUE.LAST_ERROR,      (String) null)
                // RDR-163 P1 (nexus-ztpt6): a re-enqueue is a fresh request — it
                // resets retry_count to 0, so it must also clear any stale backoff
                // (next_retry_at) left by a prior mark_retry. Otherwise the claim
                // gate would silently hold the re-enqueued row until the old
                // backoff elapses (up to base*2^(cap-1) seconds).
                .set(ASPECT_EXTRACTION_QUEUE.NEXT_RETRY_AT,   (OffsetDateTime) null)
                .execute();
            return null;
        });
    }

    /**
     * Atomically claim the oldest pending row using FOR UPDATE SKIP LOCKED.
     *
     * <p>Returns the claimed row map (including status='in_progress') or empty.
     * Concurrent callers each get a DISTINCT row — no double-claim possible.
     */
    public Optional<Map<String, Object>> claimNext(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // PG atomic claim: SELECT FOR UPDATE SKIP LOCKED picks exactly one
            // pending row, locks it, and the subsequent UPDATE is within the same
            // transaction — zero contention between concurrent workers.
            var rows = ctx.select(
                    ASPECT_EXTRACTION_QUEUE.ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT,
                    ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .from(ASPECT_EXTRACTION_QUEUE)
                // RDR-163 P0 (nexus-795gv) backoff gate: a row backed off to a
                // future next_retry_at must not be claimed early. NULL = ready now.
                // now() is the server (DB) clock — correct for both the co-located
                // local deployment and the cloud deployment where the worker host
                // clock would skew against a client-stamped comparison.
                .where(ASPECT_EXTRACTION_QUEUE.STATUS.eq("pending")
                    .and(ASPECT_EXTRACTION_QUEUE.NEXT_RETRY_AT.isNull()
                        .or(ASPECT_EXTRACTION_QUEUE.NEXT_RETRY_AT.le(
                            field("now()", OffsetDateTime.class)))))
                .orderBy(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT.asc(), ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.asc())
                .limit(1)
                .forUpdate().skipLocked()
                .fetch();
            if (rows.isEmpty()) return Optional.empty();

            long id           = rows.get(0).get(ASPECT_EXTRACTION_QUEUE.ID);
            String collection = rows.get(0).get(ASPECT_EXTRACTION_QUEUE.COLLECTION);
            String sourcePath = rows.get(0).get(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH);
            String contentHash= rows.get(0).get(ASPECT_EXTRACTION_QUEUE.CONTENT_HASH);
            contentHash = contentHash == null ? "" : contentHash;
            String content    = rows.get(0).get(ASPECT_EXTRACTION_QUEUE.CONTENT);
            content = content == null ? "" : content;
            int retryCount    = rows.get(0).get(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT);
            String docId      = rows.get(0).get(ASPECT_EXTRACTION_QUEUE.DOC_ID);
            docId = docId == null ? "" : docId;

            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            ctx.update(ASPECT_EXTRACTION_QUEUE)
               .set(ASPECT_EXTRACTION_QUEUE.STATUS, "in_progress")
               .set(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT, now)
               .where(ASPECT_EXTRACTION_QUEUE.ID.eq(id))
               .execute();

            Map<String, Object> result = new LinkedHashMap<>();
            result.put("id", id);
            result.put("collection", collection);
            result.put("source_path", sourcePath);
            result.put("content_hash", contentHash);
            result.put("content", content);
            result.put("status", "in_progress");
            result.put("retry_count", retryCount);
            result.put("doc_id", docId);
            result.put("last_attempt_at", formatTs(now));
            return Optional.of(result);
        });
    }

    /**
     * Claim up to {@code limit} pending rows using repeated atomic claimNext calls.
     */
    public List<Map<String, Object>> claimBatch(String tenant, int limit) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (int i = 0; i < limit; i++) {
            Optional<Map<String, Object>> row = claimNext(tenant);
            if (row.isEmpty()) break;
            out.add(row.get());
        }
        return out;
    }

    /**
     * Delete the queue row on success — keyed by doc_id (preferred) or (collection, source_path).
     */
    public int markDone(String tenant, String docId, String collection, String sourcePath) {
        return tenantScope.withTenant(tenant, ctx -> {
            if (docId != null && !docId.isBlank()) {
                return ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE)
                    .where(ASPECT_EXTRACTION_QUEUE.DOC_ID.eq(docId))
                    .execute();
            }
            if ((collection != null && !collection.isBlank())
                    || (sourcePath != null && !sourcePath.isBlank())) {
                return ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE)
                    .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(collection)
                        .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq(sourcePath)))
                    .execute();
            }
            return 0;
        });
    }

    /**
     * Mark a row as failed (terminal until re-enqueued).
     */
    public void markFailed(String tenant, String collection, String sourcePath, String error) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(ASPECT_EXTRACTION_QUEUE)
               .set(ASPECT_EXTRACTION_QUEUE.STATUS, "failed")
               .set(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                   field("retry_count + 1", Integer.class))
               .set(ASPECT_EXTRACTION_QUEUE.LAST_ERROR,
                   error == null ? null : error.substring(0, Math.min(error.length(), 2000)))
               .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(collection)
                   .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq(sourcePath)))
               .execute();
            return null;
        });
    }

    /**
     * Reset a row to pending for a transient retry, backing it off for
     * {@code intervalSeconds} (RDR-163 P1, nexus-ztpt6).
     *
     * <p>next_retry_at is stamped {@code now() + intervalSeconds} on the SERVER
     * (DB) clock — the worker chooses the interval, the service stamps the
     * absolute instant. This is the cloud clock-skew defense: the worker host is
     * not the DB host, so a client-computed absolute timestamp would compare
     * wrongly against the {@code now()}-based claim gate. {@code intervalSeconds
     * = 0} means "ready immediately". retry_count is incremented (monotonic; the
     * cap's source of truth); last_attempt_at is cleared.
     */
    public void markRetry(String tenant, String collection, String sourcePath, long intervalSeconds) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(ASPECT_EXTRACTION_QUEUE)
               .set(ASPECT_EXTRACTION_QUEUE.STATUS, "pending")
               .set(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                   field("retry_count + 1", Integer.class))
               .set(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT, (OffsetDateTime) null)
               .set(ASPECT_EXTRACTION_QUEUE.NEXT_RETRY_AT,
                   field("now() + make_interval(secs => ?)", OffsetDateTime.class,
                         (double) intervalSeconds))
               .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(collection)
                   .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq(sourcePath)))
               .execute();
            return null;
        });
    }

    /**
     * Reclaim stale in_progress rows (worker died).
     */
    public int reclaimStale(String tenant, int timeoutSeconds) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusSeconds(timeoutSeconds);
            return ctx.update(ASPECT_EXTRACTION_QUEUE)
                .set(ASPECT_EXTRACTION_QUEUE.STATUS, "pending")
                .set(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT, (OffsetDateTime) null)
                .where(ASPECT_EXTRACTION_QUEUE.STATUS.eq("in_progress")
                    .and(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT.lt(cutoff)))
                .execute();
        });
    }

    /** Count pending rows. */
    public int pendingCount(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectCount()
               .from(ASPECT_EXTRACTION_QUEUE)
               .where(ASPECT_EXTRACTION_QUEUE.STATUS.eq("pending"))
               .fetchOne(0, Integer.class));
    }

    /**
     * Is queue drained? (no non-failed rows).
     */
    public boolean isDrained(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectCount()
               .from(ASPECT_EXTRACTION_QUEUE)
               .where(ASPECT_EXTRACTION_QUEUE.STATUS.ne("failed"))
               .fetchOne(0, Integer.class) == 0);
    }

    /**
     * List pending rows, FIFO order.
     */
    public List<Map<String, Object>> listPending(String tenant, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var q = ctx.select(
                    ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT,
                    ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .from(ASPECT_EXTRACTION_QUEUE)
                .where(ASPECT_EXTRACTION_QUEUE.STATUS.eq("pending"))
                .orderBy(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT.asc(), ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.asc());
            var rows = (limit > 0 ? q.limit(limit) : q).fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("collection",   r.get(ASPECT_EXTRACTION_QUEUE.COLLECTION));
                m.put("source_path",  r.get(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH));
                String ch = r.get(ASPECT_EXTRACTION_QUEUE.CONTENT_HASH);
                m.put("content_hash", ch == null ? "" : ch);
                String co = r.get(ASPECT_EXTRACTION_QUEUE.CONTENT);
                m.put("content",      co == null ? "" : co);
                m.put("retry_count",  r.get(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT));
                String di = r.get(ASPECT_EXTRACTION_QUEUE.DOC_ID);
                m.put("doc_id",       di == null ? "" : di);
                out.add(m);
            }
            return out;
        });
    }

    /**
     * List terminal-{@code failed} rows, optionally scoped to one collection
     * (mirrors AspectExtractionQueue.list_failed). Drives the
     * {@code nx aspects requeue-failed} bulk-recovery CLI (nexus-2c51v).
     * doc_id is included so a re-enqueue preserves the catalog identity.
     */
    public List<Map<String, Object>> listFailed(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var cond = ASPECT_EXTRACTION_QUEUE.STATUS.eq("failed");
            if (collection != null && !collection.isBlank()) {
                cond = cond.and(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(collection));
            }
            var rows = ctx.select(
                    ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT,
                    ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .from(ASPECT_EXTRACTION_QUEUE)
                .where(cond)
                .orderBy(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT.asc(), ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.asc())
                .fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("collection",   r.get(ASPECT_EXTRACTION_QUEUE.COLLECTION));
                m.put("source_path",  r.get(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH));
                String ch = r.get(ASPECT_EXTRACTION_QUEUE.CONTENT_HASH);
                m.put("content_hash", ch == null ? "" : ch);
                String co = r.get(ASPECT_EXTRACTION_QUEUE.CONTENT);
                m.put("content",      co == null ? "" : co);
                m.put("retry_count",  r.get(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT));
                String di = r.get(ASPECT_EXTRACTION_QUEUE.DOC_ID);
                m.put("doc_id",       di == null ? "" : di);
                out.add(m);
            }
            return out;
        });
    }

    /**
     * Rename collection in queue (mirrors AspectExtractionQueue.rename_collection).
     */
    public int renameQueueCollection(String tenant, String oldColl, String newColl) {
        return tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, newColl);
            ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE)
               .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(newColl)
                   .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.in(
                       select(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH)
                           .from(ASPECT_EXTRACTION_QUEUE)
                           .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(oldColl)))))
               .execute();
            return ctx.update(ASPECT_EXTRACTION_QUEUE)
                .set(ASPECT_EXTRACTION_QUEUE.COLLECTION, newColl)
                .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(oldColl))
                .execute();
        });
    }

    /**
     * Rename collection denorm in document_highlights (mirrors DocumentHighlights rename_collection).
     *
     * <p>PK is doc_id (tumbler), so the collection column has no uniqueness constraint
     * and no collision-defense DELETE is needed — a plain UPDATE suffices.
     */
    public int renameHighlightsCollection(String tenant, String oldColl, String newColl) {
        return tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, newColl);
            return ctx.update(DOCUMENT_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.COLLECTION, newColl)
                .where(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(oldColl))
                .execute();
        });
    }

    /**
     * ETL import of a queue row — fidelity-preserving, never downgrades in_progress.
     */
    public int importQueueRow(String tenant, Map<String, Object> body) {
        return tenantScope.withTenant(tenant, ctx -> doImportQueueRow(ctx, tenant, body));
    }

    /** RDR-176 P3 (Gap 1): GUC-once bulk aspect-queue import (one withTenant per batch). */
    public int importQueueBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            // nexus-te885.3 (completes the nexus-1usso sweep): the queue import
            // was the last batch method still looping per-row .execute(). Same
            // uniform invariants as every other converted importer: skip rows
            // missing conflict-key fields; register DISTINCT collections once;
            // dedupe intra-batch (last wins — one multi-row INSERT ... ON
            // CONFLICT cannot affect the same row twice); chunk at
            // MAX_BATCH_PARAMS binds; ON CONFLICT semantics transcribed
            // verbatim from doImportQueueRow (never-downgrade STATUS CASE,
            // GREATEST retry/attempt, LEAST enqueued_at).
            List<Map<String, Object>> kept = new ArrayList<>();
            for (var body : rows) {
                if (body.get("collection") == null || body.get("source_path") == null) continue;
                kept.add(body);
            }
            if (kept.isEmpty()) return 0;

            var collections = new java.util.LinkedHashSet<String>();
            for (var body : kept) collections.add((String) body.get("collection"));
            for (String c : collections) ensureCollectionRegistered(ctx, tenant, c);

            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(kept.size());
            for (var body : kept) {
                unique.put(body.get("collection") + " " + body.get("source_path"), body);
            }
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int cols = 11;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            int written = 0;
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(ASPECT_EXTRACTION_QUEUE,
                        ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION,
                        ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                        ASPECT_EXTRACTION_QUEUE.DOC_ID,
                        ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,
                        ASPECT_EXTRACTION_QUEUE.CONTENT,
                        ASPECT_EXTRACTION_QUEUE.STATUS,
                        ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                        ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,
                        ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT,
                        ASPECT_EXTRACTION_QUEUE.LAST_ERROR);
                for (var body : batch) {
                    String enqueuedAt = (String) body.get("enqueued_at");
                    String lastAttemptAt = (String) body.get("last_attempt_at");
                    OffsetDateTime lastAttemptAtTs = lastAttemptAt != null && !lastAttemptAt.isBlank()
                        ? parseTs(lastAttemptAt) : null;
                    int retryCount = body.containsKey("retry_count")
                        ? ((Number) body.get("retry_count")).intValue() : 0;
                    insert = insert.values(
                        tenant,
                        (String) body.get("collection"),
                        (String) body.get("source_path"),
                        nullIfBlank((String) body.get("doc_id")),
                        (String) body.getOrDefault("content_hash", ""),
                        (String) body.getOrDefault("content", ""),
                        (String) body.getOrDefault("status", "pending"),
                        retryCount,
                        parseTs(enqueuedAt),
                        lastAttemptAtTs,
                        (String) body.get("last_error"));
                }
                insert.onConflict(
                        ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION,
                        ASPECT_EXTRACTION_QUEUE.SOURCE_PATH)
                    .doUpdate()
                    // Never downgrade in_progress to pending from a stale ETL import
                    .set(ASPECT_EXTRACTION_QUEUE.STATUS,          EX_Q_STATUS_CASE)
                    .set(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,     EX_Q_RETRY_GREATEST)
                    // Preserve earliest enqueue time
                    .set(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,     EX_Q_ENQUEUED_LEAST)
                    // Keep latest attempt timestamp
                    .set(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT, EX_Q_ATTEMPT_GREATEST)
                    .set(ASPECT_EXTRACTION_QUEUE.DOC_ID,          EX_Q_DOC_ID)
                    .set(ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,    EX_Q_CONTENT_HASH)
                    .set(ASPECT_EXTRACTION_QUEUE.CONTENT,         EX_Q_CONTENT)
                    .set(ASPECT_EXTRACTION_QUEUE.LAST_ERROR,      EX_Q_LAST_ERROR_CASE)
                    .execute();
                written += batch.size();
            }
            return written;
        });
    }

    private int doImportQueueRow(DSLContext ctx, String tenant, Map<String, Object> body) {
        String collection = (String) body.get("collection");
        String sourcePath = (String) body.get("source_path");
        if (collection == null || sourcePath == null) return 0;

        String enqueuedAt = (String) body.get("enqueued_at");
        String lastAttemptAt = (String) body.get("last_attempt_at");
        OffsetDateTime enqueuedAtTs = parseTs(enqueuedAt);
        OffsetDateTime lastAttemptAtTs = lastAttemptAt != null && !lastAttemptAt.isBlank()
            ? parseTs(lastAttemptAt) : null;
        String status = (String) body.getOrDefault("status", "pending");
        int retryCount = body.containsKey("retry_count")
            ? ((Number) body.get("retry_count")).intValue() : 0;

        {
            ensureCollectionRegistered(ctx, tenant, collection);
            return ctx.insertInto(ASPECT_EXTRACTION_QUEUE,
                    ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID,
                    ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,
                    ASPECT_EXTRACTION_QUEUE.CONTENT,
                    ASPECT_EXTRACTION_QUEUE.STATUS,
                    ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                    ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,
                    ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT,
                    ASPECT_EXTRACTION_QUEUE.LAST_ERROR)
                .values(
                    tenant, collection, sourcePath,
                    nullIfBlank((String) body.get("doc_id")),
                    (String) body.getOrDefault("content_hash", ""),
                    (String) body.getOrDefault("content", ""),
                    status, retryCount,
                    enqueuedAtTs,
                    lastAttemptAtTs,
                    (String) body.get("last_error"))
                .onConflict(
                    ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH)
                .doUpdate()
                // Never downgrade in_progress to pending from a stale ETL import
                .set(ASPECT_EXTRACTION_QUEUE.STATUS,          EX_Q_STATUS_CASE)
                .set(ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,     EX_Q_RETRY_GREATEST)
                // Preserve earliest enqueue time
                .set(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,     EX_Q_ENQUEUED_LEAST)
                // Keep latest attempt timestamp
                .set(ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT, EX_Q_ATTEMPT_GREATEST)
                .set(ASPECT_EXTRACTION_QUEUE.DOC_ID,          EX_Q_DOC_ID)
                .set(ASPECT_EXTRACTION_QUEUE.CONTENT_HASH,    EX_Q_CONTENT_HASH)
                .set(ASPECT_EXTRACTION_QUEUE.CONTENT,         EX_Q_CONTENT)
                .set(ASPECT_EXTRACTION_QUEUE.LAST_ERROR,      EX_Q_LAST_ERROR_CASE)
                .execute();
        }
    }

    // ── aspect_promotion_log ───────────────────────────────────────────────────

    /**
     * Record a promotion event in the audit log.
     */
    public void recordPromotion(String tenant, Map<String, Object> body) {
        String fieldName = (String) body.get("field_name");
        String sqlType   = (String) body.get("sql_type");
        String promotedAt = (String) body.get("promoted_at");
        if (fieldName == null || sqlType == null) throw new IllegalArgumentException("field_name and sql_type required");
        OffsetDateTime promotedAtTs = parseTs(promotedAt);

        int columnAdded    = body.containsKey("column_added") && Boolean.TRUE.equals(body.get("column_added")) ? 1 : 0;
        int rowsBackfilled = body.containsKey("rows_backfilled") ? ((Number) body.get("rows_backfilled")).intValue() : 0;
        int rowsPruned     = body.containsKey("rows_pruned") ? ((Number) body.get("rows_pruned")).intValue() : 0;
        int pruned         = body.containsKey("pruned") && Boolean.TRUE.equals(body.get("pruned")) ? 1 : 0;

        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(ASPECT_PROMOTION_LOG,
                    ASPECT_PROMOTION_LOG.TENANT_ID,
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.SQL_TYPE,
                    ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                    ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                    ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                    ASPECT_PROMOTION_LOG.PRUNED,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .values(tenant, fieldName, sqlType, columnAdded, rowsBackfilled, rowsPruned, pruned, promotedAtTs)
                .execute();
            return null;
        });
    }

    /**
     * List promotion history, oldest first.
     */
    public List<Map<String, Object>> listPromotions(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.SQL_TYPE,
                    ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                    ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                    ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                    ASPECT_PROMOTION_LOG.PRUNED,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .from(ASPECT_PROMOTION_LOG)
                .orderBy(ASPECT_PROMOTION_LOG.PROMOTED_AT.asc(), ASPECT_PROMOTION_LOG.ID.asc())
                .fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("field_name",       r.get(ASPECT_PROMOTION_LOG.FIELD_NAME));
                m.put("sql_type",         r.get(ASPECT_PROMOTION_LOG.SQL_TYPE));
                m.put("column_added",     r.get(ASPECT_PROMOTION_LOG.COLUMN_ADDED) != 0);
                m.put("rows_backfilled",  r.get(ASPECT_PROMOTION_LOG.ROWS_BACKFILLED));
                m.put("rows_pruned",      r.get(ASPECT_PROMOTION_LOG.ROWS_PRUNED));
                m.put("pruned",           r.get(ASPECT_PROMOTION_LOG.PRUNED) != 0);
                OffsetDateTime ts = r.get(ASPECT_PROMOTION_LOG.PROMOTED_AT);
                m.put("promoted_at", ts == null ? null : formatTs(ts));
                out.add(m);
            }
            return out;
        });
    }

    /**
     * ETL import of a promotion log row (event log — DO NOTHING on conflict).
     */
    public int importPromotionRow(String tenant, Map<String, Object> body) {
        return tenantScope.withTenant(tenant, ctx -> doImportPromotionRow(ctx, tenant, body));
    }

    /**
     * nexus-1usso: GUC-once bulk aspect-promotion import — ONE multi-row
     * {@code INSERT ... ON CONFLICT DO NOTHING} statement per chunk. No
     * dedup needed: intra-statement conflicts against {@code DO NOTHING}
     * are a documented no-op (unlike {@code DO UPDATE}, which cannot affect
     * the same row twice), and {@code .execute()} on a {@code DO NOTHING}
     * statement returns exactly the count of NEWLY inserted rows — so
     * summing it across chunks reproduces the per-row loop's "count of
     * rows actually written" contract exactly, without a synthetic dedup
     * count. Rows missing {@code field_name}/{@code promoted_at} are
     * skipped BEFORE the insert (matching the single-row path).
     */
    public int importPromotionBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> valid = new ArrayList<>();
            for (var body : rows) {
                String fieldName  = (String) body.get("field_name");
                String promotedAt = (String) body.get("promoted_at");
                if (fieldName != null && promotedAt != null) valid.add(body);
            }
            if (valid.isEmpty()) return 0;

            int written = 0;
            final int cols = 8;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < valid.size(); start += chunkSize) {
                var batch = valid.subList(start, Math.min(start + chunkSize, valid.size()));
                var insert = ctx.insertInto(ASPECT_PROMOTION_LOG,
                        ASPECT_PROMOTION_LOG.TENANT_ID,
                        ASPECT_PROMOTION_LOG.FIELD_NAME,
                        ASPECT_PROMOTION_LOG.SQL_TYPE,
                        ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                        ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                        ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                        ASPECT_PROMOTION_LOG.PRUNED,
                        ASPECT_PROMOTION_LOG.PROMOTED_AT);
                for (var body : batch) {
                    String fieldName = (String) body.get("field_name");
                    OffsetDateTime promotedAtTs = parseTs((String) body.get("promoted_at"));
                    int columnAdded    = body.containsKey("column_added") && Boolean.TRUE.equals(body.get("column_added")) ? 1 : 0;
                    int rowsBackfilled = body.containsKey("rows_backfilled") ? ((Number) body.get("rows_backfilled")).intValue() : 0;
                    int rowsPruned     = body.containsKey("rows_pruned") ? ((Number) body.get("rows_pruned")).intValue() : 0;
                    int pruned         = body.containsKey("pruned") && Boolean.TRUE.equals(body.get("pruned")) ? 1 : 0;
                    insert = insert.values(tenant, fieldName, (String) body.getOrDefault("sql_type", "TEXT"),
                            columnAdded, rowsBackfilled, rowsPruned, pruned, promotedAtTs);
                }
                written += insert.onConflict(
                        ASPECT_PROMOTION_LOG.TENANT_ID,
                        ASPECT_PROMOTION_LOG.FIELD_NAME,
                        ASPECT_PROMOTION_LOG.PROMOTED_AT)
                    .doNothing()
                    .execute();
            }
            return written;
        });
    }

    private int doImportPromotionRow(DSLContext ctx, String tenant, Map<String, Object> body) {
        String fieldName  = (String) body.get("field_name");
        String promotedAt = (String) body.get("promoted_at");
        if (fieldName == null || promotedAt == null) return 0;
        OffsetDateTime promotedAtTs = parseTs(promotedAt);
        int columnAdded    = body.containsKey("column_added") && Boolean.TRUE.equals(body.get("column_added")) ? 1 : 0;
        int rowsBackfilled = body.containsKey("rows_backfilled") ? ((Number) body.get("rows_backfilled")).intValue() : 0;
        int rowsPruned     = body.containsKey("rows_pruned") ? ((Number) body.get("rows_pruned")).intValue() : 0;
        int pruned         = body.containsKey("pruned") && Boolean.TRUE.equals(body.get("pruned")) ? 1 : 0;

        return
            ctx.insertInto(ASPECT_PROMOTION_LOG,
                    ASPECT_PROMOTION_LOG.TENANT_ID,
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.SQL_TYPE,
                    ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                    ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                    ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                    ASPECT_PROMOTION_LOG.PRUNED,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .values(
                    tenant, fieldName,
                    (String) body.getOrDefault("sql_type", "TEXT"),
                    columnAdded, rowsBackfilled, rowsPruned, pruned, promotedAtTs)
                .onConflict(
                    ASPECT_PROMOTION_LOG.TENANT_ID,
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .doNothing()
                .execute();
    }

    // ── Operator fast-path queries (RDR-152 bead nexus-l9hd8) ──────────────────

    /**
     * Filter: return source_uris from {@code document_aspects} whose
     * {@code field} column (or {@code extras.key} sub-field) matches
     * {@code predicate} (a SQL LIKE pattern such as {@code "%paxos%"}).
     *
     * <p>Mirrors Python {@code aspect_sql._query_filter} (RDR-089). Uses
     * {@code ILIKE} (case-insensitive LIKE) to match SQLite's default case-insensitive
     * LIKE behaviour for ASCII data. Per-batch IN-list pagination (300 per batch)
     * matches the SQLite fast path for exact result-set parity.
     *
     * <p>For {@code extras.key} fields ({@code field.startsWith("extras.")}):
     * Postgres {@code COALESCE(extras, '{}')::json->>'key' LIKE ?}.
     * For all other fields: {@code field LIKE ?}.
     *
     * <p>RLS enforcement: tenant isolation via {@link TenantScope#withTenant}.
     *
     * @param tenant      tenant scope
     * @param sourceUris  candidate source URIs to evaluate
     * @param field       aspect column or extras.key (e.g. {@code "proposed_method"},
     *                    {@code "experimental_datasets"}, {@code "extras.venue"})
     * @param predicate   SQL LIKE pattern (e.g. {@code "%paxos%"}, {@code "%\"TPC-C\"%"})
     * @return list of source_uris that match the predicate (subset of input)
     */
    public List<String> filterBySourceUris(
            String tenant, List<String> sourceUris, String field, String predicate) {
        if (sourceUris == null || sourceUris.isEmpty()) return List.of();
        validateField(field);
        String fieldExpr = fieldExpression(field);

        List<String> result = new ArrayList<>();
        for (int start = 0; start < sourceUris.size(); start += 300) {
            List<String> batch = sourceUris.subList(start, Math.min(start + 300, sourceUris.size()));
            // fieldExpr is validated by fieldExpression() which only emits:
            //   - a bare column name (from ALLOWED_ASPECT_COLUMNS, no injection risk)
            //   - COALESCE(extras,'{}')::json#>>'{...}' (keys validated to [A-Za-z0-9_.]+)
            // ILIKE (case-insensitive) mirrors SQLite's default LIKE behaviour for ASCII.
            Field<String> fExpr = field(fieldExpr, String.class);
            List<String> matched = tenantScope.withTenant(tenant, ctx -> {
                var rows = ctx.select(DOCUMENT_ASPECTS.SOURCE_URI)
                    .from(DOCUMENT_ASPECTS)
                    .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                        .and(condition("{0} ILIKE {1}", fExpr, inline(predicate))))
                    .fetch();
                List<String> out = new ArrayList<>();
                for (var r : rows) {
                    String v = r.get(DOCUMENT_ASPECTS.SOURCE_URI);
                    if (v != null) out.add(v);
                }
                return out;
            });
            result.addAll(matched);
        }
        return result;
    }

    /**
     * GroupBy: return a map of {@code source_uri → key_value} for each URI
     * whose aspect row exists and has a non-null value for {@code field}.
     *
     * <p>Mirrors Python {@code aspect_sql._query_groupby} (RDR-089). URIs
     * without a matching aspect row are absent from the result map; the
     * Python caller maps absent entries to {@code "unassigned"}.
     *
     * <p>For {@code extras.key}: Postgres {@code COALESCE(extras,'{}')::json->>'key'}.
     *
     * @param tenant      tenant scope
     * @param sourceUris  candidate source URIs
     * @param field       aspect column or extras.key
     * @return map of uri → value string (null values omitted from map)
     */
    public Map<String, String> groupByField(String tenant, List<String> sourceUris, String field) {
        if (sourceUris == null || sourceUris.isEmpty()) return Map.of();
        validateField(field);
        String fieldExpr = fieldExpression(field);

        Map<String, String> result = new java.util.LinkedHashMap<>();
        for (int start = 0; start < sourceUris.size(); start += 300) {
            List<String> batch = sourceUris.subList(start, Math.min(start + 300, sourceUris.size()));
            // fieldExpr validated by fieldExpression() — safe to embed in DSL.field()
            Field<String> fExpr = field(fieldExpr, String.class);

            tenantScope.withTenant(tenant, ctx -> {
                var rows = ctx.select(DOCUMENT_ASPECTS.SOURCE_URI, fExpr)
                    .from(DOCUMENT_ASPECTS)
                    .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch))
                    .fetch();
                for (var r : rows) {
                    String uri = r.get(DOCUMENT_ASPECTS.SOURCE_URI);
                    Object val = r.get(fExpr);
                    if (uri != null && val != null) {
                        result.put(uri, val.toString());
                    }
                }
                return null;
            });
        }
        return result;
    }

    /**
     * ConfidenceAggregate: compute AVG / MIN / MAX confidence across the
     * provided source URIs.
     *
     * <p>Mirrors Python {@code aspect_sql._query_confidence_aggregate} (RDR-089).
     * Uses a single SQL aggregate per batch (more efficient than fetching all
     * confidence values and folding in Java/Python).
     *
     * <p>Supported {@code reducerKind} values: {@code "avg_confidence"},
     * {@code "min_confidence"}, {@code "max_confidence"}. Any other value
     * returns {@code null}.
     *
     * @param tenant      tenant scope
     * @param sourceUris  candidate source URIs
     * @param reducerKind one of avg_confidence / min_confidence / max_confidence
     * @return the aggregate value, or null when no aspect rows exist or
     *         reducerKind is unrecognised
     */
    public Double confidenceAggregate(String tenant, List<String> sourceUris, String reducerKind) {
        if (sourceUris == null || sourceUris.isEmpty()) return null;

        String aggFunc = switch (reducerKind) {
            case "avg_confidence" -> "AVG";
            case "min_confidence" -> "MIN";
            case "max_confidence" -> "MAX";
            default -> null;
        };
        if (aggFunc == null) return null;

        // Accumulate across batches: AVG needs sum+count; MIN/MAX fold naturally.
        double sumAcc = 0.0;
        long   cntAcc = 0L;
        Double minAcc = null;
        Double maxAcc = null;

        for (int start = 0; start < sourceUris.size(); start += 300) {
            List<String> batch = sourceUris.subList(start, Math.min(start + 300, sourceUris.size()));

            final double[] localSum = {0.0};
            final long[]   localCnt = {0L};
            final Double[] localVal = {null};

            tenantScope.withTenant(tenant, ctx -> {
                if ("AVG".equals(aggFunc)) {
                    // For AVG we need SUM+COUNT to fold across batches correctly.
                    var rows = ctx.select(
                            sum(DOCUMENT_ASPECTS.CONFIDENCE),
                            count(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object sumVal = rows.get(0).get(0);
                        Object cntVal = rows.get(0).get(1);
                        if (sumVal != null) localSum[0] = ((Number) sumVal).doubleValue();
                        if (cntVal != null) localCnt[0] = ((Number) cntVal).longValue();
                    }
                } else if ("MIN".equals(aggFunc)) {
                    var rows = ctx.select(min(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object val = rows.get(0).get(0);
                        if (val != null) localVal[0] = ((Number) val).doubleValue();
                    }
                } else {
                    var rows = ctx.select(max(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object val = rows.get(0).get(0);
                        if (val != null) localVal[0] = ((Number) val).doubleValue();
                    }
                }
                return null;
            });

            if ("AVG".equals(aggFunc)) {
                sumAcc += localSum[0];
                cntAcc += localCnt[0];
            } else if (localVal[0] != null) {
                if ("MIN".equals(aggFunc)) {
                    minAcc = (minAcc == null) ? localVal[0] : Math.min(minAcc, localVal[0]);
                } else {
                    maxAcc = (maxAcc == null) ? localVal[0] : Math.max(maxAcc, localVal[0]);
                }
            }
        }

        return switch (reducerKind) {
            case "avg_confidence" -> cntAcc == 0 ? null : sumAcc / cntAcc;
            case "min_confidence" -> minAcc;
            case "max_confidence" -> maxAcc;
            default -> null;
        };
    }

    /**
     * Bare column names allowed as operator query fields.
     *
     * <p>Mirrors Python {@code _ASPECT_COLUMN_TYPES} keys. The {@code "extras"} key itself
     * is excluded because it is only valid as the {@code extras.<key>} form; direct use of
     * the JSON object column is not meaningful as a filter/groupby field.
     *
     * <p>Server-side allowlist: even though Python callers validate field names before
     * posting, the service endpoint is externally reachable (any curl). Without a server-
     * side guard, a POST with {@code "field": "x; DROP TABLE nexus.document_aspects; --"}
     * would be injected directly into the SQL string via {@link #fieldExpression}.
     */
    public static final Set<String> ALLOWED_ASPECT_COLUMNS = Set.of(
        "problem_formulation",
        "proposed_method",
        "experimental_datasets",
        "experimental_baselines",
        "experimental_results",
        "confidence"
    );

    /**
     * Validate that {@code field} is either a known scalar column or an {@code extras.<key>}
     * reference. Throws {@link IllegalArgumentException} (→ HTTP 400) for anything else.
     *
     * <p>This is the server-side allowlist guard (C1 injection fix). Python callers
     * pre-validate via {@code _ASPECT_COLUMN_TYPES}, but the service is a public HTTP
     * endpoint — this guard prevents direct-POST injection.
     */
    public static void validateField(String field) {
        if (field == null || field.isBlank()) {
            throw new IllegalArgumentException("field must not be blank");
        }
        if (field.startsWith("extras.")) {
            String key = field.substring("extras.".length());
            if (key.isBlank()) {
                throw new IllegalArgumentException("extras. field requires a non-empty key");
            }
            // Key may contain dots (nested path) — dots and alphanumerics are allowed.
            // Reject anything that would escape the JSON path (single-quote, semicolon, etc.)
            if (!key.matches("[A-Za-z0-9_.]+")) {
                throw new IllegalArgumentException(
                    "extras key must match [A-Za-z0-9_.]+; got: " + key);
            }
            return;
        }
        if (!ALLOWED_ASPECT_COLUMNS.contains(field)) {
            throw new IllegalArgumentException(
                "field " + field + " is not a known aspect column; "
                + "allowed: " + ALLOWED_ASPECT_COLUMNS + " or extras.<key>");
        }
    }

    /**
     * Build the SQL field expression for a column name or extras.key.
     *
     * <p>For {@code extras.key}: uses Postgres {@code #>>} (path operator) for
     * true JSONPath-equivalent traversal, so {@code extras.a.b} correctly resolves
     * {@code extras → a → b} (mirrors SQLite's {@code json_extract(extras, '$.a.b')}).
     * Single-level keys (e.g. {@code extras.venue}) still work correctly via the
     * path form {@code extras #>> '{venue}'}.
     *
     * <p>COALESCE: when {@code extras} IS NULL the cast to {@code json} would fail;
     * {@code COALESCE(extras,'{}')::json} substitutes an empty object, so the path
     * extraction returns null instead of throwing.
     *
     * <p>For bare column names: returned verbatim after allowlist validation in
     * {@link #validateField}. The allowlist is the injection guard; this method
     * is only called after validateField passes.
     */
    private static String fieldExpression(String field) {
        if (field.startsWith("extras.")) {
            // Split on '.' to build the Postgres array literal {a,b,...}
            // e.g. "extras.venue"   → "{venue}"
            //      "extras.a.b"     → "{a,b}"
            String keyPart = field.substring("extras.".length());
            String[] segments = keyPart.split("\\.");
            StringBuilder sb = new StringBuilder("COALESCE(extras,'{}')::json#>>'{");
            for (int i = 0; i < segments.length; i++) {
                if (i > 0) sb.append(',');
                // Segments were validated by validateField to match [A-Za-z0-9_.]+ so
                // no quoting is needed inside the Postgres array literal.
                sb.append(segments[i]);
            }
            sb.append("}'");
            return sb.toString();
        }
        // Bare column name — allowlist validated by validateField before this call.
        return field;
    }

    // ── Private helpers ────────────────────────────────────────────────────────

    private static Map<String, Object> recordToMap(org.jooq.Record r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("collection",              r.get(0));
        m.put("source_path",             r.get(1));
        m.put("problem_formulation",     r.get(2));
        m.put("proposed_method",         r.get(3));
        m.put("experimental_datasets",   r.get(4));
        m.put("experimental_baselines",  r.get(5));
        m.put("experimental_results",    r.get(6));
        m.put("extras",                  r.get(7));
        m.put("confidence",              r.get(8));
        Object eatRaw = r.get(9);
        m.put("extracted_at", eatRaw instanceof OffsetDateTime
            ? formatTs((OffsetDateTime) eatRaw) : (eatRaw == null ? null : eatRaw.toString()));
        m.put("model_version",           r.get(10));
        m.put("extractor_name",          r.get(11));
        m.put("source_uri",              r.get(12));
        m.put("salient_sentences",       r.get(13));
        m.put("doc_id",                  r.get(14) == null ? "" : r.get(14).toString());
        return m;
    }

    /**
     * Converts a doc_id string to null when blank or empty.
     * Required after nexus-b7v6i: doc_id columns in document_aspects and
     * aspect_extraction_queue are now nullable with a real FK to catalog_documents.
     * An empty string '' would fail the FK (no catalog doc with tumbler='').
     */
    private static String nullIfBlank(String s) {
        return (s == null || s.isBlank()) ? null : s;
    }

    private static Map<String, Object> highlightToMap(org.jooq.Record r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("doc_id",       r.get(0));
        m.put("source_uri",   r.get(1) == null ? "" : r.get(1).toString());
        m.put("collection",   r.get(2) == null ? "" : r.get(2).toString());
        m.put("highlights_md",r.get(3) == null ? "" : r.get(3).toString());
        m.put("mentions_md",  r.get(4) == null ? "" : r.get(4).toString());
        Object iRaw = r.get(5);
        m.put("ingested_at", iRaw instanceof OffsetDateTime
            ? formatTs((OffsetDateTime) iRaw) : (iRaw == null ? "" : iRaw.toString()));
        return m;
    }
}
