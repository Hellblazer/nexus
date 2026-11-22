// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.util.List;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the one checked list of ON CONFLICT arbiters on a collection-name key and the id-keyed twin each has, from
 * elsinore's P3.5c mapping (her reader census, op=on_conflict) and P3.4's arbiter census, with linda's rulings of 2026-11-22. Read by
 * {@link IdUniqueIndexPass} (which builds the twins of the entries that name an index), by the arbiter census test (which pins this list
 * against the live catalog: every name-holding unique index is here or exempt with a reason) and by P3.5's id-keyed upsert retry helper.
 * Columns are in key order and spelled as the catalog lists them; a predicate is in the form {@code pg_get_expr} deparses it, without its
 * outer parentheses.
 *
 * <p>Not here: {@code catalog_collections}, whose upserts keep the label (G15: {@code ON CONFLICT (tenant_id, name) WHERE superseded_by_id
 * IS NULL} infers P3.6's live-label index), so it has no id twin to arbitrate on.
 *
 * @param table       the table, in schema nexus
 * @param nameColumns the name key's columns: what an upsert arbitrates on today
 * @param idColumns   the id twin's columns: what an id-keyed upsert arbitrates on
 * @param predicate   both keys' partial predicate, or null
 * @param twinIndex   the twin index {@link IdUniqueIndexPass} builds, or null when another step provides the twin (P3.3b/c's parent UNIQUE
 *                    on chunks and taxonomy_centroids)
 */
public record ArbiterTwins(String table, String nameColumns, String idColumns, String predicate, String twinIndex) {

    /** reembed_jobs_source_lock's predicate (vectors-037), the active job states. */
    public static final String REEMBED_ACTIVE =
        "state = ANY (ARRAY['planned'::text, 'running'::text, 'paused'::text, 'cutover'::text, 'failed'::text])";

    public static final List<ArbiterTwins> ALL = List.of(
        // the P3.3b/c parent UNIQUE on the id key, attached once both swaps complete (gate 2)
        new ArbiterTwins("chunks", "tenant_id, collection, chash, embedding_model", "tenant_id, collection_id, chash, embedding_model", null, null),
        new ArbiterTwins("taxonomy_centroids", "tenant_id, collection, topic_id, embedding_model",
            "tenant_id, collection_id, topic_id, embedding_model", null, null),
        // elsinore's mapping: AspectRepository, RemapRepository, TaxonomyRepository
        new ArbiterTwins("document_aspects", "tenant_id, collection, source_path", "tenant_id, collection_id, source_path", null,
            "idx_doc_aspects_id_source_path_uq"),
        new ArbiterTwins("chash_remap", "tenant_id, source_collection, old_id", "tenant_id, source_collection_id, old_id", null,
            "idx_chash_remap_id_old_id_uq"),
        new ArbiterTwins("taxonomy_meta", "tenant_id, collection", "tenant_id, collection_id", null, "idx_taxonomy_meta_id_uq"),
        new ArbiterTwins("topics", "tenant_id, collection, label", "tenant_id, collection_id, label", "parent_id IS NULL",
            "idx_topics_root_label_id_uq"),
        // the census's two SQL arbiters: stamp_chunks_on_manifest_delete/_update and canary_query_ring_upsert (vectors-069)
        new ArbiterTwins("chunk_orphaned_at", "tenant_id, collection, chash", "tenant_id, collection_id, chash", null,
            "idx_chunk_orphaned_at_id_chash_uq"),
        new ArbiterTwins("canary_query_ring", "tenant_id, collection, vector_digest", "tenant_id, collection_id, vector_digest", null,
            "idx_canary_query_ring_id_digest_uq"),
        // target-less ON CONFLICT DO NOTHING (PipelineRepository, ReembedJobRepository): every unique index arbitrates
        new ArbiterTwins("pdf_pipeline", "tenant_id, content_hash, collection, pdf_path", "tenant_id, content_hash, collection_id, pdf_path", null,
            "idx_pdf_pipeline_id_uq"),
        new ArbiterTwins("reembed_jobs", "tenant_id, source_collection", "tenant_id, source_collection_id", REEMBED_ACTIVE,
            "idx_reembed_jobs_source_id_lock"));

    /** The entries whose twin {@link IdUniqueIndexPass} builds. */
    public static List<ArbiterTwins> builtByThePass() {
        return ALL.stream().filter(a -> a.twinIndex() != null).toList();
    }
}
