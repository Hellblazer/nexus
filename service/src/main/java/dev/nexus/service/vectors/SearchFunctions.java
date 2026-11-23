// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.jooq.binding.Vector;
import org.jooq.JSONB;
import dev.nexus.service.vectors.PgVectorRepository.WherePlan;

import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_1024;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_768;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_1024;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_768;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_768_BY_ID;

/**
 * The generated search-function table calls of {@link PgVectorRepository}, moved out of it (the file-size ratchet: extract, never
 * raise): one builder per function family, each calling the name function over the labels until gate 3 and its {@code <name>_by_id}
 * twin over the resolved ids from it (RDR-229 P3.5c, item 6; vectors-077-103..140). The caller decides by
 * {@link CollectionSelector#byId()}, which is the one reading of gate 3 for the statement. Stateless.
 */
final class SearchFunctions {

    private SearchFunctions() {
    }

    /**
     * The generated {@code plain_search_<dim>} table-function call for one dispatch dim.
     * Shared by {@link #searchWithTokens} and the per-collection fan-out arms
     * ({@link #searchPerCollection}), so both run the SAME function with the same bind
     * order, which is what the fan-out's equivalence contract rests on.
     */
    static org.jooq.Table<?> plainSearchFn(int dim, Vector queryVec, CollectionSelector sel,
                                                   WherePlan wherePlan, int nResults,
                                                   String model, String tenant) {
        if (wherePlan.manifest() != null) {
            // nexus-p89h4: the same ranking with an EXISTS on the catalog manifest inside the scan.
            return manifestSearchFn(dim, queryVec, null, null, sel, wherePlan, nResults, model, tenant);
        }
        if (sel.byId()) {
            // RDR-229 P3.5c: gate 3 holds, so the id twin (vectors-077-103..140): the same arguments with the resolved ids in
            // place of the names. One id per fan-out arm, so a per-collection id index proves; all the ids on /search.
            Long[] ids = sel.idArray();
            return switch (dim) {
                case 384  -> PLAIN_SEARCH_384_BY_ID.call(
                    queryVec, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                case 768  -> PLAIN_SEARCH_768_BY_ID.call(
                    queryVec, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                case 1024 -> PLAIN_SEARCH_1024_BY_ID.call(
                    queryVec, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                default   -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
        }
        String[] colls = sel.nameArray();
        return switch (dim) {
            case 384  -> PLAIN_SEARCH_384.call(
                queryVec, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            case 768  -> PLAIN_SEARCH_768.call(
                queryVec, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            case 1024 -> PLAIN_SEARCH_1024.call(
                queryVec, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    /**
     * The generated {@code manifest_search_<dim>} call (vectors-041, nexus-p89h4 and nexus-6pfgb.25): the ranking
     * of {@code plain_search_<dim>}, of {@code text_gated_search_hnsw_first_<dim>} when {@code queryText} is not
     * null, and of {@code text_gated_search_by_chash_<dim>} when {@code chashes} is not null, with one more
     * conjunct: some live manifest occurrence of the chunk passes {@code wherePlan.manifest()} (see {@link
     * ManifestPushdown}). It returns the same six columns, so every caller reads the result as it read the others.
     *
     * @param queryText the text gate (an FTS or trigram match), or null for none
     * @param chashes   the exact chash set to rank, or null for no set
     */
    static org.jooq.Table<?> manifestSearchFn(int dim, Vector queryVec, String queryText, byte[][] chashes,
                                                      CollectionSelector sel, WherePlan wherePlan, int nResults,
                                                      String model, String tenant) {
        ManifestPushdown.Plan m = wherePlan.manifest();
        if (sel.byId()) {
            Long[] ids = sel.idArray();
            return switch (dim) {
                case 384  -> MANIFEST_SEARCH_384_BY_ID.call(
                    queryVec, queryText, chashes, ids, wherePlan.containment(), wherePlan.jsonPath(),
                    m.containment(), m.jsonPath(), nResults, model, tenant, wherePlan.excludeSensitive());
                case 768  -> MANIFEST_SEARCH_768_BY_ID.call(
                    queryVec, queryText, chashes, ids, wherePlan.containment(), wherePlan.jsonPath(),
                    m.containment(), m.jsonPath(), nResults, model, tenant, wherePlan.excludeSensitive());
                case 1024 -> MANIFEST_SEARCH_1024_BY_ID.call(
                    queryVec, queryText, chashes, ids, wherePlan.containment(), wherePlan.jsonPath(),
                    m.containment(), m.jsonPath(), nResults, model, tenant, wherePlan.excludeSensitive());
                default   -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
        }
        String[] colls = sel.nameArray();
        return switch (dim) {
            case 384  -> MANIFEST_SEARCH_384.call(
                queryVec, queryText, chashes, colls, wherePlan.containment(), wherePlan.jsonPath(),
                m.containment(), m.jsonPath(), nResults, model, tenant, wherePlan.excludeSensitive());
            case 768  -> MANIFEST_SEARCH_768.call(
                queryVec, queryText, chashes, colls, wherePlan.containment(), wherePlan.jsonPath(),
                m.containment(), m.jsonPath(), nResults, model, tenant, wherePlan.excludeSensitive());
            case 1024 -> MANIFEST_SEARCH_1024.call(
                queryVec, queryText, chashes, colls, wherePlan.containment(), wherePlan.jsonPath(),
                m.containment(), m.jsonPath(), nResults, model, tenant, wherePlan.excludeSensitive());
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    /**
     * The exact rank over an allow-list of chashes (nexus-jypmj, RDR-232): {@code text_gated_search_by_chash_<dim>}
     * (vectors-031), which despite its name evaluates no text gate. It is the plain-search statement with one more
     * predicate, {@code chash = ANY(p_chashes)}, and the same tenant, model, collection, live(c) and where
     * predicates, row shape and order ({@code distance, then id}) as {@code plain_search_<dim>}. The caller runs it
     * with index scans off ({@link #runPlainSearchStatement}'s {@code forceExact}), so the rank is a scan of the
     * allowed rows by the primary key, then a sort, never an HNSW walk filtered down to the allowed set.
     */
    static org.jooq.Table<?> chashAllowedSearchFn(int dim, Vector queryVec, byte[][] chashAllow,
                                                          CollectionSelector sel, WherePlan wherePlan, int nResults,
                                                          String model, String tenant) {
        if (wherePlan.manifest() != null) {
            // nexus-p89h4 with nexus-jypmj: the same exact rank over the allow-list with the manifest EXISTS inside the
            // scan. manifest_search_<dim> applies p_chashes as `chash = ANY(p_chashes)`, the conjunct
            // text_gated_search_by_chash_<dim> applies, so the allow-list narrows it exactly as it narrows that sibling.
            return manifestSearchFn(dim, queryVec, null, chashAllow, sel, wherePlan, nResults, model, tenant);
        }
        return textGatedByChashFn(dim, queryVec, chashAllow, sel, wherePlan, nResults, model, tenant);
    }

    /**
     * {@code text_gated_search_by_chash_<dim>}, or its {@code _by_id} twin from gate 3 (RDR-229 P3.5c): the exact rank over a
     * set of chashes. Shared by the {@code chash_allow} search and the selective branch of the hybrid rank.
     */
    static org.jooq.Table<?> textGatedByChashFn(int dim, Vector queryVec, byte[][] chashes, CollectionSelector sel,
                                                        WherePlan wherePlan, int nResults, String model, String tenant) {
        if (sel.byId()) {
            Long[] ids = sel.idArray();
            return switch (dim) {
                case 384  -> TEXT_GATED_SEARCH_BY_CHASH_384_BY_ID.call(
                    queryVec, chashes, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                case 768  -> TEXT_GATED_SEARCH_BY_CHASH_768_BY_ID.call(
                    queryVec, chashes, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                case 1024 -> TEXT_GATED_SEARCH_BY_CHASH_1024_BY_ID.call(
                    queryVec, chashes, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                default   -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
        }
        String[] colls = sel.nameArray();
        return switch (dim) {
            case 384  -> TEXT_GATED_SEARCH_BY_CHASH_384.call(
                queryVec, chashes, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            case 768  -> TEXT_GATED_SEARCH_BY_CHASH_768.call(
                queryVec, chashes, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            case 1024 -> TEXT_GATED_SEARCH_BY_CHASH_1024.call(
                queryVec, chashes, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    /** {@code text_gated_search_allowed_<dim>}, or its {@code _by_id} twin from gate 3 (RDR-229 P3.5c). */
    static org.jooq.Table<?> textGatedAllowedFn(int dim, Vector queryVec, String gateQueryText, byte[][] chashAllow,
                                                        CollectionSelector sel, WherePlan wherePlan, int nResults,
                                                        String model, String tenant) {
        if (sel.byId()) {
            Long[] ids = sel.idArray();
            return switch (dim) {
                case 384  -> TEXT_GATED_SEARCH_ALLOWED_384_BY_ID.call(
                    queryVec, gateQueryText, chashAllow, ids, wherePlan.containment(), wherePlan.jsonPath(),
                    nResults, model, tenant, wherePlan.excludeSensitive());
                case 768  -> TEXT_GATED_SEARCH_ALLOWED_768_BY_ID.call(
                    queryVec, gateQueryText, chashAllow, ids, wherePlan.containment(), wherePlan.jsonPath(),
                    nResults, model, tenant, wherePlan.excludeSensitive());
                case 1024 -> TEXT_GATED_SEARCH_ALLOWED_1024_BY_ID.call(
                    queryVec, gateQueryText, chashAllow, ids, wherePlan.containment(), wherePlan.jsonPath(),
                    nResults, model, tenant, wherePlan.excludeSensitive());
                default   -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
        }
        String[] colls = sel.nameArray();
        return switch (dim) {
            case 384  -> TEXT_GATED_SEARCH_ALLOWED_384.call(
                queryVec, gateQueryText, chashAllow, colls, wherePlan.containment(), wherePlan.jsonPath(),
                nResults, model, tenant, wherePlan.excludeSensitive());
            case 768  -> TEXT_GATED_SEARCH_ALLOWED_768.call(
                queryVec, gateQueryText, chashAllow, colls, wherePlan.containment(), wherePlan.jsonPath(),
                nResults, model, tenant, wherePlan.excludeSensitive());
            case 1024 -> TEXT_GATED_SEARCH_ALLOWED_1024.call(
                queryVec, gateQueryText, chashAllow, colls, wherePlan.containment(), wherePlan.jsonPath(),
                nResults, model, tenant, wherePlan.excludeSensitive());
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    /** {@code text_gated_search_hnsw_first_<dim>}, or its {@code _by_id} twin from gate 3 (RDR-229 P3.5c). */
    static org.jooq.Table<?> textGatedHnswFirstFn(int dim, Vector queryVec, String gateQueryText, CollectionSelector sel,
                                                          WherePlan wherePlan, int nResults, String model, String tenant) {
        if (sel.byId()) {
            Long[] ids = sel.idArray();
            return switch (dim) {
                case 384  -> TEXT_GATED_SEARCH_HNSW_FIRST_384_BY_ID.call(
                    queryVec, gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                case 768  -> TEXT_GATED_SEARCH_HNSW_FIRST_768_BY_ID.call(
                    queryVec, gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                case 1024 -> TEXT_GATED_SEARCH_HNSW_FIRST_1024_BY_ID.call(
                    queryVec, gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                    wherePlan.excludeSensitive());
                default   -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
        }
        String[] colls = sel.nameArray();
        return switch (dim) {
            case 384  -> TEXT_GATED_SEARCH_HNSW_FIRST_384.call(
                queryVec, gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            case 768  -> TEXT_GATED_SEARCH_HNSW_FIRST_768.call(
                queryVec, gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            case 1024 -> TEXT_GATED_SEARCH_HNSW_FIRST_1024.call(
                queryVec, gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), nResults, model, tenant,
                wherePlan.excludeSensitive());
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    static org.jooq.Table<?> metadataScopedFn(int dim, CollectionSelector sel, Vector queryVec, String contentType, String author,
            Integer year, String corpus, String subtree, JSONB whereJsonb, JSONB manifestWhere, String manifestPath, int nResults,
            String model, String tenant, boolean excludeSensitive) {
        String[] colls = sel.nameArray();
        return sel.byId() ? switch (dim) {
            case 384  -> SEARCH_METADATA_SCOPED_384_BY_ID.call(
                queryVec, sel.idArray(), contentType, author, year, corpus, subtree, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 768  -> SEARCH_METADATA_SCOPED_768_BY_ID.call(
                queryVec, sel.idArray(), contentType, author, year, corpus, subtree, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 1024 -> SEARCH_METADATA_SCOPED_1024_BY_ID.call(
                queryVec, sel.idArray(), contentType, author, year, corpus, subtree, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        } : switch (dim) {
            case 384  -> SEARCH_METADATA_SCOPED_384.call(
                queryVec, colls, contentType, author, year, corpus, subtree, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 768  -> SEARCH_METADATA_SCOPED_768.call(
                queryVec, colls, contentType, author, year, corpus, subtree, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 1024 -> SEARCH_METADATA_SCOPED_1024.call(
                queryVec, colls, contentType, author, year, corpus, subtree, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    static org.jooq.Table<?> aspectScopedFn(int dim, CollectionSelector sel, Vector queryVec, String field, String pattern,
            Double minConfidence, JSONB whereJsonb, JSONB manifestWhere, String manifestPath, int nResults, String model, String tenant,
            boolean excludeSensitive) {
        String[] colls = sel.nameArray();
        return sel.byId() ? switch (dim) {
            case 384  -> SEARCH_ASPECT_SCOPED_384_BY_ID.call(
                queryVec, sel.idArray(), field, pattern, minConfidence, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 768  -> SEARCH_ASPECT_SCOPED_768_BY_ID.call(
                queryVec, sel.idArray(), field, pattern, minConfidence, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 1024 -> SEARCH_ASPECT_SCOPED_1024_BY_ID.call(
                queryVec, sel.idArray(), field, pattern, minConfidence, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        } : switch (dim) {
            case 384  -> SEARCH_ASPECT_SCOPED_384.call(
                queryVec, colls, field, pattern, minConfidence, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 768  -> SEARCH_ASPECT_SCOPED_768.call(
                queryVec, colls, field, pattern, minConfidence, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 1024 -> SEARCH_ASPECT_SCOPED_1024.call(
                queryVec, colls, field, pattern, minConfidence, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    static org.jooq.Table<?> topicScopedFn(int dim, CollectionSelector sel, String collection, Vector queryVec, String topicLabel,
            int nResults, String model, String tenant, boolean excludeSensitive) {
        return sel.byId() ? switch (dim) {
            case 384  -> SEARCH_TOPIC_SCOPED_384_BY_ID.call(queryVec, topicLabel, sel.idOf(collection), nResults, model,
                tenant, excludeSensitive);
            case 768  -> SEARCH_TOPIC_SCOPED_768_BY_ID.call(queryVec, topicLabel, sel.idOf(collection), nResults, model,
                tenant, excludeSensitive);
            case 1024 -> SEARCH_TOPIC_SCOPED_1024_BY_ID.call(queryVec, topicLabel, sel.idOf(collection), nResults, model,
                tenant, excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        } : switch (dim) {
            case 384  -> SEARCH_TOPIC_SCOPED_384.call(queryVec, topicLabel, collection, nResults, model, tenant,
                excludeSensitive);
            case 768  -> SEARCH_TOPIC_SCOPED_768.call(queryVec, topicLabel, collection, nResults, model, tenant,
                excludeSensitive);
            case 1024 -> SEARCH_TOPIC_SCOPED_1024.call(queryVec, topicLabel, collection, nResults, model, tenant,
                excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    static org.jooq.Table<?> graphHopFn(int dim, CollectionSelector sel, Vector queryVec, String[] seedArr, String linkType,
            int clampedDepth, String dir, JSONB whereJsonb, JSONB manifestWhere, String manifestPath, int nResults, String model,
            String tenant, boolean excludeSensitive) {
        String[] colls = sel.nameArray();
        return sel.byId() ? switch (dim) {
            case 384  -> SEARCH_GRAPH_HOP_384_BY_ID.call(
                queryVec, seedArr, sel.idArray(), linkType, clampedDepth, dir, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 768  -> SEARCH_GRAPH_HOP_768_BY_ID.call(
                queryVec, seedArr, sel.idArray(), linkType, clampedDepth, dir, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 1024 -> SEARCH_GRAPH_HOP_1024_BY_ID.call(
                queryVec, seedArr, sel.idArray(), linkType, clampedDepth, dir, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        } : switch (dim) {
            case 384  -> SEARCH_GRAPH_HOP_384.call(
                queryVec, seedArr, colls, linkType, clampedDepth, dir, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 768  -> SEARCH_GRAPH_HOP_768.call(
                queryVec, seedArr, colls, linkType, clampedDepth, dir, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            case 1024 -> SEARCH_GRAPH_HOP_1024.call(
                queryVec, seedArr, colls, linkType, clampedDepth, dir, whereJsonb, manifestWhere, manifestPath,
                nResults, model, tenant, excludeSensitive);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }
}
