// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.20 — Java port of {@code nexus.db.chroma_quotas.QuotaValidator}.
 *
 * <p>Single source of truth for ChromaDB Cloud quota limits.  All limits match
 * {@code QUOTAS = ChromaQuotas()} (free-tier documented limits, 2026-02-28).
 *
 * <p>Pure validator: no I/O, no Chroma imports.  Throw {@link QuotaViolation} on
 * the first violation found.  Call before any Chroma upsert/query.
 */
public final class ChromaQuotaValidator {

    // ── Quota constants (mirror chroma_quotas.py ChromaQuotas) ───────────────

    public static final int MAX_EMBEDDING_DIMENSIONS    = 4_096;
    public static final int MAX_DOCUMENT_BYTES          = 16_384;
    public static final int SAFE_CHUNK_BYTES            = 12_288;
    public static final int MAX_URI_BYTES               = 256;
    public static final int MAX_ID_BYTES                = 128;
    public static final int MAX_DB_NAME_BYTES           = 128;
    public static final int MAX_COLLECTION_NAME_BYTES   = 128;
    public static final int MAX_METADATA_KEY_BYTES      = 36;
    public static final int MAX_RECORD_METADATA_VALUE_BYTES = 4_096;
    public static final int MAX_COLLECTION_METADATA_VALUE_BYTES = 256;
    public static final int MAX_RECORD_METADATA_KEYS    = 32;
    public static final int MAX_COLLECTION_METADATA_KEYS = 32;

    public static final int MAX_QUERY_STRING_CHARS      = 256;
    public static final int MAX_WHERE_PREDICATES        = 8;
    public static final int MAX_QUERY_RESULTS           = 300;

    public static final int MAX_CONCURRENT_READS        = 10;
    public static final int MAX_CONCURRENT_WRITES       = 10;

    public static final int MAX_RECORDS_PER_WRITE       = 300;
    public static final int MAX_RECORDS_PER_COLLECTION  = 5_000_000;
    public static final int MAX_COLLECTIONS_PER_ACCOUNT = 1_000_000;

    // ── Error ─────────────────────────────────────────────────────────────────

    public static final class QuotaViolation extends RuntimeException {
        public final String field;
        public final int    actual;
        public final int    limit;

        public QuotaViolation(String field, int actual, int limit, String hint) {
            super("ChromaDB quota exceeded: " + field + " = " + actual
                    + " exceeds limit " + limit
                    + (hint != null && !hint.isEmpty() ? ". " + hint : ""));
            this.field  = field;
            this.actual = actual;
            this.limit  = limit;
        }

        public QuotaViolation(String field, int actual, int limit) {
            this(field, actual, limit, null);
        }
    }

    // ── Validate record ───────────────────────────────────────────────────────

    /**
     * Validate a single record before upsert.  Mirrors
     * {@code QuotaValidator.validate_record()} in Python.
     *
     * @param id        chunk natural ID
     * @param document  chunk text (may be null for vector-only entries)
     * @param embedding pre-computed embedding (may be null — no dim check then)
     * @param metadata  chunk metadata map
     */
    public void validateRecord(String id, String document,
                               float[] embedding, Map<String, Object> metadata) {
        // ID length
        int idBytes = id.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
        if (idBytes > MAX_ID_BYTES) {
            throw new QuotaViolation("id", idBytes, MAX_ID_BYTES,
                    "Shorten the record ID (currently " + idBytes + " bytes, max " + MAX_ID_BYTES + ")");
        }

        // Document size
        if (document != null) {
            int docBytes = document.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
            if (docBytes > MAX_DOCUMENT_BYTES) {
                throw new QuotaViolation("document", docBytes, MAX_DOCUMENT_BYTES,
                        "Chunk the document into smaller pieces (max " + MAX_DOCUMENT_BYTES + " bytes each)");
            }
        }

        // Embedding dimensions
        if (embedding != null && embedding.length > MAX_EMBEDDING_DIMENSIONS) {
            throw new QuotaViolation("embedding_dimensions", embedding.length, MAX_EMBEDDING_DIMENSIONS,
                    "Use a model with <= " + MAX_EMBEDDING_DIMENSIONS + " dimensions");
        }

        // Metadata key count
        if (metadata != null && metadata.size() > MAX_RECORD_METADATA_KEYS) {
            throw new QuotaViolation("metadata_keys", metadata.size(), MAX_RECORD_METADATA_KEYS);
        }

        // Per-key and per-value checks
        if (metadata != null) {
            for (Map.Entry<String, Object> e : metadata.entrySet()) {
                int keyBytes = e.getKey().getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
                if (keyBytes > MAX_METADATA_KEY_BYTES) {
                    throw new QuotaViolation("metadata_key", keyBytes, MAX_METADATA_KEY_BYTES,
                            "Shorten metadata key " + e.getKey() + " (max " + MAX_METADATA_KEY_BYTES + " bytes)");
                }
                if (e.getValue() instanceof String s) {
                    int valBytes = s.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
                    if (valBytes > MAX_RECORD_METADATA_VALUE_BYTES) {
                        throw new QuotaViolation("metadata_value[" + e.getKey() + "]",
                                valBytes, MAX_RECORD_METADATA_VALUE_BYTES);
                    }
                }
            }
        }
    }

    /**
     * Validate query parameters before dispatching to Chroma.
     */
    public void validateQuery(String queryText, int nResults, Map<String, Object> where) {
        if (nResults > MAX_QUERY_RESULTS) {
            throw new QuotaViolation("n_results", nResults, MAX_QUERY_RESULTS,
                    "Reduce n_results to <= " + MAX_QUERY_RESULTS);
        }
        if (where != null && where.size() > MAX_WHERE_PREDICATES) {
            throw new QuotaViolation("where_predicates", where.size(), MAX_WHERE_PREDICATES);
        }
        if (queryText != null && queryText.length() > MAX_QUERY_STRING_CHARS) {
            throw new QuotaViolation("query_text", queryText.length(), MAX_QUERY_STRING_CHARS);
        }
    }

    /**
     * Validate a batch of records (calls {@link #validateRecord} for each).
     */
    public void validateBatch(List<String> ids, List<String> documents,
                               List<float[]> embeddings, List<Map<String, Object>> metadatas) {
        for (int i = 0; i < ids.size(); i++) {
            String doc  = (documents  != null && i < documents.size())  ? documents.get(i)  : null;
            float[] emb = (embeddings != null && i < embeddings.size()) ? embeddings.get(i) : null;
            Map<String, Object> meta = (metadatas != null && i < metadatas.size()) ? metadatas.get(i) : null;
            validateRecord(ids.get(i), doc, emb, meta);
        }
    }
}
