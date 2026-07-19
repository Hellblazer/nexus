/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-180 LAND-THEN-TRANSFORM promote (nexus-jxizy.10.3): the in-DB,
 * transactional re-id from the width-free {@code staging} schema into the
 * strict {@code nexus} schema.
 *
 * <p>Design of record: T2 {@code nexus_rdr/180-land-transform-design} +
 * {@code -reconciliation}. Sibling of {@link RekeyOps} composing the SAME
 * {@link ChashSqlIdioms} fragments in the INSERT-into-possibly-populated-
 * target shape (different collision surface — reconciliation R5).
 *
 * <p>TWO operations, both under {@link TenantScope#withTenant} (nexus_svc,
 * RLS-scoped by construction):
 *
 * <h3>{@link #promoteCollection} — one txn per (tenant, collection)</h3>
 * <ol>
 *   <li>Dim precheck: staged dims must be one of the three tables AND agree
 *       with the collection name's implied dim (reconciliation H1 — the
 *       caller passes the name-implied dim from the same dispatch serving
 *       uses; belt + braces against mislabeled sources).</li>
 *   <li>C1 GUARD (tenant-wide, against COMMITTED state): any staged ref
 *       whose already-committed {@code chash_alias.new_chash} differs from
 *       this batch's computed digest fails LOUD with both digests — never
 *       a silent {@code DO NOTHING} keep. Same-ref-same-digest is the
 *       idempotent-resume case and passes.</li>
 *   <li>Alias build for genuinely-legacy refs ({@code old_bytes} via the
 *       shared {@code nexus.chash_old_bytes} function).</li>
 *   <li>Content INSERT per dim: {@code DISTINCT ON (digest)} with the M1
 *       deterministic tiebreak (already-canonical ref first, else min
 *       legacy_ref), {@code ON CONFLICT DO NOTHING} against live rows.
 *       Vectors are copied verbatim (the landing client only stages a
 *       vector when reuse is legal — staged NULL embeddings must be
 *       embed-filled in staging BEFORE promote; this op REFUSES content
 *       rows with NULL embeddings, counting them loud).</li>
 *   <li>chash_index promote via alias join (canonical at INSERT — the
 *       strict octet CHECK is satisfied by construction; sha256 output is
 *       always 32 bytes).</li>
 * </ol>
 *
 * <h3>{@link #finalizeTenant} — IDEMPOTENT, RE-RUNNABLE (reconciliation C2)</h3>
 * Runs after EVERY promote wave; a late-landed collection is handled by
 * promote + finalize again. One tenant txn:
 * <ol>
 *   <li>Manifest promote (doc-scoped, so it lives here where the alias is
 *       complete — deviation from the design memo's per-collection manifest
 *       placement, recorded in the bead close): staged manifest rows insert
 *       through the alias join (or a direct 64-hex decode), canonical at
 *       INSERT.</li>
 *   <li>Pointer stores: topic_assignments (alias-repointed where
 *       chash-shaped, verbatim where a memory title — the mixed identity
 *       space stays TEXT debt), frecency (GREATEST-merge, staging-sourced
 *       twin of the RekeyOps aggregate), relevance_log (anti-join dedupe —
 *       BIGSERIAL target has no natural key), document_aspects +
 *       aspect_extraction_queue (anti-join on (collection, source_path)).</li>
 *   <li>Item8 disposition for staged empty-text rows with TENANT-WIDE
 *       visibility (reconciliation C4): reference-only rows (alias
 *       resolves) count as resolved; orphans get the per-run policy
 *       (drop = never promoted, counted; synthesize = deterministic
 *       surrogate + {@code chash_origin='synthetic'}).</li>
 *   <li>In-txn verify: residual digest-mismatch and dangling-manifest
 *       counts MUST be zero (abort otherwise); the census extension rides
 *       nexus-jxizy.10.5.</li>
 * </ol>
 */
// SANCTIONED RAW (nexus-jxizy.10.3, RawSqlGateTest allowlist): one-shot
// migration statements composed from the ChashSqlIdioms fragments —
// sha256() digests, DISTINCT ON keeper selection, alias joins and
// GREATEST-merge aggregates have no jOOQ DSL form; never serving-path.
public final class StagingPromoteOps {

    private static final Logger log = LoggerFactory.getLogger(StagingPromoteOps.class);

    /** Digest over the STAGED text column (alias {@code s}). */
    private static final String S_DIGEST = "sha256(convert_to(s.chunk_text, 'UTF8'))";

    private final TenantScope tenantScope;

    public StagingPromoteOps(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /** Thrown on the C1 guard: a staged ref contradicts a committed alias. */
    public static final class PromoteConflictException extends RuntimeException {
        public PromoteConflictException(String message) {
            super(message);
        }
    }

    /** Thrown when staged rows cannot promote (dim mismatch, NULL vectors). */
    public static final class PromotePreconditionException extends RuntimeException {
        public PromotePreconditionException(String message) {
            super(message);
        }
    }

    /**
     * Promote one landed collection. {@code impliedDim} is the collection
     * NAME's dim per the same dispatch serving uses (reconciliation H1).
     * Returns the auditable counts envelope.
     */
    public Map<String, Object> promoteCollection(String tenant, String collection, int impliedDim) {
        if (impliedDim != 384 && impliedDim != 768 && impliedDim != 1024) {
            throw new PromotePreconditionException(
                "impliedDim must be one of 384/768/1024, got " + impliedDim);
        }
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> counts = new LinkedHashMap<>();

            // (1) dim precheck — every staged content row must carry the
            // name-implied dim (land-time classification already renamed
            // mislabeled collections to their honest target).
            Integer badDim = ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.dim <> ?",
                collection, impliedDim).get(0, Integer.class);
            if (badDim != null && badDim > 0) {
                throw new PromotePreconditionException(
                    badDim + " staged row(s) in '" + collection + "' carry a dim "
                    + "differing from the collection name's implied " + impliedDim
                    + " — the land-time classification must rename mislabeled "
                    + "sources to their honest target (nexus-nb7hr), never "
                    + "promote a name/dim disagreement");
            }
            Integer nullVec = ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.chunk_text <> '' AND s.embedding IS NULL",
                collection).get(0, Integer.class);
            if (nullVec != null && nullVec > 0) {
                throw new PromotePreconditionException(
                    nullVec + " staged content row(s) in '" + collection + "' have "
                    + "no embedding — embed-fill staging before promote (reuse "
                    + "was not legal for these rows)");
            }

            // (2) C1 GUARD: staged refs vs COMMITTED alias state, tenant-wide.
            var conflict = ctx.resultQuery(
                "SELECT s.legacy_ref, encode(a.new_chash, 'hex') AS committed, "
                + "       encode(" + S_DIGEST + ", 'hex') AS computed, a.source "
                + "FROM staging.chunks s JOIN nexus.chash_alias a ON a.old_ref = s.legacy_ref "
                + "WHERE s.collection = ? AND s.chunk_text <> '' "
                + "  AND a.new_chash IS DISTINCT FROM " + S_DIGEST,
                collection).fetchAny();
            if (conflict != null) {
                throw new PromoteConflictException(
                    "staged ref '" + conflict.get(0, String.class) + "' in '" + collection
                    + "' computes digest " + conflict.get(2, String.class)
                    + " but chash_alias already maps it to " + conflict.get(1, String.class)
                    + " (source: " + conflict.get(3, String.class) + ") — the same legacy id "
                    + "denotes different content across collections; refusing to pick "
                    + "silently (GH #1390: correct addresses only)");
            }

            // (3) alias facts for genuinely-legacy staged refs.
            counts.put("alias_rows", ctx.execute(
                "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                + "SELECT current_setting('nexus.tenant', true), s.legacy_ref, "
                + "       " + ChashSqlIdioms.chashOldBytes("s.legacy_ref") + ", "
                + "       " + S_DIGEST + ", 'staging:' || s.collection "
                + "FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.chunk_text <> '' "
                + "  AND s.legacy_ref <> encode(" + S_DIGEST + ", 'hex') "
                + "ON CONFLICT (tenant_id, old_ref) DO NOTHING",
                collection));

            // (4) collection registration stub — the chunks tables carry a
            // (tenant, collection) FK to catalog_collections (RDR-156
            // schema-enforced integrity: the FK that mechanically catches a
            // missed landing leg). Same ON-CONFLICT-DO-NOTHING shape as the
            // serving path's auto-stub; the catalog ETL's fuller row wins
            // when it already exists.
            String contentType = collection.contains("__")
                ? collection.substring(0, collection.indexOf("__")) : "knowledge";
            ctx.execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type) "
                + "VALUES (current_setting('nexus.tenant', true), ?, ?) "
                + "ON CONFLICT (tenant_id, name) DO NOTHING",
                collection, contentType);

            // (5) content INSERT for the collection's dim table. DISTINCT ON
            // digest with the M1 deterministic tiebreak; DO NOTHING against
            // live rows (idempotent resume; populated-target legal).
            String chunkTable = "nexus.chunks_" + impliedDim;
            int promoted = ctx.execute(
                "INSERT INTO " + chunkTable + " (tenant_id, collection, chash, chunk_text, embedding, metadata) "
                + "SELECT DISTINCT ON (" + S_DIGEST + ") "
                + "       current_setting('nexus.tenant', true), s.collection, "
                + "       " + S_DIGEST + ", s.chunk_text, "
                + "       s.embedding::vector(" + impliedDim + "), s.chunk_meta "
                + "FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.chunk_text <> '' "
                + "ORDER BY " + S_DIGEST + ", "
                + "         (s.legacy_ref = encode(" + S_DIGEST + ", 'hex')) DESC, "
                + "         s.legacy_ref "
                + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING",
                collection);
            counts.put("promoted", promoted);
            Integer stagedContent = ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s WHERE s.collection = ? AND s.chunk_text <> ''",
                collection).get(0, Integer.class);
            counts.put("staged_content", stagedContent);

            // (5) chash_index promote via alias (canonical at INSERT).
            counts.put("chash_index_promoted", ctx.execute(
                "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
                + "SELECT DISTINCT ON (resolved.chash) current_setting('nexus.tenant', true), "
                + "       resolved.chash, resolved.physical_collection, resolved.created_at "
                + "FROM ("
                + "  SELECT COALESCE(a.new_chash, "
                + "           CASE WHEN s.chash ~ '^[0-9a-f]{64}$' THEN decode(s.chash, 'hex') END"
                + "         ) AS chash, "
                + "         s.physical_collection, "
                + "         COALESCE(NULLIF(s.created_at, '')::timestamptz, now()) AS created_at "
                + "  FROM staging.chash_index s "
                + "  LEFT JOIN nexus.chash_alias a ON a.old_ref = s.chash "
                + "  WHERE s.physical_collection = ?"
                + ") resolved "
                + "WHERE resolved.chash IS NOT NULL "
                + "ORDER BY resolved.chash, resolved.created_at "
                + "ON CONFLICT (tenant_id, chash, physical_collection) DO NOTHING",
                collection));

            return counts;
        });
        log.info("event=staging_promote_collection tenant={} collection={} counts={}",
            tenant, collection, out);
        return out;
    }

    /**
     * The idempotent tenant finalize (reconciliation C2/C4): manifest +
     * pointer-store promotion through the (cumulative) alias, Item8
     * disposition with tenant-wide visibility, in-txn verify. Re-runnable
     * after every promote wave.
     */
    public Map<String, Object> finalizeTenant(String tenant, boolean synthesizeOrphans) {
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> counts = new LinkedHashMap<>();

            // (1) Item8, tenant-wide (C4): staged empty-text rows.
            //     reference-only = the ref resolves through the alias (content
            //     landed in ANY collection) or is already-canonical for a live
            //     chunk. Orphans get the policy.
            String orphanCond =
                "s.chunk_text = '' "
                + "AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a WHERE a.old_ref = s.legacy_ref) "
                + "AND NOT EXISTS (SELECT 1 FROM staging.chunks c2 "
                + "      WHERE c2.legacy_ref = s.legacy_ref AND c2.chunk_text <> '')";
            counts.put("reference_only_resolved", ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s WHERE s.chunk_text = '' "
                + "AND EXISTS (SELECT 1 FROM nexus.chash_alias a WHERE a.old_ref = s.legacy_ref)")
                .get(0, Integer.class));
            if (synthesizeOrphans) {
                // Alias the surrogates FIRST so pointer promotion below
                // repoints them (the RekeyOps ordering, verbatim rationale).
                ctx.execute(
                    "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                    + "SELECT current_setting('nexus.tenant', true), s.legacy_ref, "
                    + "       " + ChashSqlIdioms.chashOldBytes("s.legacy_ref") + ", "
                    + "       sha256(convert_to("
                    + "         'nexus:synthetic-chash:v1|' || current_setting('nexus.tenant', true) "
                    + "         || '|' || s.collection || '|' || s.legacy_ref, 'UTF8')), "
                    + "       'staging:synthetic' "
                    + "FROM staging.chunks s WHERE " + orphanCond + " "
                    + "ON CONFLICT (tenant_id, old_ref) DO NOTHING");
                counts.put("orphans_synthesized", ctx.execute(
                    "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding, metadata) "
                    + "SELECT current_setting('nexus.tenant', true), s.collection, a.new_chash, '', "
                    + "       s.embedding::vector(768), "
                    + "       coalesce(s.chunk_meta, '{}'::jsonb) || jsonb_build_object('chash_origin', 'synthetic') "
                    + "FROM staging.chunks s JOIN nexus.chash_alias a "
                    + "  ON a.old_ref = s.legacy_ref AND a.source = 'staging:synthetic' "
                    + "WHERE s.chunk_text = '' AND s.dim = 768 AND s.embedding IS NOT NULL "
                    + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING"));
                counts.put("orphans_dropped", 0);
            } else {
                counts.put("orphans_synthesized", 0);
                counts.put("orphans_dropped", ctx.fetchOne(
                    "SELECT count(*) FROM staging.chunks s WHERE " + orphanCond)
                    .get(0, Integer.class));
            }

            // (2) manifest promote through the alias (doc-scoped => finalize;
            //     canonical at INSERT so the octet CHECK holds by construction).
            counts.put("manifest_promoted", ctx.execute(
                "INSERT INTO nexus.catalog_document_chunks "
                + "  (tenant_id, doc_id, position, chash, chunk_index, line_start, line_end, char_start, char_end) "
                + "SELECT current_setting('nexus.tenant', true), s.doc_id, s.position, resolved.chash, "
                + "       s.chunk_index, s.line_start, s.line_end, s.char_start, s.char_end "
                + "FROM staging.document_chunks s "
                + "JOIN LATERAL (SELECT COALESCE(a.new_chash, "
                + "         CASE WHEN s.chash ~ '^[0-9a-f]{64}$' THEN decode(s.chash, 'hex') END) AS chash "
                + "       FROM (SELECT 1) one "
                + "       LEFT JOIN nexus.chash_alias a ON a.old_ref = s.chash) resolved ON true "
                + "WHERE resolved.chash IS NOT NULL "
                + "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING"));
            counts.put("manifest_unresolved", ctx.fetchOne(
                "SELECT count(*) FROM staging.document_chunks s "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.chash_alias a WHERE a.old_ref = s.chash) "
                + "  AND s.chash !~ '^[0-9a-f]{64}$'").get(0, Integer.class));

            // (3) topic_assignments: alias-repoint chash-shaped doc_ids,
            //     verbatim pass-through for memory titles (mixed identity).
            counts.put("topic_assignments_promoted", ctx.execute(
                "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id) "
                + "SELECT DISTINCT current_setting('nexus.tenant', true), "
                + "       COALESCE(encode(a.new_chash, 'hex'), s.doc_id), s.topic_id "
                + "FROM staging.topic_assignments s "
                + "LEFT JOIN nexus.chash_alias a ON a.old_ref = s.doc_id "
                + "ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING"));

            // (4) frecency: GREATEST-merge from the staged rows through the
            //     alias — staging-sourced twin of ChashSqlIdioms'
            //     frecencyAliasAggregate (same semantics, staged source).
            String stagedFrecencyAgg =
                "(SELECT COALESCE(encode(a.new_chash, 'hex'), s.chunk_id) AS target_id, "
                + "        max(s.frecency_score) AS fs, max(s.miss_count) AS mc, "
                + "        max(COALESCE(NULLIF(s.last_hit_at, '')::timestamptz, now())) AS lh, "
                + "        max(COALESCE(NULLIF(s.embedded_at, '')::timestamptz, now())) AS ea, "
                + "        max(s.ttl_days) AS td "
                + "   FROM staging.frecency s LEFT JOIN nexus.chash_alias a "
                + "     ON s.chunk_id = a.old_ref GROUP BY 1) g";
            ctx.execute(
                "UPDATE nexus.frecency t SET "
                + "  frecency_score = GREATEST(t.frecency_score, g.fs), "
                + "  miss_count     = GREATEST(t.miss_count,     g.mc), "
                + "  last_hit_at    = GREATEST(t.last_hit_at,    g.lh), "
                + "  embedded_at    = GREATEST(t.embedded_at,    g.ea), "
                + "  ttl_days       = GREATEST(t.ttl_days,       g.td) "
                + "FROM " + stagedFrecencyAgg + " WHERE t.chunk_id = g.target_id");
            counts.put("frecency_promoted", ctx.execute(
                "INSERT INTO nexus.frecency (tenant_id, chunk_id, embedded_at, ttl_days, frecency_score, miss_count, last_hit_at) "
                + "SELECT current_setting('nexus.tenant', true), g.target_id, g.ea, g.td, g.fs, g.mc, g.lh "
                + "FROM " + stagedFrecencyAgg + " "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.frecency t WHERE t.chunk_id = g.target_id)"));

            // (5) relevance_log: BIGSERIAL target, no natural key — anti-join
            //     on the full staged identity for idempotent re-finalize.
            counts.put("relevance_log_promoted", ctx.execute(
                "INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, collection, action, session_id, timestamp) "
                + "SELECT current_setting('nexus.tenant', true), s.query, "
                + "       COALESCE(encode(a.new_chash, 'hex'), s.chunk_id), s.collection, s.action, s.session_id, "
                + "       COALESCE(NULLIF(s.ts, '')::timestamptz, now()) "
                + "FROM staging.relevance_log s "
                + "LEFT JOIN nexus.chash_alias a ON a.old_ref = s.chunk_id "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.relevance_log t "
                + "  WHERE t.query = s.query "
                + "    AND t.chunk_id = COALESCE(encode(a.new_chash, 'hex'), s.chunk_id) "
                + "    AND t.action = s.action "
                + "    AND t.timestamp = COALESCE(NULLIF(s.ts, '')::timestamptz, now()))"));

            // (6) aspects (Class-D): anti-join on (collection, source_path).
            //     source_path/source_uri carry no in-flight rewrite here —
            //     chroma:// URI repoints ride the alias at READ time via the
            //     shared resolvers; staged values land verbatim.
            counts.put("document_aspects_promoted", ctx.execute(
                "INSERT INTO nexus.document_aspects "
                + "  (tenant_id, collection, source_path, problem_formulation, proposed_method, "
                + "   experimental_datasets, experimental_baselines, experimental_results, extras, "
                + "   confidence, extracted_at, model_version, extractor_name, source_uri, doc_id) "
                + "SELECT current_setting('nexus.tenant', true), s.collection, s.source_path, "
                + "       s.problem_formulation, s.proposed_method, s.experimental_datasets, "
                + "       s.experimental_baselines, s.experimental_results, s.extras, s.confidence, "
                + "       COALESCE(NULLIF(s.extracted_at, '')::timestamptz, now()), "
                + "       s.model_version, s.extractor_name, s.source_uri, s.doc_id "
                + "FROM staging.document_aspects s "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.document_aspects t "
                + "  WHERE t.collection = s.collection AND t.source_path = s.source_path)"));
            counts.put("aspect_queue_promoted", ctx.execute(
                "INSERT INTO nexus.aspect_extraction_queue "
                + "  (tenant_id, collection, source_path, doc_id, content_hash, content, status, "
                + "   retry_count, enqueued_at, last_attempt_at, last_error) "
                + "SELECT current_setting('nexus.tenant', true), s.collection, s.source_path, s.doc_id, "
                + "       s.content_hash, s.content, s.status, s.retry_count, "
                + "       COALESCE(NULLIF(s.enqueued_at, '')::timestamptz, now()), "
                + "       NULLIF(s.last_attempt_at, '')::timestamptz, s.last_error "
                + "FROM staging.aspect_extraction_queue s "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.aspect_extraction_queue t "
                + "  WHERE t.collection = s.collection AND t.source_path = s.source_path)"));

            // (7) in-txn verify (the census extension rides nexus-jxizy.10.5).
            int residual = 0;
            for (String t : ChashSqlIdioms.CHUNK_TABLES) {
                residual += ctx.fetchOne(
                    ChashSqlIdioms.residualMismatchCount(t)).get(0, Integer.class);
            }
            counts.put("residual_mismatched", residual);
            counts.put("dangling_manifest", ctx.fetchOne(
                ChashSqlIdioms.danglingManifestCount()).get(0, Integer.class));
            if (residual != 0) {
                throw new IllegalStateException(
                    "finalize left " + residual + " digest-mismatched content row(s) — aborting");
            }
            return counts;
        });
        log.info("event=staging_finalize tenant={} counts={}", tenant, out);
        return out;
    }
}
