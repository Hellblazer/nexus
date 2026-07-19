/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-180 Item6, engine half (nexus-jxizy.6): the per-tenant full-digest
 * rekey. Executed INSIDE the freeze window by the client rung via
 * {@code POST /v1/remap/rekey}; runs as nexus_svc under
 * {@link TenantScope#withTenant} (per-tenant RLS — the reason this is an
 * endpoint and not Liquibase DML, which silently sees zero rows under
 * FORCE RLS as the non-BYPASSRLS owner).
 *
 * <p>PREDICATE (design amendment 2, T2 nexus_rdr/180-engine-cohort-design-
 * amendments): a row needs rekeying when {@code chash IS DISTINCT FROM
 * sha256(chunk_text)} — digest-mismatch, NOT width. A width predicate
 * would miss 32-ASCII-char legacy ids that converted to exactly 32 bytes.
 * Idempotent by construction: a second run finds every content row equal
 * to its digest and no-ops.
 *
 * <p>ORDER, one transaction per tenant (single atomic cutover — RDR-180
 * Failure Modes: no dual-width window within a tenant):
 * <ol>
 *   <li>Conflict pre-check: one recovered old_ref mapping to two distinct
 *       digests fails LOUD (realized 128-bit collision / corpus
 *       corruption — mirrors the client build_content_map refusal).</li>
 *   <li>chash_alias build: (old_ref per the reversibility lemma,
 *       old_bytes, new digest) for every mismatched content row.</li>
 *   <li>Item8 disposition for empty-text rows: reference-only rows
 *       resolve through the alias built from content siblings; orphans
 *       get the per-run policy (drop cascades their manifest/chash_index
 *       pointers in the same transaction; synthesize mints the
 *       deterministic surrogate and stamps
 *       {@code metadata.chash_origin='synthetic'}).</li>
 *   <li>Two-phase chunk rekey per dim (RDR-185 PK-collision-under-
 *       collapse): keep one row per (collection, digest) — preferring a
 *       row already AT the digest key — delete the rest (all aliased),
 *       then UPDATE survivors.</li>
 *   <li>Cascade via the alias: manifest (plain — chash not in its PK),
 *       chash_index (two-phase on its (tenant, chash, collection) PK),
 *       topic_assignments (TEXT doc_id via old_ref match, two-phase),
 *       frecency (GREATEST-merge on collapse), relevance_log (plain).</li>
 * </ol>
 * VALIDATE of the octet CHECKs is deliberately NOT here: the client rung
 * runs it via the local admin connection after count-verify (table owner,
 * RLS-exempt scan).
 */
public final class RekeyOps {

    private static final Logger log = LoggerFactory.getLogger(RekeyOps.class);

    // Shared with StagingPromoteOps via ChashSqlIdioms (nexus-jxizy.10.2):
    // the digest formula, lemma, collapse keeper, frecency merge and verify
    // scans are single-homed there so the two chash movers cannot drift.
    private static final List<String> CHUNK_TABLES = ChashSqlIdioms.CHUNK_TABLES;

    /** Reversibility-lemma rendering of a converted key's original string. */
    private static final String OLD_REF = ChashSqlIdioms.OLD_REF_LEMMA;

    private static final String DIGEST = ChashSqlIdioms.DIGEST;

    private final TenantScope tenantScope;

    public RekeyOps(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /** Thrown when one legacy id maps to two distinct content digests. */
    public static final class RekeyConflictException extends RuntimeException {
        public RekeyConflictException(String message) {
            super(message);
        }
    }

    /**
     * Run the full rekey for *tenant*. {@code synthesizeOrphans} selects the
     * Item8 policy for orphaned empty-text rows (default caller: drop).
     * Returns the disposition + per-table counts (the auditable envelope).
     */
    // SANCTIONED RAW (nexus-jxizy.6, RawSqlGateTest allowlist): the rekey is
    // deliberately server-side SQL — sha256() over chunk_text, the
    // ctid/array_agg keeper idiom for two-phase collapse, and the
    // reversibility-lemma CASE expressions have no jOOQ DSL form; these are
    // one-shot freeze-window migration statements, never serving-path
    // queries.
    public Map<String, Object> rekey(String tenant, boolean synthesizeOrphans) {
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> counts = new LinkedHashMap<>();

            // (1) conflict pre-check across all dims: same old_ref, two digests.
            Integer conflicts = ctx.fetchOne(
                "SELECT count(*) FROM ("
                + "  SELECT old_ref FROM ("
                + unionAllContentRows()
                + "  ) u GROUP BY old_ref HAVING count(DISTINCT new_chash) > 1"
                + ") q").get(0, Integer.class);
            if (conflicts != null && conflicts > 0) {
                throw new RekeyConflictException(
                    conflicts + " legacy id(s) map to more than one content digest "
                    + "(realized 128-bit collision or corpus corruption) — refusing "
                    + "to pick silently (GH #1390: correct addresses only)");
            }

            // (2) alias facts for every mismatched CONTENT row (all dims).
            int aliased = ctx.execute(
                "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                + "SELECT current_setting('nexus.tenant', true), old_ref, old_bytes, new_chash, source "
                + "FROM (" + unionAllContentRows() + ") u "
                + "ON CONFLICT (tenant_id, old_ref) DO NOTHING");
            counts.put("alias_rows", aliased);

            // (3) Item8: empty-text rows. Reference-only rows resolve through
            // the alias just built from content-bearing siblings; the rest are
            // orphans under the per-run policy.
            int refResolved = 0;
            int orphansDropped = 0;
            int orphansSynthesized = 0;
            for (String t : CHUNK_TABLES) {
                refResolved += ctx.execute(
                    "UPDATE " + t + " c SET chash = a.new_chash "
                    + "FROM nexus.chash_alias a "
                    + "WHERE c.chunk_text = '' "
                    + "  AND a.old_bytes = c.chash "
                    + "  AND c.chash IS DISTINCT FROM a.new_chash "
                    // two-phase guard: skip if the resolved key already exists
                    // in this collection (shared-content collapse — the row is
                    // a duplicate reference; delete instead below).
                    + "  AND NOT EXISTS (SELECT 1 FROM " + t + " k "
                    + "        WHERE k.collection = c.collection AND k.chash = a.new_chash)");
                // duplicate reference rows whose resolved key already exists
                ctx.execute(
                    "DELETE FROM " + t + " c USING nexus.chash_alias a "
                    + "WHERE c.chunk_text = '' AND a.old_bytes = c.chash "
                    + "  AND EXISTS (SELECT 1 FROM " + t + " k "
                    + "        WHERE k.collection = c.collection AND k.chash = a.new_chash)");
                // ORPHAN CRITERION (width-free — the same 32-byte-ASCII
                // blindspot fix as the rekey predicate): an empty-text row is
                // an orphan when NO alias fact covers its key AND no
                // content-bearing row anywhere shares that key (a same-key
                // content sibling makes it a legitimate reference — either
                // already-canonical, needing no change, or legacy, resolved
                // via the alias above).
                String orphanCond =
                    "c.chunk_text = '' "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a "
                    + "        WHERE a.old_bytes = c.chash) "
                    // a row already AT an aliased NEW key is a reference the
                    // step-3a resolve just produced (content rows still hold
                    // their OLD keys until step 4) — never an orphan.
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a2 "
                    + "        WHERE a2.new_chash = c.chash) "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_384 k "
                    + "        WHERE k.chash = c.chash AND k.chunk_text <> '') "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_768 k "
                    + "        WHERE k.chash = c.chash AND k.chunk_text <> '') "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_1024 k "
                    + "        WHERE k.chash = c.chash AND k.chunk_text <> '')";
                if (synthesizeOrphans) {
                    // Alias the surrogates FIRST so the step-5 cascade
                    // repoints their surviving pointers (RDR-180 Failure
                    // Modes: a preserved pointer must follow the surrogate,
                    // never dangle at the old key).
                    ctx.execute(
                        "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                        + "SELECT current_setting('nexus.tenant', true), "
                        + String.format(OLD_REF, "c.chash") + ", c.chash, "
                        + "  sha256(convert_to("
                        + "    'nexus:synthetic-chash:v1|' || current_setting('nexus.tenant', true) "
                        + "    || '|' || c.collection || '|' || " + String.format(OLD_REF, "c.chash")
                        + "    , 'UTF8')), '" + t + ":synthetic' "
                        + "FROM " + t + " c WHERE " + orphanCond + " "
                        + "ON CONFLICT (tenant_id, old_ref) DO NOTHING");
                    orphansSynthesized += ctx.execute(
                        "UPDATE " + t + " c SET "
                        + "  chash = a.new_chash, "
                        + "  metadata = coalesce(c.metadata, '{}'::jsonb) "
                        + "             || jsonb_build_object('chash_origin', 'synthetic') "
                        + "FROM nexus.chash_alias a "
                        + "WHERE c.chunk_text = '' AND a.old_bytes = c.chash "
                        + "  AND a.source = '" + t + ":synthetic' "
                        + "  AND c.chash IS DISTINCT FROM a.new_chash");
                } else {
                    // drop: cascade the manifest + chash_index pointers FIRST
                    // (same transaction — RDR-180 Failure Modes: dangling
                    // manifest pointer), then the orphan rows.
                    ctx.execute(
                        "DELETE FROM nexus.catalog_document_chunks m USING " + t + " c "
                        + "WHERE m.chash = c.chash AND " + orphanCond);
                    ctx.execute(
                        "DELETE FROM nexus.chash_index i USING " + t + " c "
                        + "WHERE i.chash = c.chash AND " + orphanCond);
                    orphansDropped += ctx.execute(
                        "DELETE FROM " + t + " c WHERE " + orphanCond);
                }
            }
            counts.put("reference_only_resolved", refResolved);
            counts.put("orphans_dropped", orphansDropped);
            counts.put("orphans_synthesized", orphansSynthesized);

            // (4) two-phase content rekey per dim.
            int collapsed = 0;
            int rekeyed = 0;
            for (String t : CHUNK_TABLES) {
                // phase A: delete collapse-losers. Keeper per (collection,
                // digest): a row already AT the digest key wins, else min ctid.
                collapsed += ctx.execute(ChashSqlIdioms.contentCollapseDelete(t));
                // phase B: rekey survivors whose key mismatches their digest.
                rekeyed += ctx.execute(ChashSqlIdioms.contentRekeyUpdate(t));
            }
            counts.put("collapsed_duplicates", collapsed);
            counts.put("rehashed", rekeyed);

            // (5) cascades via the alias map.
            // manifest: chash not in its PK — plain rewrite.
            counts.put("manifest_repointed", ctx.execute(
                "UPDATE nexus.catalog_document_chunks m SET chash = a.new_chash "
                + "FROM nexus.chash_alias a "
                + "WHERE m.chash = a.old_bytes AND m.chash IS DISTINCT FROM a.new_chash"));
            // chash_index: (tenant, chash, collection) PK — two-phase, in TWO
            // collapse directions (the RekeyOpsIntegrationTest 3c catch):
            // (i) among the OLD rows themselves — two old keys mapping to one
            // new key with NO row at the target yet would both UPDATE into
            // the same PK; keep the min-ctid one per (collection, target).
            ctx.execute(
                "DELETE FROM nexus.chash_index i "
                + "USING nexus.chash_index j, nexus.chash_alias ai, nexus.chash_alias aj "
                + "WHERE ai.old_bytes = i.chash AND aj.old_bytes = j.chash "
                + "  AND ai.new_chash = aj.new_chash "
                + "  AND i.physical_collection = j.physical_collection "
                + "  AND j.ctid < i.ctid");
            // (ii) against a row already AT the target key.
            ctx.execute(
                "DELETE FROM nexus.chash_index i USING nexus.chash_alias a "
                + "WHERE i.chash = a.old_bytes "
                + "  AND EXISTS (SELECT 1 FROM nexus.chash_index k "
                + "        WHERE k.physical_collection = i.physical_collection "
                + "          AND k.chash = a.new_chash)");
            counts.put("chash_index_repointed", ctx.execute(
                "UPDATE nexus.chash_index i SET chash = a.new_chash "
                + "FROM nexus.chash_alias a "
                + "WHERE i.chash = a.old_bytes AND i.chash IS DISTINCT FROM a.new_chash"));
            // topic_assignments: TEXT doc_id matches old_ref; PK
            // (tenant, doc_id, topic_id) — two-phase in both collapse
            // directions (see the chash_index note above).
            ctx.execute(
                "DELETE FROM nexus.topic_assignments ta "
                + "USING nexus.topic_assignments tb, nexus.chash_alias aa, nexus.chash_alias ab "
                + "WHERE aa.old_ref = ta.doc_id AND ab.old_ref = tb.doc_id "
                + "  AND aa.new_chash = ab.new_chash "
                + "  AND ta.topic_id = tb.topic_id "
                + "  AND tb.ctid < ta.ctid");
            ctx.execute(
                "DELETE FROM nexus.topic_assignments ta USING nexus.chash_alias a "
                + "WHERE ta.doc_id = a.old_ref "
                + "  AND EXISTS (SELECT 1 FROM nexus.topic_assignments k "
                + "        WHERE k.topic_id = ta.topic_id "
                + "          AND k.doc_id = encode(a.new_chash, 'hex'))");
            counts.put("topic_assignments_repointed", ctx.execute(
                "UPDATE nexus.topic_assignments ta SET doc_id = encode(a.new_chash, 'hex') "
                + "FROM nexus.chash_alias a WHERE ta.doc_id = a.old_ref"));
            // frecency: PK (tenant, chunk_id) — GREATEST-merge on collapse
            // (the RDR-185 _FRECENCY_MERGE_SQL semantics, PG port), covering
            // BOTH collapse directions via a per-target group aggregate over
            // every matching old row (3c catch: two olds, no target row).
            // keeper keyed by min(chunk_id), NOT ctid: an UPDATE rewrites
            // the row and changes its ctid, so ctid-based keeper selection
            // goes stale across statements (the 3c "expected 5 was 2" catch).
            String frecencyAgg = ChashSqlIdioms.frecencyAliasAggregate();
            // (i) an existing row AT the target absorbs the whole group.
            ctx.execute(
                "UPDATE nexus.frecency t SET "
                + "  frecency_score = GREATEST(t.frecency_score, g.fs), "
                + "  miss_count     = GREATEST(t.miss_count,     g.mc), "
                + "  last_hit_at    = GREATEST(t.last_hit_at,    g.lh), "
                + "  embedded_at    = GREATEST(t.embedded_at,    g.ea), "
                + "  ttl_days       = GREATEST(t.ttl_days,       g.td) "
                + "FROM " + frecencyAgg + " "
                + "WHERE t.chunk_id = encode(g.new_chash, 'hex')");
            ctx.execute(
                "DELETE FROM nexus.frecency f USING nexus.chash_alias a "
                + "WHERE f.chunk_id = a.old_ref "
                + "  AND EXISTS (SELECT 1 FROM nexus.frecency k "
                + "        WHERE k.chunk_id = encode(a.new_chash, 'hex'))");
            // (ii) no target row: the min-ctid keeper absorbs the group,
            // the other olds are deleted (the keeper is renamed below).
            ctx.execute(
                "UPDATE nexus.frecency f SET "
                + "  frecency_score = g.fs, miss_count = g.mc, "
                + "  last_hit_at = g.lh, embedded_at = g.ea, ttl_days = g.td "
                + "FROM " + frecencyAgg + ", nexus.chash_alias a2 "
                + "WHERE a2.old_ref = f.chunk_id AND a2.new_chash = g.new_chash "
                + "  AND f.chunk_id = g.keep_id");
            ctx.execute(
                "DELETE FROM nexus.frecency f "
                + "USING " + frecencyAgg + ", nexus.chash_alias a2 "
                + "WHERE a2.old_ref = f.chunk_id AND a2.new_chash = g.new_chash "
                + "  AND f.chunk_id <> g.keep_id");
            counts.put("frecency_repointed", ctx.execute(
                "UPDATE nexus.frecency f SET chunk_id = encode(a.new_chash, 'hex') "
                + "FROM nexus.chash_alias a WHERE f.chunk_id = a.old_ref"));
            counts.put("relevance_log_repointed", ctx.execute(
                "UPDATE nexus.relevance_log r SET chunk_id = encode(a.new_chash, 'hex') "
                + "FROM nexus.chash_alias a WHERE r.chunk_id = a.old_ref"));

            // (6) verification scans, same transaction: residual mismatched
            // content rows and dangling pointers — MUST all be zero.
            int residual = 0;
            for (String t : CHUNK_TABLES) {
                residual += ctx.fetchOne(
                    ChashSqlIdioms.residualMismatchCount(t)).get(0, Integer.class);
            }
            counts.put("residual_mismatched", residual);
            counts.put("dangling_manifest", ctx.fetchOne(
                ChashSqlIdioms.danglingManifestCount()).get(0, Integer.class));
            // Census (nexus-jxizy.10.5): REPORTED here, fatal only on the
            // staging-finalize path — the shipped in-store rekey keeps its
            // contract while the envelope gains the every-column visibility
            // (critic-C3: the old verify saw 2 of ~6 surfaces).
            Map<String, Integer> census = ChashCensus.scan(ctx);
            counts.put("census_residue_columns", census.size());
            if (!census.isEmpty()) {
                counts.put("census_residue", census.toString());
                log.warn("event=rekey_census_residue tenant-scope residue={}", census);
            }
            return counts;
        });
        log.info("event=rekey_complete tenant={} counts={}", tenant, out);
        return out;
    }

    /** UNION ALL of mismatched content rows across dims with recovered old_ref. */
    private static String unionAllContentRows() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < CHUNK_TABLES.size(); i++) {
            if (i > 0) sb.append(" UNION ALL ");
            String t = CHUNK_TABLES.get(i);
            sb.append("SELECT ")
              .append(String.format(OLD_REF, "chash")).append(" AS old_ref, ")
              .append("chash AS old_bytes, ")
              .append(DIGEST).append(" AS new_chash, ")
              .append("'").append(t).append("' AS source ")
              .append("FROM ").append(t)
              .append(" WHERE chunk_text <> '' AND chash IS DISTINCT FROM ").append(DIGEST);
        }
        return sb.toString();
    }
}
