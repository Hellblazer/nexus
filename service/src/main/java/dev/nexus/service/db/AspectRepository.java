package dev.nexus.service.db;

import org.jooq.DSLContext;
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
 *
 * <p>Uses raw SQL via DSLContext (same pattern as TaxonomyRepository) to avoid
 * dependency on jOOQ-generated classes for new tables not yet in codegen.
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
            var result = ctx.fetch(
                "INSERT INTO nexus.document_aspects "
                + "(tenant_id, collection, source_path, problem_formulation, "
                + " proposed_method, experimental_datasets, experimental_baselines, "
                + " experimental_results, extras, confidence, extracted_at, "
                + " model_version, extractor_name, source_uri, salient_sentences, doc_id) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::timestamptz, ?, ?, ?, ?, ?) "
                + "ON CONFLICT (tenant_id, collection, source_path) DO UPDATE SET "
                + "  problem_formulation    = EXCLUDED.problem_formulation, "
                + "  proposed_method        = EXCLUDED.proposed_method, "
                + "  experimental_datasets  = EXCLUDED.experimental_datasets, "
                + "  experimental_baselines = EXCLUDED.experimental_baselines, "
                + "  experimental_results   = EXCLUDED.experimental_results, "
                + "  extras                 = EXCLUDED.extras, "
                + "  confidence             = EXCLUDED.confidence, "
                + "  extracted_at           = EXCLUDED.extracted_at, "
                + "  model_version          = EXCLUDED.model_version, "
                + "  extractor_name         = EXCLUDED.extractor_name, "
                + "  source_uri             = EXCLUDED.source_uri, "
                + "  salient_sentences      = EXCLUDED.salient_sentences, "
                + "  doc_id                 = EXCLUDED.doc_id "
                + "RETURNING id",
                tenant,
                collection,
                sourcePath,
                body.get("problem_formulation"),
                body.get("proposed_method"),
                body.get("experimental_datasets"),
                body.get("experimental_baselines"),
                body.get("experimental_results"),
                body.get("extras"),
                confidence,
                formatTs(extractedAtTs),
                modelVersion,
                extractorName,
                body.get("source_uri"),
                body.get("salient_sentences"),
                nullIfBlank((String) body.get("doc_id"))
            );
            return result.isEmpty() ? -1L : result.get(0).get(0, Long.class);
        });
    }

    /**
     * Get an aspect record by (collection, source_path).
     */
    public Optional<Map<String, Object>> getAspect(String tenant, String collection, String sourcePath) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT collection, source_path, problem_formulation, proposed_method, "
                + "       experimental_datasets, experimental_baselines, experimental_results, "
                + "       extras, confidence, extracted_at, model_version, extractor_name, "
                + "       source_uri, salient_sentences, doc_id "
                + "FROM nexus.document_aspects "
                + "WHERE collection = ? AND source_path = ?",
                collection, sourcePath);
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
            var rows = ctx.fetch(
                "SELECT collection, source_path, problem_formulation, proposed_method, "
                + "       experimental_datasets, experimental_baselines, experimental_results, "
                + "       extras, confidence, extracted_at, model_version, extractor_name, "
                + "       source_uri, salient_sentences, doc_id "
                + "FROM nexus.document_aspects "
                + "WHERE doc_id = ?",
                docId);
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(recordToMap(rows.get(0)));
        });
    }

    /**
     * List aspect records for a collection, paginated.
     */
    public List<Map<String, Object>> listByCollection(String tenant, String collection, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT collection, source_path, problem_formulation, proposed_method, "
                + "       experimental_datasets, experimental_baselines, experimental_results, "
                + "       extras, confidence, extracted_at, model_version, extractor_name, "
                + "       source_uri, salient_sentences, doc_id "
                + "FROM nexus.document_aspects "
                + "WHERE collection = ? "
                + "ORDER BY source_path ASC "
                + (limit > 0 ? "LIMIT " + limit + " OFFSET " + offset : ""),
                collection);
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
            var rows = ctx.fetch(
                "SELECT collection, source_path, problem_formulation, proposed_method, "
                + "       experimental_datasets, experimental_baselines, experimental_results, "
                + "       extras, confidence, extracted_at, model_version, extractor_name, "
                + "       source_uri, salient_sentences, doc_id "
                + "FROM nexus.document_aspects "
                + "WHERE extractor_name = ? AND model_version < ? "
                + "ORDER BY collection, source_path",
                extractorName, maxVersion);
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
            ctx.execute(
                "DELETE FROM nexus.document_aspects WHERE collection = ? AND source_path = ?",
                collection, sourcePath));
    }

    /**
     * Rename collection denorm cache (mirrors DocumentAspects.rename_collection).
     */
    public int renameAspectCollection(String tenant, String oldColl, String newColl) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Collision defense: delete conflicting new-side rows first
            ctx.execute(
                "DELETE FROM nexus.document_aspects "
                + "WHERE collection = ? "
                + "  AND source_path IN (SELECT source_path FROM nexus.document_aspects WHERE collection = ?)",
                newColl, oldColl);
            return ctx.execute(
                "UPDATE nexus.document_aspects SET collection = ? WHERE collection = ?",
                newColl, oldColl);
        });
    }

    /**
     * Set salient_sentences for a doc_id. Returns rows updated.
     */
    public int setSalientSentences(String tenant, String docId, String sentencesJson) {
        if (docId == null || docId.isBlank()) return 0;
        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute(
                "UPDATE nexus.document_aspects SET salient_sentences = ? WHERE doc_id = ?",
                sentencesJson, docId));
    }

    /**
     * Set salient_sentences by (collection, source_path) — pre-migration fallback.
     */
    public int setSalientSentencesByKey(String tenant, String collection, String sourcePath, String sentencesJson) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute(
                "UPDATE nexus.document_aspects SET salient_sentences = ? "
                + "WHERE collection = ? AND source_path = ?",
                sentencesJson, collection, sourcePath));
    }

    /**
     * Get salient_sentences for a doc_id. Returns null when not found.
     */
    public String getSalientSentences(String tenant, String docId) {
        if (docId == null || docId.isBlank()) return null;
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT salient_sentences FROM nexus.document_aspects WHERE doc_id = ?",
                docId);
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
        double confidence = body.containsKey("confidence") && body.get("confidence") != null
            ? ((Number) body.get("confidence")).doubleValue()
            : -1.0;
        if (confidence < MIN_CONFIDENCE) return 0;

        String extractedAt = (String) body.get("extracted_at");
        OffsetDateTime extractedAtTs = parseTs(extractedAt);

        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute(
                "INSERT INTO nexus.document_aspects "
                + "(tenant_id, collection, source_path, problem_formulation, "
                + " proposed_method, experimental_datasets, experimental_baselines, "
                + " experimental_results, extras, confidence, extracted_at, "
                + " model_version, extractor_name, source_uri, salient_sentences, doc_id) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::timestamptz, ?, ?, ?, ?, ?) "
                + "ON CONFLICT (tenant_id, collection, source_path) DO UPDATE SET "
                + "  problem_formulation    = EXCLUDED.problem_formulation, "
                + "  proposed_method        = EXCLUDED.proposed_method, "
                + "  experimental_datasets  = EXCLUDED.experimental_datasets, "
                + "  experimental_baselines = EXCLUDED.experimental_baselines, "
                + "  experimental_results   = EXCLUDED.experimental_results, "
                + "  extras                 = EXCLUDED.extras, "
                + "  confidence             = EXCLUDED.confidence, "
                + "  extracted_at           = EXCLUDED.extracted_at, "
                + "  model_version          = EXCLUDED.model_version, "
                + "  extractor_name         = EXCLUDED.extractor_name, "
                + "  source_uri             = COALESCE(EXCLUDED.source_uri, document_aspects.source_uri), "
                + "  salient_sentences      = COALESCE(EXCLUDED.salient_sentences, document_aspects.salient_sentences), "
                + "  doc_id                 = COALESCE(EXCLUDED.doc_id, document_aspects.doc_id)",
                tenant,
                body.get("collection"),
                body.get("source_path"),
                body.get("problem_formulation"),
                body.get("proposed_method"),
                body.get("experimental_datasets"),
                body.get("experimental_baselines"),
                body.get("experimental_results"),
                body.get("extras"),
                confidence,
                formatTs(extractedAtTs),
                body.get("model_version"),
                body.get("extractor_name"),
                body.get("source_uri"),
                body.get("salient_sentences"),
                nullIfBlank((String) body.get("doc_id"))
            ));
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
            ctx.execute(
                "INSERT INTO nexus.document_highlights "
                + "(tenant_id, doc_id, source_uri, collection, highlights_md, mentions_md, ingested_at) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?::timestamptz) "
                + "ON CONFLICT (tenant_id, doc_id) DO UPDATE SET "
                + "  source_uri    = EXCLUDED.source_uri, "
                + "  collection    = EXCLUDED.collection, "
                + "  highlights_md = EXCLUDED.highlights_md, "
                + "  mentions_md   = EXCLUDED.mentions_md, "
                + "  ingested_at   = EXCLUDED.ingested_at",
                tenant, docId,
                body.get("source_uri"),
                body.get("collection"),
                highlightsMd, mentionsMd, formatTs(ingestedAtTs));
            return null;
        });
        return true;
    }

    /**
     * Get a highlight record by doc_id.
     */
    public Optional<Map<String, Object>> getHighlight(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT doc_id, source_uri, collection, highlights_md, mentions_md, ingested_at "
                + "FROM nexus.document_highlights WHERE doc_id = ?",
                docId);
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(highlightToMap(rows.get(0)));
        });
    }

    /**
     * Get a highlight record by source_uri (DEVONthink UUID URI).
     */
    public Optional<Map<String, Object>> getHighlightBySourceUri(String tenant, String sourceUri) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT doc_id, source_uri, collection, highlights_md, mentions_md, ingested_at "
                + "FROM nexus.document_highlights WHERE source_uri = ? LIMIT 1",
                sourceUri);
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(highlightToMap(rows.get(0)));
        });
    }

    /**
     * List highlight records, most recent first.
     */
    public List<Map<String, Object>> listHighlights(String tenant, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT doc_id, source_uri, collection, highlights_md, mentions_md, ingested_at "
                + "FROM nexus.document_highlights "
                + "ORDER BY ingested_at DESC LIMIT ? OFFSET ?",
                limit, offset);
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
            ctx.execute("DELETE FROM nexus.document_highlights WHERE doc_id = ?", docId) > 0);
    }

    /**
     * ETL import — fidelity-preserving highlight upsert.
     */
    public int importHighlight(String tenant, Map<String, Object> body) {
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) return 0;
        String ingestedAt = (String) body.get("ingested_at");
        OffsetDateTime ingestedAtTs = parseTs(ingestedAt);

        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute(
                "INSERT INTO nexus.document_highlights "
                + "(tenant_id, doc_id, source_uri, collection, highlights_md, mentions_md, ingested_at) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?::timestamptz) "
                + "ON CONFLICT (tenant_id, doc_id) DO UPDATE SET "
                + "  source_uri    = EXCLUDED.source_uri, "
                + "  collection    = EXCLUDED.collection, "
                + "  highlights_md = EXCLUDED.highlights_md, "
                + "  mentions_md   = EXCLUDED.mentions_md, "
                + "  ingested_at   = EXCLUDED.ingested_at",
                tenant, docId,
                body.get("source_uri"),
                body.get("collection"),
                body.getOrDefault("highlights_md", ""),
                body.getOrDefault("mentions_md", ""),
                formatTs(ingestedAtTs)));
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
            ctx.execute(
                "INSERT INTO nexus.aspect_extraction_queue "
                + "(tenant_id, collection, source_path, doc_id, content_hash, content, "
                + " status, retry_count, enqueued_at, last_attempt_at, last_error) "
                + "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?::timestamptz, NULL, NULL) "
                + "ON CONFLICT (tenant_id, collection, source_path) DO UPDATE SET "
                + "  doc_id          = EXCLUDED.doc_id, "
                + "  content_hash    = EXCLUDED.content_hash, "
                + "  content         = EXCLUDED.content, "
                + "  status          = 'pending', "
                + "  retry_count     = 0, "
                + "  enqueued_at     = EXCLUDED.enqueued_at, "
                + "  last_attempt_at = NULL, "
                + "  last_error      = NULL",
                tenant, collection, sourcePath,
                nullIfBlank((String) body.get("doc_id")),
                body.getOrDefault("content_hash", ""),
                body.getOrDefault("content", ""),
                formatTs(enqueuedAtTs));
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
            var rows = ctx.fetch(
                "SELECT id, collection, source_path, content_hash, content, retry_count, doc_id "
                + "FROM nexus.aspect_extraction_queue "
                + "WHERE status = 'pending' "
                + "ORDER BY enqueued_at ASC, source_path ASC "
                + "LIMIT 1 FOR UPDATE SKIP LOCKED");
            if (rows.isEmpty()) return Optional.empty();

            long id           = rows.get(0).get(0, Long.class);
            String collection = (String) rows.get(0).get(1);
            String sourcePath = (String) rows.get(0).get(2);
            String contentHash= rows.get(0).get(3) == null ? "" : rows.get(0).get(3).toString();
            String content    = rows.get(0).get(4) == null ? "" : rows.get(0).get(4).toString();
            int retryCount    = rows.get(0).get(5, Integer.class);
            String docId      = rows.get(0).get(6) == null ? "" : rows.get(0).get(6).toString();

            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            ctx.execute(
                "UPDATE nexus.aspect_extraction_queue "
                + "SET status = 'in_progress', last_attempt_at = ?::timestamptz "
                + "WHERE id = ?",
                formatTs(now), id);

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
                return ctx.execute(
                    "DELETE FROM nexus.aspect_extraction_queue WHERE doc_id = ?", docId);
            }
            if ((collection != null && !collection.isBlank())
                    || (sourcePath != null && !sourcePath.isBlank())) {
                return ctx.execute(
                    "DELETE FROM nexus.aspect_extraction_queue "
                    + "WHERE collection = ? AND source_path = ?",
                    collection, sourcePath);
            }
            return 0;
        });
    }

    /**
     * Mark a row as failed (terminal until re-enqueued).
     */
    public void markFailed(String tenant, String collection, String sourcePath, String error) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute(
                "UPDATE nexus.aspect_extraction_queue "
                + "SET status = 'failed', retry_count = retry_count + 1, last_error = ? "
                + "WHERE collection = ? AND source_path = ?",
                error == null ? null : error.substring(0, Math.min(error.length(), 2000)),
                collection, sourcePath);
            return null;
        });
    }

    /**
     * Reset a row to pending (transient retry).
     */
    public void markRetry(String tenant, String collection, String sourcePath) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute(
                "UPDATE nexus.aspect_extraction_queue "
                + "SET status = 'pending', retry_count = retry_count + 1, last_attempt_at = NULL "
                + "WHERE collection = ? AND source_path = ?",
                collection, sourcePath);
            return null;
        });
    }

    /**
     * Reclaim stale in_progress rows (worker died).
     */
    public int reclaimStale(String tenant, int timeoutSeconds) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusSeconds(timeoutSeconds);
            return ctx.execute(
                "UPDATE nexus.aspect_extraction_queue "
                + "SET status = 'pending', last_attempt_at = NULL "
                + "WHERE status = 'in_progress' AND last_attempt_at < ?::timestamptz",
                formatTs(cutoff));
        });
    }

    /** Count pending rows. */
    public int pendingCount(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE status = 'pending'");
            return rows.get(0).get(0, Integer.class);
        });
    }

    /**
     * Is queue drained? (no non-failed rows).
     */
    public boolean isDrained(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE status != 'failed'");
            return rows.get(0).get(0, Integer.class) == 0;
        });
    }

    /**
     * List pending rows, FIFO order.
     */
    public List<Map<String, Object>> listPending(String tenant, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            String limitClause = limit > 0 ? "LIMIT " + limit : "";
            var rows = ctx.fetch(
                "SELECT collection, source_path, content_hash, content, retry_count, doc_id "
                + "FROM nexus.aspect_extraction_queue "
                + "WHERE status = 'pending' "
                + "ORDER BY enqueued_at ASC, source_path ASC " + limitClause);
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("collection",   r.get(0));
                m.put("source_path",  r.get(1));
                m.put("content_hash", r.get(2) == null ? "" : r.get(2).toString());
                m.put("content",      r.get(3) == null ? "" : r.get(3).toString());
                m.put("retry_count",  r.get(4, Integer.class));
                m.put("doc_id",       r.get(5) == null ? "" : r.get(5).toString());
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
            ctx.execute(
                "DELETE FROM nexus.aspect_extraction_queue "
                + "WHERE collection = ? "
                + "  AND source_path IN (SELECT source_path FROM nexus.aspect_extraction_queue WHERE collection = ?)",
                newColl, oldColl);
            return ctx.execute(
                "UPDATE nexus.aspect_extraction_queue SET collection = ? WHERE collection = ?",
                newColl, oldColl);
        });
    }

    /**
     * Rename collection denorm in document_highlights (mirrors DocumentHighlights rename_collection).
     *
     * <p>PK is doc_id (tumbler), so the collection column has no uniqueness constraint
     * and no collision-defense DELETE is needed — a plain UPDATE suffices.
     */
    public int renameHighlightsCollection(String tenant, String oldColl, String newColl) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute(
                "UPDATE nexus.document_highlights SET collection = ? WHERE collection = ?",
                newColl, oldColl));
    }

    /**
     * ETL import of a queue row — fidelity-preserving, never downgrades in_progress.
     */
    public int importQueueRow(String tenant, Map<String, Object> body) {
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

        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute(
                "INSERT INTO nexus.aspect_extraction_queue "
                + "(tenant_id, collection, source_path, doc_id, content_hash, content, "
                + " status, retry_count, enqueued_at, last_attempt_at, last_error) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?::timestamptz, ?::timestamptz, ?) "
                + "ON CONFLICT (tenant_id, collection, source_path) DO UPDATE SET "
                // Never downgrade in_progress to pending from a stale ETL import
                + "  status          = CASE WHEN nexus.aspect_extraction_queue.status = 'in_progress' "
                + "                         THEN nexus.aspect_extraction_queue.status "
                + "                         ELSE EXCLUDED.status END, "
                + "  retry_count     = GREATEST(EXCLUDED.retry_count, nexus.aspect_extraction_queue.retry_count), "
                // Preserve earliest enqueue time
                + "  enqueued_at     = LEAST(EXCLUDED.enqueued_at, nexus.aspect_extraction_queue.enqueued_at), "
                // Keep latest attempt timestamp
                + "  last_attempt_at = GREATEST(EXCLUDED.last_attempt_at, nexus.aspect_extraction_queue.last_attempt_at), "
                + "  doc_id          = EXCLUDED.doc_id, "
                + "  content_hash    = EXCLUDED.content_hash, "
                + "  content         = EXCLUDED.content, "
                + "  last_error      = CASE WHEN EXCLUDED.status = 'failed' THEN EXCLUDED.last_error "
                + "                         ELSE nexus.aspect_extraction_queue.last_error END",
                tenant, collection, sourcePath,
                nullIfBlank((String) body.get("doc_id")),
                body.getOrDefault("content_hash", ""),
                body.getOrDefault("content", ""),
                status, retryCount,
                formatTs(enqueuedAtTs),
                lastAttemptAtTs != null ? formatTs(lastAttemptAtTs) : null,
                body.get("last_error")));
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
            ctx.execute(
                "INSERT INTO nexus.aspect_promotion_log "
                + "(tenant_id, field_name, sql_type, column_added, rows_backfilled, rows_pruned, pruned, promoted_at) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, ?::timestamptz)",
                tenant, fieldName, sqlType, columnAdded, rowsBackfilled, rowsPruned, pruned, formatTs(promotedAtTs));
            return null;
        });
    }

    /**
     * List promotion history, oldest first.
     */
    public List<Map<String, Object>> listPromotions(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.fetch(
                "SELECT field_name, sql_type, column_added, rows_backfilled, rows_pruned, pruned, promoted_at "
                + "FROM nexus.aspect_promotion_log "
                + "ORDER BY promoted_at ASC, id ASC");
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("field_name",       r.get(0));
                m.put("sql_type",         r.get(1));
                m.put("column_added",     r.get(2, Integer.class) != 0);
                m.put("rows_backfilled",  r.get(3, Integer.class));
                m.put("rows_pruned",      r.get(4, Integer.class));
                m.put("pruned",           r.get(5, Integer.class) != 0);
                OffsetDateTime ts = r.get(6, OffsetDateTime.class);
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
        String fieldName  = (String) body.get("field_name");
        String promotedAt = (String) body.get("promoted_at");
        if (fieldName == null || promotedAt == null) return 0;
        OffsetDateTime promotedAtTs = parseTs(promotedAt);
        int columnAdded    = body.containsKey("column_added") && Boolean.TRUE.equals(body.get("column_added")) ? 1 : 0;
        int rowsBackfilled = body.containsKey("rows_backfilled") ? ((Number) body.get("rows_backfilled")).intValue() : 0;
        int rowsPruned     = body.containsKey("rows_pruned") ? ((Number) body.get("rows_pruned")).intValue() : 0;
        int pruned         = body.containsKey("pruned") && Boolean.TRUE.equals(body.get("pruned")) ? 1 : 0;

        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute(
                "INSERT INTO nexus.aspect_promotion_log "
                + "(tenant_id, field_name, sql_type, column_added, rows_backfilled, rows_pruned, pruned, promoted_at) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, ?::timestamptz) "
                + "ON CONFLICT (tenant_id, field_name, promoted_at) DO NOTHING",
                tenant, fieldName,
                body.getOrDefault("sql_type", "TEXT"),
                columnAdded, rowsBackfilled, rowsPruned, pruned, formatTs(promotedAtTs)));
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
